"""Offline Canadian address geocoding: address-range interpolation over the StatCan Road Network File.

The authoritative Canadian analogue of TIGER/Line `ADDRFEAT`, and the primary source the commercial
Canadian geocoders are themselves built from. One 310 MB national download (2,242,117 road segments),
then every geocode is a local lookup: **no API, no quota, no rate limit, no per-IP dependence, and
byte-for-byte reproducible** — the last mattering most, because a coverage number that cannot be
recomputed cannot be defended.

**Know its accuracy before you use it.** Zandbergen (2009), *Geocoding Quality and Implications for
Spatial Analysis*, names the error components of street geocoding — matching the wrong segment,
**incorrect placement along the segment** (the dominant one), the side offset, and error in the street
geometry itself — and reviews typical positional errors for street geocoding of 25-168 m. Measured
here against coordinates supplied by retailers' own APIs (n=279 in AB/SK), with both offsets tuned:

    median 38 m | 64% within 60 m | 86% within 150 m

That sits inside the published range, and is comparable to a commercial Canadian geocoder measured on
the same stores (64.7% within 60 m) — with no quota. It is still **not** rooftop truth: a coordinate
always *looks* like a coordinate, so a caller needing rooftop resolution must measure against ground
truth for its own region rather than trust this because it is offline and free
(`scripts/validate_rnf_geocoder.py` is the harness). Where a municipality publishes address points,
`geocode_points` is more precise (median 19 m) and belongs ahead of this — though not strictly better:
it matches fewer addresses, especially commercial ones (see that module).

## How it works

Each RNF segment carries the house-number range on each side of the street
(`AFL_VAL`..`ATL_VAL` left, `AFR_VAL`..`ATR_VAL` right). To place "5017 22 Ave SW, Edmonton":
find the segment whose province + municipality + street match and whose range spans 5017, then
interpolate that far along the segment's polyline.

**Interpolation happens in the RNF's native projection, deliberately.** The file is
NAD83 / Statistics Canada Lambert (**EPSG:3347**) — a *projected* CRS in **metres**, not degrees, so
"40% along this block" is genuinely 40% of the block's length. Interpolating in lat/lon would weight
the walk by degrees, which are not equal distances (a degree of longitude is ~0.6x a degree of
latitude at 55°N). So the index is built and walked in Lambert and **exactly one point is reprojected
to WGS84 per geocode** — reprojection is a per-answer cost, never a per-vertex one. (`geocode_tracts.py`
skips reprojection because TIGER's NAD83 is within ~2 m of WGS84; that shortcut does NOT apply here.)

## What it does NOT do

- **No postal code.** The RNF has none, and this module never guesses one.
- **Interpolation is not rooftop truth.** It assumes house numbers are evenly spaced along a block, so
  its error is a fraction of a block — see the measurement above.
- **It returns None rather than a guess.** An unknown street, or a house number outside every range on
  it, yields no answer. A wrong coordinate is worse than a missing one, because every consumer treats
  a coordinate as evidence.

    from rung import geocode_rnf
    g = geocode_rnf.RnfGeocoder.load(cache_dir, provinces={"AB", "SK"})
    g.geocode("5017 22 Ave SW", city="Edmonton", province="AB")   # -> (lat, lon) | None
"""

from __future__ import annotations

import asyncio
import pickle
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rung import http
from rung.addresses import normalize_city, parse_address, street_key

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

try:
    import shapefile  # ty: ignore[unresolved-import]  # pyshp
    from pyproj import Transformer  # ty: ignore[unresolved-import]
except ImportError as exc:  # pragma: no cover - needs the geo extras
    raise ImportError(
        "rung.geocode_rnf needs pyshp + pyproj: `uv run --with pyshp --with pyproj …`"
    ) from exc

RNF_URL = (
    "https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/rnf-frr/"
    "files-fichiers/lrnf000r21a_e.zip"
)
_STEM = "lrnf000r21a_e"
# The .prj reads NAD83_Statistics_Canada_Lambert; the .cpg reads "ANSI 1252" (cp1252, NOT utf-8 —
# pyshp raises dbfFileException on b'M\xc9 ' under strict utf-8, which is "MÉ").
RNF_CRS = "EPSG:3347"
_WGS84 = "EPSG:4326"
_ENCODING = "cp1252"

# Province/territory postal abbreviation -> StatCan PRUID.
PRUID = {
    "NL": "10", "PE": "11", "NS": "12", "NB": "13", "QC": "24", "ON": "35",
    "MB": "46", "SK": "47", "AB": "48", "BC": "59", "YT": "60", "NT": "61", "NU": "62",
}

