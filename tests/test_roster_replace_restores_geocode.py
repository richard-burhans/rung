"""Guard test: a function that DELETEs a state's roster and re-inserts it must restore the geocode.

`delete_dispensaries_for_state` + re-INSERT is how every roster replace works. The source
republishes only what the source publishes, so for the rosters carrying a street but no ZIP (NV, IL,
UT, MD…) the derived latitude/longitude/zip_code/city are destroyed by a scrape that SUCCEEDED.
`compare.geo_key` is `@lat,lon|zip` and `_match_key` is `number street|zip` — both carry the ZIP — so
the state then matches nothing and the project's deliverable silently stops working for it. That is
not hypothetical: it happened on 2026-07-12 to NV and MD, whose fixes `WORK_BACKLOG.md` still
records as DONE.

Fixing the three call sites is not enough. The 2026-07-14 architecture audit's H1 finding was
exactly this shape — an invariant enforced in the module that defines it and defeated at its call
sites — so this checks the CALLERS, statically, and fails on the next one that forgets.

Parses source with :mod:`ast` rather than importing it, so a regression is reported as
``file:lineno`` (the same shape as ``test_http.py``'s session chokepoint).
"""

import ast
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
SEARCH_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "rung",
    REPO_ROOT / "rung_intel",
    REPO_ROOT / "scripts",
)

DESTROYER: str = "delete_dispensaries_for_state"
REINSERT: str = "insert_dispensary"
RESTORE: str = "apply_geocode_cache"

# `reference_db` DEFINES the destroyer and the restore; it re-inserts nothing, so it is not a caller.
EXEMPT_FILES: frozenset[str] = frozenset({"reference_db.py", "db.py"})


def _called_names(node: ast.AST) -> set[str]:
    """Every function name called anywhere under `node` (bare or attribute-qualified)."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _python_files() -> list[Path]:
    return [
        path
        for directory in SEARCH_DIRS
        for path in directory.rglob("*.py")
        if path.name not in EXEMPT_FILES and ".venv" not in path.parts
    ]


def test_every_roster_replace_restores_the_geocode_cache() -> None:
    offenders: list[str] = []
    checked = 0

    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = _called_names(node)
            # Only a function that BOTH deletes a state's roster and re-inserts rows is replacing
            # it. A pure deleter (nothing re-inserted) leaves no rows to re-enrich.
            if not (DESTROYER in called and REINSERT in called):
                continue
            checked += 1
            if RESTORE not in called:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.name}() replaces the "
                    f"roster but never calls {RESTORE}() — a successful scrape will silently "
                    f"un-match this state"
                )

    assert not offenders, "roster replace without a geocode restore:\n  " + "\n  ".join(offenders)
    # If this hits zero the guard has stopped guarding — the call sites were renamed or moved and
    # the test is now vacuously green, which is worse than no test at all.
    assert checked >= 3, (
        f"expected at least 3 roster-replacing functions (scrape_all_states, bootstrap's pool "
        f"replace, ingest_pr_roster); found {checked}. Did they move or get renamed?"
    )
