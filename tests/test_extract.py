"""Pure-function tests for the extraction logic.

No network, browser, or AI — these exercise the parsing/heuristic code that the
recent hardening changed and is easy to silently regress.
"""

import asyncio
import re as _re

from rung.sources.extract import (
    _ARCGIS_PAGE_SIZE,
    _ARCGIS_SERVICE_RE,
    _aglc_record,
    _arcgis_attr,
    _arcgis_record,
    _bc_lcrb_record,
    _ca_dcc_record,
    _clean,
    _extract_address_blocks,
    _extract_bc_lcrb,
    _extract_csv,
    _extract_html,
    _extract_kml,
    _extract_on_agco,
    _header_map,
    _infer_name_column,
    _location_fraction,
    _match_field,
    _query_arcgis_layer,
    _slga_record,
    _split_name_address,
    _unmerge_name_overflow,
    _va_cca_record,
)


class _FakeArcgisResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeArcgisSession:
    """Serves a layer of `total` features paged by resultOffset, flagging more via
    exceededTransferLimit — to prove _query_arcgis_layer pages past the first window."""

    def __init__(self, total):
        self.total = total
        self.offsets = []

    async def get(self, url, timeout=None):
        offset = int(_re.search(r"resultOffset=(\d+)", url).group(1))
        self.offsets.append(offset)
        rows = [{"attributes": {"name": f"Store {i}"}}
                for i in range(offset, min(offset + _ARCGIS_PAGE_SIZE, self.total))]
        return _FakeArcgisResp(
            {"features": rows, "exceededTransferLimit": offset + _ARCGIS_PAGE_SIZE < self.total}
        )


def test_arcgis_pages_past_the_first_window():
    session = _FakeArcgisSession(_ARCGIS_PAGE_SIZE + 50)  # one full page + a partial
    records = asyncio.run(_query_arcgis_layer("https://x/FeatureServer/0", session))
    assert len(records) == _ARCGIS_PAGE_SIZE + 50          # not truncated at the first page
    assert session.offsets == [0, _ARCGIS_PAGE_SIZE]       # exactly two pages fetched


def test_arcgis_single_page_stops_immediately():
    session = _FakeArcgisSession(10)
    records = asyncio.run(_query_arcgis_layer("https://x/FeatureServer/0", session))
    assert len(records) == 10 and session.offsets == [0]   # short page → one request

# An en dash, as the state sites actually use to pack "NAME – ADDRESS" cells.
DASH = "–"


# ── _split_name_address ──────────────────────────────────────────────────────

def test_split_strips_license_tag():
    name, addr = _split_name_address(f"DAZED! {DASH} 2548 W Desert Inn Rd {DASH} Adult Use")
    assert name == "DAZED!"
    assert addr == "2548 W Desert Inn Rd"  # trailing "– Adult Use" dropped


def test_split_simple_name_address():
    assert _split_name_address(f"Green Leaf {DASH} 100 Main St") == ("Green Leaf", "100 Main St")


def test_no_split_when_tail_is_not_a_street():
    # "Reno" is a city, not a street — must not be split off as an address.
    assert _split_name_address("Beehive Farmacy - Reno") == ("Beehive Farmacy - Reno", None)


def test_no_split_without_separator():
    assert _split_name_address("Cookies Florida") == ("Cookies Florida", None)


# ── _infer_name_column ───────────────────────────────────────────────────────

def test_infer_name_column_picks_text_over_flags():
    rows = [["Y", "DAZED!"], ["N", "SOCIETY"], ["Y", "BEYOND HELLO"]]
    assert _infer_name_column(rows) == 1  # col 0 is Y/N, col 1 holds the names


def test_infer_name_column_none_when_no_text_column():
    rows = [["Y", "1"], ["N", "2"], ["Y", "3"]]
    assert _infer_name_column(rows) is None


# ── _header_map threshold (the _extract_pdf repeated-header bug) ──────────────

def test_header_map_distinguishes_header_from_data_row():
    header = ["Date", "Open", "Product", "Dispensary name", "Address",
              "City", "State", "Zip Code", "Phone", "Website"]
    data = ["", "", "", "Zen Leaf Dispensary", "123 Main St",
            "Reno", "NV", "89501", "", ""]
    # A real header matches many fields; a data row whose name merely *contains*
    # a synonym word ("Dispensary") matches only one — must stay below the ≥3 cutoff.
    assert len(_header_map(header)) >= 3
    assert len(_header_map(data)) < 3


# ── _match_field ─────────────────────────────────────────────────────────────

def test_match_field_company_synonym():
    assert _match_field("Company") == "name"
    assert _match_field("Company Name") == "name"


def test_match_field_zip_beats_generic():
    assert _match_field("Zip Code") == "zip_code"


# ── _clean ───────────────────────────────────────────────────────────────────

def test_clean_strips_zero_width():
    assert _clean("​Ascend Dispensary") == "Ascend Dispensary"
    assert _clean("  Green   Leaf  ") == "Green Leaf"
    assert _clean(None) is None


# ── _location_fraction ───────────────────────────────────────────────────────