@dataclass(frozen=True, slots=True)
class Segment:
    """One RNF road segment: the house-number range on each side + the polyline (Lambert metres)."""

    lo_l: int | None
    hi_l: int | None
    lo_r: int | None
    hi_r: int | None
    pts: tuple[tuple[float, float], ...]


def _polyline_point(pts: tuple[tuple[float, float], ...], frac: float) -> tuple[float, float]:
    """The point `frac` of the way along a polyline, by LENGTH (Lambert metres)."""
    if len(pts) == 1:
        return pts[0]
    seg_len = [
        ((pts[i + 1][0] - pts[i][0]) ** 2 + (pts[i + 1][1] - pts[i][1]) ** 2) ** 0.5
        for i in range(len(pts) - 1)
    ]
    total = sum(seg_len)
    if total <= 0:
        return pts[0]
    want = max(0.0, min(1.0, frac)) * total
    for i, length in enumerate(seg_len):
        if want <= length or i == len(seg_len) - 1:
            t = (want / length) if length else 0.0
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            return (x0 + t * (x1 - x0), y0 + t * (y1 - y0))
        want -= length
    return pts[-1]


def _side_range(seg: Segment, number: int) -> tuple[int, int, int] | None:
    """(lo, hi, side) for the side this house number is on; None if neither side spans it.

    `side` is -1 for left, +1 for right, relative to the segment's direction of digitization —
    needed to offset off the centerline (see `_interpolate`).

    Parity decides between sides: the RNF ranges an even side and an odd side per segment
    (L: 52002..52132, R: 52001..52131). Containment alone is ambiguous where the ranges overlap.
    """
    sides: list[tuple[int, int, int] | None] = []
    if seg.lo_l is not None and seg.hi_l is not None:
        sides.append((min(seg.lo_l, seg.hi_l), max(seg.lo_l, seg.hi_l), -1))
    if seg.lo_r is not None and seg.hi_r is not None:
        sides.append((min(seg.lo_r, seg.hi_r), max(seg.lo_r, seg.hi_r), +1))

    spanning = [s for s in sides if s and s[0] <= number <= s[1]]
    if not spanning:
        return None
    if len(spanning) == 1:
        return spanning[0]
    # Both sides span it — take the one whose parity matches.
    for lo, hi, side in spanning:
        if lo % 2 == number % 2:
            return lo, hi, side
    return spanning[0]


# How far to step off the road centerline toward the addressed side. The RNF is a road NETWORK: its
# geometry is the centerline, but an address is a BUILDING, which sits half a carriageway plus a
# setback away. Interpolating without this leaves a systematic centerline bias in every answer.
#
# 18 m is our own empirical optimum (`validate_rnf_geocoder.py --tune`), and the literature agrees on
# both the value and — more usefully — on how little it buys. Zandbergen (2009) surveys the practice:
# "Most geocoding techniques employ a uniform perpendicular offset of around 10 to 15 m"; reported
# values "range from 0 to 17 m, often without specific justification"; Cayo and Talbot (2003) tuned
# it in 5 m steps to an optimum of 15 m; and Zandbergen (2007) found 10, 20 and 30 m produced
# "nearly identical error distributions". Our sweep reproduced that independently — 0 m -> 18 m moved
# the share within 60 m by only 53.5% -> 58.9%. The reason is the next constant down.
OFFSET_M = 18.0

# How far to pull IN from each end of the segment before interpolating. An address range is
# inclusive of the corner lots, so a naive fraction places the first/last house exactly ON the
# intersection. Cayo and Talbot (2003) call this the **"corner inset"** (Zandbergen 2009 renames it
# the "end offset") and report, verified in their own paper rather than through the review: "We found
# the optimal combination of the street offset and corner inset for the entire sample to be 15 m and
# 50 m respectively." Zandbergen (2009) is blunt about why the inset is the bigger knob: "the effect
# of using a side offset perpendicular to the street segment is very small relative to the error
# resulting from the incorrect placement ALONG the street segment", and where displacement along the
# segment is substantial a side offset "may in fact result in decreased positional accuracy".
# So the along-segment placement is where the error lives, and this is the knob that addresses it.
#
# Applied as a fraction of segment length, capped: a literal 50 m dropback would invert a 60 m block.
#
# **50 m is Cayo and Talbot's (2003) optimum, and it REPLICATED on our data** — a different country,
# a different reference file, and 23 years later. Our own grid (`--tune`) over side x end:
#
#     side  end    median   <=60m          side  end    median   <=60m
#        0 m  0 m    53 m   54.1%            18 m 30 m    40 m   64.0%
#       18 m  0 m    47 m   59.1%            18 m 50 m    38 m   64.4%   <- adopted
#        0 m 50 m    46 m   60.7%            18 m 80 m    38 m   64.4%
#
# The end offset is worth MORE than the side offset (+5.3 pp vs +5.0) and they compound — exactly the
# ordering Zandbergen (2009) predicts, since the dominant error is placement ALONG the segment. The
# curve is flat past 50 m (80 m is identical), so this is a plateau, not a knife-edge fit to 662 rows.
END_OFFSET_M = 50.0
_END_OFFSET_MAX_FRAC = 0.25


