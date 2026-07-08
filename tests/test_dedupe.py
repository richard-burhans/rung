"""Tests for shared-brand store deduplication (rung.sources.dedupe)."""

from conftest import pg_conn

from rung import db
from rung.models import CompanyStoreRecord
from rung.sources import dedupe


def test_physical_key_prefers_coords_then_address_then_handle() -> None:
    coord = CompanyStoreRecord(
        company_id=0, canonical_name="A", state="NY", source="s",
        latitude=40.7128, longitude=-74.006, zip_code="10001",
        platform="dutchie", external_id="1",
    )
    no_coord = CompanyStoreRecord(
        company_id=0, canonical_name="A", state="NY", source="s",
        address="123 Main St", zip_code="10001", platform="weedmaps", external_id="a",
    )
    handle_only = CompanyStoreRecord(
        company_id=0, canonical_name="A", state="NY", source="s",
        platform="leafly", external_id="z",
    )
    assert dedupe.physical_key(coord).startswith("@") and dedupe.physical_key(coord).endswith("|10001")
    assert dedupe.physical_key(no_coord) == "123 main st|10001"   # store_key street + zip
    assert dedupe.physical_key(handle_only) == "leafly:z"          # last-resort handle


def test_normalize_address_folds_units_and_abbrev() -> None:
    assert dedupe.normalize_address("123 Main Street, Suite 4") == "123 main st"
    assert dedupe.normalize_address("123 N. Main St.") == "123 n main st"
    assert dedupe.normalize_address("123 Main St Unit B") == dedupe.normalize_address("123 Main Street")
    assert dedupe.normalize_address(None) == ""


def test_normalize_address_keeps_street_names_starting_with_unit_abbrev() -> None:
    # A street whose name starts with a unit abbreviation must survive (regression:
    # "ste" used to swallow "Stefko", collapsing the address to "1309 blvd").
    assert dedupe.normalize_address("1309 Stefko Blvd") == "1309 stefko blvd"
    assert dedupe.normalize_address("100 Flores Ave") == "100 flores ave"
    # …while real unit designators are still stripped, including the "#" forms.
    assert dedupe.normalize_address("1309 Stefko Blvd Ste 200") == "1309 stefko blvd"
    assert dedupe.normalize_address("1309 Stefko Blvd #125") == "1309 stefko blvd"
    assert dedupe.normalize_address("1309 Stefko Blvd # 125") == "1309 stefko blvd"


def test_normalize_address_folds_street_name_prefix_abbreviations() -> None:
    # Street-name prefixes that drift across sources (own uses the abbreviation, roster spells it
    # out) must fold so they don't read as different streets — the match key keys on the first
    # significant word, so "Mt Hermon" vs "Mount Hermon" would otherwise miss.
    assert dedupe.normalize_address("1003 Mt Hermon Rd") == dedupe.normalize_address(
        "1003 Mount Hermon Rd"
    )
    assert dedupe.normalize_address("124 St James Pl") == dedupe.normalize_address(
        "124 Saint James Pl"
    )
    assert dedupe.normalize_address("8675 Ft Hill Rd") == dedupe.normalize_address(
        "8675 Fort Hill Rd"
    )
    # Folding "saint"→"st" must NOT collide with the street-type "street"→"st" — they sit in
    # different slots (name prefix vs type suffix), so the full keys stay distinct.
    assert dedupe.normalize_address("100 Saint Charles St") == "100 st charles st"


def test_normalize_address_joins_queens_hyphenated_house_number() -> None:
    # NYC Queens/Bronx hyphenated house numbers ("219-17") are the same address as the
    # run-together form ("21917") other sources use — they must fold to ONE key, not split
    # the number into two tokens (which wrecked the number-keyed compare match for Dankley).
    assert dedupe.normalize_address("219-17 Hillside Ave.") == "21917 hillside ave"
    assert dedupe.normalize_address("219-17 Hillside Ave") == dedupe.normalize_address(
        "21917 Hillside Ave"
    )
    # A hyphen that is NOT between two digits (a route/street name) is left alone.
    assert dedupe.normalize_address("100 US-50 W") == "100 us 50 w"


def test_store_key_combines_address_and_zip() -> None:
    assert dedupe.address_key("123 Main St", "17011-1234") == "123 main st|17011"
    assert dedupe.address_key("", "17011") == ""