def test_location_fraction():
    from rung.models import DispensaryRecord
    recs = [
        DispensaryRecord(source="html", name="A", address="1 St"),
        DispensaryRecord(source="html", name="B"),
    ]
    assert _location_fraction(recs) == 0.5
    assert _location_fraction([]) == 0.0


# ── _extract_html ────────────────────────────────────────────────────────────

def test_html_inferred_name_with_split():
    html = f"""
    <table>
      <tr><th>Southern Nevada Retail Stores</th><th>Delivery</th></tr>
      <tr><td>DAZED! {DASH} 2548 W Desert Inn Rd {DASH} Adult Use</td><td>N</td></tr>
      <tr><td>SOCIETY {DASH} 4640 Paradise Rd {DASH} Adult Use</td><td>Y</td></tr>
      <tr><td>BEYOND {DASH} 100 Main St {DASH} Medical</td><td>Y</td></tr>
    </table>"""
    recs = _extract_html(html)
    assert len(recs) == 3
    assert recs[0].name == "DAZED!"
    assert recs[0].address == "2548 W Desert Inn Rd"


def test_html_header_named_table():
    html = """
    <table>
      <tr><th>Dispensary Name</th><th>Address</th><th>City</th><th>Zip</th></tr>
      <tr><td>Green Leaf</td><td>1 Main St</td><td>Reno</td><td>89501</td></tr>
      <tr><td>Happy Buds</td><td>2 Oak Ave</td><td>Las Vegas</td><td>89101</td></tr>
    </table>"""
    recs = _extract_html(html)
    assert {r.name for r in recs} == {"Green Leaf", "Happy Buds"}
    assert recs[0].city == "Reno"  # address column present → name not split


# ── _repair_swapped_address ──────────────────────────────────────────────────
# MD's dispensary locator is headed `Dispensary | County | Address` but its data rows are
# `name | street | county`. Trusting the header filed the COUNTY as the address and discarded
# the street, so all 120 rows loaded and none could ever match a company store.

def test_html_misordered_header_files_the_street_not_the_county():
    html = """
    <table>
      <tr><th>Dispensary</th><th>County</th><th>Address</th></tr>
      <tr><td>Ascend - Aberdeen</td><td>226 S Philadelphia Ave Aberdeen MD 21001</td><td>Harford</td></tr>
      <tr><td>Ascend - Crofton</td><td>1657 Crofton Blvd Crofton MD 21114</td><td>Anne Arundel</td></tr>
      <tr><td>Zen Leaf - Towson</td><td>1608 E Joppa Rd Towson MD 21286</td><td>Baltimore</td></tr>
    </table>"""
    recs = _extract_html(html)
    assert [r.address for r in recs] == [
        "226 S Philadelphia Ave Aberdeen MD 21001",
        "1657 Crofton Blvd Crofton MD 21114",
        "1608 E Joppa Rd Towson MD 21286",
    ]


def test_html_correct_header_is_never_second_guessed():
    # The repair must not touch a table whose header already agrees with its data.
    html = """
    <table>
      <tr><th>Dispensary</th><th>Address</th><th>County</th></tr>
      <tr><td>Green Leaf</td><td>1 Main St</td><td>Harford</td></tr>
      <tr><td>Happy Buds</td><td>2 Oak Ave</td><td>Howard</td></tr>
      <tr><td>Third Store</td><td>3 Elm Rd</td><td>Carroll</td></tr>
    </table>"""
    recs = _extract_html(html)
    assert [r.address for r in recs] == ["1 Main St", "2 Oak Ave", "3 Elm Rd"]


def test_html_licence_number_column_is_not_mistaken_for_a_street():
    # WA/MT ship a city-only `address` alongside a numeric licence column. STREET_RE demands
    # digits FOLLOWED BY A SPACE, so a bare licence number is not a candidate and nothing swaps.
    html = """
    <table>
      <tr><th>Dispensary</th><th>Address</th><th>License</th></tr>
      <tr><td>Green Leaf</td><td>Spokane</td><td>231001</td></tr>
      <tr><td>Happy Buds</td><td>Tacoma</td><td>231002</td></tr>
      <tr><td>Third Store</td><td>Yakima</td><td>231003</td></tr>
    </table>"""
    recs = _extract_html(html)
    assert [r.address for r in recs] == ["Spokane", "Tacoma", "Yakima"]


def test_html_two_street_like_columns_are_ambiguous_so_nothing_swaps():
    # Mailing vs physical address: we cannot tell which the header meant. Leave it alone.
    html = """
    <table>
      <tr><th>Dispensary</th><th>Mailing</th><th>Physical</th><th>Address</th></tr>
      <tr><td>Green Leaf</td><td>1 Main St</td><td>9 Oak Ave</td><td>Harford</td></tr>
      <tr><td>Happy Buds</td><td>2 Main St</td><td>8 Oak Ave</td><td>Howard</td></tr>
      <tr><td>Third Store</td><td>3 Main St</td><td>7 Oak Ave</td><td>Carroll</td></tr>
    </table>"""
    recs = _extract_html(html)
    assert [r.address for r in recs] == ["Harford", "Howard", "Carroll"]