def _segment_length(pts: tuple[tuple[float, float], ...]) -> float:
    """Total polyline length in Lambert metres."""
    return sum(
        ((pts[i + 1][0] - pts[i][0]) ** 2 + (pts[i + 1][1] - pts[i][1]) ** 2) ** 0.5
        for i in range(len(pts) - 1)
    )


def _interpolate(
    seg: Segment, number: int, *, offset_m: float = OFFSET_M, end_offset_m: float = END_OFFSET_M
) -> tuple[float, float] | None:
    """Lambert (x, y) for a house number on a segment, or None if it isn't on this segment."""
    rng = _side_range(seg, number)
    if rng is None:
        return None
    lo, hi, side = rng
    # A single-address block ("100..100") interpolates to the middle rather than an endpoint: with no
    # range there is no information about where along the block it sits, and the midpoint bounds the
    # error at half a block instead of a whole one.
    frac = 0.5 if hi == lo else (number - lo) / (hi - lo)
    if end_offset_m:
        # Squeeze [0,1] into [d, 1-d] so the first/last house is not placed on the intersection.
        length = _segment_length(seg.pts)
        d = min(end_offset_m / length, _END_OFFSET_MAX_FRAC) if length > 0 else 0.0
        frac = d + frac * (1 - 2 * d)
    x, y = _polyline_point(seg.pts, frac)
    if not offset_m:
        return x, y
    dx, dy = _direction_at(seg.pts, frac)
    # Left normal of (dx,dy) is (-dy,dx) in an x-east/y-north frame, which Lambert is; right is the
    # negation. Units are metres because the CRS is projected — the reason we never left it.
    nx, ny = (-dy, dx) if side < 0 else (dy, -dx)
    return x + nx * offset_m, y + ny * offset_m


def _direction_at(pts: tuple[tuple[float, float], ...], frac: float) -> tuple[float, float]:
    """Unit direction vector of the polyline at `frac` along it (by length)."""
    if len(pts) < 2:
        return (1.0, 0.0)
    a = _polyline_point(pts, max(0.0, frac - 0.01))
    b = _polyline_point(pts, min(1.0, frac + 0.01))
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = (dx * dx + dy * dy) ** 0.5
    if n <= 0:
        dx, dy = pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]
        n = (dx * dx + dy * dy) ** 0.5
    return (dx / n, dy / n) if n > 0 else (1.0, 0.0)


