"""Every argparse `help=` string must survive `--help`.

argparse renders help text with `%`-formatting (so `%(default)s` works), which makes a bare `%` a
format error — and it fires only when someone asks for help, never when the script runs. On
2026-10-09 one script's `--help` died with "unsupported format character ')'" over
"(and by raw terpene %)", and a scan found a second carrying the same bug in "95% CI". Read
statically, so no script is imported and no heavy dependency is needed.
"""

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TREES = ("scripts", "rung", "rung_intel", "faces")
# A `%` argparse can format: `%%`, or a `%(name)s`-style mapping key with a conversion.
_FORMATTABLE = re.compile(r"%%|%\(\w+\)[-#0 +]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]")


def _bare_percents(text: str) -> bool:
    return "%" in _FORMATTABLE.sub("", text)


def _help_strings(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            for keyword in node.keywords:
                if keyword.arg == "help" and isinstance(keyword.value, ast.Constant) \
                        and isinstance(keyword.value.value, str):
                    yield node.lineno, keyword.value.value


def test_no_argparse_help_string_carries_a_bare_percent() -> None:
    offenders = [
        f"{path.relative_to(_REPO)}:{lineno}: {text!r}"
        for tree in _TREES
        for path in sorted((_REPO / tree).rglob("*.py"))
        if ".venv" not in path.parts
        for lineno, text in _help_strings(path)
        if _bare_percents(text)
    ]
    assert not offenders, "write `%%` for a literal percent in argparse help:\n" + "\n".join(offenders)


def test_the_guard_tells_a_bare_percent_from_a_format_key() -> None:
    """ANTI-VACUITY: the exact 2026-10-09 string fails; the forms argparse can render pass."""
    assert _bare_percents("(and by raw terpene %)")
    assert not _bare_percents("(and by raw terpene %%)")
    assert not _bare_percents("default: %(default)s")