def test_html_swap_needs_enough_rows_to_be_evidence():
    # Two rows are not evidence of a systematic header defect.
    html = """
    <table>
      <tr><th>Dispensary</th><th>County</th><th>Address</th></tr>
      <tr><td>Green Leaf</td><td>1 Main St</td><td>Harford</td></tr>
      <tr><td>Happy Buds</td><td>2 Oak Ave</td><td>Howard</td></tr>
    </table>"""
    recs = _extract_html(html)
    assert [r.address for r in recs] == ["Harford", "Howard"]


def test_extract_page_falls_through_to_line_blocks_only_as_a_last_resort():
    """The line-block rung must never change a page that already yields rows.

    It is ordered strictly last (`table or address_block or line_block`), so the only pages it
    can reach are the ones we currently get NOTHING from. Alabama's roster is one; a table page
    that also happens to contain a line-block address must still be read as a table.
    """
    from rung.sources.extract import _extract_page

    line_block = "<p>Callie's Apothecary<br/>5232 Atlanta Highway<br/>Montgomery, AL 36109</p>"
    assert [r.name for r in _extract_page(line_block)] == ["Callie's Apothecary"]

    with_table = """
    <table>
      <tr><th>Dispensary</th><th>Address</th></tr>
      <tr><td>Green Leaf</td><td>1 Main St</td></tr>
      <tr><td>Happy Buds</td><td>2 Oak Ave</td></tr>
    </table>""" + line_block
    assert [r.name for r in _extract_page(with_table)] == ["Green Leaf", "Happy Buds"]


# ── atlist ───────────────────────────────────────────────────────────────────
# NJ's CRC "Find a Dispensary" page has one <table> and it lists DELIVERY SERVICES; the sibling
# `/dispensaries/roll-up/` page is a product-RECALL table. The roster is the embedded Atlist map.

def test_atlist_marker_becomes_a_roster_record():
    from rung.sources.extract import _atlist_record

    got = _atlist_record({
        "name": "Fresh Elizabeth",
        "formattedAddress": "460 Maple Ave, Elizabeth, NJ 07202, USA",
        "lat": 40.6530201, "long": -74.213966,
        "buttonLink": "https://freshcannabis.co/",
    })
    assert got is not None
    assert (got.name, got.address, got.city, got.state, got.zip_code) == (
        "Fresh Elizabeth", "460 Maple Ave", "Elizabeth", "NJ", "07202")
    assert (got.latitude, got.longitude) == (40.6530201, -74.213966)
    assert got.source == "atlist"


def test_atlist_keeps_coordinates_when_the_address_will_not_parse():
    # "NJ-66, Neptune Township, NJ, USA" is a road, not a street number. The licensee is real and
    # its coordinates pair via compare's proximity tier — dropping it would lose a real store.
    from rung.sources.extract import _atlist_record

    got = _atlist_record({"name": "Zen Leaf", "formattedAddress": "NJ-66, Neptune Township, NJ, USA",
                          "lat": 40.2281881, "long": -74.03})
    assert got is not None
    assert got.address is None and got.zip_code is None
    assert (got.latitude, got.longitude) == (40.2281881, -74.03)


def test_atlist_skips_a_nameless_marker_and_tolerates_missing_coords():
    from rung.sources.extract import _atlist_record

    assert _atlist_record({"formattedAddress": "1 Main St, Erie, PA 16501, USA"}) is None
    got = _atlist_record({"name": "No Pin", "formattedAddress": "1 Main St, Erie, PA 16501, USA",
                          "lat": None, "long": "not-a-number"})
    assert got is not None and got.latitude is None and got.longitude is None
    assert got.zip_code == "16501"


def test_atlist_map_id_is_taken_from_the_share_url():
    from rung.sources.extract import _ATLIST_MAP_ID_RE

    url = "https://my.atlist.com/map/8bed33fa-9b8c-4c51-bb33-74cd0d98628a?share=true"
    assert _ATLIST_MAP_ID_RE.search(url).group(1) == "8bed33fa-9b8c-4c51-bb33-74cd0d98628a"
    assert _ATLIST_MAP_ID_RE.search("https://example.com/map/not-a-uuid") is None


def test_atlist_is_a_handled_list_type():
    from rung.sources.extract import HANDLED_LIST_TYPES

    assert "atlist" in HANDLED_LIST_TYPES


def test_html_full_identity_dedup_keeps_multilocation_operator():
    # The MT regression: a licensee with several addressless locations must NOT
    # collapse to one row (dedup is (name,address,city,phone), not (name,address)).
    html = """
    <table>
      <tr><th>Licensee's Name</th><th>City</th></tr>
      <tr><td>ACME LLC</td><td>Helena</td></tr>
      <tr><td>ACME LLC</td><td>Billings</td></tr>
      <tr><td>ACME LLC</td><td>Bozeman</td></tr>
    </table>"""
    recs = _extract_html(html)
    assert len(recs) == 3
    assert {r.city for r in recs} == {"Helena", "Billings", "Bozeman"}


