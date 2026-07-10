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

## `paper_fetcher.py` — a real second domain: fetch open-access papers

A **network** example (unlike `custom_domain.py`) that fetches academic-paper PDFs by DOI. Fetching a
paper is a natural fit for the engine — one target reachable via several hosts at different
cost/success — so the ladder is: resolve the DOI (Crossref), then run the cheapest rung that returns a
real PDF and **persist the winner per paper** so a re-run skips straight to it. The rungs, cheapest
first: **arXiv** (cost 0) → the **direct-journal** hosts PLOS/Nature-OA/BMC/Frontiers (cost 1, fast,
no API call) → **Unpaywall** (cost 2 — an OA locator that finds a copy of *any* DOI on any
repository/preprint) → **PMC OA** (cost 3 — the PMC Open Access subset, via the OA Web Service).
Same `access.run_target` + queue + honest `http.make_session` as the farmers-market example — a
completely different domain — which is the point: the engine is domain-agnostic.

The last rung earns its place by telling the truth about *why* it failed. Asked for a paper outside the
PMC Open Access subset, it reports **not open access** rather than a failure — because "free to read on
PMC" is not a redistribution licence, and Unpaywall's `is_oa` does not imply one. Without that verdict a
broken rung looks exactly like a paywall, which is how two rungs here rotted unnoticed. A ladder should
be able to say "I can't get this" and "you may not have this" in different words.

```bash
UNPAYWALL_EMAIL=you@example.com DATABASE_URL=postgresql://rung:rung@localhost:5432/rung \
  uv run python examples/paper_fetcher.py 10.1371/journal.pone.0282396 10.1038/s41598-018-22755-2
# or a batch:  ... paper_fetcher.py --from dois.txt
```

Each DOI is routed to the rung that can serve it; the others "fail" and the ladder walks on (the
winners are recorded in `access_methods`). The pure routing/resolution logic is tested
(`tests/test_paper_fetcher_example.py`); the network fetch is not (it hits live OA hosts).

## `example_plugin.py` — provide a stage via the plugin seam

Shows the *other* extension point: registering an implementation for a stage name the built-in CLI
resolves (the reference application's pipeline). With no plugin, those stages are informative stubs
(`StageNotAvailable`); registering one makes `rung.registry.resolve(...)` return your impl. This is
how the private cannabis overlay plugs in, and how you'd replace the reference pipeline's stages with
your own targets. See the file's docstring and [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

Both examples are exercised by the test suite (`tests/test_custom_domain_example.py`,
`tests/test_example_plugin.py`), so they stay working.
