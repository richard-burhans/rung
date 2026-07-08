# Examples

Two runnable examples showing the two ways to build on `rung`. Both are non-proprietary and use
obviously-fake data — start here to see the mechanics before wiring real sources.

## `custom_domain.py` — drive the engine as a library *(start here)*

A complete, **network-free** pipeline for a non-cannabis domain — **farmers markets by city** — that
uses the four engine pieces a general adopter reuses:

1. **Your own record type** (a `Market` dataclass) + a `plausible()` predicate.
2. **Your own cost-ranked access ladder** — two `AccessMethod`s; `access.run_target` runs the
   cheapest that works, persists the winner per target, and self-heals on failure.
3. **Your own table** (`farmers_markets`) — the engine owns only the generic infra tables.
4. **The work queue** — `enqueue` + `claim_next` (FOR UPDATE SKIP LOCKED) partition targets across
   concurrent runs.

Run it (see [`../docs/quickstart.md`](../docs/quickstart.md) for the one-time Postgres setup):

```bash
DATABASE_URL=postgresql://rung:rung@localhost:5432/rung uv run python examples/custom_domain.py
```

Expected output:

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

`springfield`/`shelbyville` are served by the cheap JSON rung; `ogdenville` has no JSON, so the
ladder falls back to the costlier HTML rung — and each winner is remembered for next time. The full
walkthrough is [`../docs/build-your-own-domain.md`](../docs/build-your-own-domain.md).

## `example_plugin.py` — provide a stage via the plugin seam

Shows the *other* extension point: registering an implementation for a stage name the built-in CLI
resolves (the reference application's pipeline). With no plugin, those stages are informative stubs
(`StageNotAvailable`); registering one makes `rung.registry.resolve(...)` return your impl. This is
how the private cannabis overlay plugs in, and how you'd replace the reference pipeline's stages with
your own targets. See the file's docstring and [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

Both examples are exercised by the test suite (`tests/test_custom_domain_example.py`,
`tests/test_example_plugin.py`), so they stay working.
