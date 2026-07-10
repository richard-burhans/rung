"""Shared address / text-extraction primitives.

Used by both the state-list extractor (`sources/extract.py`) and the company-store
extractor (`sources/company_stores.py`) so neither reaches into the other's private
helpers. Sits just above `models` in the dependency order (imports only `models` +
third-party).
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from selectolax.parser import HTMLParser

from rung.models import DispensaryRecord

# US 5-digit ZIP (optional +4) or a Canadian postal code (A1A 1A1, with or without
# the space — Ontario's AGCO roster emits "P3E4M8"). Uppercase-only for the postal
# letters: provinces publish them uppercased and it keeps prose from false-matching.
ZIP_RE = re.compile(r"\b(?:\d{5}(?:-\d{4})?|[A-Z]\d[A-Z] ?\d[A-Z]\d)\b")
STREET_RE = re.compile(r"\d{1,6}\s+[A-Za-z0-9.\- ]+")
PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
# A full "street, city, ST 12345" address, for card/list (non-table) pages.
# The state group matches Canadian province codes too (ON/BC/…), the zip group
# either ZIP shape or a Canadian postal code.
BLOCK_ADDRESS_RE = re.compile(
    r"(\d{1,6}[^,\n]{2,60}?),\s*([A-Za-z .'\-]{2,40}?),\s*([A-Z]{2})\s+"
    r"(\d{5}(?:-\d{4})?|[A-Z]\d[A-Z] ?\d[A-Z]\d)"
)
NAME_ELEMENT_SEL = "a, h1, h2, h3, h4, h5, h6, strong, b"
# Zero-width space, ZWNJ, ZWJ, BOM — sometimes embedded in scraped cells.
_ZERO_WIDTH = dict.fromkeys((0x200B, 0x200C, 0x200D, 0xFEFF))


def clean(value: str | None) -> str | None:
    """Collapse whitespace and strip zero-width chars; '' becomes None."""
    if value is None:
        return None
    stripped = " ".join(str(value).translate(_ZERO_WIDTH).split())
    return stripped or None


def _node_text(el) -> str:
    """Normalized element text with spaces between child nodes (selectolax joins
    child text with no separator otherwise, fusing a name into its address)."""
    return " ".join(el.text(separator=" ").split())


def extract_address_blocks(html: str) -> list[DispensaryRecord]:
    """Extract dispensaries from non-table HTML (card / list layouts).

    For each tightest element that holds a "street, city, ST zip" address and whose
    children hold none, take the name from the first link/heading (or the text before
    the address) and emit one record per address — operators list several locations in
    one block. Dedupes on (name, address). Returns `DispensaryRecord`s; the company
    store path converts them to its own record type.
    """
    tree = HTMLParser(html)
    records: list[DispensaryRecord] = []
    seen: set[tuple[str | None, str | None]] = set()
    for el in tree.css("p, li, td, address, article, section, div"):
        text = _node_text(el)
        matches = list(BLOCK_ADDRESS_RE.finditer(text))
        if not matches:
            continue
        # Keep only the tightest container: skip if a child element holds an address.
        if any(BLOCK_ADDRESS_RE.search(_node_text(c)) for c in el.iter(include_text=False)):
            continue
        name_el = el.css_first(NAME_ELEMENT_SEL)
        name = clean(name_el.text()) if name_el is not None else None
        if not name:
            name = clean(text[: matches[0].start()])
        if not name or len(name) > 80:
            continue
        for match in matches:
            street, city, state, zip_code = (clean(g) for g in match.groups())
            key = (name, street)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                DispensaryRecord(
                    source="html", name=name, address=street,
                    city=city, state=state, zip_code=zip_code,
                )
            )
    return records


# ── Line-block addresses (no comma between street and city) ──────────────────
# A layout tables and BLOCK_ADDRESS_RE both miss: the street on one line, "City, ST zip"
# on the next. BLOCK_ADDRESS_RE needs TWO commas ("street, city, ST zip") and so returns
# nothing. Alabama's AMCC roster is exactly this shape — a <p> of <br/>-separated lines —
# and so are several operator pages (Fluent's PA page, Zen Leaf).

# A street line ("6200 Carlisle Pike") that the next line completes.
STREET_LINE_RE = re.compile(r"^\d{1,6}\s+[A-Za-z0-9][\w .,'#-]{2,50}$")
# The zip alternative in these line regexes also accepts a Canadian postal code
# (A1A 1A1 / A1A1A1); the [A-Z]{2} state slot matches province codes as-is.
CITY_STATE_ZIP_RE = re.compile(
    r"^([A-Za-z .'-]{2,40}),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?|[A-Z]\d[A-Z] ?\d[A-Z]\d)$"
)
# Single-line "street, City ST zip" with no comma before the state
# (MariMart's "865 US-22, Blairsville PA 15717").
LOOSE_LINE_RE = re.compile(
    r"^(\d{1,6}[^,\n]{2,45}),\s*([A-Za-z][A-Za-z .'-]{1,38}?)\s+([A-Z]{2})\s+"
    r"(\d{5}(?:-\d{4})?|[A-Z]\d[A-Z] ?\d[A-Z]\d)$"
)
# Button/label lines that sit next to an address but are NOT the store name.
CTA_RE = re.compile(
    r"^(shop|order|menu|learn more|directions|now open|view|visit|coming soon|open\b|get )",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LineAddress:
    """One address found in a run of text lines. `index` is the line the street sits on."""

    index: int
    street: str
    city: str
    state: str
    zip_code: str


def text_lines(html: str) -> list[str]:
    """The document's visible text as non-empty, stripped lines (nbsp normalised)."""
    text = HTMLParser(html).text(separator="\n") or ""
    return [line.replace("\xa0", " ").strip() for line in text.split("\n") if line.strip()]


