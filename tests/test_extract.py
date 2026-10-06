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
        self.urls = []

    async def get(self, url, timeout=None):
        offset = int(_re.search(r"resultOffset=(\d+)", url).group(1))
        self.offsets.append(offset)
        self.urls.append(url)
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


class _FakeClampedArcgisSession:
    """A server whose layer maxRecordCount (1000) is BELOW our requested page size: every page is
    short of the request yet flagged exceededTransferLimit — the real-world shape that silently
    truncated a >1000-row roster when "short page" was read as "last page"."""

    MAX = 1000

    def __init__(self, total):
        self.total = total
        self.offsets = []

    async def get(self, url, timeout=None):
        offset = int(_re.search(r"resultOffset=(\d+)", url).group(1))
        self.offsets.append(offset)
        served = min(self.MAX, max(0, self.total - offset))
        rows = [{"attributes": {"name": f"Store {i}"}} for i in range(offset, offset + served)]
        return _FakeArcgisResp(
            {"features": rows, "exceededTransferLimit": offset + served < self.total}
        )


def test_arcgis_pages_past_a_server_clamped_below_the_requested_size():
    session = _FakeClampedArcgisSession(1500)  # maxRecordCount=1000 < the 2000 we ask for
    records = asyncio.run(_query_arcgis_layer("https://x/FeatureServer/0", session))
    assert len(records) == 1500                  # NOT truncated to the first clamped page
    assert session.offsets == [0, 1000]          # offset advances by what actually ARRIVED


class _FakeStuckFlagArcgisSession:
    """A broken server: zero features but exceededTransferLimit stuck true."""

    def __init__(self):
        self.calls = 0

    async def get(self, url, timeout=None):
        self.calls += 1
        return _FakeArcgisResp({"features": [], "exceededTransferLimit": True})


def test_arcgis_empty_page_with_the_more_flag_stuck_terminates():
    session = _FakeStuckFlagArcgisSession()
    records = asyncio.run(_query_arcgis_layer("https://x/FeatureServer/0", session))
    assert records == [] and session.calls == 1  # stop, don't loop on offset 0 forever

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
    # The leading unit prefix ("1005-") is stripped so the key is the street number "401 COOPERS
    # BLVD SW" — the form the operator's own site publishes (else normalize_address JOINS "1005-401"
    # into "1005401" and the roster matches nothing; the AB compare-lag artifact).
    assert rec.address == "401 COOPERS BLVD SW"
    assert rec.zip_code == "T4B 4J3" and rec.phone == "4039601313"


def test_aglc_record_strips_unit_prefix_only_when_present():
    # A hyphenated unit prefix is dropped; a bare street number and a lettered house number are kept.
    assert _aglc_record(_aglc_row(address="416-222 BASELINE RD")).address == "222 BASELINE RD"
    assert _aglc_record(_aglc_row(address="1124 KENSINGTON RD NW")).address == "1124 KENSINGTON RD NW"
    assert _aglc_record(_aglc_row(address="203B BEAR STREET")).address == "203B BEAR STREET"


def test_aglc_record_drops_out_of_province_licensee_sites():
    # The report lists out-of-province supplier/producer sites — not Alberta stores.
    assert _aglc_record(_aglc_row(province="BC")) is None
    assert _aglc_record(_aglc_row(province="ON")) is None
    assert _aglc_record(_aglc_row(name=None)) is None


def test_aglc_record_joins_address_lines():
    rec = _aglc_record(_aglc_row(address2="UNIT 4"))
    assert rec.address == "401 COOPERS BLVD SW, UNIT 4"


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


def test_slga_record_strips_canadian_unit_prefix():
    # SK addresses lead with a unit hyphenated onto the street number — including LETTER units
    # ("C-125", "J2-2095") and spaced ones ("170 - 3020"). Strip so the roster keys on the street
    # number the operators' own sites publish (the same AB compare-lag class).
    assert _slga_record(_slga_row(address="C-125 Highway 364")).address == "125 Highway 364"
    assert _slga_record(_slga_row(address="J2-2095 Prince of Wales Dr")).address == "2095 Prince of Wales Dr"
    assert _slga_record(_slga_row(address="170 - 3020 Preston Ave S")).address == "3020 Preston Ave S"
    assert _slga_record(_slga_row(address="224 Main St")).address == "224 Main St"  # no prefix, unchanged


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


