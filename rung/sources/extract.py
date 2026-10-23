"""Extract dispensary records from a state's discovered list resource.

Dispatches on the coarse list_type stored by state_lists.py:

  pdf    → generic PDF table extraction (pdfplumber)
  csv    → delimited download
  kml    → Google My Maps / KML placemarks
  arcgis → resolve the ArcGIS web map's feature service and query it
  html   → parse the most dispensary-like HTML table
  lookup → search-tool front ends have no static list → AI fallback
  unknown→ AI fallback

Each handler returns DispensaryRecord objects; the caller stamps state + source.
Handlers are best-effort and return [] on failure so the caller can fall back to
the AI extractor (ai_fallback.extract_with_ai), which is only attempted on request.
"""

import asyncio
import csv
import datetime
import html
import io
import itertools
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal, get_args
from urllib.parse import parse_qs, quote, urljoin, urlparse

import openpyxl
import pdfplumber
import xlrd
from selectolax.parser import HTMLParser

from rung import db
from rung.addresses import (
    CITY_STATE_ZIP_RE,
    PHONE_RE as _PHONE_RE,
    STREET_LINE_RE,
    STREET_RE as _STREET_RE,
    ZIP_RE as _ZIP_RE,
    clean as _clean,
    extract_address_blocks as _extract_address_blocks,
    extract_line_blocks as _extract_line_blocks,
)
from rung.http import make_session
from rung.models import DispensaryRecord, LocationObservation
from rung.sources import accela_aca, salesforce_aura
from rung.sources.dedupe import location_key
from rung.text import is_placeholder_name

# Header synonyms, longest/most-specific first so "zip code" beats "code" etc.
_FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "zip_code": ("zip code", "zipcode", "postal code", "zip", "postal"),
    "address": ("street address", "address", "street", "addr"),
    "city": ("city", "town", "municipality"),
    "state": ("state", "province"),
    "phone": ("phone number", "telephone", "phone", "tel"),
    "website": ("website", "web site", "url", "homepage"),
    "name": (
        "dispensary name", "business name", "trade name", "legal name", "dba",
        "licensee", "facility name", "establishment", "entity", "name",
        "company name", "dispensary", "business", "company", "facility", "store",
    ),
}

# Separators states use to pack "NAME – 123 Main St" into one cell.
_NAME_ADDR_SEP_RE = re.compile(r"\s*[–—|]\s*|\s+-\s+")
# `_STREET_RE`, allowing a paired street number ("2370 & 2380 Homestead Rd") — for splitting a
# packed "NAME – ADDRESS" cell only, where the segment boundary already says where a street starts.
_SPLIT_STREET_RE = re.compile(r"\d{1,6}(?:\s*[&-]\s*\d{1,6})?\s+[A-Za-z0-9.\- ]+")
_YES_NO_VALUES = {"Y", "N", "YES", "NO", "X", "N/A", "NA", "-", "—"}
# iframe srcs that never hold a dispensary list (analytics, embeds, video).
_JUNK_IFRAME_RE = re.compile(
    r"googletagmanager|google-analytics|doubleclick|youtube|vimeo|recaptcha|facebook",
    re.IGNORECASE,
)
# Address primitives (_ZIP_RE, _STREET_RE, _PHONE_RE, _clean,
# _extract_address_blocks) are imported from rung.addresses above;
# that module also serves the company-store extractor, so neither reaches into the other.


# A header word that makes the column a fact ABOUT the field, not the field: "Facility Type",
# "Business License Number", "Store Status". Matched as whole words in what the synonym leaves over.
_HEADER_QUALIFIERS = frozenset({
    "type", "status", "class", "category", "date", "number", "no", "num", "#", "id", "count",
    "hours", "expiration", "expires", "issued", "capacity",
})


def _match_field(header: str) -> str | None:
    """Map a header cell to a DispensaryRecord field, or None.

    A synonym must match WHOLE WORDS — the header itself, its leading or trailing words, or one of
    its words — and the words it leaves over must not qualify it (`_HEADER_QUALIFIERS`). Until
    2026-10-06 a bare substring at either end matched, so "Capacity" was a city column and
    "Facility Type" a name column; since the first matching column wins, "Facility Type" then
    shadowed the real "Facility Name" and every row was named "Retail".
    """
    words = " ".join(header.lower().split()).split()
    if not words:
        return None
    for field, synonyms in _FIELD_SYNONYMS.items():
        for syn in synonyms:
            syn_words = syn.split()
            size = len(syn_words)
            if words[:size] == syn_words:
                rest = words[size:]
            elif words[-size:] == syn_words:
                rest = words[:-size]
            elif size == 1 and syn in words:
                rest = [word for word in words if word != syn]
            else:
                continue
            if any(word.strip(".:") in _HEADER_QUALIFIERS for word in rest):
                continue
            return field
    return None


# A bare date — never a street address. The table extractors can mis-map a date column onto
# `address` when a roster interleaves non-dispensary rows (IL's "all cannabis licenses" PDF puts a
# license-issuance date where the address would be); nulling it keeps a junk value out of `address`
# regardless of whether the row's name also tripped the placeholder filter.
_DATE_VALUE_RE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$")


def _addr_or_none(value: str | None) -> str | None:
    return None if value and _DATE_VALUE_RE.match(value) else value


def _record_from_values(source: str, values: dict[str, str | None]) -> DispensaryRecord:
    """Build a DispensaryRecord from a header→field-name value map.

    The HTML/PDF/CSV extractors map detected columns to DispensaryRecord field names
    (a subset of the text fields — see ``_FIELD_SYNONYMS``), yielding a
    ``dict[str, str | None]``. Pulling each field explicitly (rather than ``**values``)
    keeps the construction type-checkable and silently ignores any non-field key.
    Numeric ``latitude``/``longitude`` are never column-mapped — the geo extractors set
    those directly — so they are intentionally absent here.
    """
    return DispensaryRecord(
        source=source,
        name=values.get("name"),
        address=_addr_or_none(values.get("address")),
        city=values.get("city"),
        state=values.get("state"),
        zip_code=values.get("zip_code"),
        phone=values.get("phone"),
        website=values.get("website"),
    )


# ── HTML tables ──────────────────────────────────────────────────────────────

# Screen-reader-only text is part of a cell's DOM and not of its meaning: Florida's MMTC page wraps
# every licence-letter link in `<span class="sr-only">(opens in new tab)</span>`, and 24 roster rows
# were named "Ayr Cannabis Dispensary(opens in new tab)" until 2026-09-24 — each then failed to
# match its own operator's stores and surfaced as a "possible closure".
_HIDDEN_CELL_TEXT = "span.sr-only, span.visually-hidden, .screen-reader-text"


def _row_cells(tr) -> list[str]:
    cells = []
    for cell in tr.css("td, th"):
        for hidden in cell.css(_HIDDEN_CELL_TEXT):
            hidden.decompose()
        cells.append((cell.text() or "").strip())
    return cells


def _looks_like_name(value: str) -> bool:
    """A cell that could plausibly be a dispensary name (free text with letters)."""
    v = value.strip()
    if len(v) < 3 or v.upper() in _YES_NO_VALUES:
        return False
    if _ZIP_RE.fullmatch(v) or _PHONE_RE.fullmatch(v):
        return False
    return any(c.isalpha() for c in v)


def _infer_name_column(body_rows: list[list[str]], exclude: Collection[int] = ()) -> int | None:
    """Pick the column most likely to hold the dispensary name.

    Used when no header cell matched a name synonym (e.g. the header row is a
    section title like "Southern Nevada Retail Stores"). Choose the column whose
    cells are mostly free text and longest on average — names, often with an
    appended street address, dominate such tables. ``exclude`` is the columns the header already
    mapped: a street-address column is long free text too, and taking it overwrote its mapping, so
    every name became the street and the address was lost.
    """
    width = max((len(r) for r in body_rows), default=0)
    best_idx: int | None = None
    best_len = 0.0
    for idx in range(width):
        if idx in exclude:
            continue
        cells = [r[idx] for r in body_rows if idx < len(r)]
        named = [c for c in cells if _looks_like_name(c)]
        if not cells or len(named) < max(3, len(cells) * 0.6):
            continue
        avg_len = sum(len(c) for c in named) / len(named)
        if avg_len > best_len:
            best_len = avg_len
            best_idx = idx
    return best_idx


def _split_name_address(value: str) -> tuple[str, str | None]:
    """Split a combined "NAME – 123 Main St – Adult Use" cell into (name, address).

    Only splits when the segment after the name looks like a street address, so
    names that merely contain a hyphen (e.g. "BEYOND/HELLO - Reno") are left
    intact. A trailing license-type tag (no digits, few words — "Adult Use",
    "Medical") is dropped from the address. The address starts at the first segment that
    starts like a street, so a location label between the name and the street stays with
    the name ("RISE CARSON CITY – US HWY 50 – 3060 U.S. Highway 50"), and a street number
    may be a pair ("2370 & 2380 Homestead Rd") — Nevada's CCB list carries both, and each
    left the whole cell in `name` with no address.
    """
    parts = [p.strip() for p in _NAME_ADDR_SEP_RE.split(value) if p.strip()]
    if len(parts) < 2:
        return value.strip(), None
    if len(parts) > 2 and not any(ch.isdigit() for ch in parts[-1]) and len(parts[-1].split()) <= 4:
        parts = parts[:-1]
    start = next((i for i in range(1, len(parts)) if _SPLIT_STREET_RE.match(parts[i])), None)
    if start is None:
        return value.strip(), None
    return " – ".join(parts[:start]), ", ".join(parts[start:])


# A cell that SAYS it holds nothing: Florida's operator table fills its blank columns with "n/a",
# which mapped onto `phone` and made 49 address-less licensees read as fully located rows.
_PLACEHOLDER_VALUES = frozenset({"n/a", "na", "-", "—", "–", "none", "null", "tbd", "n.a.", "not applicable"})


