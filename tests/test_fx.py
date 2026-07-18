"""Tests for the FX series (docs/fx_series_design.md): the forward-fill / inversion logic and the
``product_observations_fx`` currency-conversion view."""

import datetime

import pytest
from conftest import pg_conn

from rung import db, fx, reference_db
from rung.models import StoreProductRecord


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)
    return conn


# --- forward-fill + inversion (no DB) -------------------------------------------------------------

def test_forward_fill_inverts_and_fills_weekends() -> None:
    """BoC publishes CAD-per-USD on business days only; we store USD-per-CAD for every calendar day,
    carrying the weekend forward from Friday."""
    obs = [
        (datetime.date(2026, 7, 2), 1.4181),  # Thu
        (datetime.date(2026, 7, 3), 1.4201),  # Fri
        (datetime.date(2026, 7, 6), 1.4219),  # Mon (Jul 4-5 is the weekend)
    ]
    rows = fx._forward_fill(obs, datetime.date(2026, 7, 2), datetime.date(2026, 7, 6))

    assert [r[0] for r in rows] == [datetime.date(2026, 7, d) for d in (2, 3, 4, 5, 6)]
    by_day = {r[0]: r for r in rows}
    # published day: stored as-is, inverted to USD-per-CAD, not carried
    assert by_day[datetime.date(2026, 7, 2)][3] == round(1 / 1.4181, 6)
    assert by_day[datetime.date(2026, 7, 2)][5] is False
    # weekend: carries Friday's (Jul 3) rate forward, flagged
    assert by_day[datetime.date(2026, 7, 4)][3] == round(1 / 1.4201, 6)
    assert by_day[datetime.date(2026, 7, 4)][5] is True
    assert by_day[datetime.date(2026, 7, 5)][5] is True
    # every row is the CAD->USD pair sourced from the Bank of Canada
    assert all(r[1:3] == ("CAD", "USD") and r[4] == "bank_of_canada" for r in rows)


def test_forward_fill_never_fabricates_a_leading_rate() -> None:
    """A window that opens before the first available rate — with nothing preceding it — leaves those
    days ABSENT rather than inventing a rate (the self-inflicted-gap guard)."""
    obs = [(datetime.date(2026, 7, 3), 1.42)]
    rows = fx._forward_fill(obs, datetime.date(2026, 7, 1), datetime.date(2026, 7, 3))
    assert [r[0] for r in rows] == [datetime.date(2026, 7, 3)]  # Jul 1-2 absent, not fabricated


def test_forward_fill_seeds_carry_from_a_prior_observation() -> None:
    """An observation before the window seeds the carry so the opening days are covered."""
    obs = [(datetime.date(2026, 6, 30), 1.40), (datetime.date(2026, 7, 3), 1.42)]
    rows = fx._forward_fill(obs, datetime.date(2026, 7, 1), datetime.date(2026, 7, 3))
    by_day = {r[0]: r for r in rows}
    assert by_day[datetime.date(2026, 7, 1)][3] == round(1 / 1.40, 6)
    assert by_day[datetime.date(2026, 7, 1)][5] is True  # carried from Jun 30


# --- the conversion view + CRUD (DB) --------------------------------------------------------------

def _seed_programs(conn: db.DBConn) -> None:
    conn.execute(
        "INSERT INTO state_programs (abbr, name, programs, country) VALUES "
        "('PA', 'Pennsylvania', 'med', 'US'), ('ON', 'Ontario', 'rec', 'CA')"
    )


def _observe(conn: db.DBConn, state: str, price: float, day: str) -> None:
    conn.execute(
        "INSERT INTO product_observations (product_id, store_key, state, price, scraped_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (1, f"{state.lower()}:1", state, price, f"{day}T10:00:00+00:00"),
    )


def test_view_converts_cad_and_passes_usd_through() -> None:
    conn = _conn()
    _seed_programs(conn)
    reference_db.upsert_fx_rates(
        conn, [(datetime.date(2026, 7, 3), "CAD", "USD", 0.70, "bank_of_canada", False)]
    )
    _observe(conn, "PA", 40.0, "2026-07-03")  # USD
    _observe(conn, "ON", 40.0, "2026-07-03")  # CAD, same-day rate present
    conn.commit()

    by_state = {
        r[0]: r
        for r in conn.execute(
            "SELECT state, currency, fx_rate, price_usd FROM product_observations_fx ORDER BY state"
        ).fetchall()
    }
    # Ontario (CAD) converted at 0.70: 40 * 0.70 = 28.00
    assert by_state["ON"][1] == "CAD"
    assert float(by_state["ON"][2]) == pytest.approx(0.70)
    assert float(by_state["ON"][3]) == pytest.approx(28.0)
    # Pennsylvania (USD) passes through at rate 1.0
    assert by_state["PA"][1] == "USD"
    assert float(by_state["PA"][2]) == pytest.approx(1.0)
    assert float(by_state["PA"][3]) == pytest.approx(40.0)


def test_view_leaves_price_usd_null_when_no_same_day_rate() -> None:
    """A missing same-day rate yields a NULL conversion, never a wrong one."""
    conn = _conn()
    _seed_programs(conn)
    reference_db.upsert_fx_rates(
        conn, [(datetime.date(2026, 7, 3), "CAD", "USD", 0.70, "bank_of_canada", False)]
    )
    _observe(conn, "ON", 40.0, "2026-07-10")  # no rate on Jul 10
    conn.commit()
    row = conn.execute(
        "SELECT fx_rate, price_usd FROM product_observations_fx WHERE state = 'ON'"
    ).fetchone()
    assert row == (None, None)


def test_upsert_is_idempotent_on_the_key() -> None:
    conn = _conn()
    key_day = datetime.date(2026, 7, 3)
    reference_db.upsert_fx_rates(conn, [(key_day, "CAD", "USD", 0.70, "bank_of_canada", False)])
    reference_db.upsert_fx_rates(conn, [(key_day, "CAD", "USD", 0.72, "bank_of_canada", True)])
    conn.commit()
    rows = conn.execute("SELECT rate, is_carried FROM fx_rates").fetchall()
    assert len(rows) == 1
    assert float(rows[0][0]) == pytest.approx(0.72)
    assert rows[0][1] is True


def test_currencies_needing_conversion_reads_the_snapshot() -> None:
    conn = _conn()
    _seed_programs(conn)
    ca = StoreProductRecord(
        company_id=1, state="ON", store_key="dutchie:1", platform="dutchie",
        external_id="1", source="dutchie_products", name="CA flower", price=10.0,
    )
    us = StoreProductRecord(
        company_id=1, state="PA", store_key="dutchie:2", platform="dutchie",
        external_id="2", source="dutchie_products", name="US flower", price=10.0,
    )
    db.replace_store_products(conn, "dutchie:1", [ca])
    db.replace_store_products(conn, "dutchie:2", [us])
    conn.commit()
    assert reference_db.currencies_needing_conversion(conn) == {"CAD"}


def test_backfill_start_is_the_earliest_priced_observation() -> None:
    conn = _conn()
    _seed_programs(conn)
    _observe(conn, "ON", 40.0, "2026-06-20")
    _observe(conn, "PA", 40.0, "2026-06-25")
    conn.commit()
    assert reference_db.fx_backfill_start(conn) == datetime.date(2026, 6, 20)


def test_backfill_start_is_none_on_empty_data() -> None:
    conn = _conn()
    assert reference_db.fx_backfill_start(conn) is None
