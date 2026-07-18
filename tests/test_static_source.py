"""The static-file data source — the seam that lets the D1 analysis scripts run off a frozen clean
Parquet instead of live Postgres (the Galaxy / outside-researcher reproducibility path).

The end-to-end fidelity check is `reports/clean_d1_comparison.md` (every D1 metric, live vs the static
export, matching within the ToS-posture tolerance). These tests pin the *dialect contract* the adapter
must honour so the CANONICAL scripts run unchanged: the Postgres surface they emit — the JSONB `->>`
extract, `count(*) FILTER`, `width_bucket` (the McCrary de-heap), `%s` positional params, and the derived
`products_normalized` view with its CA→CAD currency — must all parse and compute correctly on DuckDB.
A tiny fixture Parquet stands in for the clean export; no database, no analysis scripts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

import pandas as pd

from rung import static_source

# The store_products columns the products_normalized view (and the D1 queries) reference — the fixture
# must carry all of them for the view to build.
_SP_COLS = [
    "id", "company_id", "state", "store_key", "platform", "source", "external_product_id", "name",
    "brand", "category_std", "strain_type_std", "price", "size_g", "thc", "cbd", "thc_mg", "cbd_mg",
    "terp_total", "terpenes_std", "scraped_at", "product_type_std", "cannabinoids_std", "obtention_std",
]


def _row(**over):
    base = dict.fromkeys(_SP_COLS)
    base.update(
        id=1, company_id=1, state="PA", store_key="s1", platform="dutchie", source="dutchie",
        external_product_id="e1", name="Blue Dream", brand="Acme", category_std="Flower",
        strain_type_std="Hybrid", price=40.0, size_g=3.5, thc=22.0, cbd=0.1, terp_total=2.0,
        terpenes_std=json.dumps({"Myrcene": 0.8, "Limonene": 0.4}), obtention_std=None,
    )
    base.update(over)
    return base


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    rows = [
        _row(id=1, name="Blue Dream", state="PA", thc=22.0),
        _row(id=2, name="Blue Dream", state="PA", thc=34.0, brand="Beta"),   # ≥30
        _row(id=3, name="OG Kush", state="CA", platform="jane", thc=31.0, brand="Gamma",
             terpenes_std=json.dumps({"Myrcene": 1.2})),
        _row(id=4, name="No Terps", state="PA", thc=None, terpenes_std=None),  # NULL terps + NULL thc
    ]
    pd.DataFrame(rows, columns=_SP_COLS).to_parquet(tmp_path / "store_products.parquet")
    pd.DataFrame([{"abbr": "PA", "country": "US"}, {"abbr": "CA", "country": "US"}]).to_parquet(
        tmp_path / "state_programs.parquet"
    )
    return tmp_path


def test_env_wiring(monkeypatch, static_dir):
    monkeypatch.setenv("RUNG_DATA_SOURCE", "static")
    monkeypatch.setenv("RUNG_STATIC_PATH", str(static_dir))
    assert static_source.is_static() is True
    monkeypatch.setenv("RUNG_DATA_SOURCE", "")
    assert static_source.is_static() is False


def test_missing_path_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("RUNG_DATA_SOURCE", "static")
    monkeypatch.delenv("RUNG_STATIC_PATH", raising=False)
    with pytest.raises(RuntimeError, match="RUNG_STATIC_PATH"):
        static_source.connect()


def test_jsonb_extract_and_filter(static_dir):
    """The dissociation/per-terpene core: `terpenes_std->>'x'::float` and `count(*) FILTER`."""
    with static_source.StaticConnection(static_dir) as con:
        (myrcene,) = con.execute(
            "SELECT (terpenes_std->>'Myrcene')::float FROM store_products WHERE id = 1"
        ).fetchone()
        assert myrcene == pytest.approx(0.8)
        (ge30,) = con.execute(
            "SELECT count(*) FILTER (WHERE thc >= 30) FROM store_products"
        ).fetchone()
        assert ge30 == 2  # ids 2 (34) and 3 (31); id 4 thc is NULL


def test_width_bucket_macro(static_dir):
    """The McCrary de-heap histogram — Postgres width_bucket has no DuckDB builtin."""
    with static_source.StaticConnection(static_dir) as con:
        # width_bucket(x, 5, 40, 70): bucket width 0.5; thc=22 → floor(70*17/35)+1 = 35.
        (b,) = con.execute("SELECT width_bucket(22.0, 5, 40, 70)").fetchone()
        assert b == 35
        assert con.execute("SELECT width_bucket(2.0, 5, 40, 70)").fetchone()[0] == 0     # below low
        assert con.execute("SELECT width_bucket(40.0, 5, 40, 70)").fetchone()[0] == 71   # at/above high
        assert con.execute("SELECT width_bucket(NULL, 5, 40, 70)").fetchone()[0] is None


def test_percent_s_positional_params(static_dir):
    """psycopg `%s` placeholders must swap to DuckDB `?` — the tier1 famous-strain `ILIKE %s` path."""
    with static_source.StaticConnection(static_dir) as con:
        (n,) = con.execute(
            "SELECT count(*) FROM store_products WHERE name ILIKE %s", ("%blue dream%",)
        ).fetchone()
        assert n == 2


def test_products_normalized_view_and_currency(static_dir):
    """The view builds and derives currency from the state_programs country join."""
    with static_source.StaticConnection(static_dir) as con:
        cur = con.execute(
            "SELECT price_per_g, currency FROM products_normalized WHERE id = 1"
        )
        assert [d[0] for d in cur.description][:2] == ["price_per_g", "currency"]
        ppg, currency = cur.fetchone()
        assert ppg == pytest.approx(40.0 / 3.5, abs=0.01)
        assert currency == "USD"


def test_cursor_is_iterable(static_dir):
    with static_source.StaticConnection(static_dir) as con:
        ids = sorted(r[0] for r in con.execute("SELECT id FROM store_products"))
        assert ids == [1, 2, 3, 4]
