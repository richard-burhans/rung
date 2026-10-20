"""Search an Accela Citizen Access (ACA) portal's public record search, over plain HTTP.

Many regulators license through Accela, and the public face is an ACA portal — ASP.NET WebForms
pages under ``aca-prod.accela.com/<AGENCY>/Cap/CapHome.aspx?module=<Module>``. A search there is a
form postback: the page's own fields (its ``__VIEWSTATE`` and friends) sent back with a record type
chosen and ``__EVENTTARGET`` naming the Search button. That is why such a roster looks unreachable
from a crawler: the list is not at any URL, and a POST missing the page's fields lands on
``Error.aspx``. Sent whole, the postback answers with the results grid, server-rendered.

    page = await open_search(session, "https://aca-prod.accela.com/MIMM/Cap/CapHome.aspx?module=Adult_Use")
    rows = await search_all(session, page, "Adult_Use/Marihuana Retailer/License/NA", number_prefix="AU-R-0015")

Three behaviours of the portal shape the API, measured on Michigan's (``MIMM``) on 2026-10-20:

- **A search answers at most a capped number of rows** (100 in one module, 200 in another), and says
  so as ``Showing 1-10 of 100+``. `search_all` raises `SearchCapped` instead of returning the cap's
  worth; the caller partitions — the record-number field is a PREFIX match (``AU-R-0015`` finds
  ``AU-R-001500``..``AU-R-001599``), which makes record numbers the natural partition.
- **The grid pages** (10 or 20 rows per page); the "Next >" link is one more postback of the page.
- **A search that matches exactly ONE record redirects to that record's detail page**, which carries
  the number, type, status and name but not the grid's address. `search_all` returns that row with
  ``address=None`` rather than drop a licence it was shown.
"""

import html
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from rung.http import TRANSPORT_ERRORS

_SEARCH_BUTTON = "ctl00$PlaceHolderMain$btnNewSearch"
_FIELD = "ctl00$PlaceHolderMain$generalSearchForm$"
_GRID = "ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList_"
_SHOWING_RE = re.compile(r"Showing\s+\d+-\d+\s+of\s+(\d+)(\+?)")
_NEXT_RE = re.compile(r"__doPostBack\('([^']+)',''\)")
#: A guard against a pager that never ends (a portal that keeps answering "Next"): far beyond the
#: 20 pages a 200-row cap allows at the smallest page size.
_MAX_PAGES = 50


class AcaError(Exception):
    """The portal answered, but not with a search result — an HTTP error, an ``Error.aspx`` landing,
    or a page whose form is missing. Carries what the portal said."""


class SearchCapped(AcaError):
    """A search matched more records than the portal will list (``of 100+``): what it lists is a
    fragment. The caller narrows the search — a longer record-number prefix — and asks again."""


@dataclass(frozen=True)
class AcaRow:
    """One record from a search: the grid's columns, verbatim."""

    number: str
    record_type: str | None
    name: str | None
    address: str | None
    status: str | None
    expires: str | None


@dataclass(frozen=True)
class AcaPage:
    """A search page as the portal served it: its URL and the form fields a postback must send back."""

    url: str
    fields: dict[str, str]


def form_fields(page_html: str) -> dict[str, str]:
    """Every field the page's ``aspnetForm`` would submit, as a browser would: text and hidden inputs,
    CHECKED boxes and radios only, the selected option of each select. Pure. Raises `AcaError` when
    the page has no such form."""
    tree = HTMLParser(page_html)
    form = tree.css_first("form#aspnetForm")
    if form is None:
        raise AcaError("no aspnetForm on the page — not an ACA page, or its markup changed")
    fields: dict[str, str] = {}
    for node in form.css("input"):
        name = node.attributes.get("name")
        kind = (node.attributes.get("type") or "text").lower()
        if not name or kind in ("submit", "button", "image", "file"):
            continue
        if kind in ("checkbox", "radio") and "checked" not in node.attributes:
            continue
        fields[name] = node.attributes.get("value") or ""
    for node in form.css("select"):
        name = node.attributes.get("name")
        if not name:
            continue
        options = node.css("option")
        chosen = next((o for o in options if "selected" in o.attributes), options[0] if options else None)
        fields[name] = (chosen.attributes.get("value") or "") if chosen is not None else ""
    return fields


def _text(node) -> str | None:
    if node is None:
        return None
    value = re.sub(r"\s+", " ", html.unescape(node.text(separator=" "))).strip()
    return value or None


