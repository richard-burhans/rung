"""Brand → parent-company (MSO) crosswalk — one source of truth.

Reads ``data/brand_parent.yml`` and answers "who owns this brand?". Used by the cultivar-identity MSO-parent-collapse
robustness check (does the cultivar→terpene signal survive collapsing co-owned brands to one producer?)
and by the ``brand_parent`` reference table shipped in the clean cultivar-identity dataset. Keeping the crosswalk in one
data file — instead of a dict hard-coded in an analysis script — is what lets the planned SEC/website
brand-mapping task expand it in one place and have every consumer pick it up.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import yaml

_PATH = Path(__file__).parent / "data" / "brand_parent.yml"


@functools.cache
def _aliases() -> tuple[tuple[re.Pattern[str], str], ...]:
    """``(compiled token-match pattern, parent)`` pairs, in **document order**.

    An alias matches only as a whole TOKEN — not flanked by an alphanumeric — so ``legend`` matches
    "Legend" / "LEGEND" / "Legend Cannabis" but NOT "Legends" or "4Front - Legends", ``find`` matches
    "Find." / "FIND" but not "Findlay", and ``remedi`` (were it used) would not grab "Organic Remedies".
    Punctuation and spaces inside an alias are literal (``(the) essence``, ``high supply``, ``&shine``).
    Document order is load-bearing: :func:`parent_of` returns the FIRST alias that matches, so write a more
    specific alias earlier when two could collide.
    """
    doc = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
    pairs: list[tuple[re.Pattern[str], str]] = []
    for parent, spec in (doc.get("parents") or {}).items():
        for alias in (spec or {}).get("aliases", []):
            a = str(alias).lower()
            pat = re.compile(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])")
            pairs.append((pat, str(parent)))
    return tuple(pairs)


def parent_of(brand: str | None) -> str | None:
    """The parent company that owns ``brand`` (case-insensitive whole-token match), else the brand itself.

    An unmapped brand is its OWN parent — an independent producer — so a caller can group on the result
    uniformly (the cultivar-identity parent-collapse counts *distinct parents* per cultivar). Returns
    ``None`` for a null/empty brand.
    """
    if not brand:
        return brand
    lowered = brand.lower()
    for pattern, parent in _aliases():
        if pattern.search(lowered):
            return parent
    return brand


def parents_doc() -> dict:
    """The parsed crosswalk (``{"parents": {...}}``) — for provenance/manifests."""
    return yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