def _present(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in _PLACEHOLDER_VALUES


def _location_fraction(records: list[DispensaryRecord]) -> float:
    """Share of records carrying any REAL location signal (address/city/zip/phone) — a placeholder
    such as "n/a" or "-" in one of those cells is not a signal."""
    if not records:
        return 0.0
    located = sum(
        1 for r in records
        if _present(r.address) or _present(r.city) or _present(r.zip_code) or _present(r.phone)
    )
    return located / len(records)


def _rooftop_fraction(records: list[DispensaryRecord]) -> float:
    """Share of records that place a STORE somewhere — address, city or zip. A phone is a contact,
    not a rooftop: 17 of Florida's 49 licensed operators list one, and none of them is a store."""
    if not records:
        return 0.0
    placed = sum(1 for r in records if _present(r.address) or _present(r.city) or _present(r.zip_code))
    return placed / len(records)


def _street_fraction(cells: list[str]) -> float:
    """Share of the non-empty cells that open with a street number ("226 S Philadelphia Ave")."""
    values = [c.strip() for c in cells if c.strip()]
    if not values:
        return 0.0
    return sum(1 for v in values if _STREET_RE.match(v)) / len(values)


# A header-mapped `address` column holding no street at all is wrong; a lone unmapped column
# that holds streets is where the address really lives. Demand both before overriding a header.
_SWAP_ADDRESS_MAX = 0.10
_SWAP_CANDIDATE_MIN = 0.60


def _repair_swapped_address(col_map: dict[int, str], body: list[list[str]]) -> None:
    """Re-map ``address`` onto the column that actually holds streets. Mutates ``col_map``.

    A roster's header row can be misordered relative to its data. Maryland's dispensary locator
    is headed ``Dispensary | County | Address`` while every data row is ``name | street | county``,
    so trusting the header files the COUNTY as the address and drops the street entirely. The rows
    load and look fine; they simply can never match a company store. Same class of defect as the
    mis-mapped date column `_addr_or_none` nulls, and just as silent.

    Trust the header unless the data contradicts it unambiguously: the mapped `address` column must
    hold essentially no street, and EXACTLY ONE otherwise-unmapped column must mostly hold streets.
    Two candidates, or none, means we cannot tell — so leave the header's word alone. `STREET_RE`
    requires digits *followed by a space*, so a bare licence number ("231001") is not a candidate.
    """
    address_idx = next((i for i, f in col_map.items() if f == "address"), None)
    if address_idx is None or len(body) < 3:
        return

    def column(idx: int) -> list[str]:
        return [row[idx] for row in body if idx < len(row)]

    if _street_fraction(column(address_idx)) > _SWAP_ADDRESS_MAX:
        return
    width = max((len(row) for row in body), default=0)
    candidates = [
        idx for idx in range(width)
        if idx not in col_map and _street_fraction(column(idx)) >= _SWAP_CANDIDATE_MIN
    ]
    if len(candidates) != 1:
        return
    del col_map[address_idx]
    col_map[candidates[0]] = "address"


def _extract_table(table) -> tuple[list[DispensaryRecord], bool]:
    """Extract records from one table.

    Returns (records, header_named). header_named is True when a header cell
    matched a name synonym (high confidence); False when the name column was
    inferred from the body and the caller should require a location signal
    before trusting the table.
    """
    rows = table.css("tr")
    if len(rows) < 2:
        return [], False
    header = _row_cells(rows[0])
    col_map: dict[int, str] = {}
    for idx, cell in enumerate(header):
        field = _match_field(cell)
        if field is not None and field not in col_map.values():
            col_map[idx] = field

    header_named = "name" in col_map.values()
    body = rows[1:]
    body_cells = [_row_cells(r) for r in body]
    if not header_named:
        name_idx = _infer_name_column(body_cells, exclude=col_map.keys())
        if name_idx is None:
            return [], False
        col_map[name_idx] = "name"

    # The header can be misordered relative to the data (MD files its county as the address).
    _repair_swapped_address(col_map, body_cells)

    # If no address column was identified, the name cell may carry the address.
    split_name = "address" not in col_map.values()
    records: list[DispensaryRecord] = []
    for tr in body:
        cells = _row_cells(tr)
        if not cells:
            continue
        values = {field: _clean(cells[idx]) for idx, field in col_map.items() if idx < len(cells)}
        name = values.get("name")
        if not name:
            continue
        if split_name:
            name, address = _split_name_address(name)
            values["name"] = name
            if address:
                values["address"] = address
        records.append(_record_from_values("html", values))
    return records, header_named


def _extract_html(html: str) -> list[DispensaryRecord]:
    """Return dispensary records aggregated across every qualifying HTML table.

    A page may split locations over several tables (e.g. NV's northern/southern
    regions). Header-named tables are trusted outright; inferred-name tables are
    kept only when most rows carry a location signal, to avoid scooping up an
    unrelated text table. Records are de-duplicated on (name, address).
    """
    tree = HTMLParser(html)
    aggregated: list[DispensaryRecord] = []
    seen: set[tuple[str | None, ...]] = set()
    tables: list[tuple[list[DispensaryRecord], float]] = []
    for table in tree.css("table"):
        records, header_named = _extract_table(table)
        if not records:
            continue
        if not header_named and _location_fraction(records) < 0.5:
            continue
        tables.append((records, _rooftop_fraction(records)))
    # A LICENSEE table beside a STORE table is not a second store list. Florida's MMTC page carries
    # both: 49 licensed operators (name + licence number, no address) above 781 dispensing
    # locations. The header-named rule trusted the first outright, so every operator became a
    # roster row with no rooftop, which compare then reported as a store the operator had closed.
    # When any table on the page places its rows at a rooftop, a table that places almost none of
    # its own is the other kind of list and is dropped; a page whose only table is address-less
    # (NY's legal-entity licensees) is unchanged, because there is nothing better on it.
    if any(placed >= 0.5 for _, placed in tables):
        tables = [(records, placed) for records, placed in tables if placed >= 0.2]
    for records, _placed in tables:
        for record in records:
            # Dedup on the full identity, not just (name, address): operators
            # with many locations often share a name and have no street address
            # (e.g. a "Licensee's Name" + City listing), so a coarse key would
            # collapse distinct stores into one.
            key = (record.name, record.address, record.city, record.phone)
            if key not in seen:
                seen.add(key)
                aggregated.append(record)
    return aggregated


# ── PDF tables ───────────────────────────────────────────────────────────────

def _header_map(row: list) -> dict[int, str] | None:
    col_map: dict[int, str] = {}
    for idx, cell in enumerate(row):
        if not cell:
            continue
        field = _match_field(str(cell))
        if field is not None and field not in col_map.values():
            col_map[idx] = field
    return col_map if "name" in col_map.values() else None


def _unmerge_name_overflow(
    name: str | None, address: str | None, city: str | None,
) -> tuple[str | None, str | None]:
    """Repair a name whose overflow was *overprinted* onto the address column.

    PA's roster PDF draws a long store name past its column, on top of the address, at
    overlapping x-positions and in the same font — so pdfplumber interleaves the two runs
    character by character and the name is left truncated::

        name    'Restore Integrative Wellness Center - Elkins'   (truncated: lost 'Park')
        address 'P8a0rk03 Old York Road'                         ('Park' ⊕ '8003')

    No extractor can split that geometrically. But the corruption is *invertible*, because we
    already know one of the two interleaved strings: the lost characters are exactly the tail of
    the ``city``, whose head the truncated ``name`` still ends with. Subtract that tail from the
    address, in order, and both fields come back ('Restore … - Elkins Park', '8003 Old York Road').

    Deliberately strict — every condition must hold, or the row is returned untouched: the name
    must end with a *proper* prefix of the city, every overflow character must be consumed in order
    from the address's head, the repaired address must start with a digit (a street number), and
    both repaired fields must have balanced parentheses. That last guard is not hypothetical: PA
    also carries ``'Ethos - Pittsburgh North of Harmarville (Har'`` / ``'m5a rAvlipllhea) Drive
    East'``, where the overflow came from the name's own parenthetical rather than the city. Its
    real city ("Pittsburgh") already fails the prefix test, but a city of "Harmarville" would have
    slipped through into ``'5 Alpha) Drive East'`` — a stray ``)`` betrays the bad split. That row
    stays broken, which is correct: repairing a row we misread is worse than leaving one broken.
    """
    if not (name and address and city):
        return name, address
    flat_city = city.strip()
    stripped = name.rstrip()
    # The name's tail must be a proper prefix of the city ("… - Elkins" of "Elkins Park").
    overlap = next(
        (n for n in range(len(flat_city) - 1, 0, -1) if stripped.endswith(flat_city[:n])),
        0,
    )
    overflow = flat_city[overlap:].replace(" ", "")
    if not overlap or not overflow:
        return name, address

    kept: list[str] = []
    pending = list(overflow)
    for char in address:
        if pending and char == pending[0]:
            pending.pop(0)
        else:
            kept.append(char)
    repaired = "".join(kept).strip()
    repaired_name = f"{stripped}{flat_city[overlap:]}"
    balanced = all(text.count("(") == text.count(")") for text in (repaired, repaired_name))
    if pending or not repaired[:1].isdigit() or not balanced:
        return name, address  # not the overprint we know how to invert — leave it alone
    return repaired_name, repaired


def _extract_pdf(content: bytes) -> list[DispensaryRecord]:
    records: list[DispensaryRecord] = []
    col_map: dict[int, str] | None = None
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for row in table:
                if col_map is None:
                    col_map = _header_map(row)
                    continue  # this row was the header
                # Skip a header repeated atop a later page. A real header matches
                # several field synonyms; require ≥3 so a data row whose name cell
                # merely contains a synonym word ("... Dispensary", "... Store")
                # isn't mistaken for a header and dropped.
                repeated = _header_map(row)
                if repeated is not None and len(repeated) >= 3:
                    continue
                values = {
                    field: _clean(row[idx])
                    for idx, field in col_map.items()
                    if idx < len(row)
                }
                if not values.get("name"):
                    continue
                # A long name overprinted onto the address column interleaves the two (PA).
                values["name"], values["address"] = _unmerge_name_overflow(
                    values.get("name"), values.get("address"), values.get("city"),
                )
                records.append(_record_from_values("pdf", values))
    return records


# ── Arizona DHS 'Licensed Marijuana Establishments' PDF (list_type='az_dhs') ──
# A borderless, word-wrapped columnar PDF the generic pdfplumber table extractor mangles
# (it recovered 25/143 rows and picked the legal entity over the brand). Parse it by
# bucketing words into the fixed columns by x-position instead.

_AZ_CERT_RE = re.compile(r"^0*\d+ES[0-9A-Z]+$")  # e.g. 00000070ESCO78837103
# (column name, left x of its header) — a word belongs to the right-most column it clears.
_AZ_COLS = (
    ("status", 42), ("cert", 116), ("estname", 258), ("dba", 420),
    ("street", 515), ("city", 619), ("zip", 694),
)
# Header/footer lines. Whole words, and `page` only as "Page <n>" with a 1-3 digit page number: a
# bare `page` dropped every establishment in Page, AZ, and its ZIP (86040) is five digits. The certificate test runs FIRST in the parser, so a record line is never
# skipped for a word it happens to carry.
_AZ_SKIP_RE = re.compile(
    r"certificate number|establishment name|zip code|street address"
    r"|total licensees|\bupdated\b|adult use marijuana|licensed marijuana|\bpage\s+\d{1,3}(?!\d)",
    re.IGNORECASE,
)


def _az_column(x0: float) -> str:
    column = _AZ_COLS[0][0]
    for name, start in _AZ_COLS:
        if x0 >= start - 6:
            column = name
    return column


def _extract_az_dhs(content: bytes) -> list[DispensaryRecord]:
    """Parse the AZDHS Licensed Marijuana Establishments PDF.

    Words are bucketed into the Status/Cert/Establishment/DBA/Street/City/Zip columns by
    x-position; a record begins on the line carrying a certificate number and absorbs the
    wrapped continuation lines below it (header/footer lines are skipped). The name prefers
    the DBA (storefront brand) and falls back to the legal entity when no DBA is listed.
    """
    fields = ("estname", "dba", "street", "city", "zip")
    rows: list[dict[str, str]] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            lines: dict[int, list] = {}
            for word in page.extract_words():
                lines.setdefault(round(word["top"] / 3), []).append(word)
            current: dict[str, str] | None = None
            for key in sorted(lines):
                cells: dict[str, list[str]] = {}
                for word in sorted(lines[key], key=lambda w: w["x0"]):
                    cells.setdefault(_az_column(word["x0"]), []).append(word["text"])
                joined = {col: " ".join(parts) for col, parts in cells.items()}
                line_text = " ".join(joined.values())
                is_record = bool(_AZ_CERT_RE.match(joined.get("cert", "").replace(" ", "")))
                if not is_record and _AZ_SKIP_RE.search(line_text):
                    continue
                if is_record:
                    current = {col: joined.get(col, "") for col in fields}
                    rows.append(current)
                elif current is not None:  # wrapped continuation of the current record
                    for col in fields:
                        if joined.get(col):
                            current[col] = f"{current[col]} {joined[col]}".strip()
    records: list[DispensaryRecord] = []
    for row in rows:
        name = _clean(row["dba"]) or _clean(row["estname"])
        if name:
            records.append(DispensaryRecord(
                source="az_dhs", name=name, address=_clean(row["street"]),
                city=_clean(row["city"]), zip_code=_clean(row["zip"]),
            ))
    return records


# ── Illinois IDFPR 'Combined License List' PDF (list_type='il_idfpr') ─────────
# IDFPR publishes one PDF combining three lists; only the first, "Active Adult Use Dispensing
# Organization Licenses" (289 on 2026-09-24), is a store roster — the other two are conditional
# licences with no storefront. Its table is borderless and every cell wraps: the licence holder
# and the dispensary name run over two or three lines, and the address column holds the street,
# then "City, IL 60060", then the phone. The generic pdfplumber path turned each wrapped line into
# a roster row, so 111 of 248 Illinois rows were fragments like "of the Quad Cities," and "LLC"
# with no address. Like az_dhs, words are bucketed into columns by x-position; a record is closed
# by the PHONE line in the address column, which every entry ends with. The section boundaries
# come from the PDF's own table of contents on page 1, not from a page count.
_IL_COLS = (("holder", 0), ("name", 140), ("address", 245), ("date", 390), ("credential", 475))
_IL_PHONE_RE = re.compile(r"^\(\d{3}\)\s*\d{3}-\d{4}$")
_IL_CITY_RE = re.compile(r"^(.+?),\s*(?:IL|Illinois)\s+(\d{5})(?:-\d{4})?$", re.IGNORECASE)
_IL_SKIP_RE = re.compile(
    r"license holder|dispensary name|address & phone|issuance date|credential number"
    r"|total number of|highlighted in|bolded were|idpfr\.illinois|governor|secretary|director"
    r"|pritzker|treto|lindsay|^page \d+|active adult use dispensing",
    re.IGNORECASE,
)
_IL_TOC_RE = re.compile(r"(ACTIVE ADULT USE|ORIGINAL LOTTERY|SECL CONDITIONAL)[^\d]*?(\d+)\s*$",
                        re.IGNORECASE | re.MULTILINE)


def _il_column(x0: float) -> str:
    column = _IL_COLS[0][0]
    for name, start in _IL_COLS:
        if x0 >= start - 4:
            column = name
    return column


def _il_section_pages(toc_text: str) -> tuple[int, int | None]:
    """``(first page, first page of the next section)`` of the adult-use list, 1-based, from the TOC.

    Falls back to "page 2 onward" when the TOC cannot be read, so a re-formatted cover page degrades
    to over-reading the conditional lists (rows without a phone never close, so they are dropped)
    rather than to reading nothing.
    """
    starts = {label.upper()[:6]: int(page) for label, page in _IL_TOC_RE.findall(toc_text)}
    first = starts.get("ACTIVE", 2)
    following = [p for label, p in starts.items() if label != "ACTIVE" and p > first]
    return first, (min(following) if following else None)


_IL_ANCHOR_RE = re.compile(r"AUDO", re.IGNORECASE)   # every adult-use entry carries one -AUDO credential
_IL_STREET_SUFFIX_RE = re.compile(
    r"\b(?:st|ave|rd|blvd|dr|hwy|highway|pkwy|ln|ct|pl|plaza|way|route|rte|road|street|avenue"
    r"|boulevard|drive|lane|court|circle|cir|trail|trl|terrace|ter|square|sq|loop|pike|parkway)\.?$",
    re.IGNORECASE,
)
_IL_UNIT_RE = re.compile(r"^(?:#\S+|ste\.?|suite|unit|bldg\.?|building|[a-z]|\d+[a-z]?|[a-z]\d+)$", re.IGNORECASE)
_IL_PHONE_ANYWHERE_RE = re.compile(r"(?:\(\d{3}\)\s*-?\s*|\b\d{3}-)\d{3}\s*-\s*\d{4}")


def il_idfpr_records(pages: list[list[dict]]) -> list[DispensaryRecord]:
    """The adult-use roster from the words of its pages — ``[{text, x0, top}, …]`` per page.

    Every entry carries exactly one ``…-AUDO`` credential, drawn at the entry's vertical middle, so
    the credentials are the ANCHORS: each line of the table belongs to the anchor nearest it, and
    one record is one anchor. (Two earlier cuts closed a record on its phone line, then on a
    vertical gap; the first lost 83 of 289 entries into their neighbours because a phone is not on
    every entry, the second split 289 into 318 because the gaps are not uniform. The anchor count
    IS the entry count, which is the invariant a parser of this table should rest on.) Split from
    the PDF reading so a captured page of words is a fixture.
    """
    records: list[DispensaryRecord] = []
    for words in pages:
        lines: dict[int, list[dict]] = {}
        for word in words:
            lines.setdefault(round(float(word["top"]) / 3), []).append(word)
        parsed: list[tuple[float, dict[str, str]]] = []
        for key in sorted(lines):
            line_words = sorted(lines[key], key=lambda w: float(w["x0"]))
            cells: dict[str, list[str]] = {}
            for word in line_words:
                cells.setdefault(_il_column(float(word["x0"])), []).append(str(word["text"]))
            joined = {column: " ".join(parts) for column, parts in cells.items()}
            if _IL_SKIP_RE.search(" ".join(joined.values())):
                continue
            parsed.append((min(float(w["top"]) for w in line_words), joined))
        anchors = [top for top, joined in parsed if _IL_ANCHOR_RE.search(joined.get("credential", ""))]
        if not anchors:
            continue
        entries: list[dict[str, list[str]]] = [{column: [] for column, _ in _IL_COLS} for _ in anchors]
        # Between two anchors the boundary is the WIDEST vertical gap between consecutive lines —
        # the whitespace the table draws between entries — not the midpoint: a wrapped name's last
        # line or a zip on its own line sits nearer the next anchor than its own, and "nearest
        # anchor" filed 39 of 289 addresses with their neighbours.
        boundaries: list[float] = []
        for lower, upper in itertools.pairwise(anchors):
            between = [top for top, _ in parsed if lower < top <= upper]
            gaps = [(after - before, before) for before, after in itertools.pairwise(between)]
            boundaries.append(max(gaps)[1] if gaps else (lower + upper) / 2)
        for top, joined in parsed:
            index = sum(1 for boundary in boundaries if top > boundary)
            for column, text in joined.items():
                entries[index][column].append(text)
        records.extend(record for record in map(_il_record, entries) if record is not None)
    return records


def _il_split_address(lines: list[str]) -> tuple[str | None, str | None, str | None, str | None]:
    """``(street, city, zip, phone)`` from an entry's address-column lines, in any wrapping.

    The street and "City, IL 60060" may share a line or split across two, and a long city may
    wrap onto the zip line ("993 E Rollins Rd Round" / "Lake Beach, IL 60073"), so the non-phone
    lines are joined and the city is what follows the last street suffix (past any unit token).
    """
    text = " ".join(lines)
    phone_match = _IL_PHONE_ANYWHERE_RE.search(text)
    phone = re.sub(r"\s*-\s*", "-", phone_match.group(0)).replace(")-", ") ") if phone_match else None
    if phone_match:
        text = (text[:phone_match.start()] + text[phone_match.end():])
    # Source quirks, verbatim from the 2026-09-24 list: "Tel: (TBD)", a bare "TBD", "Tel:" with
    # nothing after it. None of them is an address token.
    text = re.sub(r"\s*\bTel:?\s*(?:\(TBD\))?\s*$|\s+TBD\s*$", "", text.strip(), flags=re.IGNORECASE).strip()
    # A trailing 1–2 digit token after the zip is a FOOTNOTE marker ("IL 60707 2"), not data.
    match = re.search(
        r"^(.*?),?\s*(?:(?:IL|Illinois)\.?,?\s*)?(\d{5})(?:\s*-\s*\d{4})?(?:\s+\d{1,2})?\s*$",
        text, re.IGNORECASE)
    if not match:
        return (text or None), None, None, phone
    before, zip_code = match.group(1).strip().rstrip(","), match.group(2)
    tokens = before.split()
    cut = None
    for index in range(len(tokens) - 1, -1, -1):
        if _IL_STREET_SUFFIX_RE.match(tokens[index]):
            cut = index + 1
            break
    if cut is None:
        # no recognisable suffix: everything before the comma the source itself drew is the street
        street, _, city = before.rpartition(",")
        return (street.strip() or before or None), (city.strip() or None), zip_code, phone
    while cut < len(tokens) and _IL_UNIT_RE.match(tokens[cut]):
        cut += 1
    street, city = " ".join(tokens[:cut]).rstrip(","), " ".join(tokens[cut:]).strip(", ")
    return (street or None), (city or None), zip_code, phone


def _il_record(cells: dict[str, list[str]]) -> DispensaryRecord | None:
    street, city, zip_code, phone = _il_split_address(cells["address"])
    name = _clean(" ".join(cells["name"]).rstrip("*")) or _clean(" ".join(cells["holder"]))
    if not name:
        return None
    return DispensaryRecord(
        source="il_idfpr", name=name, address=_clean(street) if street else None,
        city=city, zip_code=zip_code, phone=phone,
        licence_number=_clean(" ".join(cells["credential"])) or None,
    )


def _extract_il_idfpr(content: bytes) -> list[DispensaryRecord]:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        first, stop = _il_section_pages(pdf.pages[0].extract_text() or "")
        last = (stop or len(pdf.pages) + 1) - 1
        pages = [
            [{"text": w["text"], "x0": w["x0"], "top": w["top"]} for w in page.extract_words()]
            for page in pdf.pages[first - 1:last]
        ]
    return il_idfpr_records(pages)


# ── CSV ──────────────────────────────────────────────────────────────────────

_DBA_HEADERS = frozenset({"dba", "d/b/a", "doing business as", "trade name", "trade_name"})


_XLSX_MAGIC = b"PK\x03\x04"
_XLS_MAGIC = b"\xd0\xcf\x11\xe0"


def _sheet_cell(value: object) -> str:
    """A spreadsheet cell as roster text: blank for empty, a whole float without its ``.0``."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _spreadsheet_rows(content: bytes) -> list[list[str]] | None:
    """The first sheet of an .xlsx or .xls workbook as rows of text, or None if ``content`` is not
    one. A spreadsheet link is classified ``csv``; decoded as text, the binary workbook raised a
    swallowed csv error and the state silently got no roster."""
    if content.startswith(_XLSX_MAGIC):
        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = book.worksheets[0]
        return [[_sheet_cell(v) for v in row] for row in sheet.iter_rows(values_only=True)]
    if content.startswith(_XLS_MAGIC):
        sheet = xlrd.open_workbook(file_contents=content).sheet_by_index(0)
        return [[_sheet_cell(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
                for r in range(sheet.nrows)]
    return None


def _extract_csv(text: str) -> list[DispensaryRecord]:
    return _extract_rows(list(csv.reader(io.StringIO(text))))


def _extract_rows(rows: list[list[str]]) -> list[DispensaryRecord]:
    """Header-mapped rows (a CSV, or a spreadsheet's first sheet) → records."""
    if len(rows) < 2:
        return []
    # The storefront brand (a DBA / trade-name column) is preferred over the legal/business name
    # via a per-row override below. So the DBA column must NOT claim the `name` slot itself —
    # otherwise a row with a blank DBA but a populated legal name is dropped when the DBA column
    # happens to sort before the legal-name column (the override can't recover it). Keep `name`
    # mapped to a non-DBA column so a blank DBA falls back to the legal/business name.
    dba_idx = next(
        (idx for idx, cell in enumerate(rows[0]) if " ".join(cell.lower().split()) in _DBA_HEADERS),
        None,
    )
    col_map: dict[int, str] = {}
    for idx, cell in enumerate(rows[0]):
        field = _match_field(cell)
        if field is None or field in col_map.values():  # first column per field wins
            continue
        if field == "name" and idx == dba_idx:
            continue  # the DBA column is the override, not the fallback name source
        col_map[idx] = field
    if "name" not in col_map.values():
        # The only name-ish column IS the DBA column (no separate legal/business name) — use it.
        if dba_idx is None:
            return []
        col_map[dba_idx] = "name"
    records: list[DispensaryRecord] = []
    for row in rows[1:]:
        values = {field: _clean(row[idx]) for idx, field in col_map.items() if idx < len(row)}
        if dba_idx is not None and dba_idx < len(row) and _clean(row[dba_idx]):
            values["name"] = _clean(row[dba_idx])
        if values.get("name"):
            records.append(_record_from_values("csv", values))
    return records


# ── Colorado MED licensed stores, Google Sheet (list_type='co_med') ──────────
# The CO MED publishes its licensed stores as ONE Google Sheet with TWO tabs, `Medical` (271 rows on
# 2026-09-24) and `Retail` (654). Columns on both: License Number, Facility Name (legal entity), DBA
# (storefront brand), Facility Type, Street, City, ZIP Code, … The generic CSV path would pick
# "Facility Name" (legal) for the name because it precedes "DBA"; this handler prefers the DBA
# brand (like az_dhs).
#
# ⚠ UNTIL 2026-09-24 THIS READ THE CSV EXPORT, WHICH IS THE FIRST TAB ONLY. Colorado's roster was
# the 271 medical stores against 824 stores we hold — a 31% "completeness" that measured the export
# format, not the state. The list URL is now the XLSX export, every sheet is read, and a rooftop
# licensed under both programs (the common case: one store, two licences) is ONE roster row —
# the Retail row wins, because this is a store roster and not a licence ledger. The CSV form is
# still accepted so a text export keeps working.
_CO_STORE_TYPES = frozenset({"medical marijuana store", "retail marijuana store"})


def _co_rows(content: bytes | str) -> list[dict[str, str]]:
    """Every row of every sheet (xlsx) or of the one CSV, as ``{lower header: value}`` dicts."""
    if isinstance(content, bytes) and content[:2] == b"PK":
        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
        rows: list[dict[str, str]] = []
        for sheet in book.worksheets:
            lines = sheet.iter_rows(values_only=True)
            header = [str(h or "").strip().lower() for h in next(lines, ())]
            if "facility name" not in header:
                continue
            for values in lines:
                row = {key: ("" if v is None else str(v).strip()) for key, v in zip(header, values, strict=False)}
                if any(row.values()):
                    rows.append(row)
        return rows
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(io.StringIO(text))]


def _extract_co_med(content: bytes | str) -> list[DispensaryRecord]:
    by_rooftop: dict[tuple[str, str], DispensaryRecord] = {}
    order: list[tuple[str, str]] = []
    for row in _co_rows(content):
        facility_type = row.get("facility type", "").lower()
        if facility_type and facility_type not in _CO_STORE_TYPES:
            continue
        name = _clean(row.get("dba")) or _clean(row.get("facility name"))
        if not name:
            continue
        record = DispensaryRecord(
            source="co_med", name=name, address=_clean(row.get("street")),
            city=_clean(row.get("city")), zip_code=_clean(row.get("zip code")),
            licence_number=_clean(row.get("license number")),
        )
        rooftop = (
            (record.address or "").lower(), (record.zip_code or "").lower(),
        ) if record.address else (name.lower(), (record.city or "").lower())
        if rooftop not in by_rooftop:
            order.append(rooftop)
            by_rooftop[rooftop] = record
        elif facility_type == "retail marijuana store":
            by_rooftop[rooftop] = record  # one store, two licences: the retail row names it
    return [by_rooftop[key] for key in order]


# ── Massachusetts CCC 'commenced operations' CSV (list_type='ma_ccc') ────────
# The MA Cannabis Control Commission publishes a SODA CSV of all establishments that have
# commenced operations, with every license type. Keep only the active storefronts (Marijuana
# Retailer + Medical Marijuana Treatment Center); drop cultivators/manufacturers/transporters/
# labs. Names are the legal entity (no DBA column).
#
# THE COLUMNS ARE THE COMMISSION'S TO RENAME, AND IT DID. The file stamped `CCCLastUpdated`
# 2026-10-06 moved the premises from ADDRESS_1 / CITY / ZIP_CODE (and the PHYSICAL_* pair) to
# ESTABLISHMENT_ADDRESS_1 / ESTABLISHMENT_CITY / ESTABLISHMENT_ZIP, added a BUSINESS_* set that is
# the licensee's office, and dropped the lat/lng columns. The handler read the old names, found
# nothing, and the Sunday sweep of 2026-10-11 replaced 423 addressed rows with 498 name-only ones
# (`roster_name_only MA` went ERROR; the roster leg of the store history recorded nothing for the
# week, because a row with no address has no location key). Each chain below names every spelling
# the file has carried, premises first. BUSINESS_* is deliberately absent: an office address would
# pair `compare` to the wrong building, and no address is the honest reading of such a row. A header
# naming NONE of a chain's spellings is the rename happening again, and is refused as PARTIAL — the
# prior roster stands and the attempt records why — rather than landed as a roster of names.

_MA_STOREFRONT_TYPES = {"Marijuana Retailer", "Medical Marijuana Treatment Center"}
_MA_ADDRESS_COLUMNS = ("ESTABLISHMENT_ADDRESS_1", "PHYSICAL_ADDRESS_1", "ADDRESS_1")
_MA_CITY_COLUMNS = ("ESTABLISHMENT_CITY", "PHYSICAL_CITY", "CITY")
_MA_ZIP_COLUMNS = ("ESTABLISHMENT_ZIP", "ESTABLISHMENT_ZIP_CODE", "PHYSICAL_ZIP_CODE", "ZIP_CODE")


def _extract_ma_ccc(text: str) -> list[DispensaryRecord]:
    def _f(value: str | None) -> float | None:
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _first(row: dict[str, str | None], columns: tuple[str, ...]) -> str | None:
        for column in columns:
            if value := _clean(row.get(column)):
                return value
        return None

    reader = csv.DictReader(io.StringIO(text))
    header = set(reader.fieldnames or ())
    if header and not header & set(_MA_ADDRESS_COLUMNS):
        raise PartialRoster(
            "the CCC CSV's header names no premises address column "
            f"({' / '.join(_MA_ADDRESS_COLUMNS)}) — the commission renamed its columns again; "
            "a roster of names would pair nothing in compare-stores"
        )
    records: list[DispensaryRecord] = []
    for row in reader:
        if row.get("LICENSE_TYPE") not in _MA_STOREFRONT_TYPES:
            continue
        if (row.get("LICENSE_STATUS") or "").strip() != "Active":
            continue
        name = _clean(row.get("BUSINESS_NAME"))
        if not name:
            continue
        records.append(DispensaryRecord(
            source="ma_ccc", name=name,
            address=_first(row, _MA_ADDRESS_COLUMNS),
            city=_first(row, _MA_CITY_COLUMNS),
            zip_code=_first(row, _MA_ZIP_COLUMNS),
            latitude=_f(row.get("latitude")), longitude=_f(row.get("longitude")),
            licence_number=_clean(row.get("LICENSE_NUMBER")),
        ))
    return records


# ── KML / Google My Maps ─────────────────────────────────────────────────────

_KML_NS = "{http://www.opengis.net/kml/2.2}"


def _extract_kml(content: bytes) -> list[DispensaryRecord]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    records: list[DispensaryRecord] = []
    for placemark in root.iter(f"{_KML_NS}Placemark"):
        name = _clean(placemark.findtext(f"{_KML_NS}name"))
        if not name:
            continue
        data: dict[str, str | None] = {}
        ext = placemark.find(f"{_KML_NS}ExtendedData")
        if ext is not None:
            for d in ext.findall(f"{_KML_NS}Data"):
                key = (d.get("name") or "").lower()
                val = d.find(f"{_KML_NS}value")
                field = _match_field(key)
                if field:
                    data[field] = _clean(val.text if val is not None else None)
        address = data.get("address")
        if not address:
            desc = placemark.findtext(f"{_KML_NS}description") or ""
            parsed = _kml_description_address(desc)
            if parsed:
                address, city, state, zip_code = parsed
                data.setdefault("city", city)
                data.setdefault("state", state)
                data.setdefault("zip_code", zip_code)
            elif (cleaned := _clean(desc)) and _STREET_RE.search(cleaned):
                address = cleaned
        latitude, longitude = _kml_point(placemark)
        records.append(
            DispensaryRecord(
                source="kml", name=name, address=address,
                city=data.get("city"), state=data.get("state"),
                zip_code=data.get("zip_code"), phone=data.get("phone"),
                latitude=latitude, longitude=longitude,
            )
        )
    return records


#: A "City, State ZIP" line as a Google My Maps description writes it — the state spelled out
#: ("Macon, Georgia 31210") or abbreviated ("Macon, GA 31210"). The spelled-out form is kept as
#: written; a two-letter one is the abbreviation.
_KML_CITY_LINE_RE = re.compile(
    r"^(?P<city>[A-Za-z .'-]{2,40}),\s*(?P<state>[A-Za-z .]{2,30}?)\s+(?P<zip>\d{5}(?:-\d{4})?)$"
)
_KML_SUITE_RE = re.compile(r"^(?:suite|ste\.?|unit|#)\s*\S+$", re.IGNORECASE)


def _kml_description_address(description: str) -> tuple[str, str, str | None, str] | None:
    """``(street, city, state, zip)`` from a multi-line placemark description, or None.

    A Google My Maps description is HTML: ``<br>``-separated lines, typically the operator's legal
    name, the street, a suite line, "City, State ZIP" and a licence number (Georgia's commission
    map, 2026-10-11). The street is the last street-shaped line before the city line, a suite line
    between them is not the street, and the state is returned as the two-letter code when it is
    one and otherwise None — the caller stamps the roster's own jurisdiction.
    """
    lines = [
        cleaned for raw in re.split(r"<br\s*/?>|\n", description)
        if (cleaned := _clean(re.sub(r"<[^>]+>", " ", raw)))
    ]
    for index, line in enumerate(lines):
        city_line = _KML_CITY_LINE_RE.match(line)
        if city_line is None:
            continue
        street = next(
            (candidate for candidate in reversed(lines[:index])
             if STREET_LINE_RE.match(candidate) and not _KML_SUITE_RE.match(candidate)),
            None,
        )
        if street is None:
            return None
        state = city_line.group("state").strip()
        return (
            street, city_line.group("city").strip(),
            state.upper() if len(state) == 2 else None, city_line.group("zip"),
        )
    return None


def _kml_point(placemark) -> tuple[float | None, float | None]:
    """A placemark's ``<Point>`` coordinates as (lat, lng), or (None, None).

    KML encodes a point as ``lng,lat[,alt]`` (whitespace-padded in Google My Maps
    exports). A 0,0 placeholder or out-of-range value is dropped."""
    point = placemark.find(f"{_KML_NS}Point")
    if point is None:
        return None, None
    raw = _clean(point.findtext(f"{_KML_NS}coordinates"))
    if not raw:
        return None, None
    parts = raw.split(",")
    if len(parts) < 2:
        return None, None
    try:
        longitude, latitude = float(parts[0]), float(parts[1])
    except ValueError:
        return None, None
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        return None, None
    if latitude == 0 and longitude == 0:
        return None, None
    return latitude, longitude


# ── ArcGIS ───────────────────────────────────────────────────────────────────

class ExtractionFailed(Exception):
    """A roster handler CRASHED on what the source returned (a malformed payload, an unexpected shape).

    It is this state's failure and nobody else's, so `extract_records` turns any exception from an
    async handler into this one, and `run_extract_states` records the attempt `failed` WITH the
    reason and keeps the prior rows — the way it treats `PartialRoster`. It used to escape `gather`
    and kill every state's extraction; returning ``[]`` instead (the first fix) would have recorded
    the crash as an `empty` capture, i.e. "the source yielded nothing" — a failure of ours written
    down as a fact about the source (the 2026-10-06 whole-tree review).
    """


class PartialRoster(Exception):
    """A roster handler stopped AFTER it had gathered some rows: what it holds is a fragment.

    Also raised BEFORE any row by a handler that can see from the header that every row would
    be a fragment of itself — `_extract_ma_ccc`, when the commission's CSV names no premises
    column — since a roster of names is no less a fragment for being complete in one column.

    `run_extract_states` replaces a state's rows whenever the extraction is non-empty, so a paging
    failure that returned the pages it had would delete the full prior roster and install the
    fragment — and `compare` would then report the missing stores as absent from the state list.
    Handlers raise this instead; the orchestrator prints it and keeps the prior rows (review
    finding, 2026-09-26) — and SKIPS THE LATER TIERS for that state, records the attempt `failed`
    with this exception's text, and reports the state's method as ``none`` (2026-10-04, audit
    P-35: until then the state merely looked empty, so the opt-in AI tier could extract a roster
    of its own and replace the one this verdict had just refused to replace).
    """


def _arcgis_attr(attrs: dict, *names: str, exclude: tuple[str, ...] = ()) -> str | None:
    """Pick the first NON-EMPTY attribute whose key loosely matches any of names, in the order the
    names are given; a key containing any ``exclude`` token is never considered.

    An empty match used to end the search (Ohio's 47 stores with a blank DBA lost their licensee
    name that way, 2026-09-25), and a loose stem could land on a neighbour it was never meant for
    (`licen` on `licensetype`, which the live payload lists before `user_licen`)."""
    lower = {k.lower(): v for k, v in attrs.items()}
    for want in names:
        for key, val in lower.items():
            if want in key and not any(token in key for token in exclude):
                cleaned = _clean(str(val)) if val not in (None, "") else None
                if cleaned:
                    return cleaned
    return None


_ARCGIS_SERVICE_RE = re.compile(r"/(?:Feature|Map)Server(?:/\d+)?/?$", re.IGNORECASE)


# ArcGIS feature services cap a single response (the layer's maxRecordCount, often 1000-2000),
# so a layer with more rows MUST be paged with resultOffset or it silently truncates. The
# server's exceededTransferLimit flag is the ONLY reliable end-of-data signal: a layer whose
# maxRecordCount is BELOW our requested page size returns a short-but-not-final page with the
# flag set, so "short page" alone would silently truncate it (a 1000-row clamp served exactly
# that way). We page while the flag is set, advancing resultOffset by what actually arrived;
# the page cap is a runaway guard for a server that ignores resultOffset (it would otherwise
# re-serve page 0 forever).
_ARCGIS_PAGE_SIZE = 2000
_ARCGIS_PAGE_CAP = 25  # ≥25k features even under a 1000-row clamp — far above any state's roster


def _arcgis_float(attrs: dict, *names: str) -> float | None:
    """A numeric attribute picked like :func:`_arcgis_attr`; None when absent/non-numeric."""
    raw = _arcgis_attr(attrs, *names)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _arcgis_record(feat: dict) -> DispensaryRecord | None:
    """One ArcGIS feature → a DispensaryRecord, or None when it carries no usable name."""
    attrs = feat.get("attributes") or {}
    # Ohio's layer (maps.ohio.gov `Geocoded_Dispensaries_`) carries shapefile-truncated names —
    # `user_dispe`, `user_busin`, `user_stree`, `user_licen` — so the ten-character stems are
    # matched beside the words they truncate (2026-09-25; the roster read as 0 rows without them).
    name = _arcgis_attr(
        attrs, "dispensar", "dispe", "name", "dba", "business", "busin", "licensee", "facility", "store"
    )
    if not name:
        return None
    # Roster layers often carry lat/lng as plain attributes (Ontario's AGCO does, 100%
    # filled); a coordinate outside the valid range (or a 0,0 placeholder) is dropped.
    # Publishers misspell the field: DC's Open Data layer ships `LONGITDUE`. Match the misspelling
    # too — otherwise the roster loses its geo key, and `compare.py` can only fall back to the
    # address key, which a zip-less roster (DC's) cannot satisfy either. Correct spelling wins:
    # `_arcgis_attr` tries the names in order.
    latitude = _arcgis_float(attrs, "latitude")
    longitude = _arcgis_float(attrs, "longitude", "longitd")
    if latitude is None or longitude is None:
        # No coordinate ATTRIBUTE: the feature's own point geometry (requested in WGS84 by
        # `_query_arcgis_layer`) is the same fact — Ohio's layer names its columns `user_lat` /
        # `user_lon`, which no spelling above reaches, and its geometry is exact.
        geometry = feat.get("geometry") or {}
        if isinstance(geometry, dict) and isinstance(geometry.get("x"), (int, float)) and isinstance(geometry.get("y"), (int, float)):
            latitude, longitude = float(geometry["y"]), float(geometry["x"])
    if (
        latitude is None or longitude is None
        or not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180)
        or (latitude == 0 and longitude == 0)
    ):
        latitude = longitude = None
    return DispensaryRecord(
        source="arcgis", name=name,
        address=_arcgis_attr(attrs, "address", "addr", "street", "stree"),
        city=_arcgis_attr(attrs, "city", "town"),
        state=_arcgis_attr(attrs, "state", "province"),
        zip_code=_arcgis_attr(attrs, "zip", "postal"),
        phone=_arcgis_attr(attrs, "phone", "tel"),
        website=_arcgis_attr(attrs, "website", "web", "url"),
        latitude=latitude, longitude=longitude,
        # The regulator's own store id when the layer carries one (Ohio: `user_licen`,
        # `CCD000072-00`); `_arcgis_attr` stops at the first key, so `licensetype` must not win.
        licence_number=_arcgis_attr(
            attrs, "licencenumber", "licensenumber", "licence_number", "license_number", "licen", "permit",
            exclude=("type", "status", "class", "categ"),
        ),
    )


