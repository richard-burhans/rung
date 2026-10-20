import datetime
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from rung import db
from rung.text import (
    extract_brand,
    is_placeholder_name,
    load_company_aliases,
    normalize_brand,
    readability_key,
)

_YAML_PATH = Path(__file__).parent / "data" / "companies.yml"


def canonicalize(raw_brand: str, alias_map: dict[str, str]) -> str:
    return alias_map.get(raw_brand, raw_brand)


def fold(name: str, city: str | None, alias_map: dict[str, str]) -> tuple[str, str]:
    """``(key, brand)``: the brand a stored or roster name folds to, and its spelling-insensitive
    operator key — ONE pass of `extract_brand` with the name's own city, then the `companies.yml` alias.

    THE ONE FOLD RULE. This is how the refold migration keys a `companies` row (with the modal city of
    its stores), how the seeder extracts a roster row (with the row's own city), and how the pool
    bootstrap extracts a store. It lives here so the three cannot drift.

    `extract_brand` is not idempotent — each pass strips one tail ("Ziggyz Cannabis (MacArthur)" ->
    "Ziggyz Cannabis" -> "Ziggyz"; "Bluebird Cannabis Co. Almonte" -> "Bluebird Cannabis Co." ->
    "Bluebird") — and the refold applies it to a `canonical_name` the seeder had already extracted
    once. So the table holds the SECOND fold, and a seeder that keys a roster row on the first inserts
    the phantom the refold just merged away: re-run on 2026-10-11 against a refolded table it recreated
    62 of them. A roster row is therefore keyed as the refold would key the row it is about to become:
    ``fold(fold(name, city).brand, None)`` — see :func:`_collect_brand_state_pairs`.

    A spelling `companies.yml` rules on EXACTLY — an alias, or a canonical that has aliases — is taken as
    written and never extracted. "Local Cannabis Company" extracts to "Local", and no alias could reach
    it from there: the loader files each alias under its folded form too, so ruling on "Local" would
    rename BC's and MO's unrelated "Local" as well (2026-10-20).
    """
    ruled = alias_map.get(name) or (name if name in set(alias_map.values()) else None)
    brand = ruled or canonicalize(extract_brand(name, city), alias_map)
    return normalize_brand(brand), brand


def roster_fold(name: str, city: str | None, alias_map: dict[str, str]) -> tuple[str, str]:
    """``(key, brand)`` for a ROSTER row: `fold` with the row's own city, then `fold` again with none —
    the refold's key of the company row the seeder would store for it. The seeder groups by this; so
    does recon's website index, because the two must meet on one key."""
    _key, canonical = fold(name, city, alias_map)
    return fold(canonical, None, alias_map)


def company_keys(name: str, city: str | None, alias_map: dict[str, str]) -> tuple[str, str, str]:
    """The three keys an EXISTING company answers to, most specific first:

    * its `fold` with the modal city of its stores — exactly as the refold keys it;
    * its `roster_fold` — the key the seeder would give the stored name if it met it on a roster
      today. A row seeded before a fold rule existed and never refolded ("Joint Venture 1111 LLC",
      "Matchbox Cannabis 605 Rogers") is one pass short of the roster rows it came from;
    * `normalize_brand` of the stored spelling — for the cases the fold gets wrong ("Dank 716" ->
      "Dank"; an alias target that is not its own fold, "Bloc" -> "Bloc Dispensary").

    `existing_spellings` indexes under all three; recon's lookup asks all three. Every key names the
    same operator — none reaches across to another — so more keys can only find a row, never confuse
    two.
    """
    key, _brand = fold(name, city, alias_map)
    return key, roster_fold(name, city, alias_map)[0], normalize_brand(name)


def dominant_spelling(spellings: Counter[str]) -> str:
    """The display name for a folded brand group: the most frequent spelling in the roster,
    tie-broken toward the most readable (more spaces, then longer, then alphabetical) so the
    choice is deterministic — e.g. "Zen Leaf" wins over "ZenLeaf" when they tie."""
    return max(spellings, key=lambda s: (spellings[s], *readability_key(s)))


# Hosts that are a PLATFORM or a vendor, never an operator's own identity: two roster rows pointing
# at one of these share a vendor, not a company. Kept short and obvious; a host missing from it costs
# a false link only when two different operators share it, which these are the ones that do.
PLATFORM_HOSTS = frozenset({
    "dutchie.com", "iheartjane.com", "weedmaps.com", "leafly.com", "shopify.com", "squarespace.com",
    "wix.com", "wixsite.com", "godaddysites.com", "google.com", "facebook.com", "instagram.com",
    "linktr.ee", "business.site", "sweedpos.com", "sweed.com", "flowhub.com", "treez.io",
    "meadow.com", "dispensary.shop", "tymber.com", "hytiva.com", "breadstack.com",
})


