"""Offline address-POINT geocoding from a municipality's own open-data address file.

The accurate rung. Where `geocode_rnf` interpolates a position along a street's house-number range,
this looks the address up and returns **the point the city published for it** — no interpolation, no
assumption that house numbers are evenly spaced, no centerline offset to guess. Measured against
coordinates from retailers' own APIs, it is the most accurate rung we have: **median 19 m, 77% within
60 m**, against 38 m / 64% for range interpolation over the same ground truth.

**But read Zandbergen (2008) before assuming it is strictly better.** *A comparison of address point,
parcel and street geocoding techniques* finds address points match at rates "similar to those observed
for street network geocoding", and — the part that bites here — that **parcel** geocoding "generally
produces much lower match rates, in particular for commercial and multi-family residential addresses",
with commercial match rates "all below 50%" in one county. Both files this rung reads are **parcel**
data (Calgary publishes `address_type=Parcel` only), and a dispensary is a commercial address, often a
unit inside a multi-tenant building. So the ~56% match rate is not a bug to fix; it is the documented
behaviour of this data model on this address type — "a single parcel can be associated with many
addresses". Higher precision, comparable-or-worse recall: a FIRST rung, not a replacement.

**Straight from the authority, not an aggregator.** OpenAddresses republishes exactly these files and
would be one hop further from the source; the cities publish them directly, so we read the city. Same
reasoning as preferring a state's own roster to a menu aggregator: every hop is a chance for staleness
and an edit we cannot see. (If a city has no portal, OpenAddresses is the sensible fallback rung —
it is not built.)

**Coverage is municipal, so this is a FIRST rung and never the only one.** Calgary and Edmonton
publish; Saskatoon and Regina do not, and between them Calgary+Edmonton hold only ~38% of the AB/SK
roster rows. A caller must have somewhere to fall through to, and must not read a `None` here as "no
such address" — it means "no such city in this rung".

    from rung import geocode_points
    g = geocode_points.PointGeocoder.load(cache_dir, cities={"AB:CALGARY", "AB:EDMONTON"})
    g.geocode("5017 22 Ave SW", city="Edmonton", province="AB")   # -> (lat, lon) | None
"""

from __future__ import annotations

import asyncio
import csv
import io
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rung import http
from rung.addresses import normalize_city, parse_address, street_key

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable


@dataclass(frozen=True, slots=True)
class PointSource:
    """One municipality's published address-point file, and how to read its columns.

    `street_field` is either a pre-split trio (Calgary publishes street_name/street_type/street_quad)
    or one combined string (Edmonton publishes "ALEXANDER CIRCLE NW"). Both end at the same
    `street_key`, which is the whole point of keying on a parse rather than on raw text.
    """

    key: str                  # "AB:CALGARY"
    url: str                  # Socrata CSV export
    house_field: str
    street_combined: str | None = None            # e.g. Edmonton "street_name"
    street_split: tuple[str, str, str] | None = None  # e.g. Calgary (name, type, quad)
    lat_field: str = "latitude"
    lon_field: str = "longitude"
    status_field: str | None = None
    status_ok: str | None = None
    # This source's street-TYPE tokens -> our canonical ones. A property of the SOURCE, not of
    # address text in general, which is why it lives per-source and not in `addresses._TYPE`.
    type_map: dict[str, str] | None = None


# Socrata `$limit` must be explicit and large: the default page size is 1,000 rows, so omitting it
# silently yields a 1,000-row "complete" dataset — a geocoder that misses 99% of a city while
# reporting success.
_LIMIT = 2_000_000

# Calgary publishes its OWN two-letter street-type vocabulary, which is NOT the RNF's and not ours:
# it writes AV where we write AVE, WY for WAY, TR for TRAIL, CM for COMMON, PY for PKY. Unmapped,
# only ST/DR/RD/PL happen to coincide and the rung matched 41% of Calgary stores while holding
# 409,484 of its addresses — the join, not the coverage, was the miss.
#
# **Every entry below was verified against the published data, not recalled**: querying known streets
# for their street_type (MACLEOD -> TR, LONGVIEW -> CM, SYMONS VALLEY -> PY, 22 -> AV, ...). Two
# plausible guesses were wrong before that check — Trail is TR not "TC", Common is CM not "CO" — and
# a wrong entry here does not fail loudly, it silently geocodes to a DIFFERENT street of the same
# name (Calgary has Castlebrook WY *and* RD *and* RI). Extend only the same way: look it up.
_CALGARY_TYPES = {
    "AV": "AVE", "WY": "WAY", "CR": "CRES", "BV": "BLVD",
    "TR": "TRAIL", "CM": "COMMON", "PY": "PKY",
}

SOURCES: dict[str, PointSource] = {
    "AB:CALGARY": PointSource(
        key="AB:CALGARY",
        url=f"https://data.calgary.ca/resource/s8b3-j88p.csv?$limit={_LIMIT}",
        house_field="house_number",
        street_split=("street_name", "street_type", "street_quad"),
        type_map=_CALGARY_TYPES,
    ),
    "AB:EDMONTON": PointSource(
        key="AB:EDMONTON",
        url=f"https://data.edmonton.ca/resource/ut27-nrpn.csv?$limit={_LIMIT}",
        house_field="house_number",
        street_combined="street_name",
        status_field="status",
        status_ok="OFFICIAL",
    ),
}


