"""Tests for the Stage-3 store_products snapshot persistence: wholesale replace,
the empty-result guard, and the menu-target query over company_stores."""

import datetime

import psycopg
import pytest
from conftest import pg_conn

from rung import db, reference_db
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
    _age_snapshot(conn, "sweedpos:42", reference_db._MENU_RETAIN_MAX_AGE_HOURS + 1)
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
     address, _city, _menu_type) = rows[0]
    assert (company_id, source, platform, external_id) == (1, "next_data", "sweedpos", "42")
    assert address == "1 Main St"


# ── `retained_since`: a KEPT snapshot must say so ────────────────────────────────────────────────
#
# Keeping is right (a transient failure must not wipe a good menu); keeping SILENTLY is what let a
# Dutchie store fail 68 consecutive times since June and still publish its June menu in September,
# with `store_products` carrying no way for any reader to tell.


def _retained(conn: db.DBConn, store_key: str) -> list:
    return [
        row[0] for row in conn.execute(
            "SELECT retained_since FROM store_products WHERE store_key = %s", (store_key,)
        ).fetchall()
    ]


def test_a_real_scrape_leaves_the_snapshot_unstamped() -> None:
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product("A"), _product("B")])
    conn.commit()
    assert _retained(conn, "sweedpos:42") == [None, None]


def test_an_empty_result_stamps_the_snapshot_it_kept() -> None:
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product("A"), _product("B")])
    conn.commit()
    kept = datetime.datetime(2026, 6, 18, tzinfo=datetime.UTC)
    assert db.replace_store_products(conn, "sweedpos:42", [], now=kept) == 2
    conn.commit()
    assert _retained(conn, "sweedpos:42") == [kept, kept]


def test_the_stamp_is_the_FIRST_retention_not_the_latest_attempt() -> None:
    """A store failing for three months must not look freshly kept every morning.

    `COALESCE` is what makes the column answer "how long has this gone unrenewed" rather than
    "when did we last try", which is already in `access_methods` and is a different question.
    """
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product("A")])
    conn.commit()
    june = datetime.datetime(2026, 6, 18, tzinfo=datetime.UTC)
    for day in range(3):
        db.replace_store_products(
            conn, "sweedpos:42", [], now=june + datetime.timedelta(days=day))
    conn.commit()
    assert _retained(conn, "sweedpos:42") == [june]


def test_a_successful_rescrape_clears_the_stamp() -> None:
    """Implicitly — the replace DELETEs the kept rows and the new ones default NULL."""
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product("A")])
    db.replace_store_products(conn, "sweedpos:42", [], now=datetime.datetime(2026, 6, 18, tzinfo=datetime.UTC))
    conn.commit()
    assert _retained(conn, "sweedpos:42") != [None]

    db.replace_store_products(conn, "sweedpos:42", [_product("Fresh")])
    conn.commit()
    assert _retained(conn, "sweedpos:42") == [None]


def test_a_snapshot_kept_on_empty_results_past_the_ceiling_is_dropped() -> None:
    """The stamp alone was not enough: nothing read it, and 104 stores visited daily carried a
    June–September menu as current on 2026-10-06. At the ceiling the empty result wins."""
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product("A"), _product("B")])
    conn.commit()
    june = datetime.datetime(2026, 6, 18, tzinfo=datetime.UTC)
    # Day 0 and day 29: kept, stamped once.
    assert db.replace_store_products(conn, "sweedpos:42", [], now=june) == 2
    assert db.replace_store_products(
        conn, "sweedpos:42", [], now=june + datetime.timedelta(days=29)) == 2
    conn.commit()
    assert _retained(conn, "sweedpos:42") == [june, june]
    # Day 30: the ceiling. The snapshot goes; the return says so.
    assert db.replace_store_products(
        conn, "sweedpos:42", [], now=june + datetime.timedelta(days=30)) == 0
    conn.commit()
    assert db.count_store_products(conn, "sweedpos:42") == 0
    # A further empty result on a store with no snapshot is a no-op, not an error.
    assert db.replace_store_products(
        conn, "sweedpos:42", [], now=june + datetime.timedelta(days=31)) == 0
    # And a store that answers again gets a fresh, unstamped snapshot the day it does.
    assert db.replace_store_products(conn, "sweedpos:42", [_product("Back")]) == 1
    conn.commit()
    assert _retained(conn, "sweedpos:42") == [None]