def registrable_host(url: str | None) -> str | None:
    """The registrable domain of a URL — ``shop.apothecarium.com`` and ``apothecarium.com`` are one
    site — or None for nothing, a bare word, or a platform host (`PLATFORM_HOSTS`). The one
    per-OPERATOR identity a roster row or a recon probe carries: a licence number is per store, and
    a name is a spelling."""
    if not url or not url.strip():
        return None
    raw = url.strip()
    netloc = urlsplit(raw if "//" in raw else f"https://{raw}").netloc.lower()
    host = netloc.split("@")[-1].split(":")[0].removeprefix("www.")
    if "." not in host:
        return None
    site = ".".join(host.split(".")[-2:])
    return None if site in PLATFORM_HOSTS else site


def existing_hosts(conn: db.DBConn) -> dict[tuple[str, str], str]:
    """``{(registrable host, state): canonical_name}`` for the companies recon has already given a
    homepage — the earliest row when several share one host (that pair is the refold's business).
    Empty when either table does not exist yet."""
    for table in ("companies", "company_recon"):
        row = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()
        if row is None or row[0] is None:
            return {}
    hosts: dict[tuple[str, str], str] = {}
    for name, state, homepage in conn.execute(
        "SELECT c.canonical_name, c.state, r.homepage_url FROM companies c "
        "JOIN company_recon r ON r.company_id = c.id WHERE r.homepage_url IS NOT NULL ORDER BY c.id"
    ):
        host = registrable_host(homepage)
        if host:
            hosts.setdefault((host, state), name)
    return hosts


def _collect_brand_state_pairs(
    conn: db.DBConn, alias_map: dict[str, str]
) -> list[tuple[str, str]]:
    # Fold spelling/spacing/case variants of one brand (that companies.yml doesn't alias) into
    # a single company per (normalized-brand, state), seeded under the dominant spelling — so a
    # chain like Zen Leaf / ZenLeaf or NuEra / nuEra collapses to one operator instead of N.
    groups: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    # The registrable hosts a group's roster rows carry (`dispensaries.website`, where a roster
    # publishes one: ON, NJ, NY, PA, SK, CT, UT, NH on 2026-10-13) — the front door for the host
    # link below.
    group_hosts: dict[tuple[str, str], set[str]] = defaultdict(set)
    for name, city, state, website in conn.execute(
        "SELECT name, city, state, website FROM dispensaries"
    ):
        if is_placeholder_name(name):
            continue  # never seed a junk roster row (license-number/header/test stub) as a company
        # The row's own city folds a storefront suffix ("Zen Leaf Dayton" -> "Zen Leaf"); the second
        # pass keys the result as the refold keys a stored row — see `fold`.
        key, brand = roster_fold(name, city, alias_map)
        state_val = state if state else "Unknown"
        groups[(key, state_val)][brand] += 1
        host = registrable_host(website)
        if host:
            group_hosts[(key, state_val)].add(host)
    # A group the companies table ALREADY holds keeps the spelling it was seeded under. The dominant
    # spelling can change between runs ("ZenLeaf" most frequent on one roster, "Zen Leaf" after a
    # refresh), and the insert conflicts on the exact name, so a changed winner used to insert a
    # SECOND company for the same operator. Measured 2026-10-06: 858 (folded brand, state) keys were
    # already held by two or more rows.
    #
    # A group the table does NOT hold under its fold, but whose roster rows name a SITE an existing
    # company's recon already resolved to, is that company and is not minted (2026-10-13, scraping
    # design review item 4 phase 1): thirteen roster spellings of One Plant each became a company
    # on the fold alone, and each pulled the whole chain's directory list. The host is the one
    # per-operator identity the roster carries; a platform host never links (`PLATFORM_HOSTS`).
    # The link is at the front door only — the rows still key by their own fold elsewhere, which
    # is the refold's business, not the seeder's.
    existing = existing_spellings(conn, alias_map)
    by_host = existing_hosts(conn)
    pairs: set[tuple[str, str]] = set()
    for (key, state_val), spellings in groups.items():
        held = existing.get((key, state_val))
        if held is None:
            linked = sorted(by_host[(h, state_val)] for h in group_hosts.get((key, state_val), ())
                            if (h, state_val) in by_host)
            held = linked[0] if linked else None
        pairs.add((held or dominant_spelling(spellings), state_val))
    return sorted(pairs)


