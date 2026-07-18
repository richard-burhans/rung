"""Direct coverage for db.py helpers exercised only indirectly elsewhere.

Uses the real conftest Postgres (a throwaway schema per test) with real record
dataclasses — no mocked DB, no crafted external payloads.
"""

import datetime

import psycopg
import pytest
from conftest import _TEST_URL, pg_conn

from rung import db, seed_companies
from rung.models import (
    CompanyReconRecord,
    CompanyStoreRecord,
    StateProgramRecord,
    StoreProductRecord,
)


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)  # db.create_tables omits companies
    return conn


def test_one_returns_the_single_row_and_raises_when_empty():
    import pytest
    conn = _conn()
    assert db.one(conn, "SELECT 1, 2")[0] == 1                 # non-None, subscriptable
    assert db.one(conn, "SELECT count(*) FROM company_stores")[0] == 0
    with pytest.raises(LookupError):
        db.one(conn, "SELECT 1 WHERE false")                  # no row -> raises


def _store(company_id=1, name="Acme", *, platform=None, external_id=None, **kw):
    return CompanyStoreRecord(
        company_id=company_id, canonical_name="Acme", state="PA",
        source="x", name=name, platform=platform, external_id=external_id, **kw,
    )


# ── indexes ───────────────────────────────────────────────────────────────────

def test_create_tables_builds_the_per_state_read_indexes() -> None:
    # The per-state hot paths (Stage-2 keep-the-best replace on company_stores, the Stage-3
    # freshness group on the 4.8M-row store_products) need these composite indexes to avoid a
    # full table scan; create_tables must build them.
    conn = _conn()
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
        ).fetchall()
    }
    assert "company_stores_state_company" in indexes
    assert "store_products_state_store_key" in indexes
    assert "store_products_store_key" in indexes  # the per-store lookup index still present


def test_create_tables_builds_the_partial_pending_claim_index() -> None:
    # The FOR UPDATE SKIP LOCKED claim scans WHERE task_type=… AND status='pending' ORDER BY id;
    # a partial index over just the pending rows keeps it index-only as done/failed history grows.
    conn = _conn()
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = current_schema() AND indexname = 'jobs_pending_claim'"
    ).fetchone()
    assert row is not None                              # the index exists
    assert "status" in row[0] and "'pending'" in row[0]  # …and it is the partial (pending-only) one


# ── snapshot freshness ────────────────────────────────────────────────────────

def test_products_normalized_derives_currency_from_country() -> None:
    conn = _conn()
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="ON", name="Ontario", programs="recreational", program_term="cannabis",
        agency="AGCO", country="CA"))
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="PA", name="Pennsylvania", programs="medical", program_term="medical",
        agency="DOH"))  # country defaults to US
    for state in ("ON", "PA"):
        db.insert_store_product(conn, StoreProductRecord(
            company_id=1, state=state, store_key="s", platform="p",
            external_id="1", source="s", name="x", price=10.0))
    conn.commit()
    cur = dict(conn.execute(
        "SELECT state, currency FROM products_normalized WHERE state = ANY(%s)",
        (["ON", "PA"],)).fetchall())
    assert cur == {"ON": "CAD", "PA": "USD"}


def test_products_normalized_currency_defaults_usd_for_unregistered_state() -> None:
    # A store_products row whose state has no state_programs row (e.g. a bootstrap-only
    # jurisdiction) still gets a currency — the LEFT JOIN yields NULL country → USD.
    conn = _conn()
    db.insert_store_product(conn, StoreProductRecord(
        company_id=1, state="ZZ", store_key="s", platform="p",
        external_id="1", source="s", name="x", price=5.0))
    conn.commit()
    row = conn.execute(
        "SELECT currency FROM products_normalized WHERE state = 'ZZ'").fetchone()
    assert row[0] == "USD"


def test_latest_snapshot_times_returns_max_per_store_key() -> None:
    conn = _conn()
    for store_key in ("A", "A", "B"):
        db.insert_store_product(conn, StoreProductRecord(
            company_id=1, state="PA", store_key=store_key, platform="p",
            external_id="1", source="s", name="x"))
    conn.commit()
    # Force distinct timestamps: store A has an old and a new row; B a single row.
    conn.execute("UPDATE store_products SET scraped_at = '2026-01-01T00:00:00+00:00' "
                 "WHERE store_key = 'A'")
    conn.execute("UPDATE store_products SET scraped_at = '2026-06-01T00:00:00+00:00' "
                 "WHERE store_key = 'A' AND ctid = (SELECT ctid FROM store_products "
                 "WHERE store_key = 'A' LIMIT 1)")
    conn.execute("UPDATE store_products SET scraped_at = '2026-03-15T00:00:00+00:00' "
                 "WHERE store_key = 'B'")
    conn.commit()
    times = db.latest_snapshot_times(conn, "PA")
    assert times["A"] == datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)   # max of A's two rows
    assert times["B"] == datetime.datetime(2026, 3, 15, tzinfo=datetime.UTC)
    assert db.latest_snapshot_times(conn, "ZZ") == {}   # no rows for another state