def test_html_unrelated_text_table_rejected():
    # Inferred-name table with no location signal must be dropped, not scooped up.
    html = """
    <table>
      <tr><th>Board Members</th></tr>
      <tr><td>John Smith</td></tr>
      <tr><td>Jane Doe</td></tr>
      <tr><td>Bob Jones</td></tr>
    </table>"""
    assert _extract_html(html) == []


def test_html_aggregates_across_tables():
    html = """
    <table>
      <tr><th>Dispensary Name</th><th>Address</th></tr>
      <tr><td>North One</td><td>1 N St</td></tr>
    </table>
    <table>
      <tr><th>Dispensary Name</th><th>Address</th></tr>
      <tr><td>South One</td><td>1 S St</td></tr>
    </table>"""
    recs = _extract_html(html)
    assert {r.name for r in recs} == {"North One", "South One"}


# ── _extract_address_blocks (non-table card/list pages) ──────────────────────

def test_blocks_multi_address_per_brand():
    # DE pattern: one <p> per operator, name in an <a>, several addresses as
    # direct text. Each address becomes its own record under the same name.
    html = """
    <div class="row"><div class="col">
      <p><a href="x">Green Brand</a>
         <br>100 Main St, Dover, DE 19901
         <br>200 Oak Ave, Lewes, DE 19958</p>
    </div></div>"""
    recs = _extract_address_blocks(html)
    assert [(r.name, r.address, r.city, r.zip_code) for r in recs] == [
        ("Green Brand", "100 Main St", "Dover", "19901"),
        ("Green Brand", "200 Oak Ave", "Lewes", "19958"),
    ]


def test_blocks_minimality_keeps_per_entry_names():
    # Two operators in sibling <p>s inside one <div>. Minimality must keep each
    # address with its own name — not assign the parent div's first name to both.
    html = """
    <div>
      <p><a>Brand A</a> 1 First St, Dover, DE 19901</p>
      <p><a>Brand B</a> 2 Second St, Lewes, DE 19958</p>
    </div>"""
    recs = _extract_address_blocks(html)
    assert {(r.name, r.address) for r in recs} == {
        ("Brand A", "1 First St"), ("Brand B", "2 Second St")}


def test_blocks_name_from_heading_or_leading_text():
    heading = "<li><h3>Cool Dispensary</h3> 50 Pine Rd, Reno, NV 89501</li>"
    assert _extract_address_blocks(heading)[0].name == "Cool Dispensary"
    plain = "<p>Plain Name 10 A St, Reno, NV 89501</p>"
    assert _extract_address_blocks(plain)[0].name == "Plain Name"


def test_blocks_empty_without_address():
    assert _extract_address_blocks("<div><p>No address here, just text.</p></div>") == []


# ── ArcGIS ───────────────────────────────────────────────────────────────────

def test_arcgis_service_url_detection():
    assert _ARCGIS_SERVICE_RE.search("/services/Foo/FeatureServer/0")
    assert _ARCGIS_SERVICE_RE.search("/services/Foo/FeatureServer")
    assert _ARCGIS_SERVICE_RE.search("/rest/services/Bar/MapServer/2")
    # An app/experience page is not a direct service URL.
    assert not _ARCGIS_SERVICE_RE.search("/experience/abc123/")


def test_arcgis_attr_recognizes_dispensary_field():
    attrs = {"Dispensary": "Latitude Dispensary", "Address": "1812 Highway 52",
             "Zip_code": "65026"}
    assert _arcgis_attr(attrs, "dispensar", "name") == "Latitude Dispensary"
    assert _arcgis_attr(attrs, "zip", "postal") == "65026"
    assert _arcgis_attr(attrs, "phone") is None


# The AGCO (Ontario) roster's field shape — Province, no-space PostalCode, Website,
# and lat/lng as plain attributes (docs/canada_expansion.md §2).
_AGCO_ATTRS = {
    "PremisesName": "True North Cannabis Co.", "StreetAddress": "435 Yonge St",
    "City": "Toronto", "Province": "ON", "PostalCode": "M5B1T3",
    "Website": "https://truenorthcannabisco.com", "Latitude": 43.6606, "Longitude": -79.3832,
    "ApplicationStatus": "Authorized to Open",
}


def test_arcgis_record_maps_province_website_and_coords():
    rec = _arcgis_record({"attributes": _AGCO_ATTRS})
    assert rec is not None
    assert rec.name == "True North Cannabis Co."
    assert rec.state == "ON"
    assert rec.zip_code == "M5B1T3"
    assert rec.website == "https://truenorthcannabisco.com"
    assert rec.latitude == 43.6606 and rec.longitude == -79.3832


def test_arcgis_record_drops_invalid_or_placeholder_coords():
    rec = _arcgis_record({"attributes": {"Name": "X", "Latitude": 0, "Longitude": 0}})
    assert rec.latitude is None and rec.longitude is None
    rec = _arcgis_record({"attributes": {"Name": "X", "Latitude": 143.0, "Longitude": -79.0}})
    assert rec.latitude is None and rec.longitude is None


