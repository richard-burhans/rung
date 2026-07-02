"""Guard test: the POSITIVE half of the two-tier commit discipline (cross-cutting contract 2).

``test_commit_discipline.py`` guards the *negative* half — low-level ``db.py`` write helpers must
NOT commit. This guards the *positive* half: the high-level orchestrators that each own a unit of
work MUST commit it themselves (the queue's per-claim atomicity + the stage runners' "a scrape is
durable when the job completes" guarantee depend on it). The code honours it today, but nothing
stopped a future edit from dropping a ``conn.commit()`` — the write would then silently roll back at
connection close. This ast-parses each named orchestrator and fails if its subtree contains no
``.commit()`` call. Mirrors ``test_commit_discipline.py`` / ``test_write_isolation.py``.

``recon.run_recon`` is deliberately EXCLUDED — it reads/returns only; the CLI owns its writes
(ARCHITECTURE.md "Commit discipline"). ``seed_companies._seed`` is the committing seed orchestrator
(``main`` delegates to it).
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC = REPO_ROOT / "rung"
INTEL = REPO_ROOT / "rung_intel" / "rung_intel"

# (module path, function name) for each orchestrator contract 2 says commits its own unit of work.
_COMMITTING_ORCHESTRATORS = [
    (PUBLIC / "access.py", "run_target"),
    (PUBLIC / "sources" / "dedupe.py", "run_dedupe"),
    (PUBLIC / "sources" / "extract.py", "run_extract_states"),
    (PUBLIC / "seed_companies.py", "_seed"),
    (INTEL / "menus.py", "run_store_menus"),
    (INTEL / "company_stores.py", "run_company_stores"),
]


def _calls_commit(node: ast.AST) -> bool:
    """Whether the subtree contains a ``<something>.commit()`` call (nested closures included)."""
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "commit"
        for sub in ast.walk(node)
    )


def _find_function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.relative_to(REPO_ROOT)} no longer defines {name}() (renamed?)")


def test_orchestrators_that_own_a_unit_of_work_commit() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}::{name}"
        for path, name in _COMMITTING_ORCHESTRATORS
        if not _calls_commit(_find_function(path, name))
    ]
    assert not offenders, (
        "high-level orchestrator no longer commits its own unit of work — the write will roll back "
        f"at connection close (cross-cutting contract 2, the positive half): {offenders}"
    )