# ── The regulator's own store id, where a roster publishes one ───────────────────────────────────
#
# Alberta's AGLC report is a LICENSEE report and carries `Authorization Number` per store; we parsed
# 11 columns and kept 7, dropping it. Ontario's AGCO open-data CSV carries `LicenceNumber`
# (CRSA1161431) across 30 columns, where the ArcGIS map we consume exposes only 14 and none of them
# is an id at all.
#
# ⚠ IT IS A STORE KEY, NOT AN OPERATOR KEY. Measured 2026-09-16 against AGCO's CSV: 98 "One Plant"
# rows carry 92 distinct licence numbers, because an authorization is issued per premises. It does
# not collapse an operator's many company records — what it gives is an identity for the STORE that
# survives a rename or a reformatted address.


def test_the_alberta_record_carries_its_authorization_number() -> None:
    from rung.sources.extract import _aglc_record

    record = _aglc_record({
        "province": "AB", "name": "Freedom Cannabis", "address": "9827 279 ST",
        "city": "ACHESON", "zip_code": "T7X 6J4", "phone": "8774523760",
        "licence_number": "301320",
    })
    assert record is not None
    assert record.licence_number == "301320"


def test_a_roster_without_one_simply_has_none() -> None:
    """Most rosters publish no id; the column must stay optional rather than invented."""
    from rung.models import DispensaryRecord

    assert DispensaryRecord(source="arcgis", name="x").licence_number is None


def test_the_alberta_field_map_names_the_column_it_reads() -> None:
    """Parsed BY NAME, so a column reorder in the report cannot silently shift the id."""
    from rung.sources.extract import _AGLC_FIELDS

    assert _AGLC_FIELDS["licence_number"] == "Authorization Number"


def test_an_out_of_province_row_is_still_dropped_with_the_id_present() -> None:
    """The report lists BC/ON supplier sites too; carrying an id must not smuggle them in as AB."""
    from rung.sources.extract import _aglc_record

    assert _aglc_record({
        "province": "BC", "name": "Dunn Cannabis", "city": "ABBOTSFORD",
        "licence_number": "301671",
    }) is None


# ── Ontario's AGCO open-data CSV (list_type='on_agco_csv') ───────────────────────────────────────
#
# The same stores the ArcGIS map serves, plus the columns it omits. Verified live 2026-09-16 against
# both: filtered to `Authorized to Open` this gives 1,897 rows against the map's 1,901, only two
# addresses differing each way, identical coverage. After deduplicating on the licence number both
# sources agree on 1,860 REAL stores — the map carries 41 repeated rows and has no key to notice.

_AGCO_HEADER = (
    "﻿LicenceNumber,FileNumber,ObjectDefDescription,ApplicationStatusEn,Latitude,Longitude,"
    "PremisesName,StreetAddress,City,Province,PostalCode,LicenceStatus,ApplicationType,WebsiteEn\n"
)


def _agco_row(licence, status, name, addr="1 MAIN ST", city="TORONTO",
              lat="43.6", lon="-79.3", lic_status="Active", app="New Application", site=""):
    return (f"{licence},F1,CRSA,{status},{lat},{lon},{name},{addr},{city},ON,M1M1M1,"
            f"{lic_status},{app},{site}\n")


def test_only_authorized_to_open_stores_are_returned() -> None:
    """`Cancelled` rows are cancelled APPLICATIONS, not closed stores — 244 of the 673 sit at an
    address that is authorized today. Returning them would invent closures for a third of the set."""
    from rung.sources.extract import _extract_on_agco_csv

    text = (_AGCO_HEADER
            + _agco_row("CRSA1", "Authorized to Open", "Open Store")
            + _agco_row("CRSA2", "Cancelled", "Dead Application")
            + _agco_row("CRSA3", "In Progress", "Pending Store")
            + _agco_row("CRSA4", "Public Notice", "Noticed Store"))
    recs = _extract_on_agco_csv(text)
    assert [r.name for r in recs] == ["Open Store"]