def test_the_ceiling_counts_from_the_first_retention_not_the_snapshot_age() -> None:
    """A menu scraped in June and first kept in September is 0 days retained in September: the
    clock is "how long has this store answered empty", which is what `retained_since` holds."""
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product("A")])
    conn.commit()
    sept = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
    assert db.replace_store_products(conn, "sweedpos:42", [], now=sept) == 1
    assert db.replace_store_products(
        conn, "sweedpos:42", [], now=sept + datetime.timedelta(days=29, hours=23)) == 1
    assert db.replace_store_products(
        conn, "sweedpos:42", [], now=sept + datetime.timedelta(days=30)) == 0


def test_the_partial_fetch_guard_also_stamps_what_it_kept() -> None:
    """The throttled-partial branch keeps a snapshot too, and was equally silent about it."""
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product(f"P{i}") for i in range(10)])
    conn.commit()
    kept = datetime.datetime(2026, 6, 18, tzinfo=datetime.UTC)
    # 1 record against a fresh prior of 10 is under _MENU_RETAIN_FRACTION — read as a 406 fragment.
    assert db.replace_store_products(conn, "sweedpos:42", [_product("fragment")], now=kept) == 10
    conn.commit()
    assert set(_retained(conn, "sweedpos:42")) == {kept}


def test_stamping_one_store_does_not_touch_another() -> None:
    conn = _conn()
    db.replace_store_products(conn, "sweedpos:42", [_product("A")])
    other = _product("Other", store_key="jane:7", platform="jane", external_id="7")
    db.replace_store_products(conn, "jane:7", [other])
    conn.commit()
    db.replace_store_products(conn, "sweedpos:42", [], now=datetime.datetime(2026, 6, 18, tzinfo=datetime.UTC))
    conn.commit()
    assert _retained(conn, "jane:7") == [None]


def test_the_view_exposes_the_retention_stamp_and_the_guard_excludes_a_kept_snapshot() -> None:
    """Until 2026-10-06 `products_normalized` carried no `retained_since`, so an analysis reading the
    view counted a June menu kept on empty results as today's. The view now says, and
    `current_snapshot_where()` reads identically against the view and the table."""
    from rung import reference_db

    conn = _conn()
    conn.execute(reference_db._CREATE_PRODUCTS_NORMALIZED_VIEW)
    db.replace_store_products(conn, "sweedpos:42", [_product("A"), _product("B")])
    conn.commit()
    assert conn.execute(
        f"SELECT count(*) FROM products_normalized WHERE {reference_db.current_snapshot_where()}"
    ).fetchone()[0] == 2
    db.replace_store_products(conn, "sweedpos:42", [], now=datetime.datetime(2026, 6, 18, tzinfo=datetime.UTC))
    conn.commit()
    assert conn.execute("SELECT count(*) FROM products_normalized").fetchone()[0] == 2
    assert conn.execute(
        f"SELECT count(*) FROM products_normalized WHERE {reference_db.current_snapshot_where('products_normalized')}"
    ).fetchone()[0] == 0
    assert conn.execute(
        f"SELECT count(*) FROM store_products sp WHERE {reference_db.current_snapshot_where('sp')}"
    ).fetchone()[0] == 0
    stamps = {r[0] for r in conn.execute("SELECT retained_since FROM products_normalized").fetchall()}
    assert stamps == {datetime.datetime(2026, 6, 18, tzinfo=datetime.UTC)}
