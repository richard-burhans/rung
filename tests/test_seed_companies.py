"""Tests for the companies-table seeding (per-state uniqueness + idempotency)."""

from conftest import pg_conn

from rung import db, seed_companies


def _conn() -> db.DBConn:
    conn = pg_conn()
    seed_companies.create_companies_table(conn)
    return conn


def test_company_unique_per_state_not_global():
    # A multi-state operator gets one row per state (not collapsed to one).
    conn = _conn()
    inserted, skipped = seed_companies._seed(
        conn, [("Curaleaf", "PA"), ("Curaleaf", "FL"), ("RISE", "PA")]
    )
    assert len(inserted) == 3 and skipped == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM companies WHERE canonical_name = 'Curaleaf'"
    ).fetchone()[0] == 2


def test_seed_is_idempotent():
    conn = _conn()
    seed_companies._seed(conn, [("Curaleaf", "PA")])
    inserted, skipped = seed_companies._seed(conn, [("Curaleaf", "PA")])
    assert inserted == [] and skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


def test_collect_folds_spelling_variants_to_dominant_spelling():
    # Spelling/spacing/case variants of one brand (that companies.yml doesn't alias) collapse
    # to ONE company under the most frequent spelling — the IL Cloud 9 / Cloud9 case.
    from rung.models import DispensaryRecord
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)
    for name in ("Cloud 9", "Cloud 9 Cannabis", "Cloud9 Cannabis", "EarthMed", "Earthmed"):
        db.insert_dispensary(conn, DispensaryRecord(source="html", name=name, state="IL"))
    conn.commit()
    pairs = seed_companies._collect_brand_state_pairs(conn, {})
    brands = sorted(b for b, _ in pairs)
    assert brands == ["Cloud 9", "Earthmed"]  # one per normalized group; dominant spelling
    # "Cloud 9" wins on frequency (2 of 3 after stripping "Cannabis"); EarthMed/Earthmed tie
    # → deterministic tie-break picks one.
    assert all(state == "IL" for _, state in pairs)


def test_collect_folds_storefront_city_suffix_to_one_operator():
    # A multi-store operator's per-city storefront names fold to ONE company via the row's city.
    from rung.models import DispensaryRecord
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)
    for city in ("Dayton", "Cincinnati", "Canton"):
        db.insert_dispensary(conn, DispensaryRecord(
            source="html", name=f"Zen Leaf {city}", city=city, state="OH"))
    db.insert_dispensary(conn, DispensaryRecord(
        source="html", name="Harvest of Whitehall", city="Whitehall", state="OH"))  # connector → kept
    conn.commit()
    brands = sorted(b for b, _ in seed_companies._collect_brand_state_pairs(conn, {}))
    assert brands == ["Harvest of Whitehall", "Zen Leaf"]  # 3 Zen Leaf stores → one operator


def test_companies_yml_folds_harvest_into_trulieve():
    # Trulieve acquired Harvest HOC: the PA roster's legacy "Harvest of Whitehall" licensee and
    # Trulieve's own stores are the same physical stores. The companies.yml alias folds them to
    # ONE operator so the duplicate licensee doesn't seed a separate (menu-less) company.
    from rung.models import DispensaryRecord
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)
    db.insert_dispensary(conn, DispensaryRecord(
        source="csv", name="Harvest of Whitehall", city="Whitehall", state="PA"))
    db.insert_dispensary(conn, DispensaryRecord(
        source="html", name="Trulieve - Camp Hill", city="Camp Hill", state="PA"))
    conn.commit()
    alias_map = seed_companies.load_company_aliases(seed_companies._YAML_PATH, strict=True)
    pairs = seed_companies._collect_brand_state_pairs(conn, alias_map)
    assert sorted(pairs) == [("Trulieve", "PA")]


def test_companies_yml_folds_dankley_legal_entities():
    # One NY operator listed under its brand (Dankley / DANKLEY) and two legal entities
    # (Dankley LLC, Diamond Star Group inc.) for the same physical stores. The companies.yml
    # alias folds all spelling/entity variants to the brand so they seed as ONE operator.
    from rung.models import DispensaryRecord
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)
    for name in ("Dankley", "DANKLEY", "Dankley LLC", "Diamond Star Group inc."):
        db.insert_dispensary(conn, DispensaryRecord(source="html", name=name, state="NY"))
    conn.commit()
    alias_map = seed_companies.load_company_aliases(seed_companies._YAML_PATH, strict=True)
    pairs = seed_companies._collect_brand_state_pairs(conn, alias_map)
    assert sorted(pairs) == [("Dankley", "NY")]


def test_companies_yml_folds_cresco_entities_into_sunnyside():
    # Cresco's PA stores are licensed under several permit entities (Delta 9 Pittsburgh, Keystone
    # Integrated Care) but all operate as the Sunnyside retail brand. The companies.yml alias folds
    # them to ONE operator so they don't each seed a company and re-scrape the whole Sunnyside list
    # (which triplicated the locations). The CA "Delta 9 THC" operator must NOT be caught.
    from rung.models import DispensaryRecord
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)
    db.insert_dispensary(conn, DispensaryRecord(
        source="csv", name="Delta 9 Pittsburgh", city="Pittsburgh", state="PA"))
    db.insert_dispensary(conn, DispensaryRecord(
        source="csv", name="Keystone Integrated Care", city="Beaver Falls", state="PA"))
    db.insert_dispensary(conn, DispensaryRecord(
        source="csv", name="Sunnyside - Altoona", city="Altoona", state="PA"))
    db.insert_dispensary(conn, DispensaryRecord(
        source="csv", name="Delta 9 THC", city="Wilmington", state="CA"))
    conn.commit()
    alias_map = seed_companies.load_company_aliases(seed_companies._YAML_PATH, strict=True)
    pairs = seed_companies._collect_brand_state_pairs(conn, alias_map)
    assert sorted(pairs) == [
        ("Delta 9 THC", "CA"),  # distinct CA operator (brand "Delta 9 THC" ≠ "Delta 9"), untouched
        ("Sunnyside", "PA"),    # all three PA Cresco entities fold to one
    ]


def test_collect_skips_placeholder_junk_rows():
    # Junk roster rows (license number / header / test stub) must never become a company.
    from rung.models import DispensaryRecord
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)
    for name in ("Curaleaf", "284.000141-CL", "Dispensary Name", "Test Toker", "Data Not Available"):
        db.insert_dispensary(conn, DispensaryRecord(source="csv", name=name, state="IL"))
    conn.commit()
    pairs = seed_companies._collect_brand_state_pairs(conn, {})
    assert [b for b, _ in pairs] == ["Curaleaf"]  # only the real operator survives