def test_the_licence_number_is_captured_despite_the_bom() -> None:
    """The file is served with a UTF-8 BOM, so the first header arrives as '\\ufeffLicenceNumber'.
    Read by POSITION, so however the BOM is stripped the id still lands."""
    from rung.sources.extract import _extract_on_agco_csv

    recs = _extract_on_agco_csv(_AGCO_HEADER + _agco_row("CRSA1161431", "Authorized to Open", "X"))
    assert recs[0].licence_number == "CRSA1161431"


def test_a_repeated_licence_yields_ONE_store() -> None:
    """37 licences appear twice with every field identical but RowNum. The map has the same flaw —
    1,901 rows for 1,860 distinct stores — and no key to detect it."""
    from rung.sources.extract import _extract_on_agco_csv

    text = (_AGCO_HEADER
            + _agco_row("CRSA1161431", "Authorized to Open", "One Plant Stouffville")
            + _agco_row("CRSA1161431", "Authorized to Open", "One Plant Stouffville")
            + _agco_row("CRSA9", "Authorized to Open", "Other Store"))
    recs = _extract_on_agco_csv(text)
    assert len(recs) == 2
    assert {r.licence_number for r in recs} == {"CRSA1161431", "CRSA9"}


def test_the_core_fields_and_state_land() -> None:
    from rung.sources.extract import _extract_on_agco_csv

    recs = _extract_on_agco_csv(
        _AGCO_HEADER + _agco_row("CRSA1", "Authorized to Open", "Purple Moose",
                                 addr="575 LAVAL DR", city="OSHAWA", site="https://x.ca"))
    r = recs[0]
    assert (r.name, r.address, r.city, r.state) == ("Purple Moose", "575 LAVAL DR", "OSHAWA", "ON")
    assert (r.latitude, r.longitude) == (43.6, -79.3)
    assert r.website == "https://x.ca" and r.source == "on_agco_csv"


def test_a_blank_coordinate_is_none_rather_than_a_crash() -> None:
    from rung.sources.extract import _extract_on_agco_csv

    recs = _extract_on_agco_csv(
        _AGCO_HEADER + _agco_row("CRSA1", "Authorized to Open", "No Coords", lat="", lon="n/a"))
    assert recs[0].latitude is None and recs[0].longitude is None


def test_ontario_is_wired_to_the_csv_with_the_arcgis_handler_kept_for_revert() -> None:
    """The revert must stay one config line, so the old handler may not be deleted."""
    from pathlib import Path

    import yaml

    from rung.sources import extract

    states = yaml.safe_load(Path("rung/data/states.yml").read_text(encoding="utf-8"))
    on = next(s for s in states if s.get("abbr") == "ON")
    assert on["list_type"] == "on_agco_csv"
    assert "opendata" in on["list_url"]
    assert hasattr(extract, "_extract_on_agco"), "the ArcGIS handler is the documented revert path"
    assert "on_agco" in extract.HANDLED_LIST_TYPES


# ── The regulator's record of when a store OPENED ────────────────────────────────────────────────
#
# `store_lifecycle_events` infers an opening from a store's first appearance in OUR roster, which is
# bounded by when we started scraping rather than by when the store opened. Alberta publishes the
# authorization's `Initial Effective Date` on 100% of rows, back to 2018-10-17 — the day Canada
# legalised recreational cannabis, which is itself the check that the parse is right.


def test_the_effective_date_is_read_as_month_first() -> None:
    """⚠ M/D/YYYY — US ORDER FROM A CANADIAN AGENCY, and read as D/M it would silently mis-date more
    than half the corpus while still parsing. Settled by measurement: of 963 values, 562 have a
    second component above 12 and not one has a first component above 12."""
    from rung.sources.extract import _aglc_date

    assert _aglc_date("8/30/2021") == "2021-08-30"     # unambiguous: 30 can only be a day
    assert _aglc_date("10/4/2021") == "2021-10-04"     # ambiguous: month-first is the measured order
    assert _aglc_date("10/17/2018") == "2018-10-17"    # legalisation day, the earliest in the file


