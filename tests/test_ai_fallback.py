"""Tests for the AI-fallback result normalization (sources/ai_fallback.py).

These exercise `_locations_from_result` directly — no scrapegraphai/Ollama needed — to lock
in the fix for the silent-drop bug: with a pydantic schema, SmartScraperGraph can return the
schema instance (or models inside the list), which the old dict-only handling discarded.
"""

from rung.sources.ai_fallback import (
    _Location,
    _LocationList,
    _locations_from_result,
)


def test_normalizes_schema_instance_to_dicts() -> None:
    # The shape the old code dropped: the pydantic schema instance with model children.
    result = _LocationList(locations=[_Location(name="Store A", city="Pittsburgh")])
    locations = _locations_from_result(result)
    assert locations == [
        {"name": "Store A", "address": None, "city": "Pittsburgh",
         "state": None, "zip_code": None, "phone": None}
    ]


def test_normalizes_plain_dict_shape() -> None:
    result = {"locations": [{"name": "Store B"}]}
    assert _locations_from_result(result) == [{"name": "Store B"}]


def test_normalizes_bare_list_and_drops_non_records() -> None:
    result = [{"name": "Store C"}, "junk", 7, _Location(name="Store D")]
    locations = _locations_from_result(result)
    assert [loc["name"] for loc in locations] == ["Store C", "Store D"]


def test_unrecognized_shapes_yield_empty() -> None:
    assert _locations_from_result(None) == []
    assert _locations_from_result("nope") == []
    assert _locations_from_result({"other": 1}) == []


def test_ollama_model_defaults_and_honors_env(monkeypatch) -> None:
    import importlib

    from rung.sources import ai_fallback

    monkeypatch.delenv("DISPENSARY_OLLAMA_MODEL", raising=False)
    importlib.reload(ai_fallback)
    assert ai_fallback._OLLAMA_MODEL == "llama3.2"  # default
    assert ai_fallback._GRAPH_CONFIG["llm"]["model"] == "ollama/llama3.2"

    monkeypatch.setenv("DISPENSARY_OLLAMA_MODEL", "llama3.1:70b")
    importlib.reload(ai_fallback)
    assert ai_fallback._OLLAMA_MODEL == "llama3.1:70b"  # env override wins
    assert ai_fallback._GRAPH_CONFIG["llm"]["model"] == "ollama/llama3.1:70b"

    monkeypatch.delenv("DISPENSARY_OLLAMA_MODEL", raising=False)
    importlib.reload(ai_fallback)  # restore module state for other tests