def test_zip_key_folds_canadian_postal_forms_and_truncates_us() -> None:
    # The spaced and unspaced Canadian forms key identically (naive [:5] would split them).
    assert dedupe.zip_key("P3E 4M8") == dedupe.zip_key("P3E4M8") == "P3E4M8"
    assert dedupe.zip_key("m5v 2t6") == "M5V2T6"      # case-folded
    assert dedupe.zip_key("17011-1234") == "17011"    # US zip+4 still truncates to 5
    assert dedupe.zip_key(None) == ""


def test_location_key_accepts_canadian_postal_in_the_address_fallback() -> None:
    key = dedupe.location_key(None, None, "435 Yonge St", "P3E 4M8")
    assert key == dedupe.address_key("435 Yonge St", "P3E4M8") != ""


def test_geo_key_cells_to_rooftop_and_needs_coords() -> None:
    # 4-decimal cell + zip; coordinates that agree to ~11 m share a cell.
    assert dedupe.geo_key(40.12345, -75.67891, "19103-2200") == "@40.1234,-75.6789|19103"
    assert dedupe.geo_key(40.12348, -75.67892, "19103") == "@40.1235,-75.6789|19103"
    assert dedupe.geo_key(None, -75.6, "19103") == ""
    assert dedupe.geo_key(40.1, None, "19103") == ""


def test_geo_key_merges_same_rooftop_but_not_a_110m_neighbour() -> None:
    # The measured tradeoff behind the 4-decimal (~11 m) cell: the SAME rooftop scraped with
    # GPS that differs in the 5th decimal (~a few m) shares a cell …
    near_a = dedupe.geo_key(40.71280, -74.00600, "10001")
    near_b = dedupe.geo_key(40.71283, -74.00598, "10001")
    assert near_a == near_b == "@40.7128,-74.006|10001"
    # … but a neighbour ~110 m away (the 3rd decimal differs) lands in a DIFFERENT cell, so
    # competitors packed into one commercial zone are never merged (3 decimals/110 m would).
    neighbour = dedupe.geo_key(40.71380, -74.00600, "10001")  # +0.001 lat ≈ 111 m
    assert neighbour != near_a


def test_location_key_prefers_coords_and_guards_the_address_fallback() -> None:
    # Coords present → geo_key wins.
    assert dedupe.location_key(40.7128, -74.006, "5 Main St", "10001") == "@40.7128,-74.006|10001"
    # No coords, real street + zip → guarded address_key fallback.
    assert dedupe.location_key(None, None, "5 Main St", "10001") == dedupe.address_key("5 Main St", "10001")
    # No coords, a bare COUNTY (no house number) → unidentifiable, '' (don't fabricate a location).
    assert dedupe.location_key(None, None, "Harford", "21001") == ""
    # A real street but no 5-digit zip → also '' (the guard needs both).
    assert dedupe.location_key(None, None, "5 Main St", None) == ""


def _insert_geo(conn, company_id, canonical, name, address, zip_code, lat, lng) -> None:
    db.insert_company_store(
        conn,
        CompanyStoreRecord(
            company_id=company_id, canonical_name=canonical, state="PA",
            source="dutchie", name=name, address=address, zip_code=zip_code,
            latitude=lat, longitude=lng,
        ),
    )


def test_run_dedupe_merges_same_rooftop_via_coordinates() -> None:
    # Same physical store, divergent address TEXT (no shared address key), identical
    # coordinates — the geo fallback must still collapse it to one operator.
    conn = _conn_with_companies()
    _insert_geo(conn, 1, "RISE", "RISE Carson", "3060 US-50", "89701", 39.1638, -119.7670)
    _insert_geo(conn, 2, "RISE Carson City", "RISE", "3060 U.S. 50", "89701", 39.1638, -119.7670)
    conn.commit()
    report = dedupe.run_dedupe(conn, "PA")
    assert report.distinct_stores == 1
    assert report.duplicate_rows == 1


def test_run_dedupe_keeps_distinct_neighbors_apart() -> None:
    # Two DIFFERENT dispensaries in the same zip but ~100 m apart (distinct rooftops)
    # must NOT merge — the tight cell guards against green-zone clustering.
    conn = _conn_with_companies()
    _insert_geo(conn, 1, "Ayr", "Ayr Inverness", "3129 E Gulf to Lake Hwy", "34450", 28.8410, -82.3500)
    _insert_geo(conn, 2, "The Flowery", "Flowery Inverness", "3177 E Gulf to Lake Hwy", "34450", 28.8420, -82.3500)
    conn.commit()
    report = dedupe.run_dedupe(conn, "PA")
    assert report.distinct_stores == 2
    assert report.duplicate_rows == 0