class _FakeAgcoSession:
    """Serves the Experience-app item data, then the resolved layer's query."""

    def __init__(self):
        self.urls = []

    async def get(self, url, timeout=None):
        self.urls.append(url)
        if "/sharing/rest/content/items/" in url:
            return _FakeArcgisResp({"dataSources": {"dataSource_1": {
                "type": "WEB_MAP",
                "childDataSourceJsons": {
                    "in_progress": {"url": "https://svc/rest/services/Application_in_progress_20250620/FeatureServer/0"},
                    "authorized": {"url": "https://svc/rest/services/Authorized_to_open_20250620/FeatureServer/0"},
                    "cancelled": {"url": "https://svc/rest/services/Cancelled_Authorizations_20250620/FeatureServer/0"},
                },
            }}})
        return _FakeArcgisResp({"features": [{"attributes": _AGCO_ATTRS}]})


def test_on_agco_resolves_the_authorized_layer_from_the_app_item():
    session = _FakeAgcoSession()
    records = asyncio.run(_extract_on_agco(
        "https://experience.arcgis.com/experience/86b8b6c8725a4a6484ce60fbd0447ca6", session
    ))
    assert len(records) == 1 and records[0].state == "ON"
    # The date-stamped layer was resolved at runtime, and only the authorized layer queried.
    assert any("Authorized_to_open" in u for u in session.urls)
    assert not any("in_progress" in u or "Cancelled" in u for u in session.urls)


def test_on_agco_without_an_item_id_returns_nothing():
    assert asyncio.run(_extract_on_agco("https://agco.ca/no-item-here", _FakeAgcoSession())) == []


# ── Alberta AGLC record mapping ──────────────────────────────────────────────

def _aglc_row(**over):
    base = {
        "name": "13th Floor Cannabis", "city": "AIRDRIE",
        "address": "1005-401 COOPERS BLVD SW", "address2": None,
        "province": "AB", "zip_code": "T4B 4J3", "phone": "4039601313",
    }
    base.update(over)
    return base


def test_aglc_record_maps_alberta_retail_row():
    rec = _aglc_record(_aglc_row())
    assert rec is not None
    assert rec.name == "13th Floor Cannabis" and rec.state == "AB"
    assert rec.address == "1005-401 COOPERS BLVD SW"
    assert rec.zip_code == "T4B 4J3" and rec.phone == "4039601313"


def test_aglc_record_drops_out_of_province_licensee_sites():
    # The report lists out-of-province supplier/producer sites — not Alberta stores.
    assert _aglc_record(_aglc_row(province="BC")) is None
    assert _aglc_record(_aglc_row(province="ON")) is None
    assert _aglc_record(_aglc_row(name=None)) is None


def test_aglc_record_joins_address_lines():
    rec = _aglc_record(_aglc_row(address2="UNIT 4"))
    assert rec.address == "1005-401 COOPERS BLVD SW, UNIT 4"


# ── BC LCRB record mapping ───────────────────────────────────────────────────

_BC_OBJ = {
    "id": "2ca5208f", "addressCity": "100 Mile House", "addressPostal": "V0K2E0",
    "addressStreet": "355 Birch Avenue", "name": "Club Cannabis",
    "license": "450228", "phone": "2503952545",
    "latitude": 51.64324, "longitude": -121.29551, "isOpen": True,
}


def test_bc_lcrb_record_maps_establishment():
    rec = _bc_lcrb_record(_BC_OBJ)
    assert rec is not None
    assert rec.name == "Club Cannabis" and rec.state == "BC"
    assert rec.zip_code == "V0K2E0"
    assert rec.latitude == 51.64324 and rec.longitude == -121.29551


def test_bc_lcrb_keeps_not_yet_open_and_drops_nameless():
    assert _bc_lcrb_record({**_BC_OBJ, "isOpen": False}) is not None  # licensed = roster
    assert _bc_lcrb_record({**_BC_OBJ, "name": ""}) is None


def test_extract_bc_lcrb_parses_the_bare_list():
    class _Session:
        async def get(self, url, timeout=None):
            return _FakeArcgisResp([_BC_OBJ, {"name": ""}, "junk"])

    records = asyncio.run(_extract_bc_lcrb("https://x/api/establishments/map", _Session()))
    assert len(records) == 1 and records[0].name == "Club Cannabis"


# ── Saskatchewan SLGA record mapping ─────────────────────────────────────────

def _slga_row(**over):
    base = {
        "name": "Wiid Boutique Inc. - Regina", "city": "Regina",
        "address": "4554 Albert St", "website": "www.wiidsk.ca", "status": "Active",
    }
    base.update(over)
    return base


def test_slga_record_maps_active_retailer():
    rec = _slga_record(_slga_row())
    assert rec is not None
    assert rec.name == "Wiid Boutique Inc. - Regina" and rec.state == "SK"
    assert rec.city == "Regina" and rec.website == "www.wiidsk.ca"


def test_slga_record_drops_inactive_and_nameless_and_na_website():
    assert _slga_record(_slga_row(status="Cancelled")) is None
    assert _slga_record(_slga_row(status="Suspended")) is None
    assert _slga_record(_slga_row(name=None)) is None
    assert _slga_record(_slga_row(website="N/A")).website is None  # placeholder website dropped