def store_cities(conn: db.DBConn) -> dict[int, str]:
    """``{company id: the modal city of its stores}`` — the city the fold of an EXISTING company
    sees. `companies` stores no city, and `extract_brand` cannot strip a storefront city it was never
    given ("RISE Bristol" stays "RISE Bristol" without "Bristol"), so a company's own stores supply it.
    Ties go to the alphabetically first city. Empty when `company_stores` does not exist yet."""
    row = conn.execute("SELECT to_regclass('company_stores')").fetchone()
    if row is None or row[0] is None:
        return {}
    return dict(conn.execute(
        "SELECT company_id, mode() WITHIN GROUP (ORDER BY city) FROM company_stores "
        "WHERE company_id IS NOT NULL AND city IS NOT NULL AND city <> '' GROUP BY company_id"
    ).fetchall())


def existing_spellings(conn: db.DBConn, alias_map: dict[str, str]) -> dict[tuple[str, str], str]:
    """``{(key, state): canonical_name}`` for the companies already seeded, under TWO keys each — the
    earliest row when several share one. Empty when the table does not exist yet.

    * its `fold` key with the modal city of its stores — exactly as the refold keys it, so a row the
      refold renamed ("RISE"), or never needed to touch ("Elevate 6010", "10613 Northern LLC" — a
      singleton keeps the spelling it was seeded under), meets the roster group that folds to it;
    * its raw spelling (`normalize_brand` of the stored name) — because `extract_brand` can also strip
      too much ("Dank 716" -> "Dank", the Buffalo area code read as a street number) and an alias
      target need not be its own fold ("Bloc" -> "Bloc Dispensary"); a roster group spelled exactly
      as a stored row IS that row, whatever the fold makes of either.
    """
    row = conn.execute("SELECT to_regclass('companies')").fetchone()
    if row is None or row[0] is None:
        return {}
    cities = store_cities(conn)
    spellings: dict[tuple[str, str], str] = {}
    for cid, name, state in conn.execute("SELECT id, canonical_name, state FROM companies ORDER BY id"):
        for key in company_keys(name, cities.get(cid), alias_map):
            spellings.setdefault((key, state), name)
    return spellings


def create_companies_table(conn: db.DBConn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id             BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            state          TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            UNIQUE (canonical_name, state)
        )
        """
    )
    conn.commit()


def ensure_company(conn: db.DBConn, canonical_name: str, state: str) -> int:
    """Get-or-create the operator's companies row for (canonical_name, state); return its id.

    The get-or-create shared by the direct seeders (government monopolies, Shopify storefronts)
    that bypass the roster-derived `seed-companies` flow. Caller commits.
    """
    row = conn.execute(
        "SELECT id FROM companies WHERE canonical_name = %s AND state = %s",
        (canonical_name, state),
    ).fetchone()
    if row is not None:
        return int(row[0])
    now = datetime.datetime.now(datetime.UTC).isoformat()
    inserted = conn.execute(
        "INSERT INTO companies (canonical_name, state, created_at) VALUES (%s,%s,%s) RETURNING id",
        (canonical_name, state, now),
    ).fetchone()
    assert inserted is not None                  # INSERT ... RETURNING always yields the new row
    return int(inserted[0])


def _seed(
    conn: db.DBConn, pairs: list[tuple[str, str]]
) -> tuple[list[tuple[str, str]], int]:
    """Insert the not-yet-present pairs in ONE statement; return (inserted-in-pairs-order, skipped).

    The UNIQUE (canonical_name, state) constraint does the existence check for free — a per-pair
    COUNT-then-INSERT was ~2x round-trips per operator across the whole multi-state roster to
    insert mostly-already-present rows. ``RETURNING`` names exactly what landed.
    """
    if not pairs:
        conn.commit()
        return [], 0
    now = datetime.datetime.now(datetime.UTC).isoformat()
    rows = conn.execute(
        "INSERT INTO companies (canonical_name, state, created_at) "
        "SELECT canonical_name, state, %s FROM unnest(%s::text[], %s::text[]) "
        "AS pair(canonical_name, state) "
        "ON CONFLICT (canonical_name, state) DO NOTHING "
        "RETURNING canonical_name, state",
        (now, [name for name, _ in pairs], [state for _, state in pairs]),
    ).fetchall()
    added = {(row[0], row[1]) for row in rows}
    inserted = [pair for pair in pairs if pair in added]
    conn.commit()
    return inserted, len(pairs) - len(inserted)


def main() -> None:
    alias_map = load_company_aliases(_YAML_PATH, strict=True)
    conn = db.get_connection()
    create_companies_table(conn)
    pairs = _collect_brand_state_pairs(conn, alias_map)
    inserted, skipped = _seed(conn, pairs)
    conn.close()

    print(f"Seeded companies table: {len(inserted)} inserted, {skipped} already present.")
    for canonical_name, state in inserted:
        print(f"  {canonical_name}  ({state})")


if __name__ == "__main__":
    main()
