"""Guard test: the suite must hold exactly ONE `conftest` module, imported by exactly one name.

`tests/` has no `__init__.py` and `pyproject.toml` puts the repo root on `pythonpath`, so
`conftest.py` is reachable by two different names — and Python treats those as two unrelated modules:

    CONFTEST MODULES: {'conftest': 140444208459472, 'tests.conftest': 140444146509488}

That is not a style preference, it is a correctness boundary. `conftest.py` keeps module-level state:
`_open_conns`, the list of connections whose throwaway schemas the autouse `_drop_test_schemas` fixture
drops after each test. Pytest loads the file as `conftest` and runs the fixture against THAT copy's
list, while seven test files said `from tests.conftest import pg_conn` and appended to a SECOND copy's
list. Nothing ever drained it:

    _open_conns identity per module: {'conftest': (…, 0), 'tests.conftest': (…, 6)}

The result was ~26 leaked `test_%` schemas per run, invisible because the session-start sweep used to
drop every `test_%` schema unconditionally — which is exactly the behaviour that had to go when the
suite went parallel. The leak had been masked by a bug, and became visible only when that bug was fixed.

A rule you must remember is not a rule (`tests/test_http.py`, `tests/test_text_io_encoding.py`), and
this one is invisible when broken: the import works, the tests pass, and the only symptom is debris in
a database nobody looks at. So it is AST-enforced rather than written down.
"""

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: The only name `conftest.py` may be imported under. Pytest loads it as top-level `conftest` (there is
#: no `tests/__init__.py`), so this is the name that owns the live fixture state.
_SANCTIONED = "conftest"
_FORBIDDEN_PREFIXES = ("tests.conftest", "tests")


def _conftest_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Every `import X` / `from X import …` in the file that resolves to a conftest, as (line, name)."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == _SANCTIONED or node.module.endswith(".conftest"):
                found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _SANCTIONED or alias.name.endswith(".conftest"):
                    found.append((node.lineno, alias.name))
    return found


def test_conftest_is_never_imported_as_a_package_submodule() -> None:
    """`from conftest import …`, never `from tests.conftest import …`.

    The second form loads an independent copy whose `_open_conns` the teardown fixture never sees, so
    every schema handed out through it leaks. Seven files did this; the fix is the import line.
    """
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [
            f"{path.name}:{line}: imports `{module}` — use `{_SANCTIONED}` instead"
            for line, module in _conftest_imports(tree)
            if module != _SANCTIONED
        ]
    assert not offenders, (
        "conftest must be imported under ONE name, or its module-level `_open_conns` splits in two and "
        "the autouse schema teardown drains the wrong list (~26 leaked schemas per run):\n  "
        + "\n  ".join(offenders)
    )


def test_the_teardown_fixture_and_pg_conn_share_one_open_conns() -> None:
    """The invariant itself, checked directly rather than only through the import spelling.

    The test above pins the known cause; this pins the CONSEQUENCE, so a new way of ending up with two
    copies (a `sys.path` change, an `__init__.py` appearing, a different import mode) still fails here.
    """
    import sys

    live = {
        name: id(module._open_conns)
        for name, module in list(sys.modules.items())
        if name.split(".")[-1] == "conftest" and hasattr(module, "_open_conns")
    }
    assert len(set(live.values())) <= 1, (
        f"more than one conftest `_open_conns` list is live: {live}. Connections registered against one "
        "are invisible to the autouse teardown that drains the other, so their schemas leak."
    )
