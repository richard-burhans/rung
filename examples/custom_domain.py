"""A runnable, network-free demo: use the `rung` ENGINE for a NON-cannabis domain.

`examples/example_plugin.py` shows the *plugin seam* (registering a stage the built-in CLI
resolves). This file shows the other half — driving the **engine directly as a library** to build
your own pipeline, with none of the reference application's cannabis assumptions. The domain here is
deliberately mundane: **farmers markets by city**.

It exercises the four pieces a plugin author reuses:

  1. **Your own record type** — a plain dataclass (`Market`); the engine never needs
     `StoreProductRecord`. You supply a `plausible()` predicate so the ladder knows a "good" result.
  2. **Your own access-method ladder** — two `AccessMethod`s at different `cost_rank`;
     `access.run_target` runs the cheapest that works, persists the winner per target in
     `access_methods`, and re-walks only on failure (self-healing). A second run of the same target
     reuses the stored winner and skips the costlier rung.
  3. **Your own table** — the engine manages the generic infra tables (`jobs`, `access_methods`);
     the domain schema is yours.
  4. **The work queue** — `enqueue` + `claim_next` (FOR UPDATE SKIP LOCKED) partition targets, so
     N copies of this process split the work with no coordinator.

The two "sources" are in-memory fixtures so the demo is hermetic (a real rung would fetch over
`http.make_session()` and parse with `selectolax`); swapping those in is the only change to make it
live.

Run it against a local Postgres (see docs/quickstart.md):

    DATABASE_URL=postgresql://rung:rung@localhost:5432/rung uv run python examples/custom_domain.py

`db.create_engine_tables()` builds **only** the domain-neutral infra (jobs + access_methods +
rate-limit/proxy tables) — no cannabis reference tables — so this example creates just the engine
infra plus its own `markets` table.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from rung import access, db, queue

TASK_TYPE = "market_list"
TARGET_TYPE = "market_list"


# ── 1. Your domain record + what counts as a good one ──────────────────────────────────────────
@dataclass
class Market:
    name: str | None
    address: str | None
    city: str | None
    day: str | None = None
    vendors: int | None = None


def market_plausible(record: object) -> bool:
    """A row counts if it has a name and a city (your rule; the engine default is store-shaped)."""
    return bool(getattr(record, "name", None) and getattr(record, "city", None))


# ── Two stand-in sources (in-memory so the demo needs no network) ──────────────────────────────
_CITY_JSON = {
    "springfield": '[{"name": "Downtown Market", "street": "100 Main St", "weekday": "Saturday", "stalls": 42}]',
    "shelbyville": '[{"name": "Riverside Market", "street": "5 Water St", "weekday": "Sunday", "stalls": 18}]',
}
# A costlier fallback source, consulted only if the cheap JSON is missing for a city.
_CITY_HTML = {
    "ogdenville": "<div class='mkt' data-name='Ogdenville Green' data-street='7 Barley Rd'></div>",
}


# ── 2. The access-method ladder (each returns (records, resource_url, params)) ──────────────────
async def _from_json(_conn: db.DBConn, city: str, _hint: access.MethodHint) -> access.MethodResult:
    raw = _CITY_JSON.get(city)
    if not raw:
        return [], None, None
    markets = [
        Market(name=m["name"], address=m["street"], city=city.title(),
               day=m.get("weekday"), vendors=m.get("stalls"))
        for m in json.loads(raw)
    ]
    return markets, f"https://example.test/api/{city}.json", None


async def _from_html(_conn: db.DBConn, city: str, _hint: access.MethodHint) -> access.MethodResult:
    html = _CITY_HTML.get(city)
    if not html:
        return [], None, None
    markets = [
        Market(name=m.group(1), address=m.group(2), city=city.title())
        for m in re.finditer(r"data-name='([^']+)' data-street='([^']+)'", html)
    ]
    return markets, f"https://example.test/{city}", None


CATALOG = [
    access.AccessMethod("markets_json", cost_rank=1, run=_from_json),   # cheap → tried first
    access.AccessMethod("markets_html", cost_rank=5, run=_from_html),   # costlier fallback
]


# ── 3. Your own persistence ────────────────────────────────────────────────────────────────────
_CREATE_MARKETS = """CREATE TABLE IF NOT EXISTS farmers_markets (
    id      SERIAL PRIMARY KEY,
    city    TEXT NOT NULL,
    name    TEXT NOT NULL,
    address TEXT,
    day     TEXT,
    vendors INTEGER,
    UNIQUE (city, name)
)"""


def _save(conn: db.DBConn, markets: list[Market]) -> None:
    for m in markets:
        conn.execute(
            "INSERT INTO farmers_markets (city, name, address, day, vendors) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (city, name) DO UPDATE SET address = EXCLUDED.address, "
            "day = EXCLUDED.day, vendors = EXCLUDED.vendors",
            (m.city, m.name, m.address, m.day, m.vendors),
        )


# ── 4. Drive it through the queue + the ladder ──────────────────────────────────────────────────
async def scrape_city(conn: db.DBConn, city: str) -> tuple[str | None, list[Market]]:
    """Run the cost-ranked ladder for one city and persist the winning rung's records."""
    winner, markets = await access.run_target(
        conn, TARGET_TYPE, city, CATALOG, plausible=market_plausible
    )
    _save(conn, markets)
    conn.commit()
    return winner, markets


async def run(conn: db.DBConn, cities: list[str]) -> dict[str, tuple[str | None, int]]:
    """Create the schema, enqueue the cities, then drain the queue running the ladder per city."""
    db.create_engine_tables(conn)   # ONLY the generic infra: jobs + access_methods + rate-limit/proxy tables
    conn.execute(_CREATE_MARKETS)
    conn.commit()

    for city in cities:
        queue.enqueue(conn, TASK_TYPE, city)
    conn.commit()

    worker = queue.worker_id()
    results: dict[str, tuple[str | None, int]] = {}
    while (job := queue.claim_next(conn, TASK_TYPE, worker)) is not None:
        winner, markets = await scrape_city(conn, job.target_key)
        queue.complete(conn, job.id, "done", worker=worker)
        conn.commit()
        results[job.target_key] = (winner, len(markets))
    return results


def main() -> None:
    conn = db.get_connection()
    results = asyncio.run(run(conn, ["springfield", "shelbyville", "ogdenville"]))
    print("Scraped (city: markets via winning rung):")
    for city, (winner, count) in sorted(results.items()):
        print(f"  {city}: {count} market(s) via '{winner}'")
    # The engine remembered the cheapest method that worked, per target — a re-run reuses it:
    print("\nPersisted winners in access_methods (status='ok'):")
    rows = conn.execute(
        "SELECT target_key, method, cost_rank FROM access_methods "
        "WHERE target_type = %s AND status = 'ok' ORDER BY target_key, cost_rank",
        (TARGET_TYPE,),
    ).fetchall()
    for target_key, method, cost_rank in rows:
        print(f"  {target_key}: {method} (cost_rank {cost_rank})")


if __name__ == "__main__":
    main()