async def _download(cache_dir: Path) -> Path:
    """Fetch the RNF zip into `cache_dir` once."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{_STEM}.zip"
    if dest.exists() and dest.stat().st_size > 100_000_000:
        return dest
    print(f"  downloading {RNF_URL.rsplit('/', 1)[-1]} (~310 MB, once)…")
    async with http.make_session() as session:
        r = await session.get(RNF_URL, timeout=1800)
        if r.status_code != 200 or not r.content:
            raise RuntimeError(f"RNF download failed (HTTP {r.status_code})")
        dest.write_bytes(r.content)
    return dest


def _int_or_none(raw: object) -> int | None:
    """RNF address-range values are TEXT and may be blank or non-numeric ('1A')."""
    s = str(raw or "").strip()
    return int(s) if s.isdigit() else None


class RnfGeocoder:
    """Address -> (lat, lon) for Canada, offline, from the StatCan Road Network File."""

    def __init__(
        self, index: dict[tuple[str, str, str], list[Segment]], *,
        offset_m: float = OFFSET_M, end_offset_m: float = END_OFFSET_M,
    ) -> None:
        self._index = index
        self._offset_m = offset_m
        self._end_offset_m = end_offset_m
        self._to_wgs84 = Transformer.from_crs(RNF_CRS, _WGS84, always_xy=True)

    @property
    def street_count(self) -> int:
        return len(self._index)

    @classmethod
    def load(
        cls, cache_dir: Path, provinces: Iterable[str], *,
        offset_m: float = OFFSET_M, end_offset_m: float = END_OFFSET_M,
    ) -> RnfGeocoder:
        """Build (or reuse) the index for `provinces`, downloading the RNF once if needed."""
        wanted = sorted(set(provinces))
        cache = cache_dir / f"rnf_index_{'-'.join(wanted)}.pkl"
        if cache.exists():
            with cache.open("rb") as fh:
                return cls(pickle.load(fh), offset_m=offset_m, end_offset_m=end_offset_m)
        zip_path = asyncio.run(_download(cache_dir))
        index = cls._build_index(zip_path, wanted)
        with cache.open("wb") as fh:
            pickle.dump(index, fh, protocol=pickle.HIGHEST_PROTOCOL)
        return cls(index, offset_m=offset_m, end_offset_m=end_offset_m)

    @staticmethod
    def _build_index(
        zip_path: Path, provinces: list[str]
    ) -> dict[tuple[str, str, str], list[Segment]]:
        """One pass over the RNF, keeping only address-bearing segments in `provinces`.

        Keyed by (province, municipality, street) — a segment is filed under BOTH its left and right
        municipality, because a street can form the boundary between two and the address may be
        attributed to either side.
        """
        pruids = {PRUID[p] for p in provinces if p in PRUID}
        index: dict[tuple[str, str, str], list[Segment]] = {}
        with zipfile.ZipFile(zip_path) as zf:
            reader = shapefile.Reader(
                shp=zf.open(f"{_STEM}.shp"), dbf=zf.open(f"{_STEM}.dbf"),
                shx=zf.open(f"{_STEM}.shx"),
                encoding=_ENCODING, encodingErrors="replace",
            )
            for sr in reader.iterShapeRecords():
                d = sr.record.as_dict()
                if d.get("PRUID_L") not in pruids and d.get("PRUID_R") not in pruids:
                    continue
                lo_l, hi_l = _int_or_none(d.get("AFL_VAL")), _int_or_none(d.get("ATL_VAL"))
                lo_r, hi_r = _int_or_none(d.get("AFR_VAL")), _int_or_none(d.get("ATR_VAL"))
                if lo_l is None and lo_r is None:
                    continue  # ~60% of segments carry no address range at all
                pts = tuple((float(x), float(y)) for x, y in sr.shape.points)
                if not pts:
                    continue
                seg = Segment(lo_l, hi_l, lo_r, hi_r, pts)
                key = street_key(d.get("NAME") or "", d.get("TYPE") or "", d.get("DIR") or "")
                for pruid_f, csd_f in (("PRUID_L", "CSDNAME_L"), ("PRUID_R", "CSDNAME_R")):
                    pruid = d.get(pruid_f)
                    if pruid not in pruids:
                        continue
                    prov = next(p for p, u in PRUID.items() if u == pruid)
                    idx_key = (prov, normalize_city(d.get(csd_f)), key)
                    bucket = index.setdefault(idx_key, [])
                    if not bucket or bucket[-1] is not seg:
                        bucket.append(seg)
        return index

    def geocode(self, address: str | None, city: str | None, province: str) -> tuple[float, float] | None:
        """(lat, lon) for a Canadian street address, or None when it cannot be placed.

        Returns None rather than a guess whenever the street is unknown in that municipality or no
        segment's range spans the house number — a wrong coordinate is worse than none here, because
        every consumer treats a coordinate as evidence.
        """
        parsed = parse_address(address)
        if parsed is None:
            return None
        number, key = parsed
        segments = self._index.get((province.upper(), normalize_city(city), key))
        if not segments:
            return None
        # Among every segment whose range spans the number, take the TIGHTEST range. A street is
        # split into many segments and their ranges can overlap or be coarse; the narrowest range is
        # the most specific block, so first-match would settle for a vaguer one at random.
        best: tuple[int, Segment] | None = None
        for seg in segments:
            rng = _side_range(seg, number)
            if rng is None:
                continue
            width = rng[1] - rng[0]
            if best is None or width < best[0]:
                best = (width, seg)
        if best is None:
            return None
        xy = _interpolate(best[1], number, offset_m=self._offset_m,
                          end_offset_m=self._end_offset_m)
        if xy is None:
            return None
        lon, lat = self._to_wgs84.transform(xy[0], xy[1])
        return lat, lon