# ── KML point coordinates (Manitoba's Google My Maps export) ─────────────────

_MB_KML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
    b'<Placemark><name>Altona Motor Hotel</name>'
    b'<Point><coordinates>\n  -97.555997,49.103839,0\n  </coordinates></Point></Placemark>'
    b'<Placemark><name>No Coords Store</name></Placemark>'
    b'<Placemark><name>Null Island</name>'
    b'<Point><coordinates>0,0,0</coordinates></Point></Placemark>'
    b'</Document></kml>'
)


def test_kml_parses_point_coordinates():
    records = _extract_kml(_MB_KML)
    assert len(records) == 3
    by_name = {r.name: r for r in records}
    # lng,lat order in KML → (lat, lng) on the record
    assert by_name["Altona Motor Hotel"].latitude == 49.103839
    assert by_name["Altona Motor Hotel"].longitude == -97.555997
    assert by_name["No Coords Store"].latitude is None       # no <Point> → no coords
    assert by_name["Null Island"].latitude is None            # 0,0 placeholder dropped

def _ca_lic(**over):
    base = {
        "licenseStatus": "Active", "licenseType": "Commercial -  Retailer",
        "businessDbaName": "Green Store", "businessLegalName": "Green LLC",
        "premiseStreetAddress": "1 Main St", "premiseCity": "Oakland",
        "premiseState": "CA", "premiseZipCode": "94601", "businessPhone": "510-555-0100",
    }
    return base | over


def test_ca_dcc_active_retailer_mapped():
    rec = _ca_dcc_record(_ca_lic())
    assert rec is not None
    assert (rec.name, rec.city, rec.zip_code, rec.state) == ("Green Store", "Oakland", "94601", "CA")


def test_ca_dcc_skips_inactive_and_non_retailer():
    assert _ca_dcc_record(_ca_lic(licenseStatus="Surrendered")) is None
    assert _ca_dcc_record(_ca_lic(licenseType="Commercial -  Distributor")) is None


def test_ca_dcc_falls_back_to_legal_name():
    rec = _ca_dcc_record(_ca_lic(businessDbaName=None))
    assert rec is not None and rec.name == "Green LLC"


# ── _extract_csv ─────────────────────────────────────────────────────────────

def test_extract_csv():
    text = "name,address,city,zip\nGreen,1 Main St,Reno,89501\nBlue,2 Oak Ave,Tahoe,89001\n"
    recs = _extract_csv(text)
    assert len(recs) == 2
    assert recs[0].name == "Green"
    assert recs[0].zip_code == "89501"


def test_list_type_vocabulary_consistent() -> None:
    """The list_type producer (state_lists._classify) must only emit values the extract
    dispatcher handles, so the two vocabularies can't silently drift (audit N5)."""
    from rung.sources.extract import HANDLED_LIST_TYPES
    from rung.sources.state_lists import _classify

    expected = {
        "https://x.gov/list.pdf": "pdf",
        "https://x.gov/data.csv": "csv",
        "https://www.google.com/maps/d/viewer?mid=abc": "kml",
        "https://services.arcgis.com/abc/FeatureServer/0": "arcgis",
        "https://search.x.gov/verification": "lookup",
        "https://x.gov/dispensaries": "html",
    }
    for url, want in expected.items():
        got = _classify(url)
        assert got == want, f"{url} -> {got!r}"
        assert got in HANDLED_LIST_TYPES
    # `ca_dcc` is an override type (not emitted by _classify) but must still be dispatched.
    assert "ca_dcc" in HANDLED_LIST_TYPES


# ── Arizona DHS establishments PDF handler (az_dhs) ──────────────────────────

def test_az_column_bucketing():
    from rung.sources.extract import _az_column
    # a word is assigned to the right-most column whose header it clears
    assert _az_column(42) == "status"
    assert _az_column(116) == "cert"
    assert _az_column(258) == "estname"
    assert _az_column(420) == "dba"
    assert _az_column(515) == "street"
    assert _az_column(619) == "city"
    assert _az_column(700) == "zip"


def test_az_dhs_is_handled_list_type():
    from rung.sources.extract import HANDLED_LIST_TYPES
    assert "az_dhs" in HANDLED_LIST_TYPES


# ── Colorado MED 'Stores' Google-Sheet CSV handler (co_med) ──────────────────

def test_co_med_prefers_dba_over_facility_name():
    from rung.sources.extract import HANDLED_LIST_TYPES, _extract_co_med
    assert "co_med" in HANDLED_LIST_TYPES
    csv_text = (
        "License Number,Facility Name,DBA,Facility Type,Street,City,ZIP Code\n"
        "402-1,1-11 LLC,1:11,Medical Marijuana Store,17034 Highway 17,Moffat,81143\n"
        "402-2,NoBrand LLC,,Medical Marijuana Store,5 Main St,Denver,80202\n"
    )
    recs = _extract_co_med(csv_text)
    assert [r.name for r in recs] == ["1:11", "NoBrand LLC"]  # DBA preferred, legal-name fallback
    assert recs[0].address == "17034 Highway 17" and recs[0].zip_code == "81143"


