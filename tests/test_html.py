"""`rung.html` — the tier-0 primitives the pure platform helpers share.

The parsers below were each copied across helpers until audit P-37 (2026-10-06), and the copies
disagreed: one brace scanner was not string-aware, one flight parser had no error handling, one
embedded-state regex required one attribute order. These pin the one behaviour they share now.
The embedded-state tests use a NEUTRAL script id: the public build rewrites the well-known one.
"""

import json

import pytest

from rung.html import as_float, balanced_json, balanced_object, flight_text, script_json


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (3, 3.0), (2.5, 2.5), ("34.96", 34.96), ("320.00000", 320.0), (" 7 ", 7.0),
        (True, None), (False, None), ("", None), ("   ", None), ("n/a", None), (None, None),
        ([1], None), ({"v": 1}, None),
    ],
)
def test_as_float_reads_numbers_and_numeric_strings_and_nothing_else(value: object, expected: float | None) -> None:
    assert as_float(value) == expected


def test_balanced_json_honours_nesting_and_string_literals() -> None:
    text = 'x = {"a": {"b": [1, 2]}, "s": "has } brace", "e": "esc \\" quote"} ;'
    blob = balanced_json(text, text.index("{"))
    assert blob is not None
    assert json.loads(blob) == {"a": {"b": [1, 2]}, "s": "has } brace", "e": 'esc " quote'}
    assert balanced_json("[1, [2, 3]] rest", 0) == "[1, [2, 3]]"           # a bracket opener
    assert balanced_json('{"never": "closes"', 0) is None
    assert balanced_json('{"a": 1}', 0, limit=3) is None                   # not within the limit
    assert balanced_json("abc", 0) is None and balanced_json("", 0) is None  # no opener there


def test_balanced_object_is_the_parsed_object_or_none() -> None:
    assert balanced_object('pre {"a": {"b": 2}} post', 4) == {"a": {"b": 2}}
    assert balanced_object('[1, 2]', 0) is None                           # a list is not an object
    assert balanced_object('{"a": }', 0) is None                          # balanced but not JSON


@pytest.mark.parametrize(
    "tag",
    [
        '<script id="__PAGE_STATE__" type="application/json">{"props": {"n": 1}}</script>',
        '<script type="application/json" id="__PAGE_STATE__">{"props": {"n": 1}}</script>',
        "<script id='__PAGE_STATE__'>\n{\"props\": {\"n\": 1}}\n</script>",
    ],
)
def test_script_json_reads_the_tag_in_either_attribute_order(tag: str) -> None:
    assert script_json(f"<html>{tag}</html>", "__PAGE_STATE__") == {"props": {"n": 1}}


def test_script_json_is_empty_for_a_missing_tag_bad_json_or_a_non_object() -> None:
    assert script_json("<html>no script</html>", "__PAGE_STATE__") == {}
    assert script_json('<script id="__PAGE_STATE__">{not json</script>', "__PAGE_STATE__") == {}
    assert script_json('<script id="__PAGE_STATE__">[1, 2]</script>', "__PAGE_STATE__") == {}
    assert script_json('<script id="__OTHER__">{"a": 1}</script>', "__PAGE_STATE__") == {}
    assert script_json(None, "__PAGE_STATE__") == {} and script_json("", "__PAGE_STATE__") == {}
    # The id is escaped, not interpreted: a regex metacharacter in it matches itself.
    assert script_json('<script id="a.b">{"ok": 1}</script>', "a.b") == {"ok": 1}
    assert script_json('<script id="axb">{"ok": 1}</script>', "a.b") == {}


def test_flight_text_joins_decoded_chunks_and_skips_a_malformed_one() -> None:
    page = (
        '<script>self.__next_f.push([1,"{\\"venue\\":{\\"id\\":"])</script>'
        '<script>self.__next_f.push([1,"\\"v1\\"}}"])</script>'
        '<script>self.__next_f.push([1,"bad \\x escape"])</script>'
    )
    assert flight_text(page) == '{"venue":{"id":"v1"}}'
    assert flight_text("") == "" and flight_text(None) == ""
    assert flight_text("<html>no flight stream</html>") == ""
