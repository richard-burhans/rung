"""Tests for the Stage-3 store_products snapshot persistence: wholesale replace,
the empty-result guard, and the menu-target query over company_stores."""

import psycopg
import pytest
from conftest import pg_conn

from rung import db
from rung.models import CompanyStoreRecord, StoreProductRecord


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)
    return conn


def _product(name: str, **overrides) -> StoreProductRecord:
    fields = {
        "company_id": 1,
        "state": "PA",
        "store_key": "sweedpos:42",
        "platform": "sweedpos",
        "external_id": "42",
        "source": "sweedpos_api",
        "name": name,
    }
    fields.update(overrides)
    return StoreProductRecord(**fields)


def test_replace_swaps_snapshot_and_keeps_other_stores() -> None:
    conn = _conn()
    assert db.replace_store_products(conn, "sweedpos:42", [_product("Old A"), _product("Old B")]) == 2
    other = _product("Other", store_key="jane:7", platform="jane", external_id="7")
    assert db.replace_store_products(conn, "jane:7", [other]) == 1
    conn.commit()

    assert db.replace_store_products(conn, "sweedpos:42", [_product("New")]) == 1
    conn.commit()
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM store_products WHERE store_key = %s", ("sweedpos:42",)
        ).fetchall()
    ]
    assert names == ["New"]
    assert db.count_store_products(conn, "jane:7") == 1  # untouched


def test_empty_result_keeps_prior_snapshot() -> None:
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product("Keep me")])
    conn.commit()
    assert db.replace_store_products(conn, "sweedpos:42", []) == 1
    assert db.count_store_products(conn, "sweedpos:42") == 1


def _age_snapshot(conn: db.DBConn, store_key: str, hours: float) -> None:
    """Backdate a store's snapshot so the partial-fetch freshness guard can be exercised."""
    import datetime
    stamp = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)).isoformat()
    conn.execute(
        "UPDATE store_products SET scraped_at = %s WHERE store_key = %s", (stamp, store_key)
    )


def test_partial_rescrape_keeps_fresh_prior_snapshot() -> None:
    # A re-scrape that collapses to under half a still-fresh snapshot looks like a
    # throttled partial and must not overwrite the good menu.
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product(f"P{i}") for i in range(10)])
    conn.commit()
    assert db.replace_store_products(conn, "sweedpos:42", [_product("partial")]) == 10
    assert db.count_store_products(conn, "sweedpos:42") == 10


def test_partial_rescrape_lands_once_prior_is_stale() -> None:
    # The guard only protects a FRESH prior; once it ages past the window a genuine
    # shrink lands so a store can't be wedged on stale data forever.
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product(f"P{i}") for i in range(10)])
    _age_snapshot(conn, "sweedpos:42", db._MENU_RETAIN_MAX_AGE_HOURS + 1)
    conn.commit()
    assert db.replace_store_products(conn, "sweedpos:42", [_product("real shrink")]) == 1
    assert db.count_store_products(conn, "sweedpos:42") == 1


def test_modest_drop_is_not_treated_as_partial() -> None:
    # A drop that stays above the fraction is a normal menu change and overwrites.
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product(f"P{i}") for i in range(10)])
    conn.commit()
    assert db.replace_store_products(conn, "sweedpos:42", [_product(f"Q{i}") for i in range(7)]) == 7
    assert db.count_store_products(conn, "sweedpos:42") == 7


def test_terpenes_and_variants_round_trip_as_json() -> None:
    conn = _conn()
    # mg-dosed product: per-dose mg columns, no percent (the two units are mutually exclusive per
    # cannabinoid — enforced by store_products_potency_unit_check, see the negative test below).
    record = _product(
        "Lemon Haze",
        terpenes=[{"name": "limonene", "value": 1.2}],
        variants=[{"option": "3.5g", "price": 35.0}],
        price=35.0,
        thc=None,
        thc_mg=100.0,
        cbd_mg=5.0,
    )
    db.replace_store_products(conn, "sweedpos:42", [record])
    conn.commit()
    row = conn.execute(
        "SELECT terpenes, variants, price, thc, thc_mg, cbd_mg "
        "FROM store_products WHERE store_key = %s",
        ("sweedpos:42",),
    ).fetchone()
    assert row is not None
    assert row[0] == [{"name": "limonene", "value": 1.2}]
    assert row[1] == [{"option": "3.5g", "price": 35.0}]
    assert (row[2], row[3], row[4], row[5]) == (35.0, None, 100.0, 5.0)


