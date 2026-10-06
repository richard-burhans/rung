"""Stage-1 extraction: a partial roster is never a full one, and a header names only its own field.

Regression tests for the 2026-10-06 whole-tree review's findings in `rung.sources.extract`.
"""

import asyncio
import io

import openpyxl
import pytest
from conftest import pg_conn

from rung import db
from rung.models import DispensaryRecord
from rung.sources import extract
from rung.text import geocode_query

# ── a roster fragment never replaces the whole ───────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _ScriptedSession:
    """Answers each GET from a list of payloads (an Exception instance is raised instead)."""

    def __init__(self, payloads: list[object]) -> None:
        self.payloads = list(payloads)

    async def get(self, url: str, **_kwargs: object) -> _Resp:
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return _Resp(payload)


def _dcc_license(n: int) -> dict:
    return {"licenseStatus": "Active", "licenseType": "Commercial - Retailer",
            "businessDbaName": f"Store {n}", "premiseStreetAddress": f"{n} Main St",
            "premiseLatitude": 34.0, "premiseLongitude": -118.0}


def test_a_failed_ca_dcc_sub_box_is_a_partial_roster_not_a_short_one() -> None:
    """The root box says 2000 licences, so the sweep splits into quadrants. Quadrant 3 timing out
    used to be swallowed, and the three that answered replaced the whole California roster."""
    session = _ScriptedSession([
        {"metadata": {"totalCount": 2000}, "data": [_dcc_license(0)]},   # root: over the page cap
        {"metadata": {"totalCount": 1}, "data": [_dcc_license(1)]},
        {"metadata": {"totalCount": 1}, "data": [_dcc_license(2)]},
        TimeoutError("sub-box timed out"),
        {"metadata": {"totalCount": 1}, "data": [_dcc_license(4)]},
    ])
    with pytest.raises(extract.PartialRoster):
        asyncio.run(extract._extract_ca_dcc("https://dcc.example/api", session))


def test_a_failed_ca_dcc_root_box_is_still_an_empty_answer() -> None:
    session = _ScriptedSession([TimeoutError("root timed out")])
    assert asyncio.run(extract._extract_ca_dcc("https://dcc.example/api", session)) == []


def _arcgis_page(n: int) -> dict:
    return {"exceededTransferLimit": True,
            "features": [{"attributes": {"name": f"Store {n}", "address": f"{n} Main St"}}]}


def test_the_arcgis_page_cap_is_a_partial_roster(monkeypatch) -> None:
    """At the cap with the server still flagging more rows, the pager used to print a warning and
    return the truncated rows as the whole roster."""
    monkeypatch.setattr(extract, "_ARCGIS_PAGE_CAP", 2)
    session = _ScriptedSession([_arcgis_page(1), _arcgis_page(2)])
    with pytest.raises(extract.PartialRoster):
        asyncio.run(extract._query_arcgis_layer("https://gis.example/FeatureServer/0", session))


def test_an_arcgis_page_that_is_not_an_object_is_a_partial_roster_after_rows() -> None:
    session = _ScriptedSession([_arcgis_page(1), None])
    with pytest.raises(extract.PartialRoster):
        asyncio.run(extract._query_arcgis_layer("https://gis.example/FeatureServer/0", session))


def test_one_async_handler_crash_is_that_states_failure_not_everyones(monkeypatch) -> None:
    """A malformed payload raised out of an async handler, escaped `gather`, and killed every
    state's extraction before any was persisted."""
    async def boom(url: str, session: object) -> list[DispensaryRecord]:
        raise AttributeError("'NoneType' object has no attribute 'get'")

    monkeypatch.setitem(extract._ASYNC_HANDLERS, "arcgis", boom)
    # It is this state's FAILURE — raised as one, so the run records `failed` with the reason. It is
    # not `[]`: that would be recorded as an `empty` capture, "the source had nothing".
    with pytest.raises(extract.ExtractionFailed, match="AttributeError"):
        asyncio.run(extract.extract_records("https://gis.example/x", "arcgis"))


def test_a_partial_roster_still_reaches_the_caller(monkeypatch) -> None:
    async def partial(url: str, session: object) -> list[DispensaryRecord]:
        raise extract.PartialRoster("page 3 failed")

    monkeypatch.setitem(extract._ASYNC_HANDLERS, "arcgis", partial)
    with pytest.raises(extract.PartialRoster):
        asyncio.run(extract.extract_records("https://gis.example/x", "arcgis"))


# ── a header maps to a field only when it IS that field ──────────────────────────────────────────


