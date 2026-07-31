"""Guard test: low-level db write helpers must NOT commit (cross-cutting contract 2).

The two-tier commit discipline: low-level write helpers (``insert_*`` / ``upsert_*`` / ``set_*`` /
``delete_*`` / ``replace_*`` / ``record_*`` / ``clear_*``) leave the transaction boundary to their
caller; only the schema-creation helpers and the high-level orchestrators commit. The code currently
honours it — but nothing stopped a future edit from slipping a ``conn.commit()`` into a write helper
(which would break the per-claim atomicity the queue relies on). This parses the engine modules with
:mod:`ast` and fails with the offending function name, mirroring ``test_http.py``.

It parses **both** ``rung/db.py`` and ``rung/reference_db.py``. Until 2026-07-18 it read only the
former, while ``reference_db.py`` held 57 functions — 18 of them matching the write-helper prefixes —
entirely unguarded: the invariant enforced at one caller and bypassed at the other.
"""

import ast
from pathlib import Path

import _trees

_REPO_ROOT = Path(__file__).resolve().parents[1]
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

# Only the modules present here — the public build ships `rung/` alone, and reading an absent
# overlay module raises on a path a public contributor cannot create. See `tests/_trees`.
_CALLER_COMMITS_MODULES = tuple(p for p in _CALLER_COMMITS_MODULES if p.is_file())


def _calls_commit(node: ast.AST) -> bool:
    """Whether a function body contains a ``<something>.commit()`` call."""
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "commit"
        for sub in ast.walk(node)
    )


# Both engine modules hold write helpers under the same contract.
_WRITE_HELPER_MODULES = (
    _REPO_ROOT / "rung" / "db.py",
    _REPO_ROOT / "rung" / "reference_db.py",
)
# No exemption list, deliberately. `reference_db.py`'s two `conn.commit()` calls live in
# `create_reference_tables` / `ensure_fx_rates`, which own their transactions by design — but neither
# name starts with a write-helper prefix, so neither is a candidate and an exemption for them would be
# dead code that reads like a live decision. A name-keyed exemption would also be worse than useless:
# it would apply across BOTH modules, silently excusing a future same-named helper in `db.py`.


def test_db_write_helpers_do_not_commit() -> None:
    offenders = [
        f"{path.name}::{node.name}"
        for path in _WRITE_HELPER_MODULES
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        # AsyncFunctionDef too: matching only FunctionDef let an `async def insert_x` that commits
        # pass green. The sibling test below already used the union; this one did not.
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
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
    expected = _trees.floor(rung=1, rung_intel=2)   # 3 in the monorepo, 1 in the public build
    assert len(_CALLER_COMMITS_MODULES) >= expected, (
        f"only {len(_CALLER_COMMITS_MODULES)} of {expected} caller-commits modules found — they "
        "moved, and this guard is checking less than its docstring claims"
    )
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
