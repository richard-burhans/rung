<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo.svg" alt="rung logo" width="110">
  </picture>

  <h1>rung</h1>

  <p>
    <a href="https://rung-framework.readthedocs.io/en/latest/"><img src="https://app.readthedocs.org/projects/rung-framework/badge/?version=latest" alt="Docs"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-14b8a6.svg" alt="License: MIT"></a>
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.13%2B-14b8a6.svg" alt="Python 3.13+"></a>
  </p>

  <p><strong>run the cheapest rung that works</strong> — a broker-free, Postgres-centric framework for<br>
  resilient, polite, distributed scraping of fragmented, defensive consumer-data platforms.</p>
</div>

`rung` is a plugin-extensible scraping framework built around one idea: a target can usually be
reached several ways at wildly different cost, so **run the cheapest method that works, persist the
winner per target, and re-walk only when it breaks** (self-heal), when a cheaper rung appears, or
when the target goes stale. The access engine, the work queue, the persistence layer, the
normalization surface, and the plugin seam are domain-agnostic; per-domain catalogs plug in on top.

It runs a three-stage pipeline over one Postgres database:

- **Rosters** — discover an authoritative list resource (HTML / PDF / CSV / ArcGIS / Socrata) and
  extract it into structured records.
- **Entity sites** — scrape each entity's *own* site for its locations through the cost-ranked
  access-method engine, dedupe by physical address, and diff against the roster to surface where the
  roster lags (entries it still shows that are gone; open ones it's missing).
- **Listings** — snapshot each location's live catalog and normalize it to a cross-platform surface
  (canonical categories/types, per-unit sizing and pricing, folded attribute maps).

The Stage-1 extractors, the access engine, the queue, persistence, normalization, and the CLI ship
in this open-source core. Domain-specific catalogs and per-platform recipes load as a separate,
private plugin via the `rung.plugins` entry point — the core runs on its own, resolving those stages
to registry stubs until a plugin is installed.

**Circumvents nothing by default.** The published code makes no attempt to defeat a site's bot
protection: the HTTP client sends an honest, self-identifying User-Agent with no fingerprint spoofing
(browser TLS impersonation is opt-in, off by default), and roster extraction reads **public
records**. Respect each site's terms and `robots.txt`; you are responsible for how you use this.

## The story

I built this to compile an authoritative, multi-state dataset for a real, fast-moving consumer market
(its first application was public dispensary rosters and menus), and I'm releasing the framework in
the tradition of open scraping education — the engine is worth sharing even when the dataset and the
per-platform recipes stay private. If it saves you time, that's the point.

— Richard Burhans

> 📖 **Read [**The cheapest thing that works**](NARRATIVE.md).** It's the full story behind `rung` —
> the design, the reasoning, and the failures that taught us the design. If you read one thing here,
> read that: it's the best way to understand how and why the engine is built the way it is.

## Architecture

Two packages: the public open-source core **`rung`** (roster extraction, the cost-ranked
access-method *engine*, the work queue, persistence, normalization, the CLI, and the plugin seam) and
a private plugin overlay (the domain catalogs and per-platform recipes, the roster-comparison logic,
and the curated datasets). The core ships and runs on its own — its plug-in stages resolve to
registry stubs until an overlay registers via the `rung.plugins` entry point; the boundary is
test-enforced (`tests/test_import_layering.py`). See
[`docs/publish_split_design.md`](docs/publish_split_design.md), and [`ARCHITECTURE.md`](ARCHITECTURE.md)
for the abstraction map, dependency direction, and cross-cutting contracts.

## Setup

```bash
# Start a local Postgres matching the default DSN (or set DATABASE_URL to your own):
docker run -d --name rung-pg \
  -e POSTGRES_USER=rung -e POSTGRES_PASSWORD=rung -e POSTGRES_DB=rung \
  -p 5432:5432 postgres:16
```

The connection URL defaults to the dev container
(`postgresql://rung:rung@localhost:5432/rung`); override with `DATABASE_URL`.

Stage table contracts — which command reads/writes which table, and the work-queue claims that make
concurrent runs safe — are in [`docs/stage_contracts.md`](docs/stage_contracts.md).

## Getting started

- **[`docs/quickstart.md`](docs/quickstart.md)** — clone → Postgres → run the engine end to end in a
  few minutes (a farmers-market example — a different domain from the reference application, no
  proprietary code).
- **[`examples/custom_domain.py`](examples/custom_domain.py)** — the ~150-line runnable example the
  quickstart runs: your own record type, a cost-ranked access ladder, your own table, the work queue.
- **[`docs/build-your-own-domain.md`](docs/build-your-own-domain.md)** — build a pipeline for *your*
  targets, step by step.
- **[`docs/concepts.md`](docs/concepts.md)** — the four load-bearing ideas · **[`docs/api.md`](docs/api.md)** — the engine surface you call.

## The reference application — CLI

The commands below are `rung`'s **reference application**: a licensed-dispensary dataset pipeline
(roster → each entity's own site → reconcile → snapshot each catalog). They're a worked example of a
full pipeline on the engine; to build a *different* domain, use the engine directly (see
[`docs/build-your-own-domain.md`](docs/build-your-own-domain.md)). Commands (see
`pyproject.toml [project.scripts]`), run via `uv run <command>`:

| Stage | Commands |
|---|---|
| 1 · Rosters | `search-states`, `find-lists`, `scrape-states` (`--render`, `--ai`), `show-states` |
| 2 · Entity sites | `seed-companies`, `recon` (`--discover`), `scrape-company-stores` (`--ai`), `dedupe-stores`, `compare-stores` |
| 3 · Listings | `scrape-menus` |
| Fleet/ops | `worker` (`--state`, `--task`, `--poll-seconds`), `prune-jobs`, `reap-jobs` |
| Dev | `analyze <url>` |

- **Stage 1** — `search-states → find-lists → scrape-states`: locate each authority's list resource
  and extract the roster (static HTML/PDF/CSV/ArcGIS/Socrata by default; `--render` drives a headless
  browser and `--ai` an LLM extractor as opt-in last resorts).
- **Stage 2** — `seed-companies → recon → scrape-company-stores → dedupe-stores → compare-stores`:
  derive entities from the roster, detect each one's platform + homepage, scrape its own site through
  the access engine, dedupe by physical address (folding aliases), and diff against the roster. For
  entities with no derivable homepage, `recon --discover` web-searches candidates and prints a review
  list to promote.
- **Stage 3** — `scrape-menus` walks every deduped location that carries a scrape handle
  (`platform` + `external_id`) and snapshots its live catalog into `store_products`, normalized to the
  standard fields. `--max-age-hours N` refreshes only stale snapshots, so a daily cron's same-day
  re-runs are cheap no-ops. Per-platform catalogs are supplied by the plugin overlay.
- **Fleet** — `worker` is the distributed entrypoint (one process per egress IP; reaps crashed
  leases, then drains the queue).

Every `store_products` row keeps its platform-shaped raw fields **and** standardized ones stamped at
scrape time (canonical category/type, lineage facet, per-variant size→grams and price-per-unit, and a
folded attribute map with impossible values repaired). The `products_normalized` VIEW projects just
the standard fields for apples-to-apples queries across platforms.

## Tests

Run `ruff check` → `ty` → `pytest` (the last with a coverage floor, `--cov-fail-under`). The same gate
runs in **CI** (`.github/workflows/ci.yml`) on every PR and push to `main`, against a Postgres service
container. DB tests run against throwaway schemas in the test database; the suite covers the extractor
parsing logic, the DB-replace safety invariants, and the work-queue claim semantics; the
network/browser/AI tiers are exercised manually.