def iter_line_addresses(lines: list[str]) -> Iterator[LineAddress]:
    """Yield each address in `lines`, whether written on one line or split across two."""
    for index, line in enumerate(lines):
        loose = LOOSE_LINE_RE.match(line)
        if loose:
            yield LineAddress(index, loose.group(1), loose.group(2).strip(),
                              loose.group(3), loose.group(4))
            continue
        if not STREET_LINE_RE.match(line) or index + 1 >= len(lines):
            continue
        split = CITY_STATE_ZIP_RE.match(lines[index + 1])
        if split is not None:
            yield LineAddress(index, line, split.group(1).strip(), split.group(2), split.group(3))


def name_before(lines: list[str], index: int) -> str | None:
    """The store name on the line above an address, or None if that line isn't a name.

    Rejects the things that sit above an address and are not names: a call-to-action
    ("Now Open", "Directions"), an email, a URL, a phone number, another address line, and
    an ALL-CAPS banner — Alabama's roster puts "OPENING JUNE 4, 2026" directly above the
    dispensary's name, and only the caps rule tells the two apart.
    """
    if index <= 0:
        return None
    prev = lines[index - 1]
    if not (3 <= len(prev) <= 60) or not any(ch.isalpha() for ch in prev):
        return None
    if "@" in prev or "://" in prev or prev.isupper():
        return None
    if CTA_RE.match(prev) or PHONE_RE.fullmatch(prev):
        return None
    if STREET_LINE_RE.match(prev) or CITY_STATE_ZIP_RE.match(prev) or LOOSE_LINE_RE.match(prev):
        return None
    return clean(prev)


def extract_line_blocks(html: str) -> list[DispensaryRecord]:
    """Extract dispensaries from a line-block page (street line + "City, ST zip" line).

    The state-roster counterpart of the company-store `line_blocks` rung; both read the same
    `iter_line_addresses`/`name_before` primitives so the two cannot drift. Unnamed addresses
    are skipped — a roster row with no operator name cannot be attributed or compared.
    """
    lines = text_lines(html)
    records: list[DispensaryRecord] = []
    seen: set[str] = set()
    for found in iter_line_addresses(lines):
        street = clean(found.street)
        key = (street or "").lower()
        if not street or key in seen:
            continue
        name = name_before(lines, found.index)
        if not name:
            continue
        seen.add(key)
        records.append(DispensaryRecord(
            source="html", name=name, address=street,
            city=clean(found.city), state=found.state, zip_code=found.zip_code,
        ))
    return records
