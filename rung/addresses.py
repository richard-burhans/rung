"""Shared address / text-extraction primitives.

Used by both the state-list extractor (`sources/extract.py`) and the company-store
extractor (`sources/company_stores.py`) so neither reaches into the other's private
helpers. Sits just above `models` in the dependency order (imports only `models` +
third-party).
"""

import re

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
