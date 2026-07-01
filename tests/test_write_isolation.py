"""Guard test: the protected write-isolation contracts (docs/stage_contracts.md; cross-cutting
contract 2) are enforced, not merely honoured.

Two single-writer invariants the stage contracts assert:

1. ``company_stores.canonical_company_id`` / ``storefront_name`` are written ONLY from ``db.py``
   (``set_store_canonical`` / ``set_store_storefront`` + the dedupe clear/realign pass) — every
   other module READS them (compare/dedupe/menus filter on them) but never writes.
2. The ``access_methods`` registry table is upserted ONLY via ``db.record_access_attempt``.

The code honours both today, but nothing stopped a future overlay edit from issuing a direct
UPDATE/INSERT — it would pass ruff+ty+pytest green and silently break the single-writer guarantee the
dedupe realign + the access-method registry rely on. This ast-parses every package module OUTSIDE
``db.py`` (the sole sanctioned write home), pulls out its SQL string literals, and fails with the
offending ``file:line`` if any performs such a write. Mirrors test_commit_discipline.py / test_http.py.
Reads (``SELECT … FROM access_methods``, ``WHERE canonical_company_id = …``) are deliberately NOT
matched — only the SET clause of an ``UPDATE company_stores`` and an INSERT/UPDATE of ``access_methods``.
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = REPO_ROOT / "rung"
INTEL_DIR = REPO_ROOT / "dispensary_scraper_intel" / "dispensary_scraper_intel"
DB_PATH = PUBLIC_DIR / "db.py"  # the sole sanctioned write home — excluded from the scan

# A write (INSERT/UPDATE — never SELECT) targeting the access_methods registry table.
_ACCESS_METHODS_WRITE = re.compile(r"\b(?:insert\s+into|update)\s+access_methods\b", re.I)
# The SET clause of an ``UPDATE company_stores …`` (captured up to WHERE / RETURNING / end-of-string),
# so a column named only in a WHERE filter (a read) is not mistaken for a write.
_COMPANY_STORES_SET = re.compile(
    r"\bupdate\s+company_stores\s+set\b(.*?)(?:\bwhere\b|\breturning\b|$)", re.I | re.S
)
_PROTECTED_COLUMNS = ("canonical_company_id", "storefront_name")


def _sql_literals(tree: ast.AST) -> Iterator[tuple[str, int]]:
    """Yield ``(text, lineno)`` for every string literal — plain ``str`` constants plus the constant
    parts of an f-string (so an ``f"UPDATE {table} …"`` is still inspected for its literal text)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value, node.lineno
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            yield text, node.lineno


def _writes_protected_target(sql: str) -> bool:
    if _ACCESS_METHODS_WRITE.search(sql):
        return True
    match = _COMPANY_STORES_SET.search(sql)
    return bool(match and any(col in match.group(1).lower() for col in _PROTECTED_COLUMNS))


def _package_files_outside_db() -> list[Path]:
    roots = [PUBLIC_DIR, *([INTEL_DIR] if INTEL_DIR.exists() else [])]
    return [
        path
        for root in roots
        for path in root.rglob("*.py")
        if path.name != "__init__.py" and "__pycache__" not in path.parts and path != DB_PATH
    ]


def test_protected_columns_and_access_methods_are_written_only_in_db() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}"
        for path in _package_files_outside_db()
        for sql, lineno in _sql_literals(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if _writes_protected_target(sql)
    ]
    assert not offenders, (
        "company_stores.canonical_company_id/storefront_name + the access_methods table must be "
        "written ONLY via the db.py helpers (single-writer contract, docs/stage_contracts.md): "
        f"direct write(s) at {offenders}"
    )
