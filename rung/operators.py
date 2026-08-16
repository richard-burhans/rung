"""Retail banner → corporate parent: the CROSS-STATE key `companies` does not have.

`companies` is ``UNIQUE (canonical_name, state)`` and `company_stores.canonical_name` is a per-state
label, so "is this operator a multi-state operator?" has only ever been answered by asking whether
the same STRING appears in three states. Adversarial Round 24 (2026-07-31) named that as owed work:
a per-state table was being asked a cross-state question. String identity fails in both directions
and both failures were measured inside that round.

* **False merge.** Seven generic storefront names — `The Dispensary`, `Dispensary Near Me`,
  `Top Shelf` and friends — appear in three or more states as unrelated businesses and were counted
  as MSOs. Dropping them moved the national MSO share 9.84% → 9.25%.
* **False split.** One parent trades under different banners per state (Verano as Zen Leaf and MÜV,
  Jushi as Beyond Hello, Cresco as Sunnyside), so its stores were counted as several independent
  operators. Folding the attested parents moved Connecticut +24.0 pp and Nevada +17.4 pp — more than
  the Florida fold the refuters suspected of cherry-picking, and it WIDENS the regime contrast rather
  than manufacturing it.

So this module is deliberately a **key, not a matcher**. Lookup is exact on a normalized banner, not
a substring or token search: `rung.brands.parent_of` searches inside a brand string because product
brands appear embedded in longer names, but an operator banner is the whole label and substring
matching there is how `The Dispensary` swallows `The Dispensary of Utah`. A banner that is not in the
file is its own parent, and a banner listed as a non-identity has NO parent — the caller must exclude
it from a cross-state count rather than guess.
"""


import functools
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

_PATH = Path(__file__).parent / "data" / "operator_parent.yml"
_PUNCT = re.compile(r"[^a-z0-9]+")


def normalize_banner(name: str) -> str:
    """Casefold, fold accents, strip punctuation/spacing: ``Zen Leaf``/``ZenLeaf``/``zen-leaf`` → one key.

    Accents are folded to ASCII rather than dropped. Stripping them as punctuation would turn
    ``MÜV`` into ``mv`` and leave it unequal to the ``MUV`` the roster also publishes — the exact
    spelling split this normalization exists to close.
    """
    decomposed = unicodedata.normalize("NFKD", name.lower())
    return _PUNCT.sub("", "".join(c for c in decomposed if not unicodedata.combining(c)))


@functools.cache
def _index() -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, Any]]]:
    """``(banner -> parent, banner -> reason-it-is-not-an-identity, parent -> provenance record)``."""
    doc = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
    parents: dict[str, str] = {}
    records: dict[str, dict[str, Any]] = {}
    for parent, spec in (doc.get("parents") or {}).items():
        spec = dict(spec or {})
        records[str(parent)] = spec
        for banner in spec.get("banners", []):
            parents[normalize_banner(str(banner))] = str(parent)
    generic = {
        normalize_banner(str(name)): str(reason)
        for name, reason in (doc.get("not_corporate_identities") or {}).items()
    }
    return parents, generic, records


def parent_of(name: str | None) -> str | None:
    """The corporate parent behind a retail banner.

    * a mapped banner → its parent (this is the fold that fixes false splits);
    * an unmapped banner → **itself**, so a caller can group on the result uniformly, exactly as
      :func:`rung.brands.parent_of` does — an unmapped operator is presumed independent;
    * a banner listed as a non-corporate identity → ``None``. This is the refusal that fixes false
      merges, and it must not be read as "independent": it means the string cannot serve as a
      cross-state key at all, so the operator is unresolvable and belongs OUT of the denominator.
    """
    if not name:
        return None
    parents, generic, _ = _index()
    key = normalize_banner(name)
    if key in generic:
        return None
    return parents.get(key, name)


def operator_key(name: str | None, *, fold_spelling: bool = False) -> str | None:
    """The value to GROUP BY when counting one operator across states.

    Default (``fold_spelling=False``) applies **only the attested corporate fold**: a mapped banner
    becomes its parent, an unmapped banner keeps its own string, a generic name gets ``None``. This
    is the conservative key and the one to report.

    ``fold_spelling=True`` additionally groups unmapped banners by their normalized form, which folds
    ``Good Day Farm`` (MO) with ``Gooddayfarm`` (LA, AR). **It is a sensitivity, not an improvement,
    and it must not be the headline.** Measured on the live table it turns 72 multi-state operators
    into 107, and only six of the thirty-five new ones are corporate parents: the rest are
    case-and-spacing variants of generic shop names — ``bestbuds``, ``bloom``, ``elevate``,
    ``budz``, ``cream``, ``elite`` — merging unrelated businesses in different states into invented
    MSOs. It widens exactly the false merge the crosswalk exists to close, and it moved the coded
    regime test from p = 0.079 to p = 0.0017 while doing so. A result that only appears under this
    flag is an artifact of the key.
    """
    parent = parent_of(name)
    if parent is None:
        return None
    if not fold_spelling:
        return parent
    parents, _, _ = _index()
    return parent if normalize_banner(str(name)) in parents else normalize_banner(parent)


def is_corporate_identity(name: str | None) -> bool:
    """False when the banner is a generic storefront label that names no particular company."""
    if not name:
        return False
    _, generic, _ = _index()
    return normalize_banner(name) not in generic


def reason_not_an_identity(name: str | None) -> str | None:
    """Why a banner was ruled out as a cross-state key, for reporting what a count excluded."""
    if not name:
        return None
    _, generic, _ = _index()
    return generic.get(normalize_banner(name))


def parent_record(parent: str) -> dict[str, Any] | None:
    """The provenance behind one parent (source, quote, retrieval date) — for an audit trail."""
    return _index()[2].get(parent)


def parents_doc() -> dict[str, Any]:
    """The parsed crosswalk — for provenance dumps and the attestation loader."""
    return yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
