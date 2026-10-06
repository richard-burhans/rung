"""Brand folding, address parsing, interpolation, browser resolution, the static source, seeding.

Regression tests for the 2026-10-06 whole-tree review's findings in the public core.
"""

from collections import Counter
from pathlib import Path

import pytest
from conftest import pg_conn

from rung import addresses, browser, seed_companies, text

# ── brand folding ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("name", "brand"), [
    # 420/710 are names in this trade, not street numbers: the old rule stripped from them to the end.
    ("Score 420 Alamogordo", "Score 420 Alamogordo"),
    ("Highway 420 Dispensary - Tulsa", "Highway 420"),
    ("HANGAR 420", "HANGAR 420"),
    ("Pure 710SF", "Pure 710SF"),
    # A fold that would leave a bare generic is refused.
    ("Cannabis 247", "Cannabis 247"),
    # The street-address folds the rule exists for still happen.
    ("ONE PLANT 3003 DANFORTH", "ONE PLANT"),
    ("HIGH CANNABIS 12467", "HIGH CANNABIS"),
])
def test_a_store_suffix_never_eats_a_numbered_brand(name: str, brand: str) -> None:
    assert text.extract_brand(name) == brand


# ── address parsing ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("address", "parsed"), [
    ("Suite 100-5221 46 St", (5221, "46|ST|")),          # unit 100, house 5221 — not house 46
    ("Floor 2, 123 Main St", (123, "MAIN|ST|")),         # FLOOR before FL, on a word boundary
    ("3220 5 Ave NE #110", (3220, "5|AVE|NE")),          # a trailing unit is not part of the street
    ("123 Main St, Unit 4", (123, "MAIN|ST|")),
    ("200 Bay St", (200, "BAY|ST|")),                    # a street word is not a unit
    ("106 FL A1A", (106, "FL A1A||")),                   # nor is a Florida state road
    ("Unit-A 3992 Old Lakelse Lake Drive", (3992, "OLD LAKELSE LAKE|DR|")),
])
def test_units_never_become_the_house_number_or_the_street(address: str, parsed: tuple) -> None:
    assert addresses.parse_address(address) == parsed


# ── interpolation keeps a descending range's direction ───────────────────────────────────────────


def test_a_descending_segment_is_not_mirrored(monkeypatch) -> None:
    """The interpolation is plain arithmetic, but the module imports pyshp and pyproj at load time
    (the geo extras). Stand-ins let the arithmetic be tested on a box without them, so the test runs
    everywhere instead of skipping where the extras are absent."""
    import importlib
    import sys
    import types

    stubbed = "rung.geocode_rnf" not in sys.modules
    if stubbed:
        pyproj = types.ModuleType("pyproj")
        pyproj.Transformer = object  # ty: ignore[unresolved-attribute]
        monkeypatch.setitem(sys.modules, "shapefile", sys.modules.get("shapefile") or types.ModuleType("shapefile"))
        monkeypatch.setitem(sys.modules, "pyproj", sys.modules.get("pyproj") or pyproj)
        monkeypatch.delitem(sys.modules, "rung.geocode_rnf", raising=False)
    geocode_rnf = importlib.import_module("rung.geocode_rnf")
    if stubbed:  # never leave a module imported against stand-ins for a later test to find
        monkeypatch.setitem(sys.modules, "rung.geocode_rnf", geocode_rnf)
        monkeypatch.delitem(sys.modules, "rung.geocode_rnf")
    Segment, _interpolate = geocode_rnf.Segment, geocode_rnf._interpolate

    line = ((0.0, 0.0), (1000.0, 0.0))
    descending = _interpolate(Segment(198, 100, None, None, line), 110, offset_m=0, end_offset_m=0)
    ascending = _interpolate(Segment(100, 198, None, None, line), 110, offset_m=0, end_offset_m=0)
    assert descending is not None and ascending is not None
    assert descending[0] == pytest.approx(1000 * (110 - 198) / (100 - 198))   # ~898, near the 100 end
    assert ascending[0] == pytest.approx(1000 * (110 - 100) / (198 - 100))    # ~102


