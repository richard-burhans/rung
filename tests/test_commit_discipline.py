"""Guard test: low-level db.py write helpers must NOT commit (cross-cutting contract 2).

The two-tier commit discipline: ``db.py``'s low-level write helpers (``insert_*`` / ``upsert_*``
/ ``set_*`` / ``delete_*`` / ``replace_*`` / ``record_*`` / ``clear_*``) leave the transaction
boundary to their caller; only ``db.create_tables`` and the high-level orchestrators commit. The
code currently honours it — but nothing stopped a future edit from slipping a ``conn.commit()``
into a write helper (which would break the per-claim atomicity the queue relies on). This parses
``db.py`` with :mod:`ast` and fails with the offending function name, mirroring ``test_http.py``.
"""

import ast
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parents[1] / "rung" / "db.py"

# Function-name prefixes that denote a low-level write helper (the caller owns the commit).
_WRITE_PREFIXES = ("insert_", "upsert_", "set_", "delete_", "replace_", "record_", "clear_")


def _calls_commit(node: ast.FunctionDef) -> bool:
    """Whether a function body contains a ``<something>.commit()`` call."""
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "commit"
        for sub in ast.walk(node)
    )


def test_db_write_helpers_do_not_commit() -> None:
    tree = ast.parse(_DB_PATH.read_text(encoding="utf-8"), filename=str(_DB_PATH))
    offenders = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith(_WRITE_PREFIXES)
        and _calls_commit(node)
    ]
    assert not offenders, (
        "Low-level db write helper commits — the caller owns the transaction boundary "
        f"(cross-cutting contract 2): {offenders}"
    )