def result_rows(page_html: str) -> list[AcaRow]:
    """The records on one page of a results grid. Pure. A row is keyed on its record number; a row
    without one (the header, the pager) is not a record."""
    tree = HTMLParser(page_html)
    rows: list[AcaRow] = []
    for number_node in tree.css(f"span[id^='{_GRID}'][id$='_lblPermitNumber1']"):
        prefix = (number_node.attributes.get("id") or "").removesuffix("lblPermitNumber1")
        number = _text(number_node)
        if not number:
            continue
        def cell(suffix: str, prefix: str = prefix) -> str | None:
            return _text(tree.css_first(f"[id='{prefix}{suffix}']"))
        rows.append(AcaRow(number=number, record_type=cell("lblType"), name=cell("lblProjectName"),
                           address=cell("lblAddress") or cell("lblPermitAddress"),
                           status=cell("lblStatus"), expires=cell("lblExpirationDate")))
    return rows


def detail_row(page_html: str) -> AcaRow | None:
    """The one record a single-match search redirected to, from its detail page. Pure. The detail
    page shows no address, so the row carries none. None when the page is not a record's detail."""
    tree = HTMLParser(page_html)
    number = _text(tree.css_first("#ctl00_PlaceHolderMain_lblPermitNumber"))
    if not number:
        return None
    name = None
    label = tree.css_first("span[id*='per_permitDetail_label_project']")
    if label is not None:
        holder = label.parent.parent if label.parent is not None else None
        content = holder.css_first("table.table_child") if holder is not None else None
        name = _text(content)
    return AcaRow(number=number, record_type=_text(tree.css_first("#ctl00_PlaceHolderMain_lblPermitType")),
                  name=name, address=None,
                  status=_text(tree.css_first("#ctl00_PlaceHolderMain_lblRecordStatus")),
                  expires=_text(tree.css_first("#ctl00_PlaceHolderMain_lblExpirtionDate")))


def result_count(page_html: str) -> tuple[int, bool] | None:
    """``(n, capped)`` from the grid's ``Showing 1-10 of n[+]`` line, or None when there is no grid —
    no record matched. Pure."""
    match = _SHOWING_RE.search(page_html)
    return (int(match.group(1)), match.group(2) == "+") if match else None


def next_page_target(page_html: str) -> str | None:
    """The ``__EVENTTARGET`` of the pager's "Next >" link, or None on the last page. Pure."""
    tree = HTMLParser(page_html)
    for link in tree.css("table.aca_pagination a"):
        if (_text(link) or "").startswith("Next"):
            match = _NEXT_RE.search(html.unescape(link.attributes.get("href") or ""))
            return match.group(1) if match else None
    return None


async def _post(session, url: str, fields: dict[str, str], target: str):
    form = dict(fields) | {"__EVENTTARGET": target, "__EVENTARGUMENT": ""}
    try:
        response = await session.post(url, data=form, headers={"Referer": url}, timeout=120)
    except TRANSPORT_ERRORS as exc:
        raise AcaError(f"{url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code >= 400 or "Error.aspx" in str(response.url):
        raise AcaError(f"{url}: HTTP {response.status_code} at {response.url}")
    return response


async def open_search(session, url: str) -> AcaPage:
    """Fetch a module's search page and read the form a search posts back."""
    try:
        response = await session.get(url, timeout=60)
    except TRANSPORT_ERRORS as exc:
        raise AcaError(f"{url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code >= 400:
        raise AcaError(f"{url}: HTTP {response.status_code}")
    return AcaPage(url, form_fields(response.text))


async def search_all(session, page: AcaPage, record_type: str, *, number_prefix: str = "") -> list[AcaRow]:
    """Every record of ``record_type`` whose number starts with ``number_prefix``, walking the grid's
    pages. Raises `SearchCapped` when the portal lists only its cap's worth, and `AcaError` when a
    page cannot be read or the walk does not reach the count the grid announced."""
    fields = dict(page.fields) | {_FIELD + "ddlGSPermitType": record_type,
                                  _FIELD + "txtGSPermitNumber": number_prefix}
    response = await _post(session, page.url, fields, _SEARCH_BUTTON)
    where = f"{record_type} {number_prefix or '(all)'}"
    if "CapDetail.aspx" in str(response.url):
        row = detail_row(response.text)
        if row is None:
            raise AcaError(f"{where}: redirected to {response.url}, which shows no record")
        return [row]
    count = result_count(response.text)
    if count is None:
        return []
    total, capped = count
    if capped:
        raise SearchCapped(f"{where}: {total}+ records, more than the portal lists")
    rows: list[AcaRow] = []
    for _ in range(_MAX_PAGES):
        rows.extend(result_rows(response.text))
        target = next_page_target(response.text)
        if target is None:
            break
        url = urljoin(page.url, str(response.url))
        response = await _post(session, url, form_fields(response.text), target)
    unique = {row.number: row for row in rows}
    if len(unique) != total:
        raise AcaError(f"{where}: the grid announced {total} records and the walk read {len(unique)}")
    return list(unique.values())