def test_a_blank_or_unparseable_date_is_none_rather_than_a_guess() -> None:
    from rung.sources.extract import _aglc_date

    assert _aglc_date("") is None
    assert _aglc_date(None) is None
    assert _aglc_date("not a date") is None
    assert _aglc_date("2021-08-30") is None, "an ISO string is not this source's format; do not coerce"


def test_the_alberta_record_carries_the_opening_date() -> None:
    from rung.sources.extract import _aglc_record

    record = _aglc_record({
        "province": "AB", "name": "Freedom Cannabis", "city": "ACHESON",
        "licence_number": "301320", "licensed_since": "8/30/2021",
    })
    assert record is not None and record.licensed_since == "2021-08-30"


def test_the_alberta_field_map_names_the_date_column() -> None:
    from rung.sources.extract import _AGLC_FIELDS

    assert _AGLC_FIELDS["licensed_since"] == "Initial Effective Date"


def test_the_manager_name_is_NOT_captured() -> None:
    """A deliberate omission, pinned so it is not added back absent-mindedly.

    AGLC publishes a named manager on 86% of rows (336 distinct people). It has no identified use
    here, and holding it would create a standing obligation on every export path — the clean_d1
    parquet, the published site, any future query — which the public-build leak guard does not watch
    because that guard covers CODE, not data. It is one re-scrape away if a purpose ever appears; a
    linkage use should be met with an opaque group id derived at ingest, never the name.
    """
    from rung.models import DispensaryRecord
    from rung.sources.extract import _AGLC_FIELDS

    assert "Manager Name" not in _AGLC_FIELDS.values()
    assert not any("manager" in f for f in DispensaryRecord.__dataclass_fields__)


# ── 2026-09-24: the six roster-trust fixes ───────────────────────────────────────────────────────


def test_screen_reader_only_text_is_not_part_of_a_cell() -> None:
    """Florida wraps every licence-letter link in an sr-only "(opens in new tab)"; 24 roster rows
    carried it in their NAME and matched nothing."""
    from rung.sources.extract import _extract_html

    html = """<table><tr><th>Dispensary Name</th><th>Address</th><th>City</th><th>Zip</th></tr>
    <tr><td>Ayr Cannabis Dispensary<span class="sr-only">(opens in new tab)</span></td>
        <td>1 Main St</td><td>Tampa</td><td>33601</td></tr></table>"""
    (record,) = _extract_html(html)
    assert record.name == "Ayr Cannabis Dispensary"


def test_a_placeholder_cell_is_not_a_location_signal() -> None:
    """Florida's operator table fills its blank phone column with "n/a"; that made 49 address-less
    licensees count as located, and the table survived the rule below."""
    from rung.models import DispensaryRecord
    from rung.sources.extract import _location_fraction

    rows = [DispensaryRecord(source="html", name="A Good Decision, LLC", state="FL", phone="n/a"),
            DispensaryRecord(source="html", name="Alamanda Farms LLC", state="FL", phone="-")]
    assert _location_fraction(rows) == 0.0
    assert _location_fraction([DispensaryRecord(source="html", name="X", state="FL", city="Tampa")]) == 1.0


def test_a_licensee_table_beside_a_store_table_is_not_a_second_store_list() -> None:
    """Florida's MMTC page: 49 licensed operators (name + licence, no address) above 781
    dispensing locations. The operators are not stores; they became 49 "closures"."""
    from rung.sources.extract import _extract_html

    html = """
    <table><tr><th>MMTC Name</th><th>Phone</th><th>Authorization Status</th><th>License Number</th></tr>
      <tr><td>A Good Decision, LLC</td><td>n/a</td><td>Initial Licensure</td><td>MMTC-2026-0029</td></tr>
      <tr><td>Ayr Cannabis Dispensary</td><td>833-254-4877</td><td>Licensed</td><td>MMTC-2015-0002</td></tr>
      <tr><td>Alamanda Farms LLC</td><td>n/a</td><td>Initial Licensure</td><td>MMTC-2026-0030</td></tr></table>
    <table><tr><th>Dispensary Name</th><th>Address</th><th>City</th><th>Zip</th></tr>
      <tr><td>Mint Cannabis</td><td>10456 Stelling Drive</td><td>Riverview</td><td>33578</td></tr>
      <tr><td>Trulieve</td><td>1 Bay St</td><td>Tampa</td><td>33601</td></tr></table>"""
    names = [r.name for r in _extract_html(html)]
    assert names == ["Mint Cannabis", "Trulieve"]


