"""The county boundary file and the county population file must be the SAME Census vintage.

Both files are tracked, so CI can assert the join directly — and this is one of the few contracts CI
*can* check about the outside world, because both sides of it are in the repo.

**The bug this pins (2026-07-17).** `us_counties.geojson` carried Connecticut's pre-2022 EIGHT counties
(FIPS 09001-09015) while every current Census table keys CT as NINE PLANNING REGIONS (09110-09190). Only
the STORE side of a density analysis goes through the boundary file, so CT's stores landed on FIPS that
matched no population row and dropped, while CT's population rows — read straight from the CSV —
survived. Connecticut therefore entered ~13 analyses as nine counties with 1.7M people and **zero
dispensaries**: not an absent row, a *fabricated zero*, which propagates into every statistic computed
over it. `dispensary_density_svi` reported `09110,CT,977165,0,0.00 per 100k` for as long as the file was
stale.

Nothing errored, because an empty intersection is a perfectly valid join that returns zero. So the
invariant has to be asserted somewhere, and here is where it is cheapest.

**Checked PER STATE, and that is the whole point.** Nationally CT is 0.3% of counties, so a
whole-country match rate stays at ~99% while an entire state silently reads zero. An aggregate cannot
see a state-sized hole — which is exactly why this went unnoticed. See `data/us_counties.SOURCE.txt`
and `scripts/build_us_counties_geojson.py` (which runs the same check at build time).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "scripts" / "data"
_COUNTIES = _DATA / "us_counties.geojson"
_POP = _DATA / "county_population_2023.csv"
# State FIPS whose polygons legitimately have no county-level population row.
#   60/66/69/78 — American Samoa, Guam, N. Mariana Is., US Virgin Islands: no market, never scraped.
#   72 — PUERTO RICO, and this one is a REAL GAP, recorded here rather than hidden:
#        PR is a live medical market (`states.yml`) and we hold 323 geocoded dispensaries, but our
#        population CSV has only PR's state TOTAL (72/000) and `svi_2022_county.csv` has ZERO PR
#        municipios. So PR's 78 municipios join to nothing and its 323 stores are silently ABSENT from
#        every county-density analysis. That is the mirror of the CT bug and strictly milder: with no
#        population row there is no fabricated zero, so PR contributes nothing rather than a false
#        zero. It is an unfixed COVERAGE gap (acquire PEP + SVI municipio rows), NOT a vintage
#        mismatch, so it does not belong to this guard. See WORK_BACKLOG.md.
_NO_COUNTY_POPULATION = {"60", "66", "69", "72", "78"}


def _boundary_fips() -> set[str]:
    geo = json.loads(_COUNTIES.read_text(encoding="utf-8"))
    return {f["properties"]["STATE"] + f["properties"]["COUNTY"] for f in geo["features"]}


def _population_fips() -> set[str]:
    with _POP.open(encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        assert {"state_fips", "county_fips"} <= set(reader.fieldnames or ()), (
            f"{_POP.name} schema changed; this guard reads state_fips+county_fips"
        )
        # county_fips == '000' is the state-total row, not a county — it has no polygon by design.
        return {r["state_fips"] + r["county_fips"] for r in reader if r["county_fips"] != "000"}


def test_every_population_row_has_a_polygon() -> None:
    """A population row with no polygon is the exact shape of the CT bug: it survives the join and
    contributes a real population against a store count of zero."""
    orphans = sorted(_population_fips() - _boundary_fips())
    assert not orphans, (
        f"{len(orphans)} population rows have no county polygon: {orphans[:10]}\n"
        "Each would contribute a real population and a FABRICATED ZERO store count.\n"
        "The boundary file and the population file are one vintage decision — move them together."
    )


def test_no_state_sized_hole() -> None:
    """The per-state form of the above — the one that would actually have caught this.

    Kept as a separate test because it is the *diagnostic*: it names the state, which is the fact that
    turns "99.7% of rows joined" into "Connecticut reads zero".
    """
    orphans = _population_fips() - _boundary_fips()
    by_state: dict[str, list[str]] = {}
    for f in sorted(orphans):
        by_state.setdefault(f[:2], []).append(f)
    fatal = {st: fs for st, fs in by_state.items() if len(fs) > 2}
    assert not fatal, f"state-sized vintage hole (>2 counties with no polygon): { {k: len(v) for k, v in fatal.items()} }"


def test_feature_schema_is_the_legacy_contract() -> None:
    """The full feature shape consumers read — INCLUDING the `id` member, which is not a property.

    Pinned because dropping it shipped: the regenerated file reproduced `properties` field-for-field and
    omitted the top-level `id`, and nothing complained — it parsed, it drew, it passed the vintage
    guard. `county_hhi_map` then died with `KeyError: 'id'` (it keys on `f["id"]` twice, and Plotly's
    choropleth `featureidkey` defaults to `id`). A GeoJSON feature's identity lives OUTSIDE its
    properties; "preserve the schema" has to mean the whole feature.
    """
    geo = json.loads(_COUNTIES.read_text(encoding="utf-8"))
    for f in geo["features"][:50]:
        assert set(f) == {"type", "id", "properties", "geometry"}, f"feature shape changed: {sorted(f)}"
        assert set(f["properties"]) == {"GEO_ID", "STATE", "COUNTY", "NAME", "LSAD", "CENSUSAREA"}
    # `id` is the 5-digit FIPS and must agree with STATE+COUNTY — two encodings of one fact, so drift
    # between them is silent and would misdraw the choropleth rather than raise.
    bad = [
        f["id"] for f in geo["features"] if f["id"] != f["properties"]["STATE"] + f["properties"]["COUNTY"]
    ]
    assert not bad, f"id disagrees with STATE+COUNTY for {len(bad)} features: {bad[:5]}"


def test_connecticut_is_nine_planning_regions() -> None:
    """The specific regression, pinned by name.

    CT abolished county government for statistical purposes in 2022. Eight `090xx` counties here means
    the file has been rolled back to a pre-2022 vintage while the attribute tables have not.
    """
    ct = sorted(f for f in _boundary_fips() if f.startswith("09"))
    assert ct == ["09110", "09120", "09130", "09140", "09150", "09160", "09170", "09180", "09190"], (
        f"CT should be nine planning regions (09110-09190), got {ct}"
    )


def test_boundary_states_all_have_population() -> None:
    """The reverse direction: a polygon with no population row is fine only for the jurisdictions we
    have knowingly excluded (above). A NEW one means the population file is now the stale side."""
    unmatched = _boundary_fips() - _population_fips()
    surprising = sorted(f for f in unmatched if f[:2] not in _NO_COUNTY_POPULATION)
    assert not surprising, (
        f"polygons with no population row outside the known exclusions: {surprising[:10]}\n"
        "Stores there would drop out of county analyses silently."
    )