@pytest.mark.parametrize(("header", "field"), [
    ("Facility Type", None),              # a fact ABOUT the facility; shadowed "Facility Name"
    ("Capacity", None),                   # ended in "city"
    ("Business License Number", None),
    ("Store Status", None),
    ("Facility Name", "name"),
    ("Dispensary Address", "address"),
    ("Zip Code", "zip_code"),
    ("City", "city"),
    ("DBA", "name"),
])
def test_a_header_matches_whole_words_and_not_a_qualified_column(header: str, field: str | None) -> None:
    assert extract._match_field(header) == field


def test_a_facility_type_column_no_longer_shadows_the_facility_name() -> None:
    rows = [["Facility Type", "Facility Name", "Street Address", "City"],
            ["Retail", "Green Leaf", "1 Main St", "Springfield"],
            ["Retail", "Blue Bud", "2 Oak Ave", "Shelbyville"]]
    names = [record.name for record in extract._extract_rows(rows)]
    assert names == ["Green Leaf", "Blue Bud"]


def test_name_inference_never_takes_a_column_the_header_mapped() -> None:
    """Header 'Location | Address | City': no name synonym, so the name is inferred — and the
    longest free-text column, the street address, used to win and overwrite its own mapping."""
    rows = [["Shop", "100 Very Long Street Name Boulevard", "Springfield"],
            ["Bud Barn", "200 Another Long Street Name Avenue", "Shelbyville"],
            ["Leaf Lab", "300 Yet Another Lengthy Road Name", "Capital City"]]
    assert extract._infer_name_column(rows, exclude={1, 2}) != 1
    assert extract._infer_name_column(rows, exclude={1, 2}) == 0


def test_the_arizona_footer_rule_spares_the_town_of_page() -> None:
    assert extract._AZ_SKIP_RE.search("Page 3 of 9")
    assert not extract._AZ_SKIP_RE.search("Green Valley Dispensary 123 Lake Powell Blvd Page 86040")


# ── a spreadsheet on the csv path ─────────────────────────────────────────────────────────────────


def test_an_xlsx_roster_is_read_as_a_sheet_not_decoded_as_text() -> None:
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    for row in (["Business Name", "Address", "City", "Zip"],
                ["Green Leaf", "1 Main St", "Springfield", 62701],
                ["Blue Bud", "2 Oak Ave", "Shelbyville", 62565]):
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    rows = extract._spreadsheet_rows(buffer.getvalue())
    assert rows is not None
    records = extract._extract_rows(rows)
    assert [(r.name, r.zip_code) for r in records] == [("Green Leaf", "62701"), ("Blue Bud", "62565")]
    assert extract._spreadsheet_rows(b"name,address\nA,1 Main\n") is None


# ── coordinates ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("lat", "lon", "want"), [
    (49.28, -123.12, (49.28, -123.12)),
    ("49.28", "-123.12", (49.28, -123.12)),   # a numeric string is converted, not stored as text
    (0, 0, (None, None)),
    (95.0, -123.0, (None, None)),
    ("n/a", -123.0, (None, None)),
    (True, -123.0, (None, None)),
])
def test_bc_and_ca_coordinates_are_checked(lat: object, lon: object, want: tuple) -> None:
    record = extract._bc_lcrb_record({"name": "Shop", "latitude": lat, "longitude": lon})
    assert record is not None
    assert (record.latitude, record.longitude) == want


# ── roster history is taken after the geocode cache fills the rows ───────────────────────────────


def test_the_geocode_cache_fills_the_records_history_is_built_from() -> None:
    conn = pg_conn()
    db.create_tables(conn)
    record = DispensaryRecord(source="html", name="Shop", address="1 Main St", city="Carson City",
                              state="NV")
    query = geocode_query(record.address, record.city, record.state, record.zip_code)
    assert query is not None
    conn.execute("INSERT INTO geocode_cache (query, latitude, longitude, zip_code, city) "
                 "VALUES (%s, 39.16, -119.77, '89701', 'Carson City')", (query,))
    assert extract._fill_from_geocode_cache(conn, [record]) == 1
    assert (record.latitude, record.longitude, record.zip_code) == (39.16, -119.77, "89701")
    # A value the source published is never replaced: same cache key, coordinates already present.
    published = DispensaryRecord(source="html", name="Shop", address="1 Main St", city="Carson City",
                                 state="NV", latitude=39.0, longitude=-119.0)
    extract._fill_from_geocode_cache(conn, [published])
    assert (published.latitude, published.longitude, published.zip_code) == (39.0, -119.0, "89701")
