# Build your own domain

`rung`'s engine is domain-agnostic: a cost-ranked access ladder, a Postgres work queue, an
honest-HTTP chokepoint, and a plugin registry. The cannabis-dispensary pipeline that ships with it is
the **reference application** — one way to use the engine, not the only way. This guide builds a
pipeline for a different domain from scratch. The running example is **farmers markets by city**
([`examples/custom_domain.py`](../examples/custom_domain.py) is the complete, tested version — read it
alongside this).

You'll do five things. Only the last two touch the engine.

## 1. Define your record type

The engine never needs the reference application's `StoreProductRecord`. A plain dataclass is enough —
whatever shape your targets have:

```python
from dataclasses import dataclass

@dataclass
class Market:
    name: str | None
    address: str | None
    city: str | None
    day: str | None = None
    vendors: int | None = None
```

## 2. Say what a "good" record is

The ladder needs to know when a method *succeeded*. You supply a predicate over one record; the engine
default is store-shaped (name + a location field), so pass your own:

```python
def market_plausible(record: object) -> bool:
    return bool(getattr(record, "name", None) and getattr(record, "city", None))
```

## 3. Write your access methods

A target (here, a city's market list) can usually be reached several ways at different cost — a cheap
static JSON endpoint, a rendered page, an LLM extraction. Each method is an `async` callable that
takes `(conn, target_key, hint)` and returns `(records, resource_url, params)` — the records it found
plus the concrete locator it used (so the winner is remembered precisely). Return `([], None, None)`
when the method doesn't apply.

```python
from rung import access

async def from_json(conn, city, hint) -> access.MethodResult:
    raw = fetch_city_json(city)                      # your fetch, via http.make_session()
    if not raw:
        return [], None, None
    markets = [Market(**parse(m)) for m in raw]
    return markets, f"https://example.test/api/{city}.json", None

async def from_html(conn, city, hint) -> access.MethodResult:
    ...                                              # a costlier fallback

CATALOG = [
    access.AccessMethod("markets_json", cost_rank=1, run=from_json),   # cheap → tried first
    access.AccessMethod("markets_html", cost_rank=5, run=from_html),   # costlier fallback
]
```

`cost_rank` is **try-priority** (lower is tried first). A high-confidence method that returns a
complete result can be ranked into the cheap band so it wins before a noisy generic rung shadows it.

> **All HTTP goes through `http.make_session()`.** The public default sends an honest User-Agent and
> does not spoof a browser fingerprint. Construct your session there, not a bare client (an AST guard
> in `tests/test_http.py` enforces this in the core). Be polite: honor `robots.txt` and terms of
> service.

## 4. Create your own schema

The engine owns the generic infra tables (`jobs`, `access_methods`, and the rate-limit/proxy tables);
your domain schema is yours.

```python
from rung import db

conn = db.get_connection()          # reads DATABASE_URL
db.create_tables(conn)              # generic infra tables  (see the note below)
conn.execute("""CREATE TABLE IF NOT EXISTS farmers_markets (
    id SERIAL PRIMARY KEY, city TEXT, name TEXT, address TEXT, day TEXT, vendors INT,
    UNIQUE (city, name))""")
conn.commit()
```

> **Note (2026-07):** `db.create_tables()` today also creates the reference application's tables.
> They stay empty and harmless for your pipeline. A planned change splits out a
> `create_engine_tables()` that builds *only* the generic infra tables — at which point this step
> creates nothing you don't use.

## 5. Drive it: the ladder + the queue

`access.run_target` walks the ladder cheapest-first, runs the first method that produces a plausible
result, and **persists that winner** in `access_methods` — so the next run for the same target reuses
it and only re-walks on failure. Wrap it in the work queue and N processes partition the targets:

```python
import asyncio
from rung import queue

async def scrape_city(conn, city):
    winner, markets = await access.run_target(
        conn, "market_list", city, CATALOG, plausible=market_plausible)
    save(conn, markets)             # your INSERTs
    conn.commit()
    return winner, markets

async def run(conn, cities):
    for city in cities:
        queue.enqueue(conn, "market_list", city)
    conn.commit()
    worker = queue.worker_id()
    while (job := queue.claim_next(conn, "market_list", worker)) is not None:
        await scrape_city(conn, job.target_key)
        queue.complete(conn, job.id, "done", worker=worker)
        conn.commit()

asyncio.run(run(conn, ["springfield", "shelbyville", "ogdenville"]))
```

Run the finished version:

```bash
DATABASE_URL=postgresql://rung:rung@localhost:5432/rung uv run python examples/custom_domain.py
```

`springfield`/`shelbyville` are served by the cheap JSON rung; `ogdenville` has no JSON, so the ladder
falls back to the HTML rung — and each winner is remembered for next time.

## Optional: plug into the built-in CLI instead

The five steps above use the engine as a **library** — the most flexible path, and the right one for a
new domain shape. If your pipeline happens to match the reference application's shape (a roster of
entities → each entity's own site → reconcile → snapshot each entity's catalog), you can instead
provide the stages the built-in CLI verbs resolve, via the plugin seam — see
[`../CONTRIBUTING.md`](../CONTRIBUTING.md) ("Extending the framework") and
[`../examples/example_plugin.py`](../examples/example_plugin.py). Registered stages are discovered
automatically through the `rung.plugins` entry point.

## Where to go next

- [`concepts.md`](concepts.md) — the four load-bearing ideas in one place.
- [`api.md`](api.md) — the engine surface (`access`, `queue`, `registry`, `http`, `db`).
- [`stage_contracts.md`](stage_contracts.md) — the reference application's stage read/write contract
  (a worked example of a full pipeline built on the engine).
