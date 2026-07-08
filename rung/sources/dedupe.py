"""Collapse shared-brand duplicate stores across companies.

Some "companies" are really the same operator: Delta 9 Pittsburgh and Keystone
Integrated Care both redirect to sunnyside.shop, so each scraped Sunnyside's 20 PA
stores under its own name. Left alone, the company-vs-state comparison triple-counts
those locations.

We detect this by **physical-store identity**: two rows are the same store if they share a
normalized street address, a coordinate cell, OR a platform handle (``platform:external_id`` — a
unique store id, so it also collapses a store captured a SECOND time under the same handle without
an address, e.g. Cresco's address-less custom duplicates). This catches Delta 9 even though its
homepage is delta9pa.com, not sunnyside.shop. Companies sharing a store are unioned into operator
clusters; each cluster's canonical company is the one whose brand appears in the scraped store
names (Sunnyside stores are named "Sunnyside …"). Duplicate rows get canonical_company_id set; one
row per physical store stays canonical (NULL), and inherits a folded sibling's coordinates if it
won the slot without them, so the surviving row still maps.
"""

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from rung import db
from rung.models import CompanyStoreRecord

# A unit/suite designator + its identifier. The word keywords are bounded on BOTH sides
# (``\bste\b``) so a unit abbreviation can't swallow a street name that merely starts with
# it — without the trailing ``\b``, ``ste`` matched "Stefko" and ``[\w-]+`` ate the rest,
# turning "1309 Stefko Blvd" into "1309 blvd". ``#`` keeps its looser form ("#125", "# 125").
_UNIT_RE = re.compile(
    r"(?:\b(?:suite|ste|unit|apt|apartment|bldg|building|fl|floor|rm|room)\b|#)"
    r"\s*\.?\s*[\w-]+",
    re.IGNORECASE,
)
# Street-type and directional abbreviations → a single canonical token.
_ABBREV = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "route": "rt", "rte": "rt", "terrace": "ter", "circle": "cir",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    # Street-NAME prefixes that drift across sources as the first significant word the match key
    # keys on ("Mt Hermon Rd" vs "Mount Hermon Rd", "St Charles" vs "Saint Charles"). These only
    # appear as a name prefix after the house number, never in the street-TYPE slot, so folding
    # "saint"→"st" can't collide with the existing "street"→"st".
    "mount": "mt", "fort": "ft", "saint": "st",
}