def test_latest_snapshot_times_max_is_chronological_not_lexical() -> None:
    # The bug the timestamptz cast fixes: a LATER instant whose ISO string sorts EARLIER lexically
    # (a non-UTC offset) must still win the max. "20:00+00:00" (20:00Z) is later than
    # "23:00+05:00" (18:00Z) but sorts lexically smaller — a string max would pick the wrong one.
    conn = _conn()
    for _ in range(2):
        db.insert_store_product(conn, StoreProductRecord(
            company_id=1, state="PA", store_key="A", platform="p",
            external_id="1", source="s", name="x"))
    conn.commit()
    ctids = [r[0] for r in conn.execute("SELECT ctid FROM store_products").fetchall()]
    conn.execute("UPDATE store_products SET scraped_at='2026-01-01T23:00:00+05:00' WHERE ctid=%s",
                 (ctids[0],))  # 18:00Z — lexically larger
    conn.execute("UPDATE store_products SET scraped_at='2026-01-01T20:00:00+00:00' WHERE ctid=%s",
                 (ctids[1],))  # 20:00Z — the true chronological max
    conn.commit()
    times = db.latest_snapshot_times(conn, "PA")
    assert times["A"] == datetime.datetime(2026, 1, 1, 20, 0, tzinfo=datetime.UTC)


# ── recon companies for a state ───────────────────────────────────────────────

def test_get_recon_companies_coalesces_homeless_homepage() -> None:
    conn = _conn()
    for cid, name, state in [(1, "Curaleaf", "PA"), (2, "RISE", "PA"), (3, "Other", "NJ")]:
        conn.execute("INSERT INTO companies (id, canonical_name, state, created_at) "
                     "VALUES (%s, %s, %s, 'now')", (cid, name, state))
    db.upsert_recon(conn, CompanyReconRecord(
        company_id=1, canonical_name="Curaleaf", homepage_url="https://curaleaf.test",
        platform="sweedpos"))
    db.upsert_recon(conn, CompanyReconRecord(company_id=2, canonical_name="RISE"))  # homeless
    db.upsert_recon(conn, CompanyReconRecord(
        company_id=3, canonical_name="Other", homepage_url="https://nj.test"))
    conn.commit()
    rows = db.get_recon_companies_for_state(conn, "PA")
    assert rows == [
        (1, "Curaleaf", "https://curaleaf.test", "sweedpos"),
        (2, "RISE", "", None),   # homeless: homepage coalesced to '', not NULL
    ]   # NJ company excluded; ordered by canonical_name


# ── dedupe markers ────────────────────────────────────────────────────────────

def test_canonical_marker_hides_store_from_menu_jobs_and_clear_restores() -> None:
    conn = _conn()
    db.insert_company_store(conn, _store(name="Main", platform="jane", external_id="11"))
    db.insert_company_store(conn, _store(name="Dup", platform="jane", external_id="22"))
    conn.commit()
    assert len(db.get_menu_stores_for_state(conn, "PA")) == 2
    dup_id = next(r[0] for r in db.get_company_stores_for_dedupe(conn, "PA") if r[3] == "Dup")
    db.set_store_canonical(conn, dup_id, 1)   # fold Dup into the canonical company
    conn.commit()
    # canonical_company_id IS NOT NULL → excluded from menu jobs.
    assert {r[6] for r in db.get_menu_stores_for_state(conn, "PA")} == {"Main"}
    db.clear_store_canonical_for_state(conn, "PA")
    conn.commit()
    assert len(db.get_menu_stores_for_state(conn, "PA")) == 2


def test_set_store_storefront_stamps_display_brand() -> None:
    conn = _conn()
    db.insert_company_store(conn, _store(name="Legal Entity LLC"))
    conn.commit()
    store_id = db.get_company_stores_for_dedupe(conn, "PA")[0][0]
    db.set_store_storefront(conn, store_id, "Sunnyside")
    conn.commit()
    row = conn.execute(
        "SELECT storefront_name FROM company_stores WHERE id = %s", (store_id,)
    ).fetchone()
    assert row[0] == "Sunnyside"