def test_a_page_whose_only_table_is_address_less_still_yields_it() -> None:
    """NY's legal-entity licensee list has no addresses and is the only list there is."""
    from rung.sources.extract import _extract_html

    html = """<table><tr><th>Licensee Name</th><th>License Number</th></tr>
      <tr><td>Housing Works Cannabis Co</td><td>OCM-1</td></tr></table>"""
    assert [r.name for r in _extract_html(html)] == ["Housing Works Cannabis Co"]


def test_co_med_reads_every_sheet_of_the_xlsx_and_folds_dual_licences() -> None:
    import io

    import openpyxl

    from rung.sources.extract import _extract_co_med

    book = openpyxl.Workbook()
    med = book.active
    med.title = "Medical"
    hdr = ["License Number", "Facility Name", "DBA", "Facility Type", "Street", "City", "ZIP Code"]
    med.append(hdr)
    med.append(["402-1", "1-11 LLC", "1:11", "Medical Marijuana Store", "17034 Highway 17", "Moffat", "81143"])
    med.append(["402-2", "Dual LLC", "Dual Med", "Medical Marijuana Store", "5 Main St", "Denver", "80202"])
    med.append(["402-3", "Grower LLC", "", "Medical Marijuana Cultivation", "9 Farm Rd", "Pueblo", "81001"])
    ret = book.create_sheet("Retail")
    ret.append(hdr)
    ret.append(["403-1", "Dual LLC", "Dual Retail", "Retail Marijuana Store", "5 Main St", "Denver", "80202"])
    ret.append(["403-2", "NoBrand LLC", None, "Retail Marijuana Store", "7 High St", "Boulder", "80301"])
    buf = io.BytesIO()
    book.save(buf)

    recs = _extract_co_med(buf.getvalue())
    assert [r.name for r in recs] == ["1:11", "Dual Retail", "NoBrand LLC"]   # one rooftop; retail names it
    assert recs[1].licence_number == "403-1" and recs[2].address == "7 High St"
    assert all(r.source == "co_med" for r in recs)


def test_co_med_still_accepts_the_csv_export() -> None:
    from rung.sources.extract import _extract_co_med

    csv_text = ("License Number,Facility Name,DBA,Facility Type,Street,City,ZIP Code\n"
                "402-1,1-11 LLC,1:11,Medical Marijuana Store,17034 Highway 17,Moffat,81143\n")
    (rec,) = _extract_co_med(csv_text)
    assert rec.name == "1:11" and rec.zip_code == "81143"


def test_il_idfpr_parses_wrapped_rows_from_captured_word_positions() -> None:
    """Two real pages (2026-09-24) of the IDFPR combined list, as pdfplumber words."""
    import json
    from pathlib import Path

    from rung.sources.extract import HANDLED_LIST_TYPES, il_idfpr_records

    assert "il_idfpr" in HANDLED_LIST_TYPES
    pages = json.loads((Path(__file__).resolve().parent / "fixtures" / "il_idfpr_words.json").read_text(encoding="utf-8"))
    records = il_idfpr_records([p["words"] for p in pages])
    anchors = sum(1 for p in pages for w in p["words"] if "AUDO" in w["text"])
    assert len(records) == anchors == 24                      # one record per -AUDO credential
    by_name = {r.name: r for r in records}
    rise = by_name["Rise - Mundelein"]
    assert (rise.address, rise.city, rise.zip_code, rise.phone) == ("1325 Armour Boulevard", "Mundelein", "60060", "(847) 616-8966")
    assert "284.000001-AUDO" in (rise.licence_number or "")
    joliet = by_name["Rise - Joliet Rock Creek"]          # a name wrapped over two lines
    assert joliet.address == "1627 Rock Creek Blvd." and joliet.zip_code == "60431"
    assert all(r.address and r.zip_code and r.city for r in records)   # no fragments
    assert sum(1 for r in records if r.phone) >= 20                     # the list omits a few phones
    assert not any(r.name in ("LLC", "of the Quad Cities,") for r in records)


