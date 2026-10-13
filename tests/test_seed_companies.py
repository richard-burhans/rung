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


def test_companies_yml_folds_sessions_roster_spellings_into_one_operator():
    """Three AGCO spellings the extractor cannot fold — the Welland store filed as "Sessions Cannabis
    Retail" ("Retail" is a brand word elsewhere: Growers Retail, Sparq Retail), an unspaced hyphen at
    Waterloo, Peterborough's plaza — each seeded a company beside "Sessions" (2026-10-13)."""
    from rung.models import DispensaryRecord
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)
    for name, city in (("Sessions Cannabis - Kincardine", "KINCARDINE"), ("Sessions Cannabis Retail", "WELLAND"),
                       ("Sessions Cannabis-Waterloo", "WATERLOO"),
                       ("Sessions Cannabis Heritage on Lansdowne", "PETERBOROUGH")):
        db.insert_dispensary(conn, DispensaryRecord(source="html", name=name, city=city, state="ON"))
    conn.commit()
    alias_map = seed_companies.load_company_aliases(seed_companies._YAML_PATH, strict=True)
    pairs = seed_companies._collect_brand_state_pairs(conn, alias_map)
    assert sorted(pairs) == [("Sessions", "ON")]


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


def test_a_seeded_name_is_what_the_refold_would_fold_it_to():
    # `extract_brand` strips one tail per pass. The refold applies it to a name the seeder already
    # extracted, so the table holds the SECOND fold — and the seeder must key and name a row the
    # same way, or the next run inserts "Ziggyz Cannabis" beside the "Ziggyz" the refold left.
    from rung.models import DispensaryRecord
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)
    for name, city in (("Ziggyz Cannabis (MacArthur)", "Oklahoma City"), ("Ziggyz Cannabis (Yukon)", "Yukon"),
                       ("Trulieve Cannabis Dispensary (Findlay)", "Findlay")):
        db.insert_dispensary(conn, DispensaryRecord(source="html", name=name, city=city, state="OK"))
    conn.commit()
    assert seed_companies._collect_brand_state_pairs(conn, {}) == [("Trulieve", "OK"), ("Ziggyz", "OK")]


def test_a_company_the_refold_already_holds_is_found_under_any_spelling_it_left():
    # Rows the 2026-10-11 run duplicated: one the refold never needed to touch and so left under a
    # legal suffix, one under a street number, and one whose storefront city only its STORES know.
    # All three key through `fold` with the company's modal store city, so the roster lands on them.
    from rung.models import DispensaryRecord
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)
    for name, state, city in (("10613 Northern LLC", "NY", None), ("Elevate 6010", "CO", "Denver"),
                              ("Cookies Baltimore", "MD", "Baltimore"), ("Harvest Moon", "OR", "Astoria"),
                              ("Dank 716", "NY", "Buffalo"), ("Bloc", "NJ", "Ewing"),
                              ("Joint Venture 1111 LLC", "NY", None)):
        cid = seed_companies.ensure_company(conn, name, state)
        if city:
            conn.execute(
                "INSERT INTO company_stores (company_id, canonical_name, state, source, name, city, "
                "scraped_at) VALUES (%s, %s, %s, 'browser', %s, %s, now())", (cid, name, state, name, city))
    for name, state, city in (("10613 Northern LLC", "NY", "Flushing"), ("ELEVATE", "CO", "Denver"),
                              ("Cookies", "MD", "Baltimore"), ("Native Roots", "CO", "Denver"),
                              ("Harvest Moon Astoria", "OR", "Warrenton"), ("Dank716", "NY", "Buffalo"),
                              ("BLOC", "NJ", "Somerset"), ("Joint Venture 1111 LLC", "NY", "Buffalo")):
        db.insert_dispensary(conn, DispensaryRecord(source="html", name=name, city=city, state=state))
    conn.commit()
    alias_map = seed_companies.load_company_aliases(seed_companies._YAML_PATH, strict=True)
    pairs = seed_companies._collect_brand_state_pairs(conn, alias_map)
    # "Dank 716" is found by its RAW spelling: the fold reads 716 as a street number and keys the row
    # "dank". "Bloc" likewise — companies.yml aliases it to "Bloc Dispensary", which is not its own
    # fold. "Harvest Moon Astoria" filed under Warrenton is the limit no seeder can cross: the roster's
    # own city is wrong, so the storefront suffix cannot be seen until Stage 2 gives the row a store in
    # Astoria — which is also why the refold reads a fresh, store-less row as a singleton. It seeds
    # apart, unless companies.yml names the spelling (the three live cases of 2026-10-12 do).
    # "Joint Venture 1111 LLC" was seeded before the address strip existed: the roster row folds
    # twice to "Joint" while the stored name's own fold stops at "Joint Venture", so it is found
    # under the key the seeder would give the stored name today.
    assert pairs == [("10613 Northern LLC", "NY"), ("Bloc", "NJ"), ("Cookies Baltimore", "MD"),
                     ("Dank 716", "NY"), ("Elevate 6010", "CO"), ("Harvest Moon Astoria", "OR"),
                     ("Joint Venture 1111 LLC", "NY"), ("Native Roots", "CO")]
    inserted, skipped = seed_companies._seed(conn, pairs)
    assert inserted == [("Harvest Moon Astoria", "OR"), ("Native Roots", "CO")] and skipped == 6


def test_companies_yml_folds_the_rows_whose_roster_city_is_wrong():
    # The three rows the fixed seeder still inserted on 2026-10-12: the roster files each store under a
    # city that is not the one in its name, so no fold can see the storefront suffix. An alias can.
    from rung.models import DispensaryRecord
    conn = pg_conn()
    db.create_tables(conn)
    seed_companies.create_companies_table(conn)
    # "OREGROWN BEND" / "OREGROWN PORTLAND" are aliased for the refold's sake: two store-less company
    # rows carry those names, and a row with no store has no city to strip. Here, with no city at all,
    # the alias is the only thing that folds them.
    for name, city, state in (("RISE Bristol", "Richmond", "VA"), ("OREGROWN CANNON BEACH", "REDMOND", "OR"),
                              ("SWEET RELIEF ST. HELENS", "SAINT HELENS", "OR"), ("OREGROWN BEND", None, "OR"),
                              ("OREGROWN PORTLAND", None, "OR")):
        db.insert_dispensary(conn, DispensaryRecord(source="html", name=name, city=city, state=state))
    conn.commit()
    alias_map = seed_companies.load_company_aliases(seed_companies._YAML_PATH, strict=True)
    assert seed_companies._collect_brand_state_pairs(conn, alias_map) == [
        ("OREGROWN", "OR"), ("RISE", "VA"), ("SWEET RELIEF", "OR")]
