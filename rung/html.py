"""Generic page-structure and value primitives for the pure platform helpers — a tier-0 leaf.

Imports nothing internal, on purpose: the per-platform helpers (`PURE_HELPERS` in
`tests/test_import_layering.py`) may import only tier-0 leaves, and `rung.text` — the other
candidate home — loads the cannabis taxonomy, so a helper importing it would stop being pure.

The 2026-09-26 architecture audit (item P-37) found each of these copied across helpers, and
the copies disagreeing: three balanced-brace scanners, one of them not string-aware (a ``}``
inside a string mis-closed it); two RSC flight parsers, one with no error handling; four
embedded-state regexes, one still fragile to attribute order. They live here once.

`script_json` takes the script's id as an ARGUMENT, deliberately: this file ships in the public
package, whose build rewrites one well-known Next.js id wherever it appears, so a parser that
carried it as a literal would ship rewritten and return ``{}`` on every real page. The callers
pass the id; the shipped tests use a neutral one.
"""

import json
import re

#: One RSC flight chunk: ``self.__next_f.push([1,"…"])`` — the quoted string is a JSON string
#: literal whose decoded text is a slice of the flight stream. Objects span chunk boundaries, so
#: the chunks are decoded and joined before anything is read from them.
_FLIGHT_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_CLOSER = {"{": "}", "[": "]"}


def as_float(value: object) -> float | None:
    """A platform numeric as a float: an int or float, or a numeric STRING (``"34.96"``,
    ``"320.00000"``); None for anything else — a bool, a blank string, a non-numeric string.

    Two identical private copies (tendy, dispense) were folded into this on 2026-10-06; the
    mappers' `menu_extractors._number` has the same contract one tier up.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def balanced_json(text: str, open_at: int, *, limit: int | None = None) -> str | None:
    """The JSON substring starting at the opening brace or bracket at ``open_at``, honouring
    nesting and string literals (a closer inside a string does not count), or None if it never
    closes — or not within ``limit`` characters, where one is given."""
    if open_at >= len(text):
        return None
    opener = text[open_at]
    closer = _CLOSER.get(opener)
    if closer is None:
        return None
    end = len(text) if limit is None else min(len(text), open_at + limit)
    depth = 0
    in_string = False
    escaped = False
    for index in range(open_at, end):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[open_at : index + 1]
    return None


def balanced_object(text: str, open_at: int, *, limit: int | None = None) -> dict | None:
    """`balanced_json` parsed, when it is a JSON object; None otherwise."""
    blob = balanced_json(text, open_at, limit=limit)
    if blob is None:
        return None
    try:
        parsed = json.loads(blob)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def script_json(html: str | None, script_id: str) -> dict:
    """The JSON object a ``<script id="…">`` tag embeds, in either attribute order, or ``{}``
    when the tag is absent, its body is not JSON, or the JSON is not an object."""
    if not html:
        return {}
    pattern = re.compile(
        r"<script[^>]+id=[\"']" + re.escape(script_id) + r"[\"'][^>]*>(.*?)</script>", re.S
    )
    match = pattern.search(html)
    if match is None:
        return {}
    try:
        parsed = json.loads(match.group(1).strip())
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def flight_text(html: str | None, *, strict: bool = False) -> str:
    """A page's RSC flight stream reassembled into one decoded string (``""`` if none).

    A chunk whose string literal does not decode is skipped by default (Dispense reads venue ids out
    of whatever decodes). ``strict=True`` raises ``ValueError`` instead — for a caller that turns the
    stream into a MENU, where a skipped chunk is missing products and a partial list must not pass as
    the whole one. SweedPOS's own copy raised before P-37 merged the two, and the merge silently
    made it tolerant (the 2026-10-06 ultra review)."""
    if not html:
        return ""
    parts: list[str] = []
    for match in _FLIGHT_PUSH_RE.finditer(html):
        try:
            parts.append(json.loads(match.group(1)))
        except ValueError:
            if strict:
                raise
            continue
    return "".join(parts)
