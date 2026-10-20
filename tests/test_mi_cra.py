"""Michigan's roster from the CRA's Accela Citizen Access licence search: the form postback, the grid
and its pager, the result cap and the single-match redirect, and the record mapping (2026-10-20)."""

import asyncio
from dataclasses import dataclass

import pytest

from rung.sources import accela_aca, extract

_URL = "https://aca-prod.accela.com/MIMM/Cap/CapHome.aspx?module=Adult_Use&TabName=Adult_Use"
_GRID = "ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList_"


def _form(body: str = "") -> str:
    return ('<html><form id="aspnetForm">'
            '<input type="hidden" name="__VIEWSTATE" value="VS&amp;1" />'
            '<input type="text" name="ctl00$PlaceHolderMain$generalSearchForm$txtGSCity" />'
            '<input type="checkbox" name="unticked" value="on" />'
            '<input type="checkbox" name="ticked" value="on" checked="checked" />'
            '<input type="submit" name="go" value="Search" />'
            '<select name="ctl00$PlaceHolderMain$generalSearchForm$ddlGSPermitType">'
            '<option value="">--Select--</option><option value="X/Y">Y</option></select>'
            f"{body}</form></html>")


def _row(i: int, number: str, address: str = "3010 Dunckel RD, Lansing MI 48910", status: str = "Active") -> str:
    p = f"{_GRID}ctl{i:02d}_"
    return (f'<tr><td><span id="{p}lblPermitNumber1">{number}</span></td>'
            f'<td><span id="{p}lblType">Marihuana Retailer - License</span></td>'
            f'<td><span id="{p}lblProjectName">Store {number}</span></td>'
            f'<td><span id="{p}lblAddress">{address}</span></td>'
            f'<td><span id="{p}lblExpirationDate">09/08/2027</span></td>'
            f'<td><span id="{p}lblStatus">{status}</span></td></tr>')


def _grid(rows: list[str], total: str, next_target: str | None = None) -> str:
    pager = ('<table class="aca_pagination"><tr><td><span>&lt; Prev</span></td>'
             + (f"<td><a href=\"javascript:__doPostBack(&#39;{next_target}&#39;,&#39;&#39;)\">Next &gt;</a></td>"
                if next_target else "<td><span>Next &gt;</span></td>") + "</tr></table>")
    return _form(f"<span>Showing 1-{len(rows)} of {total}</span><table>{''.join(rows)}</table>{pager}")


def test_the_form_is_sent_back_as_a_browser_would() -> None:
    fields = accela_aca.form_fields(_form())
    assert fields == {"__VIEWSTATE": "VS&1", "ctl00$PlaceHolderMain$generalSearchForm$txtGSCity": "",
                      "ticked": "on", "ctl00$PlaceHolderMain$generalSearchForm$ddlGSPermitType": ""}
    with pytest.raises(accela_aca.AcaError, match="no aspnetForm"):
        accela_aca.form_fields("<html>not an ACA page</html>")


def test_the_grid_its_count_and_its_pager() -> None:
    page = _grid([_row(2, "AU-R-001549"), _row(3, "AU-R-001528", status="License Void")], "39", "ctl13$ctl06")
    rows = accela_aca.result_rows(page)
    assert [(r.number, r.name, r.address, r.status) for r in rows] == [
        ("AU-R-001549", "Store AU-R-001549", "3010 Dunckel RD, Lansing MI 48910", "Active"),
        ("AU-R-001528", "Store AU-R-001528", "3010 Dunckel RD, Lansing MI 48910", "License Void")]
    assert accela_aca.result_count(page) == (39, False)
    assert accela_aca.result_count(_grid([], "100+")) == (100, True)
    assert accela_aca.result_count(_form("<span>No records found.</span>")) is None
    assert accela_aca.next_page_target(page) == "ctl13$ctl06"
    assert accela_aca.next_page_target(_grid([], "2")) is None


def test_a_single_match_detail_page_is_a_row_without_an_address() -> None:
    detail = ('<span id="ctl00_PlaceHolderMain_lblPermitNumber">AU-R-001549</span>'
              '<span id="ctl00_PlaceHolderMain_lblPermitType">Marihuana Retailer - License</span>'
              '<span id="ctl00_PlaceHolderMain_lblRecordStatus">Active</span>'
              '<div><h1><span id="ctl00_PlaceHolderMain_PermitDetailList1_per_permitDetail_label_project1">'
              'Record Name:</span></h1><span><table class="table_child"><tr><td></td>'
              '<td>Pure Options Express<br/></td></tr></table></span></div>')
    row = accela_aca.detail_row(detail)
    assert row is not None and (row.number, row.name, row.status, row.address) == (
        "AU-R-001549", "Pure Options Express", "Active", None)
    assert accela_aca.detail_row("<html></html>") is None


@dataclass
class _Response:
    text: str
    url: str = _URL
    status_code: int = 200


class _Session:
    """Answers each postback from a script keyed on what was asked: the record-number prefix for a
    search, the pager target for a page turn."""

    def __init__(self, searches: dict[str, _Response], pages: dict[str, _Response] | None = None) -> None:
        self.searches, self.pages, self.posts = searches, pages or {}, []

    async def get(self, url, **_kw):
        return _Response(_form(), url)

    async def post(self, url, data, **_kw):
        self.posts.append(data)
        if data["__EVENTTARGET"] == "ctl00$PlaceHolderMain$btnNewSearch":
            return self.searches[data["ctl00$PlaceHolderMain$generalSearchForm$txtGSPermitNumber"]]
        return self.pages[data["__EVENTTARGET"]]