def normalize_address(address: str | None) -> str:
    """Normalize a street address to a comparable key (unit/punct/abbrev-folded)."""
    if not address:
        return ""
    text = _UNIT_RE.sub(" ", address.lower())
    # Queens/Bronx hyphenated house numbers ("219-17 Hillside Ave") are the same address as the
    # run-together form ("21917 Hillside Ave") some rosters use; join the digits BEFORE the next
    # rule turns the hyphen into a space (which would otherwise split the number into two tokens
    # and wreck the number-keyed match). Only digit-hyphen-digit folds, so a street name or route
    # ("US-50") is untouched; a zip+4 baked into the street text ("97031-2384") also joins, which
    # is harmless — it folds symmetrically on both sides and the 5-digit zip is recovered from the
    # raw text, not this normalized street.
    text = re.sub(r"(?<=\d)-(?=\d)", "", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [_ABBREV.get(tok, tok) for tok in text.split() if tok]
    return " ".join(tokens)


# A Canadian postal code, with or without the interior space (AGCO emits "P3E4M8",
# operator sites "P3E 4M8"). Uppercased input only — zip_key() uppercases first.
_CA_POSTAL_RE = re.compile(r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$")


def zip_key(zip_code: str | None) -> str:
    """Canonical postal identity for match keys: a US ZIP truncated to its 5 digits,
    a Canadian postal code folded to its 6-char no-space uppercase form. '' if empty.

    One helper so every key builder (here and the overlay's compare matcher) folds
    "P3E 4M8" and "P3E4M8" to the same key; naive ``[:5]`` truncation would split them
    ("P3E 4" vs "P3E4M") and lose the last, most local character."""
    raw = (zip_code or "").strip().upper()
    if _CA_POSTAL_RE.match(raw):
        return raw.replace(" ", "")
    return raw[:5]


def address_key(address: str | None, zip_code: str | None) -> str:
    """A physical-store identity: normalized address + zip/postal key. '' if unusable.

    Named ``address_key`` (not ``store_key``) to keep it distinct from the unrelated
    ``{platform}:{external_id}`` menu handle that Stage 3 calls ``store_key`` everywhere."""
    normalized = normalize_address(address)
    if not normalized:
        return ""
    return f"{normalized}|{zip_key(zip_code)}"


# ~11 m grid cell (4 decimal places of lat/lng). Deliberately TIGHT: only the same rooftop
# collides, so a geo match never merges two distinct dispensaries that merely cluster in the
# same commercial "green zone" (measured: at 4 decimals every cross-company collision in the
# dataset was a genuine same-store pair — RISE "U.S. 50"/"US-50", casing/legal-suffix variants
# — with zero false merges; 3 decimals/110 m wrongly merged neighbouring competitors).
_GEO_PRECISION = 4


def geo_key(
    latitude: float | None, longitude: float | None, zip_code: str | None
) -> str:
    """A physical-store identity from COORDINATES: an ~11 m cell + zip/postal key; '' if no coords.

    The fallback to :func:`address_key`: the same rooftop scraped with divergent address text
    (``US-50`` vs ``U.S. 50``, casing, a legal-entity suffix, a missing ``Dr``) shares a cell
    even when the normalized street differs. The tight cell + zip keep it from merging the
    distinct neighbours that pack into the same dispensary zone.
    """
    if latitude is None or longitude is None:
        return ""
    return f"@{round(latitude, _GEO_PRECISION)},{round(longitude, _GEO_PRECISION)}|{zip_key(zip_code)}"


def location_key(
    latitude: float | None, longitude: float | None,
    address: str | None, zip_code: str | None,
) -> str:
    """The store-history physical-location identity: :func:`geo_key`, with a GUARDED
    :func:`address_key` fallback; '' when the row can't identify a location.

    The guard exists because some state rosters put a non-address in the address field —
    MD stores a bare COUNTY ("Harford", no street, no zip; live-verified 2026-07-02: 119
    MD roster rows collapsed to 23 county-level keys). An ungated ``address_key`` would
    merge distinct stores into one pseudo-location and fabricate operator-change events.
    So the address fallback requires BOTH a house-number-bearing address (a digit) and a
    full zip/postal code; anything weaker returns '' (unidentifiable — skip, don't guess).
    See docs/store_history_design.md.
    """
    key = geo_key(latitude, longitude, zip_code)
    if key:
        return key
    if len(zip_key(zip_code)) < 5 or not any(ch.isdigit() for ch in (address or "")):
        return ""
    return address_key(address, zip_code)


# Same-operator cross-platform geocode-drift merge radius. The tight 11 m geo_key misses one case:
# the SAME store geocoded > 11 m apart by two platforms (Weedmaps vs Dutchie). Measured nationwide, a
# store's nearest same-operator cross-platform neighbour is either < ~100 m (the same rooftop) or km+
# away (a genuinely distinct store of the chain) — a clean valley — so this radius collapses the
# drift duplicates without merging distinct stores. Safe only WITHIN one operator: a cross-company
# 110 m merge wrongly fuses neighbouring competitors (see geo_key), but within one operator it can't.
_SAME_OP_MERGE_M = 100.0


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two lat/lng points, in metres."""
    r = math.pi / 180
    dla, dlo = (lat2 - lat1) * r, (lng2 - lng1) * r
    h = math.sin(dla / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin(dlo / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def physical_key(record: CompanyStoreRecord) -> str:
    """A cross-pool physical-store identity for one store record: the coordinate cell when
    present, else the normalized street + zip, else the platform handle (so a coord/address-less
    row is never silently merged with another). The record-level key that layers
    :func:`geo_key` over :func:`address_key`; used to dedupe the same physical store across the
    Dutchie/Weedmaps/Leafly bootstrap pools."""
    coord = geo_key(record.latitude, record.longitude, record.zip_code)
    if coord:
        return coord
    street = address_key(record.address, record.zip_code)
    if street:
        return street
    return f"{record.platform}:{record.external_id}"


class _UnionFind[T]:
    def __init__(self) -> None:
        self._parent: dict[T, T] = {}

    def find(self, item: T) -> T:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: T, b: T) -> None:
        self._parent[self.find(b)] = self.find(a)


def _brand_token(name: str) -> str:
    tokens = re.split(r"[^a-z0-9]+", name.lower())
    return tokens[0] if tokens and tokens[0] else name.lower()


def _city_in_name(city: str | None, alias_name: str) -> bool:
    """Whether a kept store's ``city`` appears as a whole token-run in a redirect alias's
    name — the robust way to pin "Harvest of Whitehall" to the Whitehall store.

    Matches the actual city against the alias name (not a guessed token), with word
    boundaries, so the full city is required: "Whitehall" in "Harvest of Whitehall" and
    "Palm Desert" in "STIIIZY Palm Desert" match, but the old last-word/substring guess
    (which produced "company"/"farms"/"access" tokens and let "bridge" hit "Cambridge")
    does not misfire. '' city → no match.
    """
    city = (city or "").strip().lower()
    if not city:
        return False
    return re.search(rf"\b{re.escape(city)}\b", alias_name.lower()) is not None


# Menu-data richness by platform (lower = richer). First-party / Dutchie menus carry
# potency / terpenes / mg; the aggregators (Weedmaps, Leafly) carry none. When one rooftop
# is captured on several platforms, the surviving (menu-scrape) row must be the richest one,
# so a Dutchie / first-party store is never demoted to its Weedmaps/Leafly twin.
_PLATFORM_MENU_RANK = {
    "dutchie": 0, "dutchie_plus": 0,
    "trulieve": 1, "cresco": 1, "sweedpos": 1, "curaleaf": 1,
    "jane": 2, "hytiva": 3,
    "weedmaps": 8, "leafly": 9,
}
_UNKNOWN_MENU_RANK = 5  # custom / unrecognized — beats an aggregator, loses to known-rich


def _menu_target_rank(row: tuple) -> tuple[int, int]:
    """Sort key for choosing a physical store's surviving (menu-scrape) row: handle-bearing
    rows first, then richest menu platform. ``row`` carries platform at index 9, the menu
    handle (external_id) at index 10 (see ``db.get_company_stores_for_dedupe``)."""
    platform = (row[9] or "").strip().lower()
    external_id = (row[10] or "").strip()
    has_handle = 0 if (platform and external_id) else 1
    return has_handle, _PLATFORM_MENU_RANK.get(platform, _UNKNOWN_MENU_RANK)


def pick_canonical(
    company_ids: set[int],
    names: dict[int, str],
    store_names: dict[int, list[str]],
) -> int:
    """The canonical company of an operator cluster: the one whose brand token most
    often appears in its scraped store names; then the one with the most stores (the
    surviving operator usually carries the full set); then shortest name, then id."""
    def score(cid: int) -> tuple[int, int, int, int]:
        brand = _brand_token(names[cid])
        matches = sum(1 for sn in store_names.get(cid, []) if brand and brand in sn.lower())
        store_count = len(store_names.get(cid, []))
        return (matches, store_count, -len(names[cid]), -cid)

    return max(company_ids, key=score)


@dataclass
class DedupeReport:
    distinct_stores: int = 0          # physical stores after dedupe
    duplicate_rows: int = 0           # rows marked as shared-brand duplicates
    located_from_sibling: int = 0     # kept rows that inherited a folded sibling's coordinates
    realigned_products: int = 0       # menu snapshots re-pointed at their handle's kept company
    clusters: list[tuple[str, list[str]]] = field(default_factory=list)  # (canonical, aliases)


def run_dedupe(conn: db.DBConn, state: str) -> DedupeReport:
    """Mark shared-brand duplicate stores in a state. Commits; returns a report."""
    rows = db.get_company_stores_for_dedupe(conn, state)
    names: dict[int, str] = {}
    store_names: dict[int, list[str]] = defaultdict(list)

    # A physical store is identified by its normalized address key OR its coordinate cell
    # (geo_key) OR its platform handle (``platform:external_id`` — a stronger identity than a fuzzy
    # address, since a platform store id is unique to one rooftop). So the same rooftop scraped with
    # divergent address text, or captured a second time under the SAME handle without an address
    # (Cresco's address-less custom duplicates), still collapses. Tie each row's keys together in a
    # key union-find, then group rows by their connected physical key; rows sharing ANY key land in
    # one group.
    key_union: _UnionFind[str] = _UnionFind()
    row_keys: list[tuple[tuple, list[str]]] = []
    for row in rows:
        (_id, company_id, canonical_name, name, address, _city, zip_code, lat, lng,
         platform, external_id) = row
        names[company_id] = canonical_name
        store_names[company_id].append(name or "")
        handle = f"{platform}:{external_id}" if platform and external_id else ""
        keys = [
            k for k in (address_key(address, zip_code), geo_key(lat, lng, zip_code), handle) if k
        ]
        for key in keys[1:]:
            key_union.union(keys[0], key)
        row_keys.append((row, keys))

    # Same-operator coarse-geo merge: union the key sets of any two rows of ONE operator
    # (same canonical_name) whose coordinates fall within _SAME_OP_MERGE_M — the cross-platform
    # geocode-drift duplicates the tight geo_key misses. O(k²) within each operator's stores, which
    # are few; restricted to same operator so co-located DIFFERENT operators never merge.
    op_points: dict[str, list[tuple[float, float, list[str]]]] = defaultdict(list)
    for row, keys in row_keys:
        if keys and row[7] is not None and row[8] is not None:
            op_points[row[2]].append((float(row[7]), float(row[8]), keys))
    for points in op_points.values():
        for i in range(len(points)):
            lat_i, lng_i, keys_i = points[i]
            for lat_j, lng_j, keys_j in points[i + 1:]:
                if _haversine_m(lat_i, lng_i, lat_j, lng_j) <= _SAME_OP_MERGE_M:
                    key_union.union(keys_i[0], keys_j[0])

    groups: dict[str, list[tuple]] = defaultdict(list)
    for row, keys in row_keys:
        if keys:
            groups[key_union.find(keys[0])].append(row)

    # Union companies that share any physical store into operator clusters.
    union: _UnionFind[int] = _UnionFind()
    for company_id in names:
        union.find(company_id)
    for grp in groups.values():
        company_ids = [r[1] for r in grp]
        for other in company_ids[1:]:
            union.union(company_ids[0], other)

    clusters: dict[int, set[int]] = defaultdict(set)
    for company_id in names:
        clusters[union.find(company_id)].add(company_id)
    canonical_of: dict[int, int] = {}
    report = DedupeReport()
    for members in clusters.values():
        canonical = pick_canonical(members, names, store_names)
        for company_id in members:
            canonical_of[company_id] = canonical
        if len(members) > 1:
            report.clusters.append(
                (names[canonical], sorted(names[c] for c in members if c != canonical))
            )

    db.clear_store_canonical_for_state(conn, state)
    kept_rows: list[tuple[int, str, int]] = []  # (store_id, city_lower, operator_id)
    for grp in groups.values():
        # Collapse every extra row at this physical address — cross-company alias
        # (Delta 9 → Sunnyside) or intra-company duplicate (same store scraped twice).
        # Keep one row per physical store. Prefer the richest-menu HANDLE (so a Dutchie /
        # first-party store isn't demoted to its Weedmaps/Leafly twin at the same rooftop),
        # then the canonical company, then a stable id; the rest collapse into the operator.
        canonical = canonical_of[grp[0][1]]
        operator_count = len(store_names.get(canonical, []))
        kept = min(grp, key=lambda r: (*_menu_target_rank(r), 0 if r[1] == canonical else 1, r[0]))
        for row in grp:
            if row[0] != kept[0]:
                db.set_store_canonical(conn, row[0], canonical)
                report.duplicate_rows += 1

        # If the kept row won the slot without coordinates (an address-less handle duplicate)
        # but a folded sibling carries them, copy the sibling's location onto the kept row so the
        # surviving store still plots — coords always; address/zip only to fill a blank.
        if kept[7] is None or kept[8] is None:
            donor = next((r for r in grp if r[7] is not None and r[8] is not None), None)
            if donor is not None:
                db.set_store_location(
                    conn, kept[0], donor[7], donor[8], kept[4] or donor[4], kept[6] or donor[6]
                )
                report.located_from_sibling += 1

        # Storefront brand for this physical location. A SUBSET alias (lists fewer
        # stores than the operator, e.g. Keystone ReLeaf's 3) brands the addresses it
        # lists; otherwise use the canonical OPERATOR's name (stable regardless of which
        # platform row won the kept slot — see the richest-handle pick above). Redirect
        # aliases (which scraped the whole list) are pinned below.
        company_ids = {r[1] for r in grp}
        subset = [
            c for c in company_ids
            if c != canonical and 0 < len(store_names.get(c, [])) < operator_count
        ]
        if subset:
            storefront = names[min(subset, key=lambda c: len(store_names.get(c, [])))]
        else:
            storefront = names.get(canonical, "")
        db.set_store_storefront(conn, kept[0], storefront)
        kept_rows.append((kept[0], (kept[5] or "").lower(), canonical))

    # Redirect-alias override: a single-location alias that scraped the operator's
    # whole list (Harvest of Whitehall → all of Trulieve) gets pinned to the one kept
    # store whose CITY appears in the alias name. Only applied when exactly one kept store
    # matches (an ambiguous or absent city leaves the default storefront in place).
    for members in clusters.values():
        if len(members) <= 1:
            continue
        operator_id = canonical_of[next(iter(members))]
        operator_count = len(store_names.get(operator_id, []))
        for alias in members:
            if alias == operator_id or len(store_names.get(alias, [])) < operator_count:
                continue
            alias_name = names[alias]
            matched = [
                kr for kr in kept_rows
                if kr[2] == operator_id and _city_in_name(kr[1], alias_name)
            ]
            if len(matched) == 1:
                db.set_store_storefront(conn, matched[0][0], alias_name)

    conn.commit()

    # Re-stamp menu snapshots onto whichever company now owns each handle's kept row. A snapshot
    # carries the company_id that owned its handle at scrape time; this pass (above) can move the
    # kept/canonical row to a different company of the same operator, so without this the snapshots
    # would keep mis-attributing menus to the now-folded alias (Sunnyside's stores, scraped under
    # the "Delta 9" alias, then folded). Keeps store_products consistent with what a fresh
    # scrape-menus run would file. See db.realign_store_products_company.
    report.realigned_products = db.realign_store_products_company(conn, state)
    conn.commit()

    # Every row not marked a duplicate is a kept/distinct store (addressless rows,
    # which can't be matched, are kept as-is).
    report.distinct_stores = len(rows) - report.duplicate_rows
    return report


def print_dedupe_report(report: DedupeReport, state: str) -> None:
    print(
        f"{state}: {report.distinct_stores} distinct physical stores; "
        f"{report.duplicate_rows} shared-brand duplicate rows marked; "
        f"{report.located_from_sibling} kept rows located from a sibling; "
        f"{report.realigned_products} menu snapshots realigned to their kept company."
    )
    for canonical, aliases in sorted(report.clusters):
        print(f"  operator {canonical} ← aliases: {', '.join(aliases)}")