def test_run_dedupe_merges_same_operator_geocode_drift() -> None:
    # ONE operator's store geocoded > 11 m apart by two platforms (different address TEXT → no shared
    # address key; different 11 m cell → no geo-key match) must still collapse via the same-operator
    # ~100 m merge. Validated safe because an operator never has two of its own stores within 100 m.
    conn = _conn_with_companies()
    _insert_geo(conn, 1, "STIIIZY", "STIIIZY DTLA", "728 E Commercial St", "90012", 34.05220, -118.23210)
    _insert_geo(conn, 2, "STIIIZY", "STIIIZY Downtown", "730 Commercial Street", "90012", 34.05250, -118.23250)
    conn.commit()
    report = dedupe.run_dedupe(conn, "PA")
    assert report.distinct_stores == 1   # ~40 m apart, same operator → one store
    assert report.duplicate_rows == 1


def test_run_dedupe_keeps_same_operator_distinct_locations_apart() -> None:
    # The over-merge guard: two GENUINELY distinct stores of one chain (km apart) must NOT merge —
    # the ~100 m radius is far below the distinct-store distances (measured: same-operator stores are
    # either < ~100 m = the same rooftop, or km+ = different stores, with a wide empty valley).
    conn = _conn_with_companies()
    _insert_geo(conn, 1, "STIIIZY", "STIIIZY DTLA", "728 E Commercial St", "90012", 34.05220, -118.23210)
    _insert_geo(conn, 2, "STIIIZY", "STIIIZY NoHo", "6218 Lankershim Blvd", "91606", 34.18360, -118.38570)
    conn.commit()
    report = dedupe.run_dedupe(conn, "PA")
    assert report.distinct_stores == 2   # ~20 km apart → two distinct stores
    assert report.duplicate_rows == 0


def test_run_dedupe_keeps_richest_menu_platform() -> None:
    # The same rooftop captured on Dutchie (rich: potency/terpenes/mg) and Leafly (none):
    # the Dutchie row must survive as the menu-scrape target (canonical_company_id IS NULL),
    # the Leafly twin demoted — regression: dedupe used to keep the canonical-company row
    # regardless of platform, demoting Dutchie to its aggregator twin and losing the rich menu.
    conn = _conn_with_companies()
    db.insert_company_store(conn, CompanyStoreRecord(
        company_id=1, canonical_name="Curaleaf", state="PA", source="leafly_directory",
        name="Curaleaf", address="100 Main St", zip_code="17011",
        latitude=40.2, longitude=-76.9, platform="leafly", external_id="curaleaf-camp-hill"))
    db.insert_company_store(conn, CompanyStoreRecord(
        company_id=2, canonical_name="Curaleaf", state="PA", source="dutchie_directory",
        name="Curaleaf", address="100 Main St", zip_code="17011",
        latitude=40.2, longitude=-76.9, platform="dutchie", external_id="d-123"))
    conn.commit()
    report = dedupe.run_dedupe(conn, "PA")
    assert report.distinct_stores == 1
    kept = conn.execute(
        "SELECT platform, external_id FROM company_stores WHERE canonical_company_id IS NULL"
    ).fetchall()
    assert kept == [("dutchie", "d-123")]  # rich Dutchie handle survives, Leafly demoted


def test_pick_canonical_prefers_brand_in_store_names() -> None:
    names = {1: "Delta 9 Pittsburgh", 2: "Sunnyside", 3: "Keystone Integrated Care"}
    store_names = {
        1: ["Sunnyside Butler", "Sunnyside Erie"],
        2: ["Sunnyside Butler", "Sunnyside Erie"],
        3: ["Sunnyside Butler", "Sunnyside Erie"],
    }
    assert dedupe.pick_canonical({1, 2, 3}, names, store_names) == 2  # Sunnyside


def _conn_with_companies() -> db.DBConn:
    conn = pg_conn()
    conn.execute(db._CREATE_COMPANY_STORES)
    conn.execute(db._CREATE_STORE_PRODUCTS)  # run_dedupe realigns snapshots onto kept rows
    conn.execute(
        "CREATE TABLE companies (id INTEGER PRIMARY KEY, canonical_name TEXT, state TEXT)"
    )
    return conn


