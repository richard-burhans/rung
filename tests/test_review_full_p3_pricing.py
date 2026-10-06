"""Sizes, the price-channel SQL and the state-program upsert — fixes from the 2026-10-06
whole-tree review. Each test fails on the code before that review."""

import json

import pytest
from conftest import pg_conn

from rung import db, reference_db
from rung.models import StateProgramRecord
from rung.normalize import price_channel, size_to_grams, variant_pricing


@pytest.mark.parametrize(("label", "grams"), [
    (".5g", 0.5),            # a leading decimal point read as 5 g
    (".1g", 0.1),
    ("1/2 g", 0.5),          # a fraction of a gram read its denominator: 2 g
    ("7/10 g", 0.7),
    ("1/2 lb", 224.0),       # a fraction of a pound read as 2 lb (896 g)
    ("1/4lb", 112.0),
    ("5x0.5g", 2.5),         # a compact multipack sized as ONE unit (0.5 g)
    ("10x0.35g", 3.5),
    ("2x1g", 2.0),
    # unchanged behaviour
    ("0.5g", 0.5), ("1.5g", 1.5), ("10.5g", 10.5), ("1/8 oz", 3.5), ("5 x 0.5g", 2.5),
    ("5pk 0.5g", 2.5), ("1x1g", 1.0), ("100mg", 0.1), ("10x", None),
])
def test_size_labels(label: str, grams: float | None) -> None:
    assert size_to_grams(label) == grams


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)
    return conn


def _program(abbr: str, programs: str, term: str = "cannabis") -> StateProgramRecord:
    return StateProgramRecord(abbr=abbr, name=abbr, programs=programs, program_term=term,
                              agency="agency", country="US")


def _sql_price(conn: db.DBConn, state: str, menu_type: str | None, variant: dict) -> float | None:
    row = conn.execute(
        "SELECT " + reference_db.EFFECTIVE_VARIANT_PRICE + " "
        "FROM (VALUES (%s::text, %s::text)) AS sp(state, menu_type) "
        "CROSS JOIN LATERAL (SELECT %s::jsonb AS vv) AS v",
        (state, menu_type, json.dumps(variant)),
    ).fetchone()
    assert row is not None
    return row[0]


def test_an_undeclared_menu_outside_medical_only_states_prices_on_rec_in_sql() -> None:
    """`menu_type = 'medical'` is NULL for an undeclared row, so both channel branches were NULL and
    the row fell to the all-channel minimum: the medical price, where Python prices rec."""
    conn = _conn()
    reference_db.upsert_state_program(conn, _program("OR", "both"))
    reference_db.upsert_state_program(conn, _program("FL", "medical"))
    variant = {"price_med": 30.0, "price_rec": 40.0, "special_price_rec": 36.0}
    for state, menu_type in (("OR", None), ("FL", None), ("OR", "medical"), ("OR", "adult_use")):
        python = variant_pricing(variant, price_channel(menu_type, medical_only=state == "FL"))[0]
        assert _sql_price(conn, state, menu_type, variant) == python, (state, menu_type)
    assert _sql_price(conn, "OR", None, variant) == 36.0


def test_a_registry_program_change_reaches_the_stored_row() -> None:
    """The upsert never rewrote `programs`, so a state moved to medical-only in the registry kept
    `both` — and its undeclared menus kept the adult-use price (DC and VA on 2026-10-06)."""
    conn = _conn()
    reference_db.upsert_state_program(conn, _program("DC", "both"))
    reference_db.upsert_state_program(conn, _program("DC", "medical", term="medical cannabis"))
    row = conn.execute("SELECT programs, program_term FROM state_programs WHERE abbr = 'DC'").fetchone()
    assert row == ("medical", "medical cannabis")
    assert "DC" in reference_db.medical_only_states(conn)