def test_search_all_walks_the_pages_and_checks_the_count() -> None:
    session = _Session({"AU-R-0015": _Response(_grid([_row(2, "AU-R-001501")], "2", "next"))},
                       {"next": _Response(_grid([_row(2, "AU-R-001502")], "2"))})
    page = asyncio.run(accela_aca.open_search(session, _URL))
    rows = asyncio.run(accela_aca.search_all(session, page, "X/Y", number_prefix="AU-R-0015"))
    assert [r.number for r in rows] == ["AU-R-001501", "AU-R-001502"]
    assert session.posts[0]["ctl00$PlaceHolderMain$generalSearchForm$ddlGSPermitType"] == "X/Y"
    assert session.posts[0]["__VIEWSTATE"] == "VS&1"     # the page's own state goes back with it
    short = _Session({"P": _Response(_grid([_row(2, "AU-R-1")], "3"))})
    with pytest.raises(accela_aca.AcaError, match="announced 3 records and the walk read 1"):
        asyncio.run(accela_aca.search_all(short, page, "X/Y", number_prefix="P"))
    capped = _Session({"P": _Response(_grid([_row(2, "AU-R-1")], "100+"))})
    with pytest.raises(accela_aca.SearchCapped):
        asyncio.run(accela_aca.search_all(capped, page, "X/Y", number_prefix="P"))
    error = _Session({"P": _Response("oops", "https://aca-prod.accela.com/MIMM/Error.aspx")})
    with pytest.raises(accela_aca.AcaError, match=r"Error\.aspx"):
        asyncio.run(accela_aca.search_all(error, page, "X/Y", number_prefix="P"))


def test_mi_cra_records() -> None:
    def row(status="Active", address="3010 Dunckel RD, Lansing MI 48910-1234"):
        return accela_aca.AcaRow("AU-R-001549", "Marihuana Retailer - License", "Pure Options", address, status, None)
    record = extract.mi_cra_record(row())
    assert record is not None and (record.source, record.name, record.address, record.city, record.state,
                                   record.zip_code, record.licence_number) == (
        "mi_cra", "Pure Options", "3010 Dunckel RD", "Lansing", "MI", "48910", "AU-R-001549")
    suite = extract.mi_cra_record(row(address="1525 W Michigan AVE Suite 11 & 13, Battle Creek MI 49037"))
    assert suite is not None and (suite.address, suite.city) == ("1525 W Michigan AVE Suite 11 & 13", "Battle Creek")
    odd = extract.mi_cra_record(row(address="Somewhere without a zip"))
    assert odd is not None and (odd.address, odd.city, odd.zip_code) == ("Somewhere without a zip", None, None)
    assert extract.mi_cra_record(row(status="Active - Late Renewal")) is not None
    for inactive in ("License Void", "Closed", "Suspended", "Closed - Suspended", None):
        assert extract.mi_cra_record(row(status=inactive)) is None


def test_a_capped_search_is_split_and_a_rooftop_is_one_row(monkeypatch) -> None:
    """Retailers are split by the next digit wherever capped; a medical licence at a retailer's rooftop
    adds no row, and an inactive licence none at all."""
    asked: list[str] = []

    async def search_all(_session, _page, record_type, *, number_prefix=""):
        asked.append(number_prefix)
        if record_type.endswith("Marihuana Retailer/License/NA"):
            if number_prefix in ("AU-R-", "AU-R-0"):
                raise accela_aca.SearchCapped(number_prefix)
            if number_prefix == "AU-R-01":
                return [accela_aca.AcaRow("AU-R-000100", None, "Retail", "1 Main ST, Lansing MI 48910", "Active", None),
                        accela_aca.AcaRow("AU-R-000101", None, "Gone", "2 Main ST, Lansing MI 48910", "License Void", None)]
            return []
        if record_type.startswith("Licenses/"):
            return [accela_aca.AcaRow("PC-000001", None, "Medical", "1 Main ST, Lansing MI 48910", "Active", None),
                    accela_aca.AcaRow("PC-000002", None, "Med only", "9 Oak ST, Flint MI 48502", "Active", None)]
        return []

    async def open_search(_session, url):
        return accela_aca.AcaPage(url, {})

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(accela_aca, "search_all", search_all)
    monkeypatch.setattr(accela_aca, "open_search", open_search)
    monkeypatch.setattr(extract.asyncio, "sleep", no_sleep)
    records = asyncio.run(extract._extract_mi_cra(_URL, None))
    assert [(r.licence_number, r.name) for r in records] == [("AU-R-000100", "Retail"), ("PC-000002", "Med only")]
    assert "AU-R-01" in asked and "AU-R-1" in asked and "AU-R-00" in asked


def test_a_search_still_capped_at_the_longest_prefix_is_a_partial_roster(monkeypatch) -> None:
    async def search_all(_session, _page, _record_type, *, number_prefix=""):
        raise accela_aca.SearchCapped(number_prefix)

    async def open_search(_session, url):
        return accela_aca.AcaPage(url, {})

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(accela_aca, "search_all", search_all)
    monkeypatch.setattr(accela_aca, "open_search", open_search)
    monkeypatch.setattr(extract.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(extract, "_MI_CRA_MAX_PREFIX_DIGITS", 1)
    with pytest.raises(extract.PartialRoster, match="longest prefix"):
        asyncio.run(extract._extract_mi_cra(_URL, None))


def test_mi_cra_is_a_handled_list_type() -> None:
    assert "mi_cra" in extract.HANDLED_LIST_TYPES and extract._ASYNC_HANDLERS["mi_cra"] is extract._extract_mi_cra
