"""The `attestations` table records an externally-sourced fact together with the evidence for it.

An analysis that compares a firm to a market needs premises it cannot derive from its own data —
"brand X is owned by company Y". A premise with no recorded source is unfalsifiable: a reader cannot
check it without redoing the author's research. These tests pin the properties that make the store
useful rather than decorative: a source is mandatory, two sources may disagree and both survive, and
a negative attestation ("X is NOT owned by Y") is representable, because name collisions are the
failure mode a brand→producer join actually hits.
"""

import pytest

from rung import db
from tests.conftest import pg_conn


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_engine_tables(conn)
    return conn


def _fact(**over: object) -> db.Attestation:
    base: dict[str, object] = {
        "subject_type": "brand", "subject": "Savvy", "predicate": "owned_by",
        "object": "Verano Holdings", "source_type": "sec_filing",
        "source_ref": "Verano FY2024 Form 10-K", "source_url": "https://www.sec.gov/…",
        "quote": "…including Savvy™…", "confidence": "verified", "retrieved_at": "2026-07-10",
    }
    base.update(over)
    return db.Attestation(**base)  # type: ignore[arg-type]


def test_roundtrip_carries_the_evidence() -> None:
    pg_conn = _conn()
    db.upsert_attestation(pg_conn, _fact())
    pg_conn.commit()

    [got] = db.attestations_for(pg_conn, "brand", "savvy")   # case-insensitive subject
    assert got.object == "Verano Holdings"
    assert got.source_ref == "Verano FY2024 Form 10-K"
    assert got.quote == "…including Savvy™…"          # the claim is checkable in place
    assert got.retrieved_at == "2026-07-10"           # a fact is true *of a source*, at a date
    assert got.confidence == "verified"


def test_upsert_is_idempotent_per_source() -> None:
    pg_conn = _conn()
    db.upsert_attestation(pg_conn, _fact())
    db.upsert_attestation(pg_conn, _fact(quote="corrected quote"))
    pg_conn.commit()

    rows = db.attestations_for(pg_conn, "brand", "Savvy")
    assert len(rows) == 1                              # same triple + same source → one row
    assert rows[0].quote == "corrected quote"


def test_two_sources_for_one_fact_both_survive() -> None:
    # A reader is better served by both sources than by whichever was written last. Keying on
    # source_ref is what makes a disagreement visible instead of silently last-write-wins.
    pg_conn = _conn()
    db.upsert_attestation(pg_conn, _fact())
    db.upsert_attestation(pg_conn, _fact(source_ref="verano.com/brand/savvy/",
                                         source_type="company_site", confidence="verified"))
    pg_conn.commit()

    rows = db.attestations_for(pg_conn, "brand", "Savvy")
    assert {r.source_ref for r in rows} == {"Verano FY2024 Form 10-K", "verano.com/brand/savvy/"}


def test_a_negative_attestation_is_representable() -> None:
    # The collision that would corrupt a naive brand join: Verano's "(the) Essence" product brand vs
    # the Nevada "Essence Cannabis Dispensary" chain acquired by a different MSO.
    pg_conn = _conn()
    db.upsert_attestation(pg_conn, _fact(
        subject_type="company", subject="Essence Cannabis Dispensary",
        predicate="not_owned_by", source_type="trade_press",
        source_ref="Las Vegas Review-Journal", confidence="reported", quote=None,
    ))
    pg_conn.commit()

    [got] = db.attestations_for(pg_conn, "company", "Essence Cannabis Dispensary", "not_owned_by")
    assert got.object == "Verano Holdings"
    assert got.confidence == "reported"


def test_predicate_filter_narrows() -> None:
    pg_conn = _conn()
    db.upsert_attestation(pg_conn, _fact(subject="Standard Farms", object="TILT Holdings",
                                         source_ref="MJBizDaily", source_type="trade_press",
                                         confidence="reported", quote=None))
    db.upsert_attestation(pg_conn, _fact(subject="Standard Farms", predicate="not_owned_by",
                                         source_ref="MJBizDaily-neg", source_type="trade_press",
                                         confidence="reported", quote=None))
    pg_conn.commit()

    assert len(db.attestations_for(pg_conn, "brand", "Standard Farms")) == 2
    [only] = db.attestations_for(pg_conn, "brand", "Standard Farms", "owned_by")
    assert only.object == "TILT Holdings"


def test_confidence_vocabulary_is_enforced() -> None:
    # 'probably' is not a confidence grade. The DB, not the caller, is the last line of defence.
    pg_conn = _conn()
    with pytest.raises(Exception, match="attestations_confidence_check"):
        db.upsert_attestation(pg_conn, _fact(confidence="probably"))
        pg_conn.commit()
