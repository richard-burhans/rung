"""Every text read/write names its encoding — enforced, not remembered.

`dignified-python` requires `encoding="utf-8"` on all text I/O, and the packages honoured it: `rung/` and
`rung_intel/` were 100% clean. `scripts/` and `research/` were not — 52 calls relied on the platform
default, because nothing checked and a human had to remember.

The one that mattered is `scripts/build_public_repo.py`, which **writes the published artifact**. On a
box where `locale.getpreferredencoding()` is not UTF-8, every non-ASCII character in the public README,
the generated `CLAUDE.md`, the design references — the → and – and ≥ this project's prose is full of —
either mangles or raises. It would have been a build that "worked on my machine" and shipped mojibake.

The house style is an AST guard at the chokepoint (`test_http.py` does the same for `make_session`),
because a rule you must remember is not a rule. Ruff's own check for this (`PLW1514`) is preview-only
and could not be enabled without dragging in the rest of the preview rule set.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREES = ("rung", "rung_intel", "scripts", "research", "tests", "examples")

# `Path.read_text`/`write_text` and builtin `open` are text-mode by default. `read_bytes`/`write_bytes`
# take no encoding, and a `"b"` mode makes `open` binary — those are skipped below.
TEXT_IO = frozenset({"read_text", "write_text", "open"})


def _binary(call: ast.Call) -> bool:
    """An `open(..., 'rb')`-style call: binary mode takes no encoding."""
    return any(
        isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "b" in arg.value
        and set(arg.value) <= set("rwaxbt+")
        for arg in call.args
    )


def _imported_modules(tree: ast.Module) -> set[str]:
    """The names bound by `import X` / `import X as Y`.

    Needed because `open` is not only a builtin: `pdfplumber.open(io.BytesIO(...))` is a THIRD-PARTY open
    that takes no `encoding` at all, and flagging it would make this guard cry wolf — which is how a guard
    gets muted. A `<module>.open(...)` is somebody else's API; only `open(...)` and `<path>.open(...)` are
    ours to get right.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}
    return out


def _zipfile_handles(tree: ast.Module) -> set[str]:
    """Names bound to a `ZipFile(...)` — `with zipfile.ZipFile(p) as zf:` binds `zf`.

    `ZipFile.open` returns a BINARY stream and accepts no `encoding` — `ty` rejects it outright. So a
    guard that demanded one here would be demanding a type error, and the two checks would contradict
    each other. (They did: the first draft of this test "fixed" three `zf.open(...)` calls and `ty`
    failed the gate.) The AST can see where the handle came from, so it doesn't have to guess.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        call = node.context_expr if isinstance(node, ast.withitem) else None
        target = node.optional_vars if isinstance(node, ast.withitem) else None
        match call:
            case ast.Call(func=ast.Attribute(attr="ZipFile") | ast.Name(id="ZipFile")):
                if isinstance(target, ast.Name):
                    out.add(target.id)
    return out


def _offenders(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = _imported_modules(tree) | _zipfile_handles(tree)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        match node.func:
            case ast.Attribute(value=ast.Name(id=recv), attr=str(name)) if recv in modules:
                continue          # a third party's `open`, not a file open (pdfplumber, zipfile, …)
            case ast.Attribute(attr=str(name)) | ast.Name(id=str(name)) if name in TEXT_IO:
                pass
            case _:
                continue
        if _binary(node) or any(kw.arg == "encoding" for kw in node.keywords):
            continue
        out.append((node.lineno, name))
    return out


def test_every_text_io_call_names_its_encoding() -> None:
    found: list[str] = []
    for tree in TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            found += [
                f"{path.relative_to(ROOT)}:{line} — {name}() with no encoding="
                for line, name in _offenders(path)
            ]
    assert not found, (
        "text I/O without an explicit encoding=\"utf-8\" (the platform default is not a decision):\n  "
        + "\n  ".join(found)
    )