def test_il_section_pages_come_from_the_table_of_contents() -> None:
    from rung.sources.extract import _il_section_pages

    toc = ("ACTIVE ADULT USE DISPENSING ORGANIZATION LICENSES.......... 2\n"
           "ORIGINAL LOTTERY CONDITIONAL LICENSE LIST ................ 23\n"
           "SECL CONDITIONAL LICENSE LIST .......................... 32\n")
    assert _il_section_pages(toc) == (2, 23)
    assert _il_section_pages("no toc here") == (2, None)


# Ohio's hosted layer (maps.ohio.gov `Geocoded_Dispensaries_`, 2026-09-25): shapefile-truncated
# field names, coordinates only as `user_lat`/`user_lon` plus the point geometry, a licence number,
# and a `licensetype` column that a loose "licen" match must not mistake for the licence.
_OHIO_FEATURE = {
    "attributes": {   # the live payload lists `licensetype` BEFORE `user_licen`
        "editor": "ogrip_agol", "licensetype": "Dual",
        "objectid": 1, "user_licen": "CCD000072-00", "user_busin": "Slightly Toasted, LLC",
        "user_dispe": "Bliss Ohio", "user_stree": "331 E Main St", "user_city": "Kent",
        "user_zip": 44240, "user_state": "Ohio", "user_count": "Portage", "user_phone": "(330) 765-2508",
        "user_type": "Dual Use Dispensary", "user_lic_1": "Active", "user_lat": 41.15384408,
        "user_lon": -81.35419411, "hours": "M-Sat: 10AM-8PM",
    },
    "geometry": {"x": -81.353434499785, "y": 41.153903999853036},
}


def test_arcgis_record_reads_ohios_truncated_fields_and_geometry() -> None:
    rec = _arcgis_record(_OHIO_FEATURE)
    assert rec is not None
    assert rec.name == "Bliss Ohio"                  # the DBA, before the licensee
    assert rec.address == "331 E Main St" and rec.city == "Kent" and rec.zip_code == "44240"
    assert rec.state == "Ohio" and rec.phone == "(330) 765-2508"
    assert rec.licence_number == "CCD000072-00"      # not `licensetype`
    assert rec.latitude == 41.153903999853036 and rec.longitude == -81.353434499785


def test_arcgis_attr_skips_an_empty_match_and_excluded_keys() -> None:
    """47 of Ohio's 228 stores carry a blank DBA: the empty first match must not end the search."""
    blank_dba = dict(_OHIO_FEATURE["attributes"], user_dispe="")
    assert _arcgis_record({"attributes": blank_dba}).name == "Slightly Toasted, LLC"
    assert _arcgis_attr({"licensetype": "Dual", "user_licen": "CCD1"}, "licen", exclude=("type",)) == "CCD1"
    assert _arcgis_attr({"licensetype": "Dual"}, "licen", exclude=("type",)) is None
    assert _arcgis_attr({"Dispensary": "", "Name": "  "}, "dispensar", "name") is None


def test_arcgis_record_prefers_a_coordinate_attribute_over_geometry() -> None:
    rec = _arcgis_record({"attributes": {"Name": "X", "Latitude": 43.66, "Longitude": -79.38},
                          "geometry": {"x": -1.0, "y": 1.0}})
    assert rec.latitude == 43.66 and rec.longitude == -79.38
    rec = _arcgis_record({"attributes": {"Name": "X"}, "geometry": {"x": 0, "y": 0}})
    assert rec.latitude is None and rec.longitude is None   # a 0,0 geometry is a placeholder too


def test_arcgis_query_asks_for_wgs84_geometry() -> None:
    session = _FakeArcgisSession(3)
    asyncio.run(_query_arcgis_layer("https://x/FeatureServer/0", session))
    assert session.urls and all("returnGeometry=true" in u and "outSR=4326" in u for u in session.urls)