# ── browser resolution ────────────────────────────────────────────────────────────────────────────


def test_playwright_builds_sort_by_number(tmp_path: Path, monkeypatch) -> None:
    for build in ("chromium-999", "chromium-1099"):
        binary = tmp_path / build / "chrome-linux64" / "chrome"
        binary.parent.mkdir(parents=True)
        binary.write_text("", encoding="utf-8")
    monkeypatch.delenv("RUNG_CHROME_PATH", raising=False)
    monkeypatch.delenv("DISPENSARY_CHROME_PATH", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    resolved = browser._resolve_chrome_path()
    assert resolved is not None and "chromium-1099" in str(resolved)


def test_a_system_chromium_is_the_binary_the_launch_uses(tmp_path: Path, monkeypatch) -> None:
    """`chromium_available` used to say yes to a bare `chromium` on PATH while the options set no
    binary and pydoll looked only for google-chrome; now the resolved path IS what launches."""
    monkeypatch.delenv("RUNG_CHROME_PATH", raising=False)
    monkeypatch.delenv("DISPENSARY_CHROME_PATH", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(browser.shutil, "which",
                        lambda name: "/usr/bin/chromium" if name == "chromium" else None)
    assert browser._resolve_chrome_path() == Path("/usr/bin/chromium")


# ── the static source ─────────────────────────────────────────────────────────────────────────────


#: The store_products columns the static products_normalized view reads.
_SP_COLS = [
    "id", "company_id", "state", "store_key", "platform", "source", "external_product_id", "name",
    "brand", "category_std", "strain_type_std", "price", "size_g", "thc", "cbd", "thc_mg", "cbd_mg",
    "terp_total", "terpenes_std", "scraped_at", "product_type_std", "cannabinoids_std", "obtention_std",
]


def _deposit(tmp_path: Path, *, with_programs: bool) -> Path:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    row = dict.fromkeys(_SP_COLS)
    row.update(id=1, company_id=1, state="PA", store_key="k", platform="x", source="x", name="Gummy",
               thc_mg=25.0)
    pd.DataFrame([row], columns=_SP_COLS).to_parquet(tmp_path / "store_products.parquet")
    if with_programs:
        pd.DataFrame([{"abbr": "PA", "country": "US"}]).to_parquet(tmp_path / "state_programs.parquet")
    return tmp_path


def test_a_deposit_without_state_programs_names_the_missing_file(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    from rung import static_source

    with pytest.raises(FileNotFoundError, match=r"state_programs\.parquet"):
        static_source.StaticConnection(_deposit(tmp_path, with_programs=False))


def test_a_doubled_percent_is_a_literal_percent_as_psycopg_reads_it(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    from rung import static_source

    with static_source.StaticConnection(_deposit(tmp_path, with_programs=True)) as con:
        row = con.execute("SELECT thc_mg::int %% 10 FROM store_products WHERE state = %s", ("PA",)).fetchone()
    assert row == (5,)


# ── seeding keeps the spelling a company was seeded under ────────────────────────────────────────


def test_reseeding_reuses_an_existing_spelling_rather_than_adding_a_twin() -> None:
    """The dominant spelling can flip between roster refreshes; the insert conflicts on the exact
    name, so a flipped winner used to seed a second company for the same operator."""
    conn = pg_conn()
    seed_companies.create_companies_table(conn)
    conn.execute("CREATE TABLE dispensaries (name TEXT, city TEXT, state TEXT)")
    conn.execute("INSERT INTO companies (canonical_name, state, created_at) VALUES ('ZenLeaf', 'PA', 'x')")
    for name in ("Zen Leaf", "Zen Leaf", "ZenLeaf"):
        conn.execute("INSERT INTO dispensaries VALUES (%s, NULL, 'PA')", (name,))
    pairs = seed_companies._collect_brand_state_pairs(conn, {})
    assert pairs == [("ZenLeaf", "PA")]
    assert seed_companies.dominant_spelling(Counter({"Zen Leaf": 2, "ZenLeaf": 1})) == "Zen Leaf"
