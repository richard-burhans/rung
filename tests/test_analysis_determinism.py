"""Guard test: a cross-validation fold drawn over an unordered SQL query is not reproducible.

`sklearn.model_selection.cross_val_score(est, X, y, cv=5)` — and a bare `KFold`/`StratifiedKFold`
without `shuffle=True` — assigns folds as **contiguous blocks of row positions**. If `X` comes from a
`SELECT` with no `ORDER BY`, the database returns rows in a plan-dependent order, so the *same seed*
selects different rows each run and the reported statistic silently drifts run-to-run. It never raises;
it just prints a different number. This is an instance of assuming a deterministic implementation of a
non-deterministic specification (Shi et al., ICST 2016) over a data-access API — and it bit this
project repeatedly before it was expressed as this invariant.

The rule: **a script that draws an UNSHUFFLED cross-validation fold must not execute a row-returning
`SELECT` that lacks a total order.** A query is exempt when it is an aggregate (a scalar aggregate or a
`GROUP BY` — one row per group, order-invariant) or already carries `ORDER BY`. A module that passes
`shuffle=True` to its splitter is exempt wholesale (the shuffle supplies the order).

Two things this static check deliberately does NOT do, because they need dataflow the AST does not
give cheaply and would make the gate cry wolf:
- It does not map *which* query feeds *which* splitter — so it requires *every* non-aggregate SELECT
  in a CV-using module to be ordered, not only the one feeding the fold. Ordering a query that did not
  need it costs a sort; not ordering one that did costs a wrong number in a paper.
- It does not cover positional `rng.choice` / `permutation` over a query result. That pattern spans
  cosmetic plot subsamples (harmless) and stat-bearing subsamples (not), and telling them apart needs
  dataflow. That case stays a rule for humans; this gate enforces only the high-confidence CV case.

Parses source with :mod:`ast` (no import, no DB) so a regression is reported as ``file:lineno``.
"""

import ast
import re
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
SCRIPTS_DIR: Path = REPO_ROOT / "scripts"

_SPLITTERS = frozenset({"KFold", "StratifiedKFold", "GroupKFold", "TimeSeriesSplit"})
# select-list aggregate → the query returns one row per group; order cannot affect the result.
_AGGREGATE = re.compile(r"\b(count|sum|avg|min|max|stddev|variance|corr)\s*\(", re.IGNORECASE)
_SELECT = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


def _sql_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string / f-string literal in the module that starts a SELECT, with its line number.

    f-strings are flattened with a placeholder for each interpolation so the ``{cols}`` hole in
    ``f"SELECT {cols} FROM …"`` does not hide the SELECT.
    """
    # ast.walk descends INTO a JoinedStr's Constant children, so a chunk before an f-string hole
    # ("SELECT … FROM … WHERE ") would be seen on its own — missing the ORDER BY that trails the hole.
    # Skip nodes nested inside a JoinedStr; judge the whole f-string as one flattened unit.
    nested = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in nested:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else " _hole "
                           for v in node.values)
        else:
            continue
        if _SELECT.match(text) and " FROM " in text.upper():
            out.append((node.lineno, text))
    return out


def _draws_unshuffled_cv(tree: ast.AST) -> bool:
    """True if the module uses an UNSHUFFLED CV splitter (the fold = contiguous row positions)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name) else "")
        shuffled = any(
            kw.arg == "shuffle" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        if name in _SPLITTERS and not shuffled:
            return True
        # cross_val_score(..., cv=<int>) → an unshuffled (Stratified)KFold under the hood.
        if name == "cross_val_score":
            cv = next((kw.value for kw in node.keywords if kw.arg == "cv"), None)
            if cv is None or (isinstance(cv, ast.Constant) and isinstance(cv.value, int)):
                return True
    return False


def _module_shuffles(tree: ast.AST) -> bool:
    """True if ANY splitter in the module is constructed with ``shuffle=True`` — module-wide exempt."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else "")
            if name in _SPLITTERS and any(
                kw.arg == "shuffle" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in node.keywords
            ):
                return True
    return False


def _is_ordered_or_aggregate(sql: str) -> bool:
    up = sql.upper()
    if "ORDER BY" in up or "GROUP BY" in up:
        return True
    # a scalar aggregate (SELECT count(*)/round(avg(...)) FROM …) returns one row → order-invariant
    select_list = up.split(" FROM ", 1)[0]
    return bool(_AGGREGATE.search(select_list))


def test_no_unordered_select_feeds_an_unshuffled_cv_fold() -> None:
    offenders: list[str] = []
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not _draws_unshuffled_cv(tree) or _module_shuffles(tree):
            continue
        for lineno, sql in _sql_strings(tree):
            if not _is_ordered_or_aggregate(sql):
                offenders.append(f"{path.name}:{lineno} — SELECT without ORDER BY in a module drawing "
                                 f"an unshuffled CV fold: {sql.strip()[:70]}…")
    assert not offenders, (
        "A cross-validation fold is drawn over an unordered SQL query — the folds depend on the "
        "database's row order, so the result is not reproducible. Add `ORDER BY <primary key>` to the "
        "query, or pass `shuffle=True` to the splitter.\n  "
        + "\n  ".join(offenders)
    )


# ── unit-level checks of the classifier, so the guard itself is trustworthy ──

def _t(src: str) -> ast.AST:
    return ast.parse(src)


def test_shuffle_true_is_recognized_and_exempts_the_module() -> None:
    assert _module_shuffles(_t("KFold(n_splits=5, shuffle=True, random_state=0)"))
    assert not _module_shuffles(_t("KFold(n_splits=5)"))
    assert _draws_unshuffled_cv(_t("KFold(5)"))
    assert not _draws_unshuffled_cv(_t("KFold(5, shuffle=True)"))
    assert _draws_unshuffled_cv(_t("cross_val_score(m, X, y, cv=5)"))


def test_aggregate_and_ordered_queries_are_exempt() -> None:
    assert _is_ordered_or_aggregate("SELECT a, b FROM t ORDER BY id")
    assert _is_ordered_or_aggregate("SELECT state, count(*) FROM t GROUP BY state")
    assert _is_ordered_or_aggregate("SELECT round(100.0*count(*)/count(*),1) FROM t WHERE x")
    assert not _is_ordered_or_aggregate("SELECT a, b FROM t WHERE x")