def test_ma_ccc_keeps_active_storefronts_only():
    from rung.sources.extract import HANDLED_LIST_TYPES, _extract_ma_ccc
    assert "ma_ccc" in HANDLED_LIST_TYPES
    csv_text = (
        "BUSINESS_NAME,LICENSE_TYPE,LICENSE_STATUS,ADDRESS_1,CITY,ZIP_CODE,latitude,longitude\n"
        "Store A LLC,Marijuana Retailer,Active,1 Main St,Boston,02118,42.3,-71.0\n"
        "Grow Co,Marijuana Cultivator,Active,9 Farm Rd,Athol,01331,42.5,-72.2\n"
        "MTC Inc,Medical Marijuana Treatment Center,Active,5 Elm St,Salem,01970,42.5,-70.9\n"
        "Closed LLC,Marijuana Retailer,Revoked,7 Oak St,Lowell,01852,42.6,-71.3\n"
    )
    recs = _extract_ma_ccc(csv_text)
    assert [r.name for r in recs] == ["Store A LLC", "MTC Inc"]  # cultivator + revoked dropped
    assert recs[0].latitude == 42.3 and recs[0].zip_code == "02118"


def test_csv_prefers_dba_column_over_business_name():
    from rung.sources.extract import _extract_csv
    csv_text = (
        "type,business,dba,license,street,city,zipcode\n"
        "Hybrid Retailer,FFD WEST LLC,Fine Fettle Stamford,AMHF.1,12 Research Dr,Stamford,06906\n"
        "Retailer,NoDBA LLC,,X.2,5 Main St,Hartford,06103\n"
    )
    recs = _extract_csv(csv_text)
    assert [r.name for r in recs] == ["Fine Fettle Stamford", "NoDBA LLC"]  # DBA wins; legal fallback
    assert recs[0].address == "12 Research Dr" and recs[0].zip_code == "06906"


def test_csv_falls_back_to_legal_name_when_dba_column_sorts_first():
    from rung.sources.extract import _extract_csv
    # DBA column BEFORE the legal-name column: a blank DBA must still fall back to the legal name
    # (used to claim the name slot and drop the row when its cell was blank).
    csv_text = (
        "dba,legal name,street,city,zip\n"
        "Green Brand,Green Co LLC,1 Main St,Reno,89501\n"
        ",NoDBA Holdings LLC,2 Oak Ave,Tahoe,89001\n"
    )
    recs = _extract_csv(csv_text)
    assert [r.name for r in recs] == ["Green Brand", "NoDBA Holdings LLC"]  # DBA wins; legal fallback
    assert recs[1].address == "2 Oak Ave"


def test_csv_uses_dba_when_it_is_the_only_name_column():
    from rung.sources.extract import _extract_csv
    csv_text = "dba,street,city,zip\nOnly Brand,1 Main St,Reno,89501\n,2 Oak Ave,Tahoe,89001\n"
    recs = _extract_csv(csv_text)
    assert [r.name for r in recs] == ["Only Brand"]  # the blank-DBA row has no name source → dropped


def test_table_extractors_null_a_date_mis_mapped_to_address():
    from rung.sources.extract import _record_from_values
    # IL's "all cannabis licenses" PDF interleaves license rows whose issuance-date column lands
    # in `address`; a bare date is never an address and must be nulled, not stored.
    assert _record_from_values("pdf", {"name": "X", "address": "7/22/2022"}).address is None
    assert _record_from_values("pdf", {"name": "X", "address": "5/3/24"}).address is None
    # A real street address (even one that merely contains digits) is untouched.
    assert _record_from_values("pdf", {"name": "X", "address": "100 Main St"}).address == "100 Main St"


# ── PA roster PDF: a long name overprinted onto the address column ──────────────
# The PDF draws the wrapped store name on top of the address at overlapping x-positions in the
# same font, so pdfplumber interleaves the two runs character by character. All five cases below
# are verbatim from the live 2026-07 PA roster PDF.

def test_unmerge_name_overflow_inverts_the_real_pa_overprints() -> None:
    cases = [
        ("Restore Integrative Wellness Center - Elkins", "P8a0rk03 Old York Road", "Elkins Park",
         "Restore Integrative Wellness Center - Elkins Park", "8003 Old York Road"),
        ("Restore Integrative Wellness Center - Philade", "l9p5h7ia-963 Frankford Avenue", "Philadelphia",
         "Restore Integrative Wellness Center - Philadelphia", "957-963 Frankford Avenue"),
        ("Restore Integrative Wellness Center - Doyles", "t8o1w2n N Easton Road, Unit 6", "Doylestown",
         "Restore Integrative Wellness Center - Doylestown", "812 N Easton Road, Unit 6"),
        ("Restore Integrative Wellness Center - Pottsto", "w1n450 East High Street", "Pottstown",
         "Restore Integrative Wellness Center - Pottstown", "1450 East High Street"),
        ("Restore Integrative Wellness Center - East Pe", "t5e4r7s1bu Mrgain Street", "East Petersburg",
         "Restore Integrative Wellness Center - East Petersburg", "5471 Main Street"),
    ]
    for name, address, city, want_name, want_address in cases:
        assert _unmerge_name_overflow(name, address, city) == (want_name, want_address)


