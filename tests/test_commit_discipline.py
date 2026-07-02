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

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DB_PATH = _REPO_ROOT / "rung" / "db.py"
_INTEL_DIR = _REPO_ROOT / "rung_intel" / "rung_intel"

# Function-name prefixes that denote a low-level write helper (the caller owns the commit).
_WRITE_PREFIXES = ("insert_", "upsert_", "set_", "delete_", "replace_", "record_", "clear_")

# The distributed-policy modules that document "caller commits" (like the db.py helpers): the write
# helper only stages the row; the orchestrator owns the transaction. Same contract, different home.
_CALLER_COMMITS_MODULES = (
    _REPO_ROOT / "rung" / "rate_limit.py",
    _INTEL_DIR / "proxy_store.py",
    _INTEL_DIR / "proxy_tiers.py",
)


def _calls_commit(node: ast.AST) -> bool:
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


def test_distributed_policy_helpers_leave_commit_to_caller() -> None:
    """The cross-worker policy modules (rate_limit / proxy_store / proxy_tiers) each document
    'caller commits' — the same two-tier discipline as db.py, just outside it. A stray commit in,
    say, ``proxy_store.report_proxy`` would break the caller's per-claim atomicity and pass green,
    since ``test_db_write_helpers_do_not_commit`` scans only db.py."""
    offenders = [
        f"{path.name}::{node.name}"
        for path in _CALLER_COMMITS_MODULES
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _calls_commit(node)
    ]
    assert not offenders, (
        "distributed-policy helper commits — the caller owns the transaction boundary "
        f"(these modules document 'caller commits'): {offenders}"
    )