async def _query_arcgis_layer(layer_url: str, session) -> list[DispensaryRecord]:
    """Query one feature/map service layer and parse dispensary records, paging all rows.

    A service-root URL (…/FeatureServer) defaults to layer 0. A `where=` query
    string on the URL filters the layer (e.g. to retailer-only license types);
    otherwise all rows are returned. Pages with resultOffset so a layer larger than its
    maxRecordCount isn't silently truncated at the first page. Geometry is requested in WGS84 so
    a layer with no coordinate attributes still yields a geo key (`_arcgis_record`).
    """
    parsed = urlparse(layer_url)
    where = parse_qs(parsed.query).get("where", ["1=1"])[0] if parsed.query else "1=1"
    base = parsed._replace(query="").geturl().rstrip("/")
    if not re.search(r"/\d+$", base):
        base = f"{base}/0"
    records: list[DispensaryRecord] = []
    offset = 0
    for page in range(_ARCGIS_PAGE_CAP):
        try:
            q = (
                f"{base}/query?where={quote(where)}&outFields=*&f=json"
                f"&resultRecordCount={_ARCGIS_PAGE_SIZE}"
                f"&resultOffset={offset}&returnGeometry=true&outSR=4326"
            )
            payload = (await session.get(q, timeout=30)).json()
        except Exception as exc:
            if records:
                raise PartialRoster(
                    f"arcgis layer {base}: page at offset {offset} failed after {len(records)} "
                    f"row(s) ({type(exc).__name__})") from exc
            break
        if not isinstance(payload, dict):
            # A 200 carrying JSON `null` or a list is not a page; treat it as the page failing.
            if records:
                raise PartialRoster(
                    f"arcgis layer {base}: page at offset {offset} was not a JSON object after "
                    f"{len(records)} row(s)")
            break
        features = payload.get("features") or []
        if not features and payload.get("exceededTransferLimit") and records:
            raise PartialRoster(
                f"arcgis layer {base}: an empty page at offset {offset} while the server still flags "
                f"more, after {len(records)} row(s)")
        records.extend(rec for feat in features if (rec := _arcgis_record(feat)) is not None)
        # The server's flag — not our requested page size — says whether more rows remain: a
        # server clamped below _ARCGIS_PAGE_SIZE serves short-but-not-final pages. An empty page
        # with the flag still set is a broken server; stop rather than loop on offset 0 forever.
        if not features or not payload.get("exceededTransferLimit"):
            break
        offset += len(features)
        if page == _ARCGIS_PAGE_CAP - 1:
            # The server still says more rows remain and we will not ask for them: what we hold is
            # a fragment, and returning it would replace the full stored roster with it.
            raise PartialRoster(
                f"arcgis layer {base}: hit the {_ARCGIS_PAGE_CAP}-page cap with the server still "
                f"flagging more rows, after {len(records)} row(s)")
    return records