def _insert(conn, company_id, canonical, name, address, zip_code) -> None:
    db.insert_company_store(
        conn,
        CompanyStoreRecord(
            company_id=company_id, canonical_name=canonical, state="PA",
            source="browser", name=name, address=address, zip_code=zip_code,
        ),
    )


def test_run_dedupe_collapses_shared_brand() -> None:
    conn = _conn_with_companies()
    # Three companies, same two physical Sunnyside stores under each.
    for cid, cname in [(1, "Delta 9 Pittsburgh"), (2, "Sunnyside"), (3, "Keystone Integrated Care")]:
        _insert(conn, cid, cname, "Sunnyside Butler", "100 Butler St", "16001")
        _insert(conn, cid, cname, "Sunnyside Erie", "200 Erie Ave", "16501")
    # An unrelated company with its own distinct store.
    _insert(conn, 9, "Trulieve", "Trulieve Camp Hill", "3401 Hartzdale Dr", "17011")
    conn.commit()

    report = dedupe.run_dedupe(conn, "PA")
    # 2 Sunnyside stores + 1 Trulieve store = 3 distinct; 4 duplicate rows (2 aliases × 2 stores).
    assert report.distinct_stores == 3
    assert report.duplicate_rows == 4
    assert report.clusters == [("Sunnyside", ["Delta 9 Pittsburgh", "Keystone Integrated Care"])]

    # Canonical (Sunnyside, id 2) rows stay NULL; alias rows point to id 2.
    canon_null = conn.execute(
        "SELECT COUNT(*) FROM company_stores WHERE canonical_company_id IS NULL"
    ).fetchone()[0]
    assert canon_null == 3  # 2 Sunnyside + 1 Trulieve
    dupes = conn.execute(
        "SELECT DISTINCT canonical_company_id FROM company_stores "
        "WHERE canonical_company_id IS NOT NULL"
    ).fetchall()
    assert dupes == [(2,)]


def test_run_dedupe_realigns_snapshots_to_kept_company() -> None:
    # Sunnyside's store was scraped under the "Delta 9" alias (snapshots stamped company 1);
    # dedupe makes Sunnyside (2) the kept/canonical company for the shared custom:162 handle, so
    # the snapshots must move to company 2 — the company a fresh scrape-menus run files under.
    conn = _conn_with_companies()
    for cid, cname in [(1, "Delta 9 Pittsburgh"), (2, "Sunnyside")]:
        db.insert_company_store(conn, CompanyStoreRecord(
            company_id=cid, canonical_name=cname, state="PA", source="browser",
            name="Sunnyside Butler", address="100 Butler St", zip_code="16001",
            platform="custom", external_id="162"))
    for i in range(2):
        conn.execute(
            "INSERT INTO store_products (company_id, state, store_key, platform, external_id, "
            "source, scraped_at) VALUES (1, 'PA', 'custom:162', 'custom', '162', 'cresco_api', %s)",
            (f"2026-06-20T00:0{i}:00+00:00",))
    conn.commit()

    report = dedupe.run_dedupe(conn, "PA")
    assert report.realigned_products == 2
    owners = conn.execute(
        "SELECT DISTINCT company_id FROM store_products WHERE store_key = 'custom:162'"
    ).fetchall()
    assert owners == [(2,)]  # moved off the folded "Delta 9" alias onto Sunnyside
    assert dedupe.run_dedupe(conn, "PA").realigned_products == 0  # idempotent


def test_run_dedupe_folds_addressless_handle_duplicate() -> None:
    # The same store captured twice under one platform handle — once with an address+coords, once
    # address-less (Cresco's custom duplicates). No shared address key, so only the handle groups
    # them; they must collapse to one store and the kept row keeps its coordinates so it maps.
    conn = _conn_with_companies()
    db.insert_company_store(conn, CompanyStoreRecord(
        company_id=1, canonical_name="Sunnyside", state="PA", source="cresco",
        name="Sunnyside", address="211 52nd St", zip_code="15201",
        latitude=40.4804, longitude=-79.9548, platform="custom", external_id="899"))
    db.insert_company_store(conn, CompanyStoreRecord(
        company_id=1, canonical_name="Sunnyside", state="PA", source="browser",
        name="Sunnyside", platform="custom", external_id="899"))  # address-less twin
    conn.commit()
    report = dedupe.run_dedupe(conn, "PA")
    assert report.distinct_stores == 1
    assert report.duplicate_rows == 1
    kept = conn.execute(
        "SELECT latitude FROM company_stores WHERE canonical_company_id IS NULL"
    ).fetchall()
    assert len(kept) == 1 and kept[0][0] is not None  # one store, still geocoded


