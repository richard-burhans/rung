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


import os
import sys
from pathlib import Path
from typing import Any, Self

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
       sp.obtention_std, sp.potency_implausible
FROM store_products sp
LEFT JOIN state_programs prog ON prog.abbr = sp.state
"""


class _Cursor:
    """A psycopg-cursor-shaped view over a DuckDB result: iterable, fetchone/fetchall, description, close.

    Fetching CONSUMES rows, exactly as psycopg's cursor does: ``fetchone`` advances and returns None
    when exhausted (so the standard ``while (row := cur.fetchone()) is not None`` drain terminates),
    ``fetchall`` returns only what has not been fetched yet, and iteration shares the same position.
    """

    def __init__(self, rows: list[tuple], description: list[Any]) -> None:
        self._rows = rows
        self._pos = 0
        self.description = description

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list[tuple]:
        remaining = self._rows[self._pos:]
        self._pos = len(self._rows)
        return remaining

    def close(self) -> None:
        self._rows = []
        self._pos = 0


# Columns the live `store_products` schema carries that a previously-frozen Parquet vintage may not.
# Add a column here when you add it to the DDL, or a static-mode script dies in the DuckDB binder
# against every dataset built before it. `tests/test_static_source.py` pins the behaviour against a
# deliberately pre-column fixture.
#
# All three below are genuinely absent from the `clean_d1/v3` vintage of **2026-07-17** (29 columns) —
# verified, not assumed: before this map existed, `SELECT terpenes_repaired FROM store_products` against
# that parquet raised `BinderException`. They ARE present in the 2026-08-03 vintage (32 columns), where
# this back-fill is simply a no-op, so the map stays correct for both. Name the vintage, not just the
# posture: a cut is identified by (posture, date) and the older is not reproducible.
# A frozen vintage is deliberately never rebuilt in place, because rebuilding moves numbers a published
# paper already quotes, so the reader has to absorb the gap. NULL is also the honest value: the vintage
# predates the check, so the row was never assessed, which is why
# `reference_db.plausible_potency_where` tests `IS NOT TRUE` rather than `= FALSE`.
_COLUMNS_ADDED_AFTER_FREEZE: dict[str, str] = {
    "category_overridden": "BOOLEAN",
    "terpenes_repaired": "BOOLEAN",
    "potency_implausible": "BOOLEAN",
}


class StaticConnection:
    """A read-only, psycopg-shaped connection backed by DuckDB over the clean-dataset Parquet.

    Only the surface the analysis scripts use is implemented: ``execute`` (returning a :class:`_Cursor`),
    ``commit``/``rollback`` (no-ops — the source is read-only), ``close``, and the context-manager protocol.
    Writes are refused: a static source is a frozen snapshot, not a place to persist.
    """

    def __init__(self, path: Path) -> None:
        # duckdb is OPTIONAL — needed only by this static-parquet mode, and not required to
        # install or run the package — so the import is unresolvable in a plain environment and
        # the type checker has to be told so rather than failing on it.
        import duckdb  # ty: ignore[unresolved-import]  # local import: only needed in static mode

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
        # A FROZEN VINTAGE PREDATES ANY COLUMN ADDED AFTER IT, and it is frozen on purpose — the D1
        # dataset is the published replication path, so rebuilding it to pick up a new column would
        # change the numbers a paper already quotes. Back-fill anything the live schema has and this
        # parquet does not as a typed NULL, so an older vintage still OPENS instead of dying in the
        # binder. (v3 has 29 columns and no `potency_implausible`; without this, adding that column to
        # the view below broke every static-mode script against it.) A NULL flag is also the honest
        # value: the vintage was written before the check existed, so the row was never assessed —
        # which is why `plausible_potency_where` tests `IS NOT TRUE` rather than `= FALSE`.
        self._con.execute(f"CREATE VIEW _sp_raw AS SELECT * FROM read_parquet('{sp}')")
        present = {row[0] for row in self._con.execute("DESCRIBE _sp_raw").fetchall()}
        backfilled = ", ".join(
            f"NULL::{sql_type} AS {column}"
            for column, sql_type in _COLUMNS_ADDED_AFTER_FREEZE.items()
            if column not in present
        )
        self._con.execute(
            f"CREATE VIEW store_products AS SELECT *{', ' + backfilled if backfilled else ''} FROM _sp_raw"
        )
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def is_static() -> bool:
    return os.environ.get("RUNG_DATA_SOURCE", "").lower() == "static"


#: Announce the swap ONCE per process, not once per connection — a script that opens ten connections
#: should say this once, and a banner nobody can read past is a banner people learn to ignore.
_ANNOUNCED = False


def connect() -> StaticConnection:
    """Open the static source at ``RUNG_STATIC_PATH`` (a build_clean_d1 export directory).

    **AND SAY SO ON STDERR.** This function silently substitutes a frozen file for the live database,
    which is exactly what it is for — and exactly why it must be audible. `RUNG_DATA_SOURCE=static` is
    an ambient variable: this sandbox exported it globally from `/etc/sandbox-persistent.sh`, sourced
    before every command AND by `/etc/profile.d` for login shells, so **every** analysis in it read the
    2026-08-03 deposit while believing it was on live data. Adversarial round 39 found it — an
    adjudicator noticed that any agent who did not `unset` it "queried the DEPOSIT while believing it
    was live" — and the round's own live figures were sound only because they happened to go through
    `psycopg` directly rather than this seam.

    Removing the variable from that one file fixes that one sandbox. Announcing the swap fixes the
    class: a future environment can ship it again and nobody is misled. This project's own maxim, moved
    one step sideways — *a gate nobody can observe firing is indistinguishable from one that is not
    there*, and so is a data source nobody can observe being swapped.

    stderr, not stdout: several callers pipe their stdout into a summary or a TSV, and a diagnostic
    that corrupts the artifact it is warning you about would be its own defect.
    """
    global _ANNOUNCED
    path = os.environ.get("RUNG_STATIC_PATH")
    if not path:
        raise RuntimeError("RUNG_DATA_SOURCE=static requires RUNG_STATIC_PATH=<clean-dataset export dir>")
    if not _ANNOUNCED:
        _ANNOUNCED = True
        print(f"rung: RUNG_DATA_SOURCE=static — reading the FROZEN deposit at {path}, "
              "NOT the live database. Unset RUNG_DATA_SOURCE for live data.", file=sys.stderr)
    return StaticConnection(Path(path))