# ── Ontario AGCO cannabis-store map (list_type='on_agco') ────────────────────

# A 32-hex ArcGIS item id anywhere in the configured URL.
_ARCGIS_ITEM_ID_RE = re.compile(r"\b[0-9a-f]{32}\b")


async def _extract_on_agco(url: str, session) -> list[DispensaryRecord]:
    """Extract Ontario's roster from AGCO's ArcGIS Experience app.

    AGCO republishes its layers under date-stamped service names
    (``Authorized_to_open_20250620``), so a pinned FeatureServer URL rotates dead on
    every update; the stable entry point is the Experience-app item. Resolve the app's
    data sources at runtime and query the "Authorized To Open" layer — its siblings
    (Application_in_progress / Cancelled_Authorizations / Public_Notice) are other
    lifecycle stages, not the open-store roster.
    """
    match = _ARCGIS_ITEM_ID_RE.search(url)
    if not match:
        return []
    try:
        data = (
            await session.get(
                f"https://www.arcgis.com/sharing/rest/content/items/{match.group(0)}/data?f=json",
                timeout=30,
            )
        ).json()
    except Exception:
        return []
    for source in (data.get("dataSources") or {}).values():
        if not isinstance(source, dict):
            continue
        for child in (source.get("childDataSourceJsons") or {}).values():
            layer_url = child.get("url") or ""
            if "authorized_to_open" in layer_url.lower():
                return await _query_arcgis_layer(layer_url, session)
    return []


# ── Ontario AGCO open-data CSV (list_type='on_agco_csv') ─────────────────────
#
# The SAME stores the ArcGIS map serves, plus the columns the map omits. Measured 2026-09-16 against
# both sources live: filtering this file to `Authorized to Open` gives 1,897 rows against the map's
# 1,901, with only TWO addresses differing in each direction and identical field coverage (100% on
# name/address/city/postcode/coords, 69.8% website against 69.9%). It is refreshed daily —
# `Last-Modified` was that morning.
#
# WHAT IT ADDS, and why it is worth a handler:
#   * `LicenceNumber` on 100% of rows — the regulator's own per-store id, the same durable key
#     Alberta's `Authorization Number` gives. A name, a trade name and an address format all change;
#     `CRSA1161431` does not, and name+address is what every dedupe and compare path falls back on.
#   * `LicenceStatus` — which distinguishes the 58 stores on `Expired - Deemed to Continue` from the
#     1,839 `Active`. That population is invisible in the map entirely.
#   * `ApplicationType` — 127 `Transfer` rows, an ownership-change signal better than inferring one
#     from an operator rename.
#
# ⚠ **`Cancelled` IS NOT A CLOSURE, AND MUST NOT BE READ AS ONE.** The file carries 673 cancelled
# rows and they are cancelled APPLICATIONS: 244 of them sit at an address that is authorized today,
# so the store is open and it was an earlier application that died. Their only dates are the original
# public-notice window (often 2019), not a cancellation date. Reading them as closures would be wrong
# for a third of the set. This handler returns open stores only.
#
# ⚠ **IT DOES NOT CARRY AN OPERATOR.** No Ontario source does — all four AGCO map layers publish the
# same 14 fields and none names a licensee, and the 98 "One Plant" rows here hold 92 distinct licence
# numbers because an authorization is issued per premises. This does not collapse the duplicate
# company records, and that is settled rather than untried.
#
# The ArcGIS handler above is deliberately LEFT IN PLACE though nothing calls it: reverting is then
# one `list_type` in `states.yml` rather than a code change. A dead URL here is caught loudly —
# `coverage_healthcheck`'s `roster_missing` fires on a zero-row roster.
_AGCO_OPEN_STATUS = "Authorized to Open"


def _extract_on_agco_csv(text: str) -> list[DispensaryRecord]:
    """Ontario's roster from AGCO's open-data CSV, open stores only, carrying the licence number."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return []
    # The file is served with a UTF-8 BOM, so the first header arrives as '\ufeffLicenceNumber'.
    # Read it by position rather than by a name that depends on how the BOM was stripped.
    licence_key = next(iter(rows[0]))
    out: list[DispensaryRecord] = []
    # ⚠ THE EXPORT REPEATS ROWS, AND THE LICENCE NUMBER IS WHAT LETS US SEE IT. 37 licences appear
    # twice with every field identical but `RowNum` — one store, two lines. The ArcGIS map has the
    # same flaw and no key to detect it with: 1,901 rows for 1,860 distinct (name, address) pairs, 41
    # repeats we have been storing as separate dispensaries all along. Both sources agree on 1,860
    # real stores once deduplicated, so this is not a choice between them — it is the first thing the
    # regulator's id buys.
    seen: set[str] = set()
    for row in rows:
        if (row.get("ApplicationStatusEn") or "").strip() != _AGCO_OPEN_STATUS:
            continue
        name = (row.get("PremisesName") or "").strip()
        if not name:
            continue
        licence = (row.get(licence_key) or "").strip()
        if licence:
            if licence in seen:
                continue
            seen.add(licence)
        out.append(DispensaryRecord(
            source="on_agco_csv", name=name,
            address=(row.get("StreetAddress") or "").strip() or None,
            city=(row.get("City") or "").strip() or None,
            state="ON",
            zip_code=(row.get("PostalCode") or "").strip() or None,
            website=(row.get("WebsiteEn") or "").strip() or None,
            latitude=_float_or_none(row.get("Latitude")),
            longitude=_float_or_none(row.get("Longitude")),
            licence_number=licence or None,
        ))
    return out


def _float_or_none(value: str | None) -> float | None:
    """A coordinate cell as a float, or None when blank or unparseable."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# ── Alberta AGLC cannabis-licensee report (list_type='ab_aglc') ──────────────

# The report's stable header names (verified live 2026-07-04); parsed by name so a
# column reorder doesn't silently shift fields.
_AGLC_FIELDS = {
    "name": "Establishment Name",
    "city": "Site City Name",
    "address": "Site Address Line 1",
    "address2": "Site Address Line 2",
    "province": "Site Province Abbrev",
    "zip_code": "Site Postal Code",
    "phone": "Telephone Number",
    # AGLC's own per-store id. Numeric in the sheet, so `_xls_text` folds 803780.0 -> '803780'.
    "licence_number": "Authorization Number",
    # When the authorization first took effect — the regulator's record of the store OPENING.
    "licensed_since": "Initial Effective Date",
}