def test_run_dedupe_carries_coords_to_addressless_kept_row() -> None:
    # When the address-less row WINS the kept slot (lower id), it must inherit the folded
    # geocoded sibling's coordinates + address so the surviving row still plots.
    conn = _conn_with_companies()
    db.insert_company_store(conn, CompanyStoreRecord(  # address-less, lower id → wins the tiebreak
        company_id=1, canonical_name="Sunnyside", state="PA", source="browser",
        name="Sunnyside", platform="custom", external_id="899"))
    db.insert_company_store(conn, CompanyStoreRecord(
        company_id=1, canonical_name="Sunnyside", state="PA", source="cresco",
        name="Sunnyside", address="211 52nd St", zip_code="15201",
        latitude=40.4804, longitude=-79.9548, platform="custom", external_id="899"))
    conn.commit()
    report = dedupe.run_dedupe(conn, "PA")
    assert report.distinct_stores == 1
    assert report.located_from_sibling == 1
    row = conn.execute(
        "SELECT latitude, longitude, address FROM company_stores WHERE canonical_company_id IS NULL"
    ).fetchone()
    assert row[0] == 40.4804 and row[2] == "211 52nd St"  # inherited the sibling's location


def test_run_dedupe_idempotent() -> None:
    conn = _conn_with_companies()
    for cid, cname in [(1, "Delta 9 Pittsburgh"), (2, "Sunnyside")]:
        _insert(conn, cid, cname, "Sunnyside Butler", "100 Butler St", "16001")
    conn.commit()
    first = dedupe.run_dedupe(conn, "PA")
    second = dedupe.run_dedupe(conn, "PA")
    assert (first.distinct_stores, first.duplicate_rows) == (second.distinct_stores, second.duplicate_rows)
    assert second.duplicate_rows == 1


def test_pick_canonical_prefers_more_stores_when_brand_tie() -> None:
    # Neither brand token appears in city-named stores → tie; the operator with more
    # stores wins (Apothecarium 6 over Keystone ReLeaf 3 — the surviving brand).
    names = {1: "Keystone ReLeaf", 2: "Apothecarium Dispensary"}
    store_names = {1: ["Allentown", "Bethlehem", "Stroudsburg"],
                   2: ["Allentown", "Bethlehem", "Stroudsburg", "Lancaster", "Thorndale", "Plymouth Meeting"]}
    assert dedupe.pick_canonical({1, 2}, names, store_names) == 2


def test_storefront_subset_alias_brands_its_locations() -> None:
    conn = _conn_with_companies()
    # Operator with 2 stores; a subset alias (1 store) shares one address.
    _insert(conn, 10, "Apothecarium Dispensary", "A One", "100 A St", "17000")
    _insert(conn, 10, "Apothecarium Dispensary", "A Two", "200 B St", "17001")
    _insert(conn, 11, "Keystone ReLeaf", "K One", "100 A St", "17000")  # shared
    conn.commit()
    dedupe.run_dedupe(conn, "PA")
    storefronts = dict(conn.execute(
        "SELECT address, storefront_name FROM company_stores "
        "WHERE canonical_company_id IS NULL"))
    assert storefronts["100 A St"] == "Keystone ReLeaf"          # subset alias brands it
    assert storefronts["200 B St"] == "Apothecarium Dispensary"  # operator's own store


def test_replace_company_stores_keeps_better_prior() -> None:
    conn = _conn_with_companies()
    three = [CompanyStoreRecord(company_id=1, canonical_name="A", state="PA",
             source="x", name=f"S{i}", address=f"{i} St") for i in range(3)]
    assert db.replace_company_stores(conn, 1, "PA", three) == (3, False)   # initial write
    assert db.replace_company_stores(conn, 1, "PA", three[:1]) == (3, True)  # fewer → kept prior
    assert db.count_company_stores(conn, 1, "PA") == 3
    four = [*three, CompanyStoreRecord(company_id=1, canonical_name="A", state="PA",
            source="x", name="S3", address="3 St")]
    assert db.replace_company_stores(conn, 1, "PA", four) == (4, False)    # ≥ → replace
    assert db.replace_company_stores(conn, 1, "PA", []) == (4, False)      # empty → no change


