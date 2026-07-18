"""Static-file data source — run the analysis off a frozen clean dataset instead of live Postgres.

The analysis suite (``scripts/conference_*.py``) funnels every query through
:func:`rung.db.get_connection`, which normally opens live Postgres. This module lets that same seam
serve a **DuckDB view over the clean-dataset Parquet** instead, so the *canonical* analysis scripts run
**unchanged** off a portable file — the mechanism that lets a Galaxy workflow (or an outside researcher)
reproduce every D1 analysis with no re-implementation and no database.

It is enabled by two environment variables (read by ``db.get_connection``):

    RUNG_DATA_SOURCE=static   RUNG_STATIC_PATH=/path/to/clean_d1/v3

``RUNG_STATIC_PATH`` is a directory holding ``store_products.parquet`` + ``state_programs.parquet`` (the
``build_clean_d1.py`` export). DuckDB speaks the Postgres dialect the scripts emit — the JSONB
``terpenes_std->>'x'::float`` extract, ``count(*) FILTER (WHERE …)``, the ``products_normalized`` view — so
the queries need no rewrite; the one requirement is that ``terpenes_std`` arrives as a JSON **string** (the
export writes it that way). A thin psycopg-shaped wrapper exposes the ``.execute() → iterable cursor`` +
``.fetchone()/.fetchall()/.close()`` surface the scripts use.

Leak-safe by construction: it takes a file path, never a credential — a published Galaxy tool that sets
``RUNG_DATA_SOURCE=static`` carries no ``DATABASE_URL``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# DuckDB expression the products_normalized view derives (mirrors reference_db, DuckDB-dialect).
_CURRENCY = "CASE WHEN prog.country = 'CA' THEN 'CAD' ELSE 'USD' END"

# The DuckDB twin of reference_db._CREATE_PRODUCTS_NORMALIZED_VIEW: the SAME output columns in the SAME
# order, only the dialect differs (::double vs ::numeric, local _CURRENCY). Hoisted to a module constant
# so a lockstep test can pin its column list to the canonical Postgres view — a column added to one view
# but not this one silently diverges the static/Galaxy path from live Postgres, and the import guard
# (which inspects imports, not SQL) can't catch it. See tests/test_db.py::test_products_normalized_views_stay_in_sync.
_PRODUCTS_NORMALIZED_VIEW_SQL = f"""
CREATE VIEW products_normalized AS
SELECT sp.id, sp.company_id, sp.state, sp.store_key, sp.platform, sp.source,
       sp.external_product_id, sp.name, sp.brand, sp.category_std AS category,
       sp.strain_type_std AS strain_type, sp.price, sp.size_g,
       CASE WHEN sp.size_g > 0 AND sp.price IS NOT NULL
            THEN round((sp.price / sp.size_g)::double, 2) END AS price_per_g,
       sp.thc, sp.cbd, sp.thc_mg, sp.cbd_mg, sp.terp_total, sp.terpenes_std, sp.scraped_at,
       sp.product_type_std AS product_type, sp.cannabinoids_std, {_CURRENCY} AS currency,
       sp.obtention_std
FROM store_products sp
LEFT JOIN state_programs prog ON prog.abbr = sp.state
"""


class _Cursor:
    """A psycopg-cursor-shaped view over a DuckDB result: iterable, fetchone/fetchall, description, close."""

    def __init__(self, rows: list[tuple], description: list[Any]) -> None:
        self._rows = rows
        self.description = description

    def __iter__(self):
        return iter(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return self._rows

    def close(self) -> None:
        self._rows = []


class StaticConnection:
    """A read-only, psycopg-shaped connection backed by DuckDB over the clean-dataset Parquet.

    Only the surface the analysis scripts use is implemented: ``execute`` (returning a :class:`_Cursor`),
    ``commit``/``rollback`` (no-ops — the source is read-only), ``close``, and the context-manager protocol.
    Writes are refused: a static source is a frozen snapshot, not a place to persist.
    """

    def __init__(self, path: Path) -> None:
        import duckdb  # local import: only needed in static mode

        sp = path / "store_products.parquet"
        prog = path / "state_programs.parquet"
        if not sp.exists():
            raise FileNotFoundError(f"static source missing {sp} — build it with scripts/build_clean_d1.py --parquet")
        self._con = duckdb.connect(":memory:")
        # Postgres `width_bucket(x, low, high, count)` (the McCrary de-heaping histogram) has no DuckDB
        # builtin — supply it as a macro with Postgres's exact semantics (0 below low, count+1 at/above
        # high, else floor(count*(x-low)/(high-low))+1).
        self._con.execute("""
            CREATE MACRO width_bucket(x, lo, hi, n) AS
              CASE WHEN x IS NULL THEN NULL
                   WHEN x < lo THEN 0
                   WHEN x >= hi THEN n + 1
                   ELSE floor(n * (x - lo) / (hi - lo)) + 1 END
        """)
        self._con.execute(f"CREATE VIEW store_products AS SELECT * FROM read_parquet('{sp}')")
        if prog.exists():
            self._con.execute(f"CREATE VIEW state_programs AS SELECT * FROM read_parquet('{prog}')")
        # the products_normalized view the scripts (and _scope currency) may read
        self._con.execute(_PRODUCTS_NORMALIZED_VIEW_SQL)

    def execute(self, query: str, params: Any = None) -> _Cursor:
        if params is not None:
            # psycopg uses `%s` positional placeholders; DuckDB uses `?`. The scripts pass positional
            # params only (a tuple), so a plain swap is exact. Literal `%` in a LIKE pattern lives in the
            # PARAM value, not the query text, so it is untouched.
            rel = self._con.execute(query.replace("%s", "?"), list(params))
        else:
            rel = self._con.execute(query)
        try:
            rows = rel.fetchall()
        except Exception:  # a statement with no result set (unlikely in analysis)
            rows = []
        description = list(rel.description) if rel.description else []
        return _Cursor(rows, description)

    def commit(self) -> None:  # read-only source
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> StaticConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def is_static() -> bool:
    return os.environ.get("RUNG_DATA_SOURCE", "").lower() == "static"


def connect() -> StaticConnection:
    """Open the static source at ``RUNG_STATIC_PATH`` (a build_clean_d1 export directory)."""
    path = os.environ.get("RUNG_STATIC_PATH")
    if not path:
        raise RuntimeError("RUNG_DATA_SOURCE=static requires RUNG_STATIC_PATH=<clean-dataset export dir>")
    return StaticConnection(Path(path))