def _aglc_date(value: str | None) -> str | None:
    """AGLC's `Initial Effective Date` (`8/30/2021`) as an ISO date, or None.

    ⚠ IT IS M/D/YYYY — US ORDER, FROM A CANADIAN AGENCY, which is the trap. Settled by measuring
    rather than assuming: of 963 values, 562 have a SECOND component above 12 (`8/30/2021`,
    `10/31/2022`) and NOT ONE has a first component above 12. Read as D/M this would silently
    mis-date more than half the corpus, and every wrong date would still parse.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _xls_text(cell) -> str | None:
    """A cell's value as clean text. Numeric cells (phone numbers, license ids) come
    back as floats from xlrd — fold 2508842774.0 → '2508842774'."""
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        return str(int(cell.value))
    return _clean(str(cell.value))


# AGLC files an Alberta address with the UNIT number hyphenated in front of the street number
# ("416-222 BASELINE RD" = unit 416, #222). The operators' own sites publish the bare street number
# ("222 Baseline Rd"), and `normalize_address` JOINS digit-hyphen-digit (correct for Queens-style
# HOUSE numbers like "219-17"), so the roster keyed "416222 baseline" and matched nothing — the
# dominant reason Alberta's compare over-stated its roster lag. Strip the unit prefix here, where the
# row is known to be Alberta (no Queens ambiguity — that lives only in NY).
# A leading ``<unit>-`` prefix on a Canadian roster address: the unit hyphenated onto the street
# number ("416-222 BASELINE RD" = unit 416, #222; SK also uses LETTER units — "C-125", "J2-2095",
# "P-3322"). Bounded to 1-4 chars + a digit lookahead so it strips the unit, never a hyphenated
# street name ("Trans-Canada", where "Canada" isn't a digit) or a route.
_CA_UNIT_PREFIX_RE = re.compile(r"^\s*[0-9A-Za-z]{1,4}\s*-\s*(?=\d)")


def _strip_ca_unit_prefix(address: str | None) -> str | None:
    """Drop a leading ``<unit>-`` prefix from a Canadian roster address (AGLC/SLGA); unchanged if none.

    Canadian addresses lead with the unit ("416-222 Baseline Rd"); operators' own sites publish the
    bare street number, and ``normalize_address`` JOINS digit-hyphen-digit (correct for Queens house
    numbers like "219-17"), so the roster keyed "416222 baseline" and matched nothing. Safe in these
    extractors because the rows are Canadian (AB/SK) — the Queens ambiguity is US-only.
    """
    if not address:
        return address
    return _CA_UNIT_PREFIX_RE.sub("", address, count=1).strip() or address


def _aglc_record(values: dict[str, str | None]) -> DispensaryRecord | None:
    """One AGLC report row → a record, keeping only Alberta retail sites.

    The licensee report also lists out-of-province supplier/producer sites (ON/BC/QC…,
    152 of 955 rows on the 2026-07-04 pull) — those are not Alberta stores, and the
    persist path would otherwise stamp them state=AB.
    """
    if values.get("province") != "AB":
        return None
    name = values.get("name")
    if not name:
        return None
    address = _strip_ca_unit_prefix(values.get("address"))
    if values.get("address2"):
        address = f"{address}, {values['address2']}" if address else values["address2"]
    return DispensaryRecord(
        source="ab_aglc", name=name, address=address,
        city=values.get("city"), state="AB",
        zip_code=values.get("zip_code"), phone=values.get("phone"),
        licence_number=values.get("licence_number"),
        licensed_since=_aglc_date(values.get("licensed_since")),
    )


def _extract_ab_aglc(content: bytes) -> list[DispensaryRecord]:
    """Extract Alberta's roster from AGLC's licensee report (legacy OLE2 .xls).

    Direct download (no auth): aglc.ca/cannabis/cannabis-licensee-report/EXCEL.
    Header row 0, one licensee per row; no website or coordinate columns exist
    (menu handles and coords come from Stage 2 / the pool bootstrap).
    """
    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheets()[0]
    if sheet.nrows < 2:
        return []
    header = [_clean(str(sheet.cell_value(0, c))) for c in range(sheet.ncols)]
    columns = {field: header.index(col) for field, col in _AGLC_FIELDS.items() if col in header}
    if "name" not in columns:
        return []
    records = []
    for r in range(1, sheet.nrows):
        values = {field: _xls_text(sheet.cell(r, c)) for field, c in columns.items()}
        record = _aglc_record(values)
        if record is not None:
            records.append(record)
    return records


# ── Saskatchewan SLGA cannabis retailers (list_type='sk_slga') ───────────────

# The SLGA retailers spreadsheet's stable header names (verified live 2026-07-04).
_SLGA_FIELDS = {
    "name": "Operating Name",
    "city": "City",
    "address": "Street Address",
    "website": "Website",
    "status": "StatusDesc",
}
# The download link on the SLGA page is date-stamped (…-excel--june-30.xlsx) and rotates,
# so resolve the current retailers .xlsx href from the page rather than pinning it.
_SLGA_XLSX_RE = re.compile(r'href="([^"]*cannabis-retailers-excel[^"]*\.xlsx)"', re.IGNORECASE)


def _slga_record(values: dict[str, str | None]) -> DispensaryRecord | None:
    """One SLGA spreadsheet row → a record, keeping only active retailers."""
    if (values.get("status") or "").strip().lower() != "active":
        return None
    name = values.get("name")
    if not name:
        return None
    website = values.get("website")
    if website and website.strip().upper() in ("N/A", "NA", "-"):
        website = None
    return DispensaryRecord(
        source="sk_slga", name=name, address=_strip_ca_unit_prefix(values.get("address")),
        city=values.get("city"), state="SK", website=website,
    )


def _extract_slga_xlsx(content: bytes) -> list[DispensaryRecord]:
    """Parse the SLGA cannabis-retailers .xlsx (header row 0, one retailer per row)."""
    import io

    book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheet = book.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = [_clean(str(c)) if c is not None else None for c in next(rows, [])]
    columns = {field: header.index(col) for field, col in _SLGA_FIELDS.items() if col in header}
    if "name" not in columns:
        return []
    records = []
    for row in rows:
        values = {
            field: (_clean(str(row[i])) if i < len(row) and row[i] is not None else None)
            for field, i in columns.items()
        }
        record = _slga_record(values)
        if record is not None:
            records.append(record)
    return records


async def _extract_sk_slga(url: str, session) -> list[DispensaryRecord]:
    """Extract Saskatchewan's roster from SLGA's authorized-retailers page.

    Resolves the date-stamped retailers .xlsx link from the page at runtime (the URL
    rotates on each monthly update), downloads it, and parses the active retailers.
    """
    try:
        page = (await session.get(url, timeout=30)).text
    except Exception:
        return []
    match = _SLGA_XLSX_RE.search(page)
    if not match:
        return []
    xlsx_url = urljoin(url, match.group(1))
    try:
        resp = await session.get(xlsx_url, timeout=60)
    except Exception:
        return []
    if resp.status_code >= 400 or resp.content[:4] != b"PK\x03\x04":
        return []
    return _extract_slga_xlsx(resp.content)


# ── British Columbia LCRB establishments map (list_type='bc_lcrb') ────────────

def _checked_point(latitude: object, longitude: object) -> tuple[float | None, float | None]:
    """A source's raw coordinate pair as floats, or ``(None, None)`` when it is not a usable point:
    non-numeric, out of range, or the 0,0 placeholder. A numeric STRING is accepted and converted —
    stored raw, it is a text value bound for a float column."""
    def _num(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    lat, lon = _num(latitude), _num(longitude)
    if lat is None or lon is None:
        return None, None
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None, None
    return lat, lon


def _bc_lcrb_record(obj: dict) -> DispensaryRecord | None:
    """One LCRB establishment object → a record.

    The open JSON API behind justice.gov.bc.ca/lcrb/map (the app is open-source,
    bcgov/jag-lcrb-carla-public): name/addressStreet/addressCity/addressPostal/
    latitude/longitude/phone/isOpen. Licensed-but-not-yet-open rows (isOpen=false)
    are kept, mirroring Ontario's authorized-to-open roster.
    """
    name = _clean(obj.get("name"))
    if not name:
        return None
    latitude, longitude = _checked_point(obj.get("latitude"), obj.get("longitude"))
    return DispensaryRecord(
        source="bc_lcrb", name=name,
        address=_clean(obj.get("addressStreet")),
        city=_clean(obj.get("addressCity")),
        state="BC",
        zip_code=_clean(obj.get("addressPostal")),
        phone=_clean(obj.get("phone")),
        latitude=latitude, longitude=longitude,
    )


# Virginia's CCA dispensary page is a Squarespace site: no `<table>`, and the addresses are not in
# the visible DOM as text we can block-parse. Each dispensary is a map block whose JSON config is
# stashed in an HTML *attribute* (so it arrives entity-escaped, `&quot;addressLine1&quot;`). Unescape
# the document, then lift each `"location": {...}` object. The objects are flat (no nested braces),
# which is what makes the non-greedy `[^{}]*` safe here rather than needing a real parser.
_VA_LOCATION_RE = re.compile(r'"location":(\{[^{}]*\})')
# addressLine2 is always "City, VA, ZIP" on this page (verified: 23/23, 2026-07-09).
_VA_LINE2_RE = re.compile(r"^\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2}),?\s*(?P<zip>\d{5})")
# ⚠ BUT NOT ALWAYS TRUE (2026-10-23): six blocks carry the whole address on line 1 ("7635 Granby Street
# Norfolk, VA 23505") and line 2 left at the map block's default, "Richmond, VA, 23219" — so Bristol,
# Norfolk, Portsmouth, Suffolk and Glen Allen were filed in Richmond 23219 (their marker coordinates are
# right). When line 1 itself ends in "City, ST ZIP", that is the address and line 2 is not read. The town
# is the words after the street's last number or street-type word ("…Broad Street | Glen Allen",
# "…Building 30 | Richmond").
_VA_LINE1_TAIL_RE = re.compile(r"^(?P<head>.+?),\s*(?P<state>[A-Z]{2}),?\s*(?P<zip>\d{5})(?:-\d{4})?\s*$")
_VA_STREET_END_RE = re.compile(
    r"(?i)^(?:\S*\d\S*|st\.?|street|rd\.?|road|ave\.?|avenue|blvd\.?|boulevard|dr\.?|drive|ct\.?|court|"
    r"hwy\.?|highway|pkwy\.?|parkway|ln\.?|lane|way|pl\.?|place|cir\.?|circle|ter\.?|terrace|pike|turnpike)$")


def _va_line1_address(line1: str) -> tuple[str, str, str, str] | None:
    """``(street, city, state, zip)`` when line 1 carries the whole address, else None."""
    tail = _VA_LINE1_TAIL_RE.match(line1)
    if tail is None:
        return None
    words = tail["head"].split()
    cut = max((i for i, word in enumerate(words) if _VA_STREET_END_RE.match(word)), default=-1)
    if cut < 0 or cut == len(words) - 1:
        return None
    return " ".join(words[:cut + 1]), " ".join(words[cut + 1:]), tail["state"], tail["zip"]


def _va_cca_record(loc: dict) -> DispensaryRecord | None:
    """One CCA map-block `location` object → a DispensaryRecord, or None if it isn't a dispensary."""
    name = _clean(str(loc.get("addressTitle") or ""))
    street = _clean(str(loc.get("addressLine1") or ""))
    if not name or not street:
        return None                      # a non-dispensary map block (the page carries one)
    city = state = zip_code = None
    if whole := _va_line1_address(street):
        street, city, state, zip_code = whole
    elif match := _VA_LINE2_RE.match(str(loc.get("addressLine2") or "")):
        city, state, zip_code = (match["city"].strip(), match["state"], match["zip"])
    latitude, longitude = loc.get("markerLat"), loc.get("markerLng")
    if not (isinstance(latitude, (int, float)) and isinstance(longitude, (int, float))
            and -90 <= latitude <= 90 and -180 <= longitude <= 180
            and not (latitude == 0 and longitude == 0)):
        latitude = longitude = None
    return DispensaryRecord(
        source="va_cca", name=name, address=street, city=city, state=state,
        zip_code=zip_code, phone=None, website=None,
        latitude=latitude, longitude=longitude,
    )


# ── Atlist (my.atlist.com embedded maps) ─────────────────────────────────────
# New Jersey's CRC "Find a Dispensary" page renders an Atlist map in an <iframe>. The page's own
# HTML holds ONE table, and it lists DELIVERY SERVICES — parsing it yielded 3 rows named
# "Passing Puff Delivery", "TerpTaxi" and "Weedies" while we held 186 NJ company stores. (The
# neighbouring `/dispensaries/roll-up/` page is a product-RECALL table, not a roster. Both are
# traps for a table-hunting parser.)
#
# The real roster is the map's markers. Atlist's SPA reads `/v1/map/{id}/markers` and, for a
# publicly-shared map, sends the literal bearer token `public` — its bundle falls back to the
# string "public" when no user token exists. That is the anonymous read path the embed itself uses.
_ATLIST_HOST = "https://api.atlist.com"
_ATLIST_MAP_ID_RE = re.compile(r"/map/([0-9a-fA-F-]{36})")
# "460 Maple Ave, Elizabeth, NJ 07202, USA" — Google-formatted, so the shape is stable.
_ATLIST_ADDRESS_RE = re.compile(
    r"^(?P<street>.+?),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?"
    r"(?:,\s*[A-Za-z .]+)?$"
)
_ATLIST_MAX_PAGES = 40   # 250 markers/page; a runaway-cursor backstop, not an expected limit


def _atlist_record(marker: dict) -> DispensaryRecord | None:
    """One Atlist marker → a roster record. Coordinates are kept even when the address won't parse.

    A marker whose `formattedAddress` is a road rather than a street number ("NJ-66, Neptune
    Township, NJ, USA") still carries lat/long, and `compare`'s proximity tier pairs on those alone.
    Dropping it because the text is unparseable would lose a real licensee.
    """
    name = _clean(marker.get("name"))
    if not name:
        return None
    latitude, longitude = _coord(marker.get("lat")), _coord(marker.get("long"))
    parsed = _ATLIST_ADDRESS_RE.match((marker.get("formattedAddress") or "").strip())
    if parsed is None:
        return DispensaryRecord(source="atlist", name=name, website=_clean(marker.get("buttonLink")),
                                latitude=latitude, longitude=longitude)
    return DispensaryRecord(
        source="atlist", name=name, address=_clean(parsed["street"]), city=_clean(parsed["city"]),
        state=parsed["state"], zip_code=parsed["zip"], website=_clean(marker.get("buttonLink")),
        latitude=latitude, longitude=longitude,
    )