# ── per-company store deletion ────────────────────────────────────────────────

def test_delete_company_stores_for_company_is_scoped() -> None:
    conn = _conn()
    db.insert_company_store(conn, _store(company_id=1, name="C1a"))
    db.insert_company_store(conn, _store(company_id=1, name="C1b"))
    db.insert_company_store(conn, _store(company_id=2, name="C2a"))
    conn.commit()
    deleted = db.delete_company_stores_for_company(conn, 1, "PA")
    conn.commit()
    assert deleted == 2
    assert db.count_company_stores(conn, 1, "PA") == 0
    assert db.count_company_stores(conn, 2, "PA") == 1   # other company untouched


# ── keep-the-best replace (quality-aware: menu handles beat aggregator listings) ──

def _agg(name, ext):   # an aggregator-only handle (Weedmaps/Leafly directory listing)
    return _store(name=name, platform="leafly", external_id=ext)


def _menu(name, ext):  # a real-menu-rung handle (Jane/Dutchie/Sweed/…)
    return _store(name=name, platform="jane", external_id=ext)


def test_replace_prefers_menu_handles_over_more_aggregator_stores() -> None:
    """Terra Pharm case: 3 real Jane handles must beat 4 stored empty Leafly handles."""
    conn = _conn()
    for i in range(4):
        db.insert_company_store(conn, _agg(f"Agg{i}", f"L{i}"))
    conn.commit()
    n, kept = db.replace_company_stores(conn, 1, "PA", [_menu(f"Jane{i}", f"J{i}") for i in range(3)])
    conn.commit()
    assert kept is False and n == 3
    platforms = {r[0] for r in conn.execute(
        "SELECT platform FROM company_stores WHERE company_id=1 AND state='PA'").fetchall()}
    assert platforms == {"jane"}   # aggregator rows replaced by the real-menu handles


def test_replace_does_not_downgrade_menu_handles_to_aggregator() -> None:
    """A larger aggregator scrape must NOT clobber a smaller real-menu set."""
    conn = _conn()
    for i in range(3):
        db.insert_company_store(conn, _menu(f"Jane{i}", f"J{i}"))
    conn.commit()
    n, kept = db.replace_company_stores(conn, 1, "PA", [_agg(f"Agg{i}", f"L{i}") for i in range(5)])
    conn.commit()
    assert kept is True and n == 3   # kept the 3 Jane stores despite 5 aggregator candidates
    platforms = {r[0] for r in conn.execute(
        "SELECT platform FROM company_stores WHERE company_id=1 AND state='PA'").fetchall()}
    assert platforms == {"jane"}


def test_replace_equal_menu_count_falls_back_to_distinct_count() -> None:
    """Same menu-bearing count (here zero) → the distinct-count rule decides (more wins)."""
    conn = _conn()
    for i in range(2):
        db.insert_company_store(conn, _agg(f"Agg{i}", f"L{i}"))
    conn.commit()
    n, kept = db.replace_company_stores(conn, 1, "PA", [_agg(f"New{i}", f"N{i}") for i in range(3)])
    conn.commit()
    assert kept is False and n == 3   # 0==0 menu → count logic: 3 >= 2 overwrites


def _bare(name):  # a bare-address store: no menu platform, no external_id handle
    return _store(name=name)


def test_replace_menu_gain_accepted_at_half_retention_boundary() -> None:
    """Gaining menu handles wins at exactly _MENU_UPGRADE_RETENTION (0.5): 4 agg → 2 Jane."""
    conn = _conn()
    for i in range(4):
        db.insert_company_store(conn, _agg(f"Agg{i}", f"L{i}"))   # 0 menu handles
    conn.commit()
    n, kept = db.replace_company_stores(conn, 1, "PA", [_menu(f"Jane{i}", f"J{i}") for i in range(2)])
    conn.commit()
    # new_menu 2 > existing_menu 0 → accept iff new_distinct(2) >= existing_distinct(4) * 0.5 == 2.0
    assert kept is False and n == 2
    platforms = {r[0] for r in conn.execute(
        "SELECT platform FROM company_stores WHERE company_id=1 AND state='PA'").fetchall()}
    assert platforms == {"jane"}


