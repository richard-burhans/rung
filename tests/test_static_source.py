"""The static-file data source — the seam that lets the D1 analysis scripts run off a frozen clean
Parquet instead of live Postgres (the Galaxy / outside-researcher reproducibility path).

The end-to-end fidelity check is `reports/clean_d1_comparison.md` (every D1 metric, live vs the static
export, matching within the ToS-posture tolerance). These tests pin the *dialect contract* the adapter
must honour so the CANONICAL scripts run unchanged: the Postgres surface they emit — the JSONB `->>`
extract, `count(*) FILTER`, `width_bucket` (the McCrary de-heap), `%s` positional params, and the derived
`products_normalized` view with its CA→CAD currency — must all parse and compute correctly on DuckDB.
A tiny fixture Parquet stands in for the clean export; no database, no analysis scripts.
"""


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


def test_a_frozen_vintage_without_a_later_column_still_opens(static_dir):
    """A Parquet frozen before a column existed must still bind — the freeze is deliberate.

    `intel/data/clean_d1/v3` is the **2026-07-17** vintage: 29 columns, written before
    `potency_implausible` existed. It is immutable on purpose, because rebuilding it to pick up a new
    column would move numbers a paper already quotes — so a later cut is a NEW vintage beside it
    (`intel/data/clean_d1/2026-08-03/`, 32 columns), not a refresh of it. `StaticConnection` back-fills
    any column the live schema has and a vintage does not (`_COLUMNS_ADDED_AFTER_FREEZE`) as a typed
    NULL, which is a no-op for the newer cut and load-bearing for the older one. Without
    that, adding the column to `_PRODUCTS_NORMALIZED_VIEW_SQL` killed every static-mode script here
    with a DuckDB binder error. `static_dir` is exactly such a pre-column vintage — `_SP_COLS` does
    not list the flag — so this test is the regression.
    """
    assert "potency_implausible" not in _SP_COLS, (
        "the fixture must stay a PRE-column vintage; that is the case this test exists to pin"
    )
    with static_source.StaticConnection(static_dir) as con:
        rows = con.execute(
            "SELECT id, potency_implausible FROM products_normalized ORDER BY id"
        ).fetchall()
    assert rows, "the view returned nothing — the back-fill did not bind"
    # NULL, not FALSE: this vintage was written before the check existed, so no row was ever assessed.
    # `plausible_potency_where` tests `IS NOT TRUE` precisely so a NULL here keeps the row.
    assert all(flag is None for _, flag in rows)


def test_a_rebuilt_vintage_passes_the_real_flag_through(tmp_path):
    """The back-fill must not shadow a real value once a dataset is rebuilt with the column."""
    rows = [_row(id=1, thc=3.0692, cbd=0.0), _row(id=2, thc=30.692, cbd=0.0)]
    frame = pd.DataFrame(rows, columns=[*_SP_COLS, "potency_implausible"])
    frame["potency_implausible"] = [True, False]
    frame.to_parquet(tmp_path / "store_products.parquet")
    pd.DataFrame([{"abbr": "PA", "country": "US"}]).to_parquet(tmp_path / "state_programs.parquet")

    with static_source.StaticConnection(tmp_path) as con:
        got = dict(
            con.execute("SELECT id, potency_implausible FROM products_normalized").fetchall()
        )
    assert got == {1: True, 2: False}


def test_the_static_swap_announces_itself_once(monkeypatch, capsys, static_dir) -> None:
    """A data source that substitutes a frozen file for the live database must be AUDIBLE.

    `RUNG_DATA_SOURCE=static` is an ambient variable, and this sandbox exported it globally from
    `/etc/sandbox-persistent.sh` — sourced before every command and by `/etc/profile.d` for login
    shells — so every analysis in it read the frozen deposit while believing it was live. Adversarial
    round 39 found it: an adjudicator noticed that any agent who did not `unset` it "queried the DEPOSIT
    while believing it was live".

    Removing the variable from one file fixes one sandbox. Announcing the swap fixes the class — a
    future environment can ship it again and nobody is misled. It is this project's own maxim moved
    sideways: a gate nobody can observe firing is indistinguishable from one that is not there, and so
    is a data source nobody can observe being swapped.
    """
    monkeypatch.setattr(static_source, "_ANNOUNCED", False)
    monkeypatch.setenv("RUNG_DATA_SOURCE", "static")
    monkeypatch.setenv("RUNG_STATIC_PATH", str(static_dir))

    static_source.connect().close()
    first = capsys.readouterr().err
    assert "FROZEN deposit" in first and str(static_dir) in first, first
    assert "NOT the live database" in first, first

    # ONCE per process, not once per connection: a script opening ten connections should say it once,
    # and a banner nobody can read past is one people learn to ignore.
    static_source.connect().close()
    assert capsys.readouterr().err == ""


def test_the_live_path_announces_nothing(monkeypatch, capsys) -> None:
    """The converse, and it is the half that keeps the warning meaningful: a diagnostic printed on the
    normal path is noise, and noise is what makes a real warning invisible."""
    monkeypatch.setattr(static_source, "_ANNOUNCED", False)
    monkeypatch.delenv("RUNG_DATA_SOURCE", raising=False)
    assert static_source.is_static() is False
    assert capsys.readouterr().err == ""


def test_cursor_fetchone_consumes_like_psycopg() -> None:
    """`fetchone` must ADVANCE. It used to return `rows[0]` forever, so the standard psycopg drain
    `while (row := cur.fetchone()) is not None` looped on the first row and a bounded loop saw it
    duplicated — silently divergent results between static mode and live Postgres."""
    cur = static_source._Cursor([(1,), (2,)], [])
    drained = []
    while (row := cur.fetchone()) is not None:
        drained.append(row)
    assert drained == [(1,), (2,)]
    assert cur.fetchone() is None  # stays exhausted


def test_cursor_fetchall_and_iteration_share_the_position() -> None:
    cur = static_source._Cursor([(1,), (2,), (3,)], [])
    assert cur.fetchone() == (1,)
    assert cur.fetchall() == [(2,), (3,)]  # only the unfetched remainder, as psycopg returns
    assert cur.fetchall() == []
    it = static_source._Cursor([(1,), (2,)], [])
    assert next(iter(it)) == (1,)
    assert it.fetchone() == (2,)  # iteration consumed the first row