def _coord(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _atlist_stop(records: list[DispensaryRecord], why: str) -> list[DispensaryRecord]:
    """[] when nothing was gathered (the request's own failure); a fragment is never returned."""
    if records:
        raise PartialRoster(f"atlist map: a later page failed after {len(records)} marker(s) ({why})")
    return []


async def _extract_atlist(url: str, session) -> list[DispensaryRecord]:
    """Every marker on a publicly-shared Atlist map, paging through its opaque `nextToken`."""
    found = _ATLIST_MAP_ID_RE.search(url)
    if not found:
        return []
    endpoint = f"{_ATLIST_HOST}/v1/map/{found.group(1)}/markers"
    headers = {"Authorization": "Bearer public"}
    records: list[DispensaryRecord] = []
    token: str | None = None
    for _page in range(_ATLIST_MAX_PAGES):
        params = {"nextToken": token} if token else None
        try:
            response = await session.get(endpoint, headers=headers, params=params, timeout=60)
        except Exception as exc:
            return _atlist_stop(records, f"{type(exc).__name__}")
        if response.status_code >= 400:
            return _atlist_stop(records, f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            return _atlist_stop(records, "unparseable payload")
        if not isinstance(payload, dict):
            return _atlist_stop(records, "payload is not an object")
        for marker in payload.get("markers") or []:
            if isinstance(marker, dict):
                record = _atlist_record(marker)
                if record is not None:
                    records.append(record)
        token = payload.get("nextToken")
        if not token:
            break
    return records


async def _extract_va_cca(url: str, session) -> list[DispensaryRecord]:
    """Virginia CCA's medical-cannabis dispensary locations (Squarespace map blocks)."""
    try:
        response = await session.get(url, timeout=60, allow_redirects=True)
    except Exception:
        return []
    if response.status_code >= 400:
        return []
    document = html.unescape(response.text or "")
    records: list[DispensaryRecord] = []
    seen: set[tuple[str, str]] = set()
    for match in _VA_LOCATION_RE.finditer(document):
        try:
            location = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(location, dict):
            continue
        record = _va_cca_record(location)
        if record is None:
            continue
        identity = ((record.name or "").lower(), (record.address or "").lower())
        if identity in seen:                 # the same block can render twice (desktop + mobile)
            continue
        seen.add(identity)
        records.append(record)
    return records


_NH_TAB_RE = re.compile(r'<a[^>]*href="#tab-[^"]*-(?P<index>\d+)"[^>]*>(?P<label>[^<]+)</a>')
_NH_ITEM_RE = re.compile(r"<li[^>]*>(?P<body>.*?)</li>", re.S)
_NH_WEBSITE_RE = re.compile(r'Website:\s*<a[^>]*href="(?P<url>[^"]+)"')
_NH_SECTION_RE = re.compile(r"<h3[^>]*>\s*ATC Locations\s*</h3>")
_NH_PANEL_RE = re.compile(r"<div[^>]*>\s*<p[^>]*>Dispensaries located", re.S)


def _nh_dhhs_records(document: str) -> list[DispensaryRecord]:
    """New Hampshire DHHS's Alternative Treatment Centers page → one record per dispensary.

    The page is a tabbed component: a list of tab links names the operators in order, and one
    panel per operator follows in the same order, each holding its dispensaries as list items
    ("street<br>City, NH zip<br>phone") and a "Website:" link. The operator name is the TAB
    label, which no address line carries — the generic line-block reader found three of the
    seven stores and named each after its panel's lead-in sentence (2026-10-11). Pure.
    """
    document = html.unescape(document)
    heading = _NH_SECTION_RE.search(document)
    if heading is None:
        return []
    section = document[heading.start():]
    labels = [clean_label for _index, label in _NH_TAB_RE.findall(section)
              if (clean_label := _clean(label))]
    if not labels:
        return []
    # Panels start after the tab list; each is the run of markup up to the next panel's lead-in.
    panels = _NH_PANEL_RE.split(section.split("</ul>", 1)[1])[1:]
    records: list[DispensaryRecord] = []
    for label, panel in zip(labels, panels, strict=False):
        website_match = _NH_WEBSITE_RE.search(panel)
        website = website_match.group("url") if website_match else None
        for item in _NH_ITEM_RE.finditer(panel):
            lines = [cleaned for raw in re.split(r"<br\s*/?>", item.group("body"))
                     if (cleaned := _clean(re.sub(r"<[^>]+>", " ", raw)))]
            if len(lines) < 2:
                continue
            city_line = CITY_STATE_ZIP_RE.match(lines[1])
            if city_line is None or not _STREET_RE.match(lines[0]):
                continue
            phone = next((line for line in lines[2:] if _PHONE_RE.search(line)), None)
            records.append(DispensaryRecord(
                source="nh_dhhs", name=label, address=lines[0], city=city_line.group(1).strip(),
                state=city_line.group(2), zip_code=city_line.group(3), phone=phone, website=website,
            ))
    return records


async def _extract_nh_dhhs(url: str, session) -> list[DispensaryRecord]:
    """New Hampshire DHHS's ATC page (tabbed operator panels; `_nh_dhhs_records`).

    A fetch that fails or is refused RAISES, so `extract_records` records the attempt as `failed`
    with the reason. The first host run (2026-10-11) read 0 rows and printed method `none`, because
    this handler returned [] on whatever the site answered — indistinguishable from a page that
    lists nothing, and the prior rows were kept with no word of why.
    """
    response = await session.get(url, timeout=60, allow_redirects=True)
    if response.status_code >= 400:
        raise ExtractionFailed(f"HTTP {response.status_code} from {response.url}")
    records = _nh_dhhs_records(response.text or "")
    if not records:
        raise ExtractionFailed(
            f"HTTP {response.status_code}, {len(response.text or ''):,} bytes, landed on {response.url}: "
            "no ATC Locations section — a challenge page, a redirect, or the page changed shape")
    return records


# ── The four small official rosters that replaced Dutchie bootstraps (2026-10-19) ──────────────────
# West Virginia, North Dakota, South Dakota and Hawaii each publish their list on one public page; all
# four had run on a Dutchie bootstrap since June, whose refresh that day was refused in WV/ND/SD/HI for
# sweeping fewer stores than the roster held. Each parser is pure (HTML in, records out) and each
# handler RAISES when the page no longer has the shape it reads — an empty roster is never a result.

_DRUPAL_SETTINGS_RE = re.compile(
    r'<script type="application/json" data-drupal-selector="drupal-settings-json">(.*?)</script>', re.S)


def _wv_omc_records(page: str) -> list[DispensaryRecord]:
    """WV Office of Medical Cannabis "Find a Dispensary": a Leaflet map whose markers sit in the page's
    `drupal-settings-json` — coordinates plus a popup with trade name, address, phone and status. Only
    an "Operational…" dispensary is kept. Pure."""
    match = _DRUPAL_SETTINGS_RE.search(page)
    settings = json.loads(match.group(1)) if match else {}
    records: list[DispensaryRecord] = []
    for layer in (settings.get("leaflet") or {}).values():
        for feature in (layer.get("features") or []) if isinstance(layer, dict) else []:
            popup = feature.get("popup")
            popup = popup.get("value") if isinstance(popup, dict) else popup
            tree = HTMLParser(popup if isinstance(popup, str) else "")
            status = _first_text(tree, ".views-field-views-conditional-field .field-content") or ""
            if not status.startswith("Operational"):
                continue
            phone = tree.css_first('a[href^="tel:"]')
            records.append(DispensaryRecord(
                # Three of 65 markers carried no trade name on the first run (2026-10-19), and a nameless
                # row is dropped by the roster write; the popup still names the licensee.
                source="wv_omc", name=(_first_text(tree, ".views-field-field-trade-name .field-content")
                                       or _first_text(tree, ".views-field-nothing a")),
                address=_first_text(tree, ".address-line1"), city=_first_text(tree, ".locality"), state="WV",
                zip_code=_first_text(tree, ".postal-code"), phone=phone.text(strip=True) if phone is not None else None,
                latitude=_to_float_or_none(feature.get("lat")), longitude=_to_float_or_none(feature.get("lon"))))
    return records


def _first_text(tree: HTMLParser, selector: str) -> str | None:
    """The stripped text of the first node matching ``selector``, or None. Pure."""
    node = tree.css_first(selector)
    text = node.text(strip=True) if node is not None else ""
    return text or None


def _to_float_or_none(value: object) -> float | None:
    """A float from a JSON number or numeric string, or None. Pure."""
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _nd_hhs_records(page: str) -> list[DispensaryRecord]:
    """ND HHS "Dispensary Locations": blocks of `<h2>` city, `<h3>` dispensary, then `<p>` street, phone
    and a link to its site, then hours. Walked in document order so a city with two stores is two rows.
    Pure."""
    records: list[DispensaryRecord] = []
    for block in HTMLParser(page).css("div.field--name-field-wysiwyg-text"):
        city: str | None = None
        current: dict[str, str | None] | None = None
        for node in block.iter():
            text = node.text(strip=True)
            if node.tag == "h2":
                city = text or city
            elif node.tag == "h3" and text:
                if current:
                    records.append(_nd_record(current))
                current = {"name": text, "city": city, "address": None, "phone": None, "website": None}
            elif node.tag == "h4":
                if current:
                    records.append(_nd_record(current))
                current = None                      # "Store Hours": the rest of the block is hours
            elif node.tag == "p" and current is not None and text:
                link = node.css_first("a[href]")
                href = (link.attributes.get("href") or "") if link is not None else ""
                if href.startswith("http"):
                    current["website"] = href
                elif _PHONE_RE.fullmatch(text):
                    current["phone"] = text
                elif current["address"] is None:
                    current["address"] = text
        if current:
            records.append(_nd_record(current))
    return records


def _nd_record(fields: dict[str, str | None]) -> DispensaryRecord:
    return DispensaryRecord(source="nd_hhs", name=fields["name"], address=fields["address"],
                            city=fields["city"], state="ND", phone=fields["phone"], website=fields["website"])


def _sd_doh_records(page: str) -> list[DispensaryRecord]:
    """SD DOH "Medical Cannabis Establishments List": the table under the "Dispensary Establishments"
    heading — Legal Name, Doing Business As, Location (a town; the list publishes no street). The
    cultivation, manufacturing and testing tables beside it are not storefronts. Pure."""
    for table in HTMLParser(page).css("table"):
        heading = table.prev
        while heading is not None and not (heading.tag in ("h2", "h3") and heading.text(strip=True)):
            heading = heading.prev
        if heading is None or "dispensary" not in heading.text(strip=True).lower():
            continue
        records = []
        for row in table.css("tr")[1:]:
            cells = [cell.text(strip=True) for cell in row.css("td")]
            if len(cells) >= 3 and (cells[1] or cells[0]):
                records.append(DispensaryRecord(source="sd_doh", name=cells[1] or cells[0], city=cells[2] or None,
                                                state="SD"))
        return records
    return []


def _hi_doh_records(page: str) -> list[DispensaryRecord]:
    """HI DOH "Dispensary Websites and Locations": the licensed OPERATORS by county, each linked to its
    own site. Hawaii licenses operators, not storefronts, and the page lists no store address — so a row
    is a licensee with its website, and its stores come from that site (Stage 2). Pure."""
    records: list[DispensaryRecord] = []
    for cell in HTMLParser(page).css("table td"):
        for link in cell.css("a[href]"):
            name, href = link.text(strip=True), link.attributes.get("href") or ""
            if name and href.startswith("http"):
                records.append(DispensaryRecord(source="hi_doh", name=name, state="HI", website=href))
    return records


_TX_LICENCE = re.compile(r"License\s*#\s*(\d+)", re.I)


def _tx_dps_records(page: str) -> list[DispensaryRecord]:
    """TX DPS "Licensed Dispensaries": one paragraph per licensed dispensing organization — the name in
    its first bold run, "License #0004", an e-mail and a website. Texas licenses organizations and
    publishes no address. The website is read from the link TEXT: DPS's hrefs are mail-security
    rewrites (urldefense, Outlook safelinks) that wrap the real address. Pure."""
    records: list[DispensaryRecord] = []
    for para in HTMLParser(page).css("p"):
        licence = _TX_LICENCE.search(para.text())
        bold = para.css_first("strong")
        name = bold.text(strip=True) if bold is not None else ""
        if not licence or not name or _TX_LICENCE.search(name):
            continue
        sites = [a.text(strip=True) for a in para.css("a[href]")
                 if a.text(strip=True).startswith(("http://", "https://"))]
        records.append(DispensaryRecord(source="tx_dps", name=name, state="TX",
                                        website=sites[0] if sites else None,
                                        licence_number=licence.group(1)))
    return records


def _static_page_handler(parse, label: str):
    """An async handler for a roster that is one public page: fetch, parse, and RAISE on a refused fetch
    or a page that yields nothing (`_extract_nh_dhhs`'s rule — an empty answer is never a roster)."""
    async def handler(url: str, session) -> list[DispensaryRecord]:
        response = await session.get(url, timeout=60, allow_redirects=True)
        if response.status_code >= 400:
            raise ExtractionFailed(f"{label}: HTTP {response.status_code} from {response.url}")
        records = parse(response.text or "")
        if not records:
            raise ExtractionFailed(f"{label}: HTTP {response.status_code}, {len(response.text or ''):,} bytes "
                                   f"from {response.url} — no roster in the shape this handler reads")
        return records
    handler.__name__ = f"_extract_{label}"
    return handler


_extract_wv_omc = _static_page_handler(_wv_omc_records, "wv_omc")
_extract_nd_hhs = _static_page_handler(_nd_hhs_records, "nd_hhs")
_extract_sd_doh = _static_page_handler(_sd_doh_records, "sd_doh")
_extract_hi_doh = _static_page_handler(_hi_doh_records, "hi_doh")
_extract_tx_dps = _static_page_handler(_tx_dps_records, "tx_dps")


# ── Oklahoma OMMA Verify (list_type='ok_omma') ─────────────────────────────────

#: OMMA Verify's guest Apex method, and the cap the site's SOQL OFFSET puts on one search's rows.
_OMMA_CLASS, _OMMA_METHOD = "OMMA_VerifyLicenseNumberController", "getBusinessLicenses"
_OMMA_PAGE = 2000
#: Licence-number prefixes a dispensary carries. `DAAA-` is every one but one: on 2026-10-19 a single
#: dispensary held a grower-style `GAAA-` number, so those partitions are searched too, filtered by type.
_OMMA_PREFIXES = ("DAAA-", "GAAA-")
_OMMA_PARTITIONS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def omma_address(physical: str | None) -> tuple[str | None, str | None, str | None]:
    """``(street, city, zip)`` from OMMA's ``physicalAddress`` — ``street[, suite], city, county or
    "Oklahoma", zip``. Pure. The county (or the state's name, which some rows carry instead) is dropped."""
    parts = [part.strip() for part in (physical or "").split(",") if part.strip()]
    if len(parts) < 3:
        return (", ".join(parts) or None), None, None
    zip_code = parts[-1] if re.fullmatch(r"\d{5}(-\d{4})?", parts[-1]) else None
    rest = parts[:-1] if zip_code else parts
    rest = rest[:-1] if len(rest) >= 3 else rest            # the county, or "Oklahoma"
    return (", ".join(rest[:-1]) or None), rest[-1], (zip_code[:5] if zip_code else None)


def omma_record(obj: dict) -> DispensaryRecord | None:
    """One OMMA Verify business record as a roster row, or None when it is not an ACTIVE dispensary.
    Pure. "Active - Renewal Pending" is active: the licence holds while the renewal is reviewed."""
    if obj.get("licenseType") != "Dispensary" or not str(obj.get("licenseStatus") or "").startswith("Active"):
        return None
    street, city, zip_code = omma_address(obj.get("physicalAddress"))
    # Names arrive HTML-escaped ("All Love Cannabis &amp; Wellness"), and the fold cut one at the `;`.
    name = html.unescape(str(obj.get("tradeName") or obj.get("legalEntityName") or "")) or None
    return DispensaryRecord(
        source="ok_omma", name=name,
        address=street, city=city, state="OK", zip_code=zip_code,
        phone=(obj.get("telephoneNumber") or None), licence_number=(obj.get("licenseNumber") or None),
        # The licensee's own choice, which the owner ruled the public map honours (2026-10-19).
        address_withheld=obj.get("addressDisclosure") is False,
    )


async def _extract_ok_omma(url: str, session) -> list[DispensaryRecord]:
    """Oklahoma's dispensaries from OMMA Verify, the regulator's live licence lookup.

    OMMA's roster moved into its Salesforce MedPortal in 2026, and `find-lists` saw a JavaScript shell;
    the page's own guest Apex call (`salesforce_aura`) returns every business licence with its type,
    status, trade name, phone and address. One search returns at most `_OMMA_PAGE` rows (SOQL's OFFSET
    cap — the next page answers empty), so it runs once per licence-number prefix, 72 searches of a
    few dozen to a few hundred rows each. A partition AT the cap may have been cut short:
    that raises `PartialRoster` rather than return a roster missing rows. Measured 2026-10-19: 4,850 of
    the export's 4,851 dispensary licences under `DAAA-`, 1,241 of them active.
    """
    parsed = urlparse(url)
    ctx = await salesforce_aura.page_context(session, f"{parsed.scheme}://{parsed.netloc}", parsed.path)
    records: dict[str, DispensaryRecord] = {}
    for prefix in _OMMA_PREFIXES:
        for part in _OMMA_PARTITIONS:
            value = await salesforce_aura.call_apex(
                session, ctx, _OMMA_CLASS, _OMMA_METHOD,
                {"searchString": prefix + part, "offsetSize": 0, "pageSize": _OMMA_PAGE})
            inner = value.get("returnValue") if isinstance(value, dict) else None
            rows = inner.get("records") if isinstance(inner, dict) else None
            if not isinstance(rows, list):
                raise ExtractionFailed(f"OMMA {prefix}{part}: no record list in {str(value)[:200]}")
            if len(rows) >= _OMMA_PAGE:
                raise PartialRoster(f"OMMA {prefix}{part}: {len(rows)} rows, at the {_OMMA_PAGE}-row cap — "
                                    "the search may be cut short; partition it further")
            for row in rows:
                if isinstance(row, dict) and (record := omma_record(row)) and record.licence_number:
                    records[record.licence_number] = record
            await asyncio.sleep(0.5)    # 72 calls; a guest endpoint is not a bulk API
    return list(records.values())


# ── Michigan CRA, Accela Citizen Access (list_type='mi_cra') ───────────────────

#: The record types that are a store a customer can buy from, each with the record-number prefix its
#: numbers carry, searched in this order. Adult-use first: a rooftop licensed under both programs is
#: ONE roster row and the adult-use licence names it (the `co_med` rule). Designated Consumption
#: Establishments are left out — a lounge, not a store. ``""`` searches the whole type, which is
#: right while it lists fewer records than the portal's cap; a capped search is split anyway.
_MI_CRA_TYPES: tuple[tuple[str, str, str], ...] = (
    ("Adult_Use", "Adult_Use/Marihuana Retailer/License/NA", "AU-R-"),
    ("Adult_Use", "Adult_Use/Marihuana Microbusiness/License/NA", ""),
    ("Adult_Use", "Adult_Use/Class A Microbusiness/License/NA", ""),
    ("Adult_Use", "Adult_Use/Tribal Retailer/Tribal /NA", ""),
    ("Licenses", "Licenses/Provisioning Center/License/NA", "PC-"),
)
#: How deep a capped search is split, one digit at a time, before it is reported as a fragment.
#: Record numbers are six digits; four of them already put every partition under the 100-row cap.
_MI_CRA_MAX_PREFIX_DIGITS = 6
_MI_CRA_ADDRESS_RE = re.compile(r"^(?P<street>.+?),\s*(?P<city>[^,]+?)\s+MI\s+(?P<zip>\d{5})(?:-\d{4})?$", re.I)


def mi_cra_record(row: accela_aca.AcaRow) -> DispensaryRecord | None:
    """One Accela search row as a Michigan roster row, or None when the licence is not ACTIVE. Pure.
    "Active - Late Renewal" is active; "License Void", "Closed", "Suspended" and "Closed - Suspended"
    are not. The grid's address is ``street, City MI zip``; one that does not parse is kept whole as
    the street rather than guessed at."""
    if not (row.status or "").startswith("Active"):
        return None
    match = _MI_CRA_ADDRESS_RE.match(row.address or "")
    street, city, zip_code = ((match["street"], match["city"], match["zip"]) if match
                              else (row.address, None, None))
    return DispensaryRecord(source="mi_cra", name=row.name, address=street, city=city, state="MI",
                            zip_code=zip_code, licence_number=row.number)


async def _mi_cra_walk(session, page: accela_aca.AcaPage, record_type: str, prefix: str,
                       root_digits: int) -> list[accela_aca.AcaRow]:
    """Every row of one record type under ``prefix``, splitting a capped search by the next digit."""
    try:
        rows = await accela_aca.search_all(session, page, record_type, number_prefix=prefix)
    except accela_aca.SearchCapped as capped:
        if len(prefix) - root_digits >= _MI_CRA_MAX_PREFIX_DIGITS:
            raise PartialRoster(f"MI CRA: {capped}, even at the longest prefix") from capped
        rows = []
        for digit in "0123456789":
            rows.extend(await _mi_cra_walk(session, page, record_type, prefix + digit, root_digits))
    await asyncio.sleep(0.3)    # ~220 postbacks a run; a public portal is not a bulk API
    return rows


async def _extract_mi_cra(url: str, session) -> list[DispensaryRecord]:
    """Michigan's dispensaries from the Cannabis Regulatory Agency's Accela Citizen Access portal.

    The CRA's own lists are a dead ArcGIS app and JavaScript-only reports, so Michigan was a Dutchie
    bootstrap (591 stores on 2026-10-19). Its licence search is not JavaScript-only: it is an ASP.NET
    form whose postback answers a server-rendered grid (`accela_aca`). One search lists at most 100
    records (200 in the medical module), so the store types are searched by record-number prefix, one
    more digit wherever a search is capped. ``url`` is the Adult-Use module's search page; the medical
    module is the same page under ``module=Licenses``. Measured 2026-10-20: 1,973 store-type licences,
    941 of them active, 859 rooftops, in about two minutes.
    """
    pages: dict[str, accela_aca.AcaPage] = {}
    by_rooftop: dict[str, DispensaryRecord] = {}
    for module, record_type, prefix in _MI_CRA_TYPES:
        if module not in pages:
            module_url = re.sub(r"module=[^&]*", f"module={module}", url)
            module_url = re.sub(r"TabName=[^&]*", f"TabName={module}", module_url)
            pages[module] = await accela_aca.open_search(session, module_url)
        for row in await _mi_cra_walk(session, pages[module], record_type, prefix, len(prefix)):
            record = mi_cra_record(row)
            if record is None:
                continue
            rooftop = location_key(None, None, record.address, record.zip_code) or f"licence:{row.number}"
            by_rooftop.setdefault(rooftop, record)  # one store, two licences: the first type names it
    return list(by_rooftop.values())


async def _extract_bc_lcrb(url: str, session) -> list[DispensaryRecord]:
    """Fetch BC's roster from the LCRB map API (a bare JSON list, no paging/auth)."""
    try:
        payload = (await session.get(url, timeout=60)).json()
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    return [rec for obj in payload if isinstance(obj, dict)
            if (rec := _bc_lcrb_record(obj)) is not None]


async def _extract_arcgis(url: str, session) -> list[DispensaryRecord]:
    """Extract from an ArcGIS source.

    Accepts either a direct feature/map service URL (queried straight away) or a
    webappviewer/Experience app URL (with an ?id= item), which is resolved to its
    operational layers — directly or via a nested web map.
    """
    # A direct feature/map service URL — query it straight away.
    if _ARCGIS_SERVICE_RE.search(urlparse(url).path):
        return await _query_arcgis_layer(url, session)

    item_id = None
    for part in urlparse(url).query.split("&"):
        if part.startswith("id="):
            item_id = part[3:]
            break
    if not item_id:
        return []
    sharing = url.split("/apps/", 1)[0] + "/sharing/rest"

    async def _item_data(iid: str) -> dict:
        try:
            return (
                await session.get(
                    f"{sharing}/content/items/{iid}/data?f=json", timeout=30
                )
            ).json()
        except Exception:
            return {}

    def _layers(d: dict) -> list[str]:
        return [
            layer["url"]
            for layer in (d.get("operationalLayers") or [])
            if isinstance(layer, dict) and layer.get("url")
        ]

    # The viewer's id is the *app* item. If its data already lists operational
    # layers, use them; a Web AppBuilder app instead nests the web map id under
    # map.itemId, whose data carries the operational layers.
    data = await _item_data(item_id)
    layer_urls = _layers(data)
    if not layer_urls:
        webmap_id = (
            (data.get("map") or {}).get("itemId")
            or (data.get("values") or {}).get("webmap")
        )
        if webmap_id:
            layer_urls = _layers(await _item_data(webmap_id))

    for layer_url in layer_urls:
        records = await _query_arcgis_layer(layer_url, session)
        if records:
            return records
    return []


# ── California DCC retailer API (list_type='ca_dcc') ─────────────────────────

# California bounding box (minLat, maxLat, minLng, maxLng).
_CA_DCC_BBOX = (32.3, 42.1, -124.6, -114.0)
_CA_DCC_PAGE = 1000  # the API caps a response at this many rows and ignores ?page


def _ca_dcc_record(lic: dict) -> DispensaryRecord | None:
    """Map one DCC license object to a record, keeping only active retailers."""
    if lic.get("licenseStatus") != "Active":
        return None
    if "retailer" not in (lic.get("licenseType") or "").lower():
        return None
    name = _clean(lic.get("businessDbaName") or lic.get("businessLegalName"))
    if not name:
        return None
    latitude, longitude = _checked_point(lic.get("premiseLatitude"), lic.get("premiseLongitude"))
    return DispensaryRecord(
        source="ca_dcc", name=name,
        address=_clean(lic.get("premiseStreetAddress")),
        city=_clean(lic.get("premiseCity")),
        state=_clean(lic.get("premiseState")),
        zip_code=_clean(lic.get("premiseZipCode")),
        phone=_clean(lic.get("businessPhone")),
        latitude=latitude,
        longitude=longitude,
    )


async def _extract_ca_dcc(url: str, session) -> list[DispensaryRecord]:
    """Sweep California's DCC RetailerLocationSearch API for active retailers.

    The endpoint returns {metadata:{totalCount,…}, data:[license, …]} for a lat/long
    box but caps a response at 1000 rows and ignores ?page. So we recursively split
    any box whose totalCount exceeds one page into quadrants until every box fits,
    then dedupe distinct premises.

    The FIRST box failing is the request's own failure and yields []. A SUB-box failing is a
    hole in the sweep — the other quadrants still return rows — so it raises
    :class:`PartialRoster` rather than letting the fragment replace the stored roster.
    """
    records: list[DispensaryRecord] = []
    seen: set[tuple[str | None, str | None]] = set()

    async def sweep(box: tuple[float, float, float, float], depth: int) -> None:
        min_lat, max_lat, min_lng, max_lng = box
        q = (
            f"{url}?minLatitude={min_lat}&maxLatitude={max_lat}"
            f"&minLongitude={min_lng}&maxLongitude={max_lng}&pageSize={_CA_DCC_PAGE}"
        )
        try:
            payload = (await session.get(q, timeout=60)).json()
        except Exception as exc:
            if depth:
                raise PartialRoster(
                    f"ca_dcc: a sub-box at depth {depth} failed ({type(exc).__name__}) — the sweep "
                    "would keep a fragment") from exc
            return
        if not isinstance(payload, dict):
            if depth:
                raise PartialRoster(f"ca_dcc: a sub-box at depth {depth} answered a non-object body")
            return
        data = payload.get("data") or []
        total = (payload.get("metadata") or {}).get("totalCount", len(data))
        if total > len(data) and depth >= 6:
            raise PartialRoster(
                f"ca_dcc: a box at subdivision depth {depth} still holds {total} > {len(data)} "
                "licences — the page cap would keep a fragment")
        if total > len(data) and depth < 6:  # capped — subdivide into quadrants
            mid_lat, mid_lng = (min_lat + max_lat) / 2, (min_lng + max_lng) / 2
            await sweep((min_lat, mid_lat, min_lng, mid_lng), depth + 1)
            await sweep((min_lat, mid_lat, mid_lng, max_lng), depth + 1)
            await sweep((mid_lat, max_lat, min_lng, mid_lng), depth + 1)
            await sweep((mid_lat, max_lat, mid_lng, max_lng), depth + 1)
            return
        for lic in data:
            record = _ca_dcc_record(lic)
            if record is None:
                continue
            key = (record.name, record.address)
            if key not in seen:
                seen.add(key)
                records.append(record)

    await sweep(_CA_DCC_BBOX, 0)
    return records


# ── Dispatcher ───────────────────────────────────────────────────────────────

# The list_type vocabulary this dispatcher understands. `state_lists._classify` (the
# producer) must stay a subset of this — guarded by test_list_type_vocabulary_consistent
# so the two never drift. Any other value falls through to the html parser.
ListType = Literal[
    "pdf", "csv", "kml", "arcgis", "atlist", "ca_dcc", "az_dhs", "co_med", "ma_ccc",
    "on_agco", "on_agco_csv", "ab_aglc", "bc_lcrb", "sk_slga", "va_cca", "il_idfpr", "nh_dhhs", "ok_omma",
    "mi_cra", "wv_omc", "nd_hhs", "sd_doh", "hi_doh", "tx_dps", "lookup",
    "html",
]
HANDLED_LIST_TYPES: frozenset[str] = frozenset(get_args(ListType))


_ASYNC_HANDLERS = {
    "arcgis": _extract_arcgis,
    "ca_dcc": _extract_ca_dcc,
    "on_agco": _extract_on_agco,
    "bc_lcrb": _extract_bc_lcrb,
    "va_cca": _extract_va_cca,
    "nh_dhhs": _extract_nh_dhhs,
    "ok_omma": _extract_ok_omma,
    "mi_cra": _extract_mi_cra,
    "wv_omc": _extract_wv_omc,
    "nd_hhs": _extract_nd_hhs,
    "sd_doh": _extract_sd_doh,
    "hi_doh": _extract_hi_doh,
    "tx_dps": _extract_tx_dps,
    "atlist": _extract_atlist,
    "sk_slga": _extract_sk_slga,
}


async def extract_records(list_url: str, list_type: ListType | str) -> list[DispensaryRecord]:
    """Extract dispensary records from a list resource.

    Returns [] when the resource yields nothing. A fragment raises `PartialRoster`, and an async
    handler that crashes raises `ExtractionFailed` — both reach `run_extract_states`, which records
    the attempt `failed` with the reason instead of `empty`.

    `list_type` should be one of `HANDLED_LIST_TYPES`; an unrecognised value falls through
    to the html table/address-block parser (the same path as `"html"`).
    """
    async with make_session() as session:
        handler = _ASYNC_HANDLERS.get(str(list_type))
        if handler is not None:
            # The async handlers fetch and parse in one step, so a malformed payload (an ArcGIS
            # layer answering JSON `null`) raised from inside them, escaped, and `gather` re-raised
            # it — every state's extraction died unpersisted. A fragment is still the caller's to
            # hear (`PartialRoster`); anything else is this one state's failure (`ExtractionFailed`).
            try:
                return await handler(list_url, session)
            except (PartialRoster, ExtractionFailed):
                raise
            except Exception as exc:
                raise ExtractionFailed(
                    f"{list_type} extraction failed for {list_url}: {type(exc).__name__}: {exc}"
                ) from exc
        if list_type in ("lookup",):
            return []  # dynamic search front ends — caller uses AI fallback
        try:
            resp = await session.get(list_url, timeout=60, allow_redirects=True)
        except Exception:
            return []
        if resp.status_code >= 400:
            return []

        try:
            # A URL ending in .pdf sometimes serves an HTML error/redirect page;
            # trust the bytes, not the extension.
            if list_type == "pdf" and resp.content[:5].startswith(b"%PDF"):
                return _extract_pdf(resp.content)
            if list_type == "az_dhs" and resp.content[:5].startswith(b"%PDF"):
                return _extract_az_dhs(resp.content)
            if list_type == "il_idfpr" and resp.content[:5].startswith(b"%PDF"):
                return _extract_il_idfpr(resp.content)
            # OLE2 magic — an .xls URL sometimes serves an HTML error page; trust the bytes.
            if list_type == "ab_aglc" and resp.content[:4] == b"\xd0\xcf\x11\xe0":
                return _extract_ab_aglc(resp.content)
            if list_type == "co_med":
                return _extract_co_med(resp.content)
            if list_type == "ma_ccc":
                return _extract_ma_ccc(resp.text)
            if list_type == "on_agco_csv":
                return _extract_on_agco_csv(resp.text)
            if list_type == "csv":
                sheet_rows = _spreadsheet_rows(resp.content)
                if sheet_rows is not None:
                    return _extract_rows(sheet_rows)
                return _extract_csv(resp.text)
            if list_type == "kml":
                return _extract_kml(resp.content)
            # html / unknown, or a mislabeled pdf that was really HTML: try the
            # table extractor, then the non-table card/list fallback.
            return _extract_page(resp.text)
        except Exception:
            return []


# ── Browser-rendered HTML (opt-in) ───────────────────────────────────────────

def _content_iframes(html: str) -> list[str]:
    """Return http(s) iframe srcs that might embed a dispensary list/app."""
    tree = HTMLParser(html)
    urls: list[str] = []
    for frame in tree.css("iframe"):
        src = frame.attributes.get("src") or ""
        if src.startswith("http") and not _JUNK_IFRAME_RE.search(src):
            urls.append(src)
    return urls


def _extract_page(html: str) -> list[DispensaryRecord]:
    """Tables, then the address-block fallback, then line blocks.

    Strictly ordered, each a LAST RESORT for the one before, so a page that already yields
    rows cannot change behaviour: the line-block rung only ever runs where we currently get
    **nothing**. It reads a street line followed by a "City, ST zip" line — the shape
    `BLOCK_ADDRESS_RE` cannot span, because that needs a comma between street and city.
    Alabama's AMCC roster is prose of exactly this form.
    """
    return _extract_html(html) or _extract_address_blocks(html) or _extract_line_blocks(html)


async def extract_rendered(url: str, tab) -> list[DispensaryRecord]:
    """Render a JS-driven page in Chrome and extract its dispensaries.

    Tries the table extractor then the card/list extractor on the rendered DOM,
    falling back to the largest content iframe when the top document yields
    nothing (covers Tableau/ArcGIS/embedded-locator iframes).
    """
    from rung.browser import render_html

    html = await render_html(tab, url)
    if not html:
        return []
    records = _extract_page(html)
    if records:
        return records
    for iframe_url in _content_iframes(html)[:2]:
        records = _extract_page(await render_html(tab, iframe_url))
        if records:
            return records
    return []


async def _render_empty_targets(
    targets: list,
    extracted: dict[str, list[DispensaryRecord]],
    methods: dict[str, str],
) -> None:
    """Render each still-empty target in one shared Chrome session, in place.

    A machine with no Chromium SKIPS the tier and says so — the states stay empty and keep their
    prior rows; nothing is recorded as a verdict about them (the same rule as `access.Unequipped`
    one stage over; review finding, 2026-09-26).
    """
    from pydoll.browser.chromium import Chrome

    from rung.browser import chromium_available, make_browser_options

    if not chromium_available():
        print(f"  render tier SKIPPED for {len(targets)} state(s): no Chromium on this machine "
              "(toolchain.toml names the install route) — prior rows are kept")
        return

    async with Chrome(options=make_browser_options()) as browser:
        tab = await browser.start()
        for rec in targets:
            try:
                records = await extract_rendered(rec.list_url, tab)
            except Exception:
                records = []
            if records:
                extracted[rec.abbr] = records
                methods[rec.abbr] = "render"


# ── Orchestration ────────────────────────────────────────────────────────────

@dataclass
class ExtractResult:
    abbr: str
    name: str
    list_type: str | None
    count: int
    method: str  # 'static' | 'render' | 'ai' | 'none'


def _fill_from_geocode_cache(conn: db.DBConn, records: list[DispensaryRecord]) -> int:
    """Fill each record's missing lat/lon, ZIP and city from the geocode cache, in memory, by
    `apply_geocode_cache`'s rules: lat+lon only together and only when the record has none; ZIP and
    city each only when empty; a value the source published is never replaced. Returns the number
    of records changed."""
    from rung.db import get_geocode_cache
    from rung.text import geocode_query

    keyed = [
        (record, query) for record in records
        if (record.latitude is None or not record.zip_code or not record.city) and record.address
        and (query := geocode_query(record.address, record.city, record.state, record.zip_code))
    ]
    cached = get_geocode_cache(conn, sorted({query for _, query in keyed}))
    changed = 0
    for record, query in keyed:
        if query not in cached:
            continue
        lat, lon, zip_code, city = cached[query]
        before = (record.latitude, record.zip_code, record.city)
        if record.latitude is None and lat is not None and lon is not None:
            record.latitude, record.longitude = lat, lon
        if not record.zip_code and zip_code:
            record.zip_code = zip_code
        if not record.city and city:
            record.city = city
        changed += before != (record.latitude, record.zip_code, record.city)
    return changed


def record_roster_observations(
    conn: db.DBConn, state: str, records: list[DispensaryRecord],
    *, now: datetime.datetime | None = None,
) -> int:
    """Append store-lifecycle history from a state's just-extracted roster. Caller commits.

    The Stage-1 ``state_roster`` twin of the overlay's ``company_site`` capture: build one
    :class:`LocationObservation` per physical location (``dedupe.geo_key``, ``address_key``
    fallback — the roster is only partially geocoded, so the address key carries more weight
    here) and hand them to the shared engine (``db.record_location_observations``). Runs only
    on a NON-EMPTY extraction — a failed roster fetch records nothing, so a store's observed
    absence stays a real signal rather than an artifact of a dead list URL. Roster rows carry
    no menu handle; the state-registered name goes in ``operator`` (raw, canonicalized at
    read). See docs/store_history_design.md. Returns observation rows appended.
    """
    observations: dict[str, LocationObservation] = {}
    for record in records:
        key = location_key(record.latitude, record.longitude, record.address, record.zip_code)
        if not key or key in observations:
            continue  # unidentifiable (e.g. MD's county-only rows), or a duplicate rooftop row
        observations[key] = LocationObservation(
            location_key=key, state=state, latitude=record.latitude,
            longitude=record.longitude, address=record.address, city=record.city,
            zip_code=record.zip_code, operator=record.name,
        )
    return db.record_location_observations(conn, "state_roster", observations, now=now)


async def run_extract_states(
    conn: db.DBConn,
    only: set[str] | None = None,
    use_ai: bool = False,
    use_render: bool = False,
    record_history: bool = False,
) -> list[ExtractResult]:
    """Extract dispensary records for every state with a discovered list URL.

    Three tiers run in order of cost: static handlers concurrently; then, when
    use_render is set, one shared Chrome renders JS-driven pages that came back
    empty; then, when use_ai is set, the local-Ollama fallback. Each state's
    existing rows are replaced atomically and only when extraction yields data,
    so re-runs are idempotent and a dead URL never wipes prior good rows.

    A state whose tier-1 handler raised :class:`PartialRoster` is NOT an empty state: the
    source answered and we refused the answer. It keeps its prior rows, is offered to
    neither later tier, and its capture attempt is recorded ``failed`` with the reason.

    ``record_history`` (opt-in): also append store-lifecycle history
    (``state_roster`` observations) alongside each non-empty state replace, in the
    same commit — see :func:`record_roster_observations`.
    """
    from rung.db import (
        apply_geocode_cache,
        delete_dispensaries_for_state,
        get_all_state_programs,
        insert_dispensary,
    )

    # A jurisdiction that DECLARES it publishes no licensee roster (`publishes_licensee_roster:
    # false` in states.yml, with the evidence beside it) has nothing to extract, whatever URL the
    # crawler once stored for it. Mississippi's stored "list" was the Department of Revenue's
    # medical-cannabis landing page, and its one extracted row was the department's own street
    # address — a roster of one building, which compare then reported as 76 stores the state was
    # missing. The healthcheck already exempts these states; the extraction now agrees with it.
    from rung.sources.state_lists import load_states

    roster_less = {s.abbr for s in load_states() if not s.publishes_licensee_roster}
    targets = [
        r for r in get_all_state_programs(conn)
        if r.list_url and r.list_status in ("found", "override", "stored")
        and r.abbr not in roster_less
        and (only is None or r.abbr in only)
    ]

    # Tier 1 — static extraction, concurrently.
    async def _extract(rec):
        try:
            records = await extract_records(rec.list_url, rec.list_type or "html")
        except PartialRoster as exc:
            # A fragment must not replace the prior roster: an empty result below keeps it. The
            # REASON travels with it, because "empty" is exactly what this state is not.
            print(f"  {rec.abbr}: roster extraction was PARTIAL and is discarded — {exc}")
            return rec, [], str(exc)
        except ExtractionFailed as exc:
            # The same treatment for a crash: the prior rows stand and the attempt is `failed` with
            # the reason — never `empty`, which would say the source had nothing to give.
            print(f"  {rec.abbr}: roster extraction FAILED — {exc}")
            return rec, [], str(exc)
        return rec, records, None

    raw = await asyncio.gather(*(_extract(r) for r in targets))
    extracted: dict[str, list[DispensaryRecord]] = {rec.abbr: recs for rec, recs, _ in raw}
    methods: dict[str, str] = {
        rec.abbr: ("static" if recs else "none") for rec, recs, _ in raw
    }
    # States whose roster was REFUSED as a fragment, with why. Kept beside `methods` rather than
    # as a fifth method word: `ExtractResult.method` is a documented four-word vocabulary the CLI
    # prints, and a refusal is not a way of extracting.
    partial: dict[str, str] = {rec.abbr: why for rec, _, why in raw if why is not None}

    # Tier 2 — browser render of still-empty JS pages (one shared Chrome).
    if use_render:
        # `not in partial` is DEFENSIVE here: every handler that raises `PartialRoster` today
        # serves a list_type (arcgis, on_agco, atlist, ca_dcc, ma_ccc) this filter already excludes, so
        # no refused state can be rendered. It holds the line if an html-path handler ever raises.
        renderable = [
            r for r in targets
            if not extracted[r.abbr] and r.abbr not in partial
            and (r.list_type in (None, "html", "unknown"))
        ]
        if renderable:
            await _render_empty_targets(renderable, extracted, methods)

    # Tier 3 — AI fallback (per state) + persist.
    results: list[ExtractResult] = []
    for rec in targets:
        records = extracted[rec.abbr]
        method = methods[rec.abbr]
        # `not in partial` is THE FIX (audit P-35). This tier has no list_type filter, so a state
        # whose roster was just refused as a fragment looked merely empty, the AI extracted a
        # roster of its own, and the DELETE below replaced the rows the refusal existed to keep.
        if not records and use_ai and rec.list_url and rec.abbr not in partial:
            try:
                from rung.sources.ai_fallback import extract_with_ai
                records = await extract_with_ai(rec.list_url, source_tag="ai")
                method = "ai" if records else "none"
            except Exception as exc:
                # The AI tier is opt-in and best-effort, so one state's failure must not abort
                # the run — but surface it (Ollama down, bad import, schema error) so a
                # misconfigured tier isn't silently indistinguishable from a genuinely empty page.
                print(f"  ai fallback failed for {rec.abbr}: {exc}")
                method = "none"

        # Replace this state's rows only when we actually extracted something.
        # Drop non-operator junk rows (license-number/header/test stubs) so they never enter
        # `dispensaries`. Filter BEFORE the guard below, so a junk-only roster counts as 0 real
        # records and keeps the prior good rows rather than wiping them.
        records = [record for record in records if not is_placeholder_name(record.name)]
        # A transient failure or a list URL that has gone 404 yields 0 records;
        # wiping the prior good rows in that case would be data loss.
        if records:
            delete_dispensaries_for_state(conn, rec.abbr)
            for record in records:
                record.state = rec.abbr  # scope rows to the state for idempotent replace
                insert_dispensary(conn, record)
            # Put back what the DELETE above just destroyed. The source republishes only what the
            # source publishes, so for a roster carrying a street but no ZIP (NV, IL, UT, MD…) the
            # geocoded lat/lon/ZIP/city are gone — and `compare`'s keys BOTH carry the ZIP, so the
            # state silently stops matching on a scrape that SUCCEEDED. The guard above protects the
            # anticipated failure (an empty scrape wiping good rows) and never saw this one. Costs no
            # geocoder calls; same commit as the replace, so the rows are never briefly un-enriched.
            apply_geocode_cache(conn, "dispensaries", rec.abbr)
            if record_history:
                # Store-lifecycle history from the fresh roster, same commit as the replace — and
                # AFTER the cache, with the same values filled onto the records: a roster with no
                # ZIP keys no location until the cache restores one, so history taken from the raw
                # records skipped exactly the rows the cache was about to make identifiable.
                _fill_from_geocode_cache(conn, records)
                record_roster_observations(conn, rec.abbr, records)
        if record_history:
            # What was ATTEMPTED this cycle, beside what was seen: an empty extraction writes no
            # observations (the guard above keeps the prior rows), and without this row the
            # roster leg's silence that cycle would read as every store leaving the roster.
            #
            # A refused fragment is `failed`, not `empty`: "attempted, nothing yielded" is false
            # of a source that yielded and was turned away, and the reason is worth keeping.
            outcome = "succeeded" if records else ("failed" if rec.abbr in partial else "empty")
            db.record_capture_attempt(
                conn, state=rec.abbr, source="state_roster", outcome=outcome, method=method,
                error=partial.get(rec.abbr), record_count=len(records),
            )
        if records or record_history:
            conn.commit()

        results.append(
            ExtractResult(rec.abbr, rec.name, rec.list_type, len(records), method)
        )
    return results


def print_extract_report(results: list[ExtractResult]) -> None:
    rows = sorted(results, key=lambda r: (-r.count, r.name))
    sep = "-" * 70
    print(sep)
    print(f"{'State':<22} | {'Type':<8} | {'Method':<7} | Records")
    print(sep)
    total = 0
    with_data = 0
    for r in rows:
        total += r.count
        if r.count:
            with_data += 1
        print(f"{r.name:<22} | {(r.list_type or '-'):<8} | {r.method:<7} | {r.count}")
    print(sep)
    print(f"States with records: {with_data}/{len(results)} | Total records: {total}")
    print(sep)
