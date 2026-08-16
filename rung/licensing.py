"""Retail cannabis licence regime per jurisdiction — the coded, cited form of "limited-license".

Reads ``data/license_regime.yml`` and answers "does this jurisdiction cap the number of retail
licences?". Every analysis that wants to split states by licensing regime must come through here.

The point is not convenience, it is falsifiability. Before this module existed, "limited-license
state" was a string in the prose of three analysis scripts — one of which interpolated the live
concentration ranking into a sentence that then *called* the top states limited-license, so the
variable was defined by the result it was being used to explain. Adversarial Round 24 (2026-07-31)
found that circle and withheld D4 §3.4's causal verb until the regime was coded from statute
(``reports/adversarial_log.md``; ``docs/analysis/license_regime.md``).

Three properties this module has and a hard-coded set never did:

* **An uncoded jurisdiction is not a regime.** :func:`regime_of` returns ``None`` for a jurisdiction
  nobody has read the statute for. It does NOT fall back to "open", which is the shape of error that
  makes a missing observation look like a measured one — this project's E1 class.
* **Every coded jurisdiction carries its evidence.** :func:`record_of` returns the citation, the
  verbatim quote and the retrieval date alongside the regime, so a reader can check a claim without
  re-running anything.
* **The coding rule is written down** in the YAML header, and it excludes per-holder licence limits.
  Several states cap how many licences one person may hold while capping entry not at all; coding
  those as "limited" would invert the variable.
"""


import functools
from pathlib import Path
from typing import Any

import yaml

_PATH = Path(__file__).parent / "data" / "license_regime.yml"

#: The regimes a jurisdiction can actually be IN. ``unchecked`` is deliberately absent: it is the
#: absence of a coding, not a third value, and every consumer must treat it as missing data.
CODED_REGIMES = frozenset({"limited", "open"})


@functools.cache
def _jurisdictions() -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
    return {
        str(abbr).upper(): dict(spec or {})
        for abbr, spec in (doc.get("jurisdictions") or {}).items()
    }


def record_of(abbr: str | None) -> dict[str, Any] | None:
    """The full coded record for a jurisdiction (regime, basis, citation, quote, source, date).

    ``None`` when the jurisdiction is not in the file at all — which is different from being in it
    and ``unchecked``. A caller that needs to tell "we never listed this state" from "we listed it
    and did not read its statute" gets that distinction from the ``regime`` key of the result.
    """
    if not abbr:
        return None
    return _jurisdictions().get(abbr.upper())


def regime_of(abbr: str | None) -> str | None:
    """``"limited"`` / ``"open"`` for a coded jurisdiction, else ``None``.

    ``None`` covers both "not listed" and "listed but ``unchecked``". Neither is a regime, and a
    caller must exclude the jurisdiction from a regime comparison rather than defaulting it.
    """
    record = record_of(abbr)
    if record is None:
        return None
    regime = record.get("regime")
    return str(regime) if regime in CODED_REGIMES else None


def coded() -> dict[str, str]:
    """``{abbr: regime}`` for every jurisdiction whose statute has actually been read."""
    return {
        abbr: str(spec["regime"])
        for abbr, spec in _jurisdictions().items()
        if spec.get("regime") in CODED_REGIMES
    }


def unchecked() -> tuple[str, ...]:
    """The jurisdictions listed but not yet coded — the worklist, and the coverage denominator."""
    return tuple(sorted(
        abbr for abbr, spec in _jurisdictions().items() if spec.get("regime") not in CODED_REGIMES
    ))


def regime_doc() -> dict[str, Any]:
    """The parsed file (``{"jurisdictions": {...}}``) — for provenance dumps and the loader."""
    return yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