def test_replace_rejects_menu_gain_that_collapses_below_half_retention() -> None:
    """A 15→1 menu-gaining collapse is still rejected (drops below _MENU_UPGRADE_RETENTION)."""
    conn = _conn()
    for i in range(15):
        db.insert_company_store(conn, _agg(f"Agg{i}", f"L{i}"))
    conn.commit()
    n, kept = db.replace_company_stores(conn, 1, "PA", [_menu("Jane0", "J0")])
    conn.commit()
    # new_menu 1 > existing_menu 0 but new_distinct(1) < existing_distinct(15) * 0.5 == 7.5 → reject
    assert kept is True and n == 15
    platforms = {r[0] for r in conn.execute(
        "SELECT platform FROM company_stores WHERE company_id=1 AND state='PA'").fetchall()}
    assert platforms == {"leafly"}   # the lone Jane handle did NOT clobber 15 listings


def test_replace_upgrades_bare_addresses_to_handles_at_080_retention() -> None:
    """Equal (zero) menu count: a bare-address set is upgraded to handle-bearing rows at ≥0.8."""
    conn = _conn()
    for i in range(5):
        db.insert_company_store(conn, _bare(f"Store{i}"))   # no external_id handles
    conn.commit()
    n, kept = db.replace_company_stores(conn, 1, "PA", [_agg(f"Agg{i}", f"L{i}") for i in range(4)])
    conn.commit()
    # 0==0 menu, new_distinct(4) < existing(5) → handle-upgrade: existing_handles 0, new_handles 4,
    # 4 >= 5 * _HANDLE_UPGRADE_RETENTION(0.8) == 4.0 → accept
    assert kept is False and n == 4
    handles = {r[0] for r in conn.execute(
        "SELECT external_id FROM company_stores WHERE company_id=1 AND state='PA'").fetchall()}
    assert handles == {"L0", "L1", "L2", "L3"}


def test_replace_rejects_handle_upgrade_below_080_retention() -> None:
    """A handle upgrade that loses >20% of the stores is rejected (below _HANDLE_UPGRADE_RETENTION)."""
    conn = _conn()
    for i in range(5):
        db.insert_company_store(conn, _bare(f"Store{i}"))
    conn.commit()
    n, kept = db.replace_company_stores(conn, 1, "PA", [_agg(f"Agg{i}", f"L{i}") for i in range(3)])
    conn.commit()
    # 0==0 menu, new_distinct(3) < existing(5) * 0.8 == 4.0 → reject; bare addresses retained
    assert kept is True and n == 5
    handles = {r[0] for r in conn.execute(
        "SELECT external_id FROM company_stores WHERE company_id=1 AND state='PA'").fetchall()}
    assert handles == {None}   # still the bare-address rows, no handles


# ── state programs round-trip ─────────────────────────────────────────────────

def test_get_all_state_programs_round_trips_ordered_by_name() -> None:
    conn = _conn()
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="PA", name="Pennsylvania", programs="medical", program_term="medical",
        agency="DOH", best_url="https://pa.gov", source_type="pdf"))
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="AZ", name="Arizona", programs="both", program_term="adult-use",
        agency="ADHS"))
    conn.commit()
    programs = db.get_all_state_programs(conn)
    assert [p.abbr for p in programs] == ["AZ", "PA"]   # ordered by name
    pa = next(p for p in programs if p.abbr == "PA")
    assert pa.programs == "medical" and pa.best_url == "https://pa.gov"
    assert pa.source_type == "pdf"


def test_state_program_country_round_trips_and_defaults_to_us() -> None:
    conn = _conn()
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="ON", name="Ontario", programs="recreational", program_term="cannabis",
        agency="AGCO", country="CA"))
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="PA", name="Pennsylvania", programs="medical", program_term="medical",
        agency="DOH"))
    conn.commit()
    assert db.get_state_program(conn, "ON").country == "CA"
    assert db.get_state_program(conn, "PA").country == "US"   # default when unspecified


def test_create_engine_tables_builds_only_the_generic_infra() -> None:
    # Genericization Workstream B2: create_engine_tables() makes the domain-neutral engine tables and
    # NONE of the cannabis reference tables — the contract a build-your-own-domain plugin relies on
    # (docs/build-your-own-domain.md). A fresh throwaway schema so nothing pre-exists.
    conn = pg_conn()
    db.create_engine_tables(conn)
    for table in ("jobs", "access_methods", "token_buckets", "proxies", "proxy_tiers"):
        assert db.one(conn, "SELECT to_regclass(%s) IS NOT NULL", (table,))[0], f"{table} should exist"
    for table in ("dispensaries", "company_stores", "store_products", "products",
                  "product_observations", "state_programs"):
        assert not db.one(conn, "SELECT to_regclass(%s) IS NOT NULL", (table,))[0], \
            f"{table} must NOT be created by create_engine_tables"


