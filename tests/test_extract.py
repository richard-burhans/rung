"""Pure-function tests for the extraction logic.

No network, browser, or AI — these exercise the parsing/heuristic code that the
recent hardening changed and is easy to silently regress.
"""

import asyncio
import re as _re

from rung.sources.extract import (
    _ARCGIS_PAGE_SIZE,
    _ARCGIS_SERVICE_RE,
    _arcgis_attr,
    _ca_dcc_record,
    _clean,
    _extract_address_blocks,
    _extract_csv,
    _extract_html,
    _header_map,
    _infer_name_column,
    _location_fraction,
    _match_field,
    _query_arcgis_layer,
    _split_name_address,
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


# ── CA DCC record mapping ────────────────────────────────────────────────────

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