def _store(cid: int, i: int, *, handle: bool) -> CompanyStoreRecord:
    return CompanyStoreRecord(
        company_id=cid, canonical_name="A", state="PA", source="x",
        name=f"S{i}", address=f"{i} St", external_id=str(100 + i) if handle else None,
    )


def test_replace_company_stores_handle_upgrade_wins_small_loss() -> None:
    conn = _conn_with_companies()
    plain5 = [_store(1, i, handle=False) for i in range(5)]
    assert db.replace_company_stores(conn, 1, "PA", plain5) == (5, False)
    # 4 stores WITH handles vs 5 without: 4 >= 5*0.8 → the Stage-3 handles win.
    handled4 = [_store(1, i, handle=True) for i in range(4)]
    assert db.replace_company_stores(conn, 1, "PA", handled4) == (4, False)
    assert all(r[0] for r in conn.execute("SELECT external_id FROM company_stores"))


def test_replace_company_stores_handle_upgrade_rejects_big_loss() -> None:
    conn = _conn_with_companies()
    plain15 = [_store(1, i, handle=False) for i in range(15)]
    assert db.replace_company_stores(conn, 1, "PA", plain15) == (15, False)
    # 1 handled store vs 15 without: 1 < 15*0.8 → too big a loss, keep prior (Restore case).
    assert db.replace_company_stores(conn, 1, "PA", [_store(1, 0, handle=True)]) == (15, True)


def test_replace_counts_distinct_stores_not_rows() -> None:
    conn = _conn_with_companies()
    # A noisy extractor emitted TWO rows per physical store (name variants of the
    # same address) — 6 rows, 3 physical stores (the Curaleaf sitemap case).
    noisy = [
        CompanyStoreRecord(company_id=1, canonical_name="A", state="PA", source="sitemap",
                           name=f"{prefix}{i}", address=f"{i} Main St")
        for i in range(3)
        for prefix in ("Store ", "Acme Dispensary Store ")
    ]
    assert db.replace_company_stores(conn, 1, "PA", noisy) == (6, False)
    # A clean handled result covering the same 3 physical stores must displace it
    # (3 rows < 6 rows, but 3 distinct >= 3 distinct).
    clean = [
        CompanyStoreRecord(company_id=1, canonical_name="A", state="PA", source="api",
                           name=f"Store {i}", address=f"{i} Main St", external_id=str(i))
        for i in range(3)
    ]
    assert db.replace_company_stores(conn, 1, "PA", clean) == (3, False)
    assert db.count_company_stores(conn, 1, "PA") == 3


def test_city_in_name_matches_whole_city_only() -> None:
    assert dedupe._city_in_name("Whitehall", "Harvest of Whitehall")
    assert dedupe._city_in_name("Palm Desert", "STIIIZY Palm Desert")
    # the old last-word/substring guess misfired on these — the city is NOT in the name:
    assert not dedupe._city_in_name("Salem", "HOMEGROWN OREGON")
    assert not dedupe._city_in_name("Cambridge", "Green Bridge Dispensary")  # no substring hit
    assert not dedupe._city_in_name(None, "Some Alias")


def test_redirect_alias_pins_to_city_named_store() -> None:
    conn = _conn_with_companies()
    # Operator Trulieve with 2 stores; a redirect alias ("Harvest of Whitehall") scraped the
    # WHOLE list (both addresses) → it should pin to the Whitehall store only.
    for cid, cname in [(1, "Trulieve"), (2, "Harvest of Whitehall")]:
        db.insert_company_store(conn, CompanyStoreRecord(
            company_id=cid, canonical_name=cname, state="PA", source="x",
            name="Store", address="100 A St", city="Whitehall", zip_code="18052"))
        db.insert_company_store(conn, CompanyStoreRecord(
            company_id=cid, canonical_name=cname, state="PA", source="x",
            name="Store", address="200 B St", city="Reading", zip_code="19601"))
    conn.commit()
    dedupe.run_dedupe(conn, "PA")
    storefronts = dict(conn.execute(
        "SELECT city, storefront_name FROM company_stores WHERE canonical_company_id IS NULL"))
    assert storefronts["Whitehall"] == "Harvest of Whitehall"   # pinned by city-in-name
    assert storefronts["Reading"] != "Harvest of Whitehall"     # not the Whitehall alias