def test_natural_flower_predicates_stay_in_sync() -> None:
    # Two spellings of one rule: NATURAL_FLOWER_WHERE targets `store_products`, the _NORMALIZED variant
    # targets the `products_normalized` VIEW, which renames category_std/product_type_std. If someone
    # edits the adulteration name-tells in one, the other must follow — a drift here would silently let
    # infused flower back into the price/chemovar analyses that use the view.
    plain = db.NATURAL_FLOWER_WHERE
    view = db.NATURAL_FLOWER_WHERE_NORMALIZED
    assert plain.replace("category_std", "category").replace("product_type_std", "product_type") == view


def _view_output_columns(create_view_sql: str) -> list[tuple[str, str]]:
    """The ordered ``(output_name, normalized_source_expression)`` pairs of a
    ``CREATE VIEW … AS SELECT <list> FROM …`` statement.

    Paren-aware split on top-level commas (so `round(x, 2)` and a CASE don't fool it). Each item's
    output name is its `AS <alias>`, else the last dotted/word token; the expression is the item minus
    that trailing alias, whitespace-collapsed and with `::<type>` dialect casts dropped (so the Postgres
    `::numeric` and its DuckDB `::double` twin compare equal). Comparing the *expression*, not just the
    name, is what catches a same-name/different-source divergence (e.g. one view repointing `price` to a
    different underlying column while keeping the alias). Comments are stripped first."""
    import re

    m = re.search(r"\bSELECT\b(.*?)\bFROM\b", create_view_sql, re.S | re.I)
    assert m is not None, "no top-level `SELECT … FROM` found in the view SQL"
    body = re.sub(r"--[^\n]*", "", m.group(1))  # strip line comments FIRST: they carry stray commas/parens
    items: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        items.append("".join(cur))
    cols: list[tuple[str, str]] = []
    for item in items:
        clean = item.strip()
        alias = re.search(r"\bAS\s+(\w+)\s*$", clean, re.I)
        if alias:
            name, expr = alias.group(1), clean[: alias.start()]
        else:
            name, expr = re.split(r"[\s.]+", clean)[-1], clean
        expr = re.sub(r"::\w+", "", expr)         # drop dialect casts: ::numeric ≡ ::double
        expr = re.sub(r"\s+", " ", expr).strip()  # collapse whitespace
        cols.append((name.lower(), expr))
    return cols


def test_products_normalized_views_stay_in_sync() -> None:
    # Two spellings of one view: reference_db builds `products_normalized` over Postgres; static_source
    # rebuilds it over DuckDB (the Galaxy / outside-researcher path that runs the D1 scripts off a frozen
    # Parquet). Only the dialect differs (::numeric vs ::double, the local currency CASE). If a column is
    # added, dropped, reordered, or repointed to a different source column in one but not the other, the
    # static export silently diverges from live Postgres — and the import-layering guard can't see it (it
    # inspects imports, not SQL). Pin the output NAME + normalized source EXPRESSION of every column equal.
    from rung import reference_db, static_source

    assert (_view_output_columns(reference_db._CREATE_PRODUCTS_NORMALIZED_VIEW)
            == _view_output_columns(static_source._PRODUCTS_NORMALIZED_VIEW_SQL))


def test_country_subqueries_partition_state_programs() -> None:
    # `state = 'CA'` is California; Canada is `country = 'CA'`. Guard the trap: the two subqueries must
    # be disjoint and, together, cover every seeded program row.
    conn = pg_conn()
    db.create_reference_tables(conn)
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="CA", name="California", programs="both", program_term="cannabis", agency="DCC"))
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="ON", name="Ontario", programs="both", program_term="cannabis", agency="AGCO",
        country="CA"))
    conn.commit()
    us = {r[0] for r in conn.execute(f"SELECT abbr FROM state_programs WHERE abbr IN {db.US_JURISDICTIONS_SUBQUERY}")}
    ca = {r[0] for r in conn.execute(f"SELECT abbr FROM state_programs WHERE abbr IN {db.CA_PROVINCES_SUBQUERY}")}
    assert "CA" in us and "CA" not in ca, "California must be a US state, not a Canadian province"
    assert ca == {"ON"}
    assert not (us & ca)


