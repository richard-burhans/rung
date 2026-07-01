"""AI-powered extraction fallback for the generic state pipeline.

extract_with_ai runs SmartScraperGraph (scrapegraphai + local Ollama) against a
list URL when the static and browser-render tiers return nothing. Not imported at
startup — extract.py imports it lazily on fallback.
"""

import asyncio
import os

from pydantic import BaseModel

from rung.models import DispensaryRecord

# The local Ollama model SmartScraperGraph runs against. Override with the DISPENSARY_OLLAMA_MODEL
# env var (e.g. a bigger model for hard pages, or one you've already pulled) — same env-var
# override convention as http's DISPENSARY_IMPERSONATE.
_OLLAMA_MODEL = os.environ.get("DISPENSARY_OLLAMA_MODEL") or "llama3.2"

_GRAPH_CONFIG = {
    "llm": {
        "model": f"ollama/{_OLLAMA_MODEL}",
        "temperature": 0,
        "format": "json",
    },
    "verbose": False,
    "headless": True,
}

_EXTRACT_PROMPT = (
    "Extract all dispensary store locations from this page. "
    "For each location extract: name, address, city, state, zip_code, phone. "
    "Return as a structured list."
)


class _Location(BaseModel):
    name: str | None = None  # the rest of the pipeline tolerates a nameless row; don't reject it
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    phone: str | None = None


class _LocationList(BaseModel):
    locations: list[_Location]


def _locations_from_result(result: object) -> list[dict]:
    """Normalize a SmartScraperGraph result to a list of plain location dicts.

    scrapegraphai may hand back the pydantic schema instance, a plain ``{"locations": [...]}``
    dict, or a bare list depending on the model/version — and the items inside may themselves
    be ``_Location`` models or dicts. Coerce every shape to dicts so the caller's dict-shaped
    reads work regardless (a schema instance otherwise slips through and gets silently dropped).
    """
    if isinstance(result, _LocationList):
        locations: list = list(result.locations)
    elif isinstance(result, dict):
        raw = result.get("locations")
        locations = raw if isinstance(raw, list) else []
    elif isinstance(result, list):
        locations = result
    else:
        return []
    return [
        loc.model_dump() if isinstance(loc, BaseModel) else loc
        for loc in locations
        if isinstance(loc, (BaseModel, dict))
    ]


def _run_graph(url: str) -> list[dict]:
    from scrapegraphai.graphs import SmartScraperGraph

    graph = SmartScraperGraph(
        prompt=_EXTRACT_PROMPT,
        source=url,
        config=_GRAPH_CONFIG,
        schema=_LocationList,
    )
    return _locations_from_result(graph.run())


async def extract_with_ai(url: str, source_tag: str) -> list[DispensaryRecord]:
    """Extract dispensary locations using ScrapeGraphAI + Ollama.

    Results are tagged source_tag so they are distinguishable in the DB from
    static-scraper results.
    """
    loop = asyncio.get_running_loop()
    raw_locations: list[dict] = await loop.run_in_executor(None, _run_graph, url)

    records: list[DispensaryRecord] = []
    for loc in raw_locations:
        if not isinstance(loc, dict):
            continue
        records.append(
            DispensaryRecord(
                source=source_tag,
                name=loc.get("name"),
                address=loc.get("address"),
                city=loc.get("city"),
                state=loc.get("state"),
                zip_code=loc.get("zip_code"),
                phone=loc.get("phone"),
            )
        )
    return records
