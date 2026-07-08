# Quickstart

Get `rung` running and see the cost-ranked access engine work end to end — in a few minutes, with
no cannabis and no proprietary code. The example domain is **farmers markets by city**.

## 1. Install

```bash
git clone https://github.com/richard-burhans/rung.git
cd rung
uv sync            # Python ≥ 3.13, managed with uv (https://docs.astral.sh/uv/)
```

## 2. Start a Postgres

`rung` uses Postgres for persistence (the work queue, the access-method registry, and your data).
Any Postgres works; one quick way:

```bash
docker run -d --name rung-pg \
  -e POSTGRES_USER=rung -e POSTGRES_PASSWORD=rung -e POSTGRES_DB=rung \
  -p 5432:5432 postgres:16
```

The default connection string is `postgresql://rung:rung@localhost:5432/rung`; override it by setting
`DATABASE_URL`.

## 3. Run the example

```bash
DATABASE_URL=postgresql://rung:rung@localhost:5432/rung uv run python examples/custom_domain.py
```

You should see:

```
Scraped (city: markets via winning rung):
  ogdenville: 1 market(s) via 'markets_html'
  shelbyville: 1 market(s) via 'markets_json'
  springfield: 1 market(s) via 'markets_json'

Persisted winners in access_methods (status='ok'):
  ogdenville: markets_html (cost_rank 5)
  shelbyville: markets_json (cost_rank 1)
  springfield: markets_json (cost_rank 1)
```

That single run exercised the whole engine:

- a **cost-ranked access ladder** ran the cheapest method that works for each city — the cheap JSON
  rung for `springfield`/`shelbyville`, and (because it has no JSON) the costlier HTML fallback for
  `ogdenville`;
- the winning method was **persisted per target** in `access_methods`, so a re-run skips the ladder
  and reuses it (self-healing only re-walks when the winner breaks);
- the cities were dispatched through the **work queue** (`FOR UPDATE SKIP LOCKED`), so running several
  copies of the process would split the work with no coordinator;
- the results were written to the example's **own table** (`farmers_markets`) — the engine owns only
  the generic infra tables.

## 4. What next

- **[`examples/custom_domain.py`](../examples/custom_domain.py)** — read the ~150 lines you just ran;
  every engine call is commented.
- **[`docs/build-your-own-domain.md`](build-your-own-domain.md)** — the step-by-step tutorial for
  building a pipeline for *your* targets (your records, your schema, your access ladder, your stages).
- **[`docs/concepts.md`](concepts.md)** — the four load-bearing ideas (the access ladder, the work
  queue, the plugin seam, the honest-HTTP chokepoint).
- **[`docs/api.md`](api.md)** — the engine surface a plugin/pipeline author calls.

## Running the tests

```bash
# a throwaway test database (the suite hands each test an isolated schema):
psql postgresql://rung:rung@localhost:5432/rung -c 'CREATE DATABASE rung_test;'
uv run pytest
```

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the full dev workflow.