def test_unmerge_name_overflow_leaves_a_row_it_cannot_prove() -> None:
    # An intact row is untouched.
    assert _unmerge_name_overflow("Ethos - Allentown", "1 Main St", "Allentown") == (
        "Ethos - Allentown", "1 Main St")
    # Missing pieces: nothing to invert against.
    assert _unmerge_name_overflow("Store", "P8a0rk03 Old York Road", None) == (
        "Store", "P8a0rk03 Old York Road")
    # The name does not end with a prefix of the city -> not this corruption.
    assert _unmerge_name_overflow("Ethos - Pittsburgh North of Harmarville (Har",
                                  "m5a rAvlipllhea) Drive East", "Pittsburgh") == (
        "Ethos - Pittsburgh North of Harmarville (Har", "m5a rAvlipllhea) Drive East")
    # The near-miss: the overflow came from the NAME's parenthetical, not the city. Subtracting the
    # city tail *would* leave a digit-leading address ("5 Alpha) Drive East"), but the stray ")"
    # betrays the bad split, so the balanced-parens guard declines. Real PA row.
    assert _unmerge_name_overflow("Ethos - Pittsburgh North of Harmarville (Har",
                                  "m5a rAvlipllhea) Drive East", "Harmarville") == (
        "Ethos - Pittsburgh North of Harmarville (Har", "m5a rAvlipllhea) Drive East")
    # Overflow chars not all consumable from the address head -> decline.
    assert _unmerge_name_overflow("Shop - Elkins", "8003 Old York Road", "Elkins Park") == (
        "Shop - Elkins", "8003 Old York Road")


def test_arcgis_record_reads_dcs_misspelled_longitude_field() -> None:
    # DC's Open Data layer (Licensed Medical Cannabis Retailers) ships the longitude column as
    # `LONGITDUE`. Without matching the misspelling the coordinate pair is dropped, the roster
    # loses its geo key, and compare.py cannot match it against company stores — DC's roster also
    # carries no zip, so the address key can't rescue it either. Real data, real typo.
    feat = {"attributes": {
        "TRADE_NAME": "Takoma Wellness Center",
        "ADDRESS": "6925 Blair Rd NW",
        "STATUS": "Active",
        "LATITUDE": 38.9757,
        "LONGITDUE": -77.0203,
    }}
    rec = _arcgis_record(feat)
    assert rec is not None
    assert rec.name == "Takoma Wellness Center"
    assert rec.latitude == 38.9757
    assert rec.longitude == -77.0203


def test_arcgis_record_prefers_correctly_spelled_longitude() -> None:
    # The correct spelling must win when both are present (`_arcgis_attr` tries names in order).
    feat = {"attributes": {"NAME": "X", "LATITUDE": 1.0, "LONGITUDE": 2.0, "LONGITDUE": 99.0}}
    rec = _arcgis_record(feat)
    assert rec is not None and rec.longitude == 2.0


# --- va_cca ------------------------------------------------------------------------------------

def test_va_cca_record_parses_a_squarespace_map_block() -> None:
    # Real shape from cca.virginia.gov/medicalcannabis/dispensaries: a flat `location` object whose
    # addressLine2 is "City, VA, ZIP" and whose coordinates are markerLat/markerLng.
    rec = _va_cca_record({
        "mapZoom": 13, "mapLat": 38.79, "mapLng": -77.06,
        "markerLat": 38.7919763, "markerLng": -77.0602591,
        "addressTitle": "Beyond Hello Alexandria",
        "addressLine1": "5902 Richmond Highway",
        "addressLine2": "Alexandria, VA, 22303",
        "addressCountry": "United States",
    })
    assert rec is not None
    assert (rec.name, rec.address) == ("Beyond Hello Alexandria", "5902 Richmond Highway")
    assert (rec.city, rec.state, rec.zip_code) == ("Alexandria", "VA", "22303")
    assert (rec.latitude, rec.longitude) == (38.7919763, -77.0602591)
    assert rec.source == "va_cca"


def test_va_cca_record_skips_a_non_dispensary_map_block() -> None:
    # The page carries a map block with no addressTitle/addressLine1; it is not a dispensary.
    assert _va_cca_record({"mapZoom": 13, "mapLat": 38.0, "mapLng": -77.0}) is None


def test_va_cca_record_drops_a_placeholder_coordinate() -> None:
    rec = _va_cca_record({
        "addressTitle": "X", "addressLine1": "1 Main St", "addressLine2": "Richmond, VA, 23220",
        "markerLat": 0, "markerLng": 0,
    })
    assert rec is not None and rec.latitude is None and rec.longitude is None
    assert rec.zip_code == "23220"          # the address still parses


def test_va_cca_record_tolerates_a_missing_address_line2() -> None:
    rec = _va_cca_record({"addressTitle": "Y", "addressLine1": "2 Oak Ave"})
    assert rec is not None
    assert rec.city is None and rec.zip_code is None and rec.address == "2 Oak Ave"
