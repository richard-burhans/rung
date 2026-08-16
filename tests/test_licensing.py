"""The licence-regime coding's contract.

These pin the properties that make `rung/data/license_regime.yml` an instrument rather than an
opinion: a coded row carries its evidence, an uncoded row makes no claim, and the gap between them
is a number that may shrink but not grow.

Why it needs a test at all: the variable this file replaces was prose. Three analysis scripts
described states as "limited-license" in their own output text, and `scripts/platform_share.py`
interpolated the live ranking into a sentence that CALLED the top states limited-license — so the
regime was defined by the result it was invoked to explain. Nothing could have failed. See
`reports/adversarial_log.md` Round 24 and `docs/analysis/license_regime.md`.
"""


import datetime

import pytest

from rung import licensing

#: Every jurisdiction listed but not yet read, as of 2026-08-01. This may only go DOWN: a new entry
#: added without its statute would otherwise be invisible, and an `unchecked` row is a claim the
#: analysis silently drops. Lower it in the PR that reads the statute — a ratchet nobody tightens
#: rots at exactly the rate the work succeeds (the `drafts` bound sat 35 wide for twelve days).
_UNCHECKED_BASELINE = 20

#: The coding rule's vocabulary. `basis` says WHICH form of rule put the jurisdiction in its regime,
#: and the set is closed so that a new, unargued form cannot enter by being typed into the YAML.
_LIMITED_BASES = frozenset({"statutory_cap", "regulatory_cap"})
_OPEN_BASES = frozenset({"local_cap_only", "mandatory_issuance", "no_statewide_cap"})

_EVIDENCE_FIELDS = ("citation", "quote", "source_type", "source_url", "retrieved_at", "confidence")
_CONFIDENCE_GRADES = frozenset({"verified", "reported", "inferred"})


def _coded_records() -> dict[str, dict]:
    return {abbr: licensing.record_of(abbr) or {} for abbr in licensing.coded()}


def test_every_regime_value_is_in_the_vocabulary() -> None:
    doc = licensing.regime_doc()["jurisdictions"]
    bad = {a: s.get("regime") for a, s in doc.items()
           if s.get("regime") not in (licensing.CODED_REGIMES | {"unchecked"})}
    assert not bad, f"regime must be limited/open/unchecked: {bad}"


@pytest.mark.parametrize("field", _EVIDENCE_FIELDS)
def test_a_coded_jurisdiction_carries_its_evidence(field: str) -> None:
    """A regime with no citation, quote, source or date is an assertion, not a coding."""
    missing = [abbr for abbr, rec in _coded_records().items() if not str(rec.get(field, "")).strip()]
    assert not missing, f"coded jurisdictions missing `{field}`: {sorted(missing)}"


def test_the_quote_is_long_enough_to_be_a_quote() -> None:
    """Guards the failure mode that actually happened: a citation assembled from a search snippet.

    A one-line `quote` is usually a paraphrase somebody typed. The threshold is deliberately low —
    it catches an empty gesture, not a terse statute.
    """
    short = {abbr: rec["quote"] for abbr, rec in _coded_records().items() if len(rec["quote"]) < 60}
    assert not short, f"quotes too short to be verbatim statutory text: {short}"


def test_basis_matches_regime() -> None:
    """`limited` needs a cap-shaped basis and `open` a no-cap-shaped one; nothing may cross over."""
    wrong = {
        abbr: (rec.get("regime"), rec.get("basis"))
        for abbr, rec in _coded_records().items()
        if rec.get("basis") not in (_LIMITED_BASES if rec["regime"] == "limited" else _OPEN_BASES)
    }
    assert not wrong, f"basis does not match regime (or is outside the vocabulary): {wrong}"


def test_confidence_is_a_grade() -> None:
    bad = {abbr: rec.get("confidence") for abbr, rec in _coded_records().items()
           if rec.get("confidence") not in _CONFIDENCE_GRADES}
    assert not bad, f"confidence must be verified/reported/inferred: {bad}"


def test_retrieved_at_is_a_real_date() -> None:
    """A regime is true *of a date*. An unparseable one cannot be aged."""
    for abbr, rec in _coded_records().items():
        value = rec["retrieved_at"]
        stamp = value if isinstance(value, datetime.date) else datetime.date.fromisoformat(str(value))
        assert stamp <= datetime.date.today(), f"{abbr}: retrieved_at is in the future ({stamp})"


def test_an_unchecked_jurisdiction_makes_no_regime_claim() -> None:
    """The load-bearing property: uncoded must not read as `open`.

    Defaulting a missing observation to a real value is this project's E1 class — a defaulted field
    reported as a fact about the world — and it is the exact error Round 24 escalated.
    """
    for abbr in licensing.unchecked():
        assert licensing.regime_of(abbr) is None, f"{abbr} is unchecked but regime_of returned a value"
        assert licensing.record_of(abbr) is not None, f"{abbr} should still be listed, with a note"


def test_an_unlisted_jurisdiction_is_none_not_an_error() -> None:
    assert licensing.regime_of("ZZ") is None
    assert licensing.regime_of(None) is None
    assert licensing.record_of("ZZ") is None


def test_coded_and_unchecked_partition_the_file() -> None:
    doc = licensing.regime_doc()["jurisdictions"]
    assert set(licensing.coded()) | set(licensing.unchecked()) == set(doc)
    assert not set(licensing.coded()) & set(licensing.unchecked())


def test_the_unchecked_backlog_does_not_grow() -> None:
    count = len(licensing.unchecked())
    assert count <= _UNCHECKED_BASELINE, (
        f"{count} jurisdictions carry no statute reading, baseline {_UNCHECKED_BASELINE}. "
        "Adding a jurisdiction without reading its statute silently shrinks every regime comparison."
    )


def test_every_coded_jurisdiction_has_a_source_url_that_is_a_url() -> None:
    bad = {abbr: rec["source_url"] for abbr, rec in _coded_records().items()
           if not str(rec["source_url"]).startswith(("http://", "https://"))}
    assert not bad, f"source_url must be fetchable: {bad}"