def _row_key(src: PointSource, row: dict[str, str]) -> tuple[int, str] | None:
    """(house number, street_key) for one published row, or None if unusable."""
    raw = (row.get(src.house_field) or "").strip()
    if not raw.isdigit():
        return None  # "1234A", a range, or blank — not a plain civic number
    if src.status_field and (row.get(src.status_field) or "").strip().upper() != src.status_ok:
        return None
    if src.street_split:
        name, type_, dir_ = ((row.get(f) or "").strip() for f in src.street_split)
        if src.type_map:
            type_ = src.type_map.get(type_.upper(), type_)
        return int(raw), street_key(name, type_, dir_)
    combined = (row.get(src.street_combined or "") or "").strip()
    if not combined:
        return None
    # Re-use the ONE parser by handing it a synthetic "<number> <street>" line, so a city's combined
    # street string is folded to the identical key as our own address text.
    parsed = parse_address(f"{raw} {combined}")
    return parsed if parsed else None


async def _download(cache_dir: Path, src: PointSource) -> bytes:
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"points_{src.key.replace(':', '_')}.csv"
    if dest.exists() and dest.stat().st_size > 1_000:
        return dest.read_bytes()
    print(f"  downloading {src.key} address points…")
    async with http.make_session() as session:
        r = await session.get(src.url, timeout=600)
        if r.status_code != 200 or not r.content:
            raise RuntimeError(f"{src.key} address-point download failed (HTTP {r.status_code})")
        dest.write_bytes(r.content)
    return dest.read_bytes()


class PointGeocoder:
    """Address -> (lat, lon) by exact lookup in a city's published address-point file."""

    def __init__(self, index: dict[tuple[str, int, str], tuple[float, float]]) -> None:
        self._index = index
        self._cities = {k[0] for k in index}
        # (city, number, "NAME|TYPE") -> the directions published for it. Calgary and Edmonton are
        # quadrant cities: "16616 95 Street" omits the quad our source always carries, so the exact
        # key misses. Resolving it needs to know whether the quad is RECOVERABLE (exactly one
        # quadrant has that civic number on that street) or genuinely ambiguous (several do).
        self._dirless: dict[tuple[str, int, str], set[str]] = {}
        for city, number, key in index:
            name_type, _, direction = key.rpartition("|")
            self._dirless.setdefault((city, number, name_type), set()).add(direction)

    @property
    def address_count(self) -> int:
        return len(self._index)

    def covers(self, province: str, city: str | None) -> bool:
        """Is this (province, city) in the rung at all?

        The distinction a caller MUST make: `geocode` returning None because the city is not in this
        rung ("fall through to the next rung") is not the same as returning None because the city
        publishes no such address ("this address does not exist"). Only `covers` tells them apart.
        """
        return f"{province.upper()}:{normalize_city(city)}" in self._cities

    @classmethod
    def load(cls, cache_dir: Path, cities: Iterable[str]) -> PointGeocoder:
        wanted = sorted({c.upper() for c in cities})
        unknown = [c for c in wanted if c not in SOURCES]
        if unknown:
            raise KeyError(f"no address-point source for {unknown}; known: {sorted(SOURCES)}")
        cache = cache_dir / f"points_index_{'-'.join(c.replace(':', '_') for c in wanted)}.pkl"
        if cache.exists():
            with cache.open("rb") as fh:
                return cls(pickle.load(fh))

        index: dict[tuple[str, int, str], tuple[float, float]] = {}
        for key in wanted:
            src = SOURCES[key]
            raw = asyncio.run(_download(cache_dir, src))
            rows = kept = 0
            for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))):
                rows += 1
                rk = _row_key(src, row)
                if rk is None:
                    continue
                try:
                    lat = float(row.get(src.lat_field) or "")
                    lon = float(row.get(src.lon_field) or "")
                except ValueError:
                    continue
                if not (lat and lon):
                    continue
                # A parcel can publish several points (suites); first wins — they agree to metres,
                # and picking arbitrarily among them beats dropping the address entirely.
                index.setdefault((key, rk[0], rk[1]), (lat, lon))
                kept += 1
            print(f"  {src.key}: {rows:,} published rows -> {kept:,} usable, "
                  f"{len({k for k in index if k[0] == key}):,} distinct addresses")
        with cache.open("wb") as fh:
            pickle.dump(index, fh, protocol=pickle.HIGHEST_PROTOCOL)
        return cls(index)

    def geocode(self, address: str | None, city: str | None, province: str) -> tuple[float, float] | None:
        """(lat, lon) for an address, or None when this rung has no point for it.

        Falls back ONLY on a unique answer: an address written without its quadrant
        ("16616 95 Street") resolves iff exactly one quadrant publishes that civic number on that
        street. Two candidates means two real places kilometres apart, and picking either would be
        a coin flip dressed as a coordinate — the same reason the Census backend demands a unique
        match when a query carries no city or ZIP.
        """
        parsed = parse_address(address)
        if parsed is None:
            return None
        number, key = parsed
        city_key = f"{province.upper()}:{normalize_city(city)}"
        hit = self._index.get((city_key, number, key))
        if hit is not None:
            return hit
        name_type, _, direction = key.rpartition("|")
        if direction:
            return None  # a direction WAS given and did not match — not ours to second-guess
        candidates = self._dirless.get((city_key, number, name_type))
        if candidates is None or len(candidates) != 1:
            return None
        return self._index.get((city_key, number, f"{name_type}|{next(iter(candidates))}"))