def test_potency_unit_check_rejects_both_percent_and_mg_for_one_cannabinoid() -> None:
    """store_products_potency_unit_check: a cannabinoid is a percent OR a per-dose mg, never both
    (CLAUDE.md potency convention). The DB now enforces it — a direct write of both is rejected,
    not silently stored (the gap audit finding H1, 2026-07-02)."""
    conn = _conn()
    base = (
        "INSERT INTO store_products "
        "(company_id, state, store_key, platform, external_id, source, scraped_at, {cols}) "
        "VALUES (1, 'PA', 'sweedpos:42', 'sweedpos', '42', 'sweedpos_api', '2026-01-01T00:00:00Z', {vals})"
    )
    # thc + thc_mg together → rejected.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(base.format(cols="thc, thc_mg", vals="21.5, 100.0"))
    conn.rollback()  # a failed statement poisons the transaction; reset before reusing the conn.
    # cbd + cbd_mg together → rejected.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(base.format(cols="cbd, cbd_mg", vals="2.0, 10.0"))
    conn.rollback()
    # A percent-only row and an mg-only row are both fine (no violation raised).
    conn.execute(base.format(cols="thc, cbd", vals="21.5, 0.5"))
    conn.execute(
        base.format(cols="thc_mg, cbd_mg", vals="100.0, 5.0").replace(
            "'sweedpos:42'", "'sweedpos:43'"
        )
    )
    conn.commit()


def test_normalized_fields_round_trip_and_view_derives_price_per_g() -> None:
    conn = _conn()
    record = _product(
        "Lemon Haze",
        price=35.0,
        size_g=3.5,
        thc=21.5,
        category_std="Flower",
        product_type_std="Bud",        # 2nd-level type surfaced by the view as `product_type`
        strain_type="Sativa-Hybrid",   # raw lineage preserved on store_products
        strain_type_std="Hybrid",      # canonical facet surfaced by the view
        terpenes=[{"name": "limonene", "value": 1.2}],
        terpenes_std={"Limonene": 1.2},
        terp_total=1.2,
        variants=[{"option": "3.5g", "price": 35.0, "size_g": 3.5, "price_per_g": 10.0}],
    )
    db.replace_store_products(conn, "sweedpos:42", [record])
    conn.commit()
    stored = conn.execute(
        "SELECT size_g, terpenes_std, terp_total, strain_type, strain_type_std "
        "FROM store_products WHERE store_key = %s",
        ("sweedpos:42",),
    ).fetchone()
    assert stored == (3.5, {"Limonene": 1.2}, 1.2, "Sativa-Hybrid", "Hybrid")
    # The combined surface exposes the standardized fields, derives price-per-gram, and surfaces
    # the canonical strain facet (strain_type_std) under the `strain_type` name.
    view = conn.execute(
        "SELECT category, size_g, price_per_g, thc, terp_total, terpenes_std, strain_type, product_type "
        "FROM products_normalized WHERE store_key = %s",
        ("sweedpos:42",),
    ).fetchone()
    assert view is not None
    assert (view[0], view[1]) == ("Flower", 3.5)
    assert float(view[2]) == 10.0  # 35 / 3.5
    assert (view[3], view[4], view[5]) == (21.5, 1.2, {"Limonene": 1.2})
    assert view[6] == "Hybrid"  # strain_type_std AS strain_type — the canonical facet, not the raw
    assert view[7] == "Bud"     # product_type_std AS product_type — the 2nd-level type


def _store(company_id: int, name: str, **overrides) -> CompanyStoreRecord:
    fields = {
        "company_id": company_id,
        "canonical_name": f"Company {company_id}",
        "state": "PA",
        "source": "next_data",
        "name": name,
        "address": "1 Main St",
    }
    fields.update(overrides)
    return CompanyStoreRecord(**fields)


def test_menu_stores_query_filters_and_dedupes_handles() -> None:
    conn = _conn()
    db.insert_company_store(conn, _store(1, "Has handle", platform="sweedpos", external_id="42"))
    db.insert_company_store(conn, _store(1, "No handle"))  # platform/external_id NULL
    db.insert_company_store(conn, _store(2, "Same handle", platform="sweedpos", external_id="42"))
    db.insert_company_store(conn, _store(3, "Other state", platform="jane", external_id="7", state="NY"))
    # A second CANONICAL operator on the same physical storefront — DISTINCT ON
    # the handle must collapse it into one menu target.
    db.insert_company_store(conn, _store(4, "Alias storefront", platform="sweedpos", external_id="42"))
    conn.commit()
    # A shared-brand duplicate row (canonical_company_id set) is excluded.
    conn.execute(
        "UPDATE company_stores SET canonical_company_id = 1 WHERE company_id = 2"
    )
    conn.commit()

    rows = db.get_menu_stores_for_state(conn, "PA")
    assert len(rows) == 1
    (company_id, _name, source, platform, external_id, _store_url, _store_name,
     address, _city) = rows[0]
    assert (company_id, source, platform, external_id) == (1, "next_data", "sweedpos", "42")
    assert address == "1 Main St"