def test_us_subqueries_differ_by_exactly_the_declared_territories() -> None:
    """Two live scripts spelled "US" two different ways: `_scope.predicate("us")` INCLUDED Puerto
    Rico, `_terpene_source.JURISDICTIONS["USA"]` EXCLUDED it. Both are now named constants, and this
    pins the only difference between them to `US_TERRITORIES`, so adding a territory to that tuple
    without updating the SQL fails the build rather than a downstream number.
    """
    conn = pg_conn()
    db.create_reference_tables(conn)
    for abbr, name in (("CA", "California"), ("DC", "District of Columbia"), ("PR", "Puerto Rico")):
        db.upsert_state_program(conn, StateProgramRecord(
            abbr=abbr, name=name, programs="both", program_term="cannabis", agency="x"))
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="ON", name="Ontario", programs="both", program_term="cannabis", agency="x", country="CA"))
    conn.commit()

    def abbrs(subquery: str) -> set[str]:
        return {r[0] for r in conn.execute(f"SELECT abbr FROM state_programs WHERE abbr IN {subquery}")}

    everything = abbrs(db.US_JURISDICTIONS_SUBQUERY)
    states_only = abbrs(db.US_EXCL_TERRITORIES_SUBQUERY)

    assert everything - states_only == set(db.US_TERRITORIES)
    assert "PR" in everything, "the price guard must keep PR: it is a US territory pricing in USD"
    assert "DC" in states_only, "DC is not a territory — holding it out would be a different bug"
    assert "ON" not in everything


# ── apply_and_verify: a migration must confirm itself on a FRESH connection (incident 5) ──────────────
#
# `refold_companies` reported "APPLIED: 9 companies merged", a query in its OWN session confirmed it (it
# was reading its own uncommitted work), and a fresh connection still saw all 15 — the `with
# conn.transaction()` block had committed nothing. These pin the helper that encodes the fix.

def _fresh_into(schema: str) -> db.DBConn:
    """A brand-new backend into `schema` — unregistered, so apply_and_verify owns and closes it."""
    conn = psycopg.connect(_TEST_URL)
    conn.execute(f"SET search_path TO {schema}")
    return conn


def test_apply_and_verify_confirms_a_committed_change_on_a_fresh_connection():
    conn = pg_conn()
    conn.execute("CREATE TABLE widget (id int)")
    conn.commit()
    schema = conn.execute("SELECT current_schema()").fetchone()[0]

    got = db.apply_and_verify(
        conn,
        apply=lambda c: c.execute("INSERT INTO widget VALUES (1), (2), (3)"),
        verify=lambda c: c.execute("SELECT count(*) FROM widget").fetchone()[0],
        expected=3,
        connect=lambda: _fresh_into(schema),
    )
    assert got == 3
    # and it really committed: a brand-new connection still sees the rows
    other = _fresh_into(schema)
    assert other.execute("SELECT count(*) FROM widget").fetchone()[0] == 3
    other.close()


def test_apply_and_verify_raises_when_the_fresh_read_disagrees():
    conn = pg_conn()
    conn.execute("CREATE TABLE widget (id int)")
    conn.commit()
    schema = conn.execute("SELECT current_schema()").fetchone()[0]

    with pytest.raises(db.VerificationError, match="did not land"):
        db.apply_and_verify(
            conn,
            apply=lambda c: c.execute("INSERT INTO widget VALUES (1)"),
            verify=lambda c: c.execute("SELECT count(*) FROM widget").fetchone()[0],
            expected=99,                               # the migration claims 99; the DB says 1
            connect=lambda: _fresh_into(schema),
        )


def test_a_fresh_connection_cannot_see_uncommitted_work():
    """Why the verify must run on a NEW connection: uncommitted work is visible to its own session
    (which fooled incident 5's same-session confirm) but invisible to a fresh connection."""
    conn = pg_conn()
    conn.execute("CREATE TABLE widget (id int)")
    conn.commit()
    schema = conn.execute("SELECT current_schema()").fetchone()[0]

    conn.execute("INSERT INTO widget VALUES (1), (2)")          # NOT committed
    assert conn.execute("SELECT count(*) FROM widget").fetchone()[0] == 2   # same session: sees its own work
    other = _fresh_into(schema)
    assert other.execute("SELECT count(*) FROM widget").fetchone()[0] == 0  # fresh connection: the truth
    other.close()
