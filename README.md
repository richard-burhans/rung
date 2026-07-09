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

*How we built a polite, resilient, distributed scraper on nothing but Postgres — and what we learned
the hard way.*

This is the story behind `rung`: the design, the reasoning, and the failures that taught us the
design. It's written for the next person building a scraper against a fragmented, fast-moving corner
of the web — in the give-back tradition of the people who taught us. It stays at the level of
**patterns and architecture**; the per-platform recipes are yours to discover, the way we had to. The
engine is the gift.

### The problem, honestly

We wanted an authoritative, multi-state picture of a real consumer market: who's licensed, what each
operator actually sells, and at what price and potency — refreshed often enough to be true. That
sounds like "just scrape some menus." It isn't.

The data lives across many different consumer platforms, each with its own way in. Some hand you
clean JSON; some hide it in a page; some want a real browser; some only exist behind a search box.
Listings churn daily. And every one of these platforms would rather you didn't. A naive scraper
"works on my machine," then quietly dies the moment it runs from a server — half the results empty,
no error, no idea why.

So the actual problem isn't fetching a page. It's: **how do you collect from dozens of moving,
defensive targets, cheaply, without lying to yourself about what you got?**

### The philosophy

Four commitments shaped everything:

- **Polite.** Slow down before you rotate. Back off a host before you push it harder. We'd rather
  be slow than be a burden.
- **Resilient.** Assume you *will* be blocked and you *will* crash. Design so neither loses data.
- **Reproducible.** A single command tells us the whole thing is still green. The core is published.
- **Honest.** Silent under-capture is the enemy. A zero that should be a number must be loud.

Everything below falls out of taking those seriously.

### Idea 1 — "the cheapest thing that works"

The central abstraction isn't a scraper; it's a **registry of ways to reach a target**.

Every target — an operator's store list, a store's menu — can usually be reached several ways, at
wildly different cost: a static JSON endpoint is nearly free; a documented API is cheap; driving a
real headless browser is expensive; an AI-assisted extraction is the last resort. We arrange those
as an ordered, cost-ranked ladder and let one function walk it: **run the cheapest method that
works, and remember which one did.**

That "remember" is the whole game. The first time we touch a target we might climb the ladder; every
time after, we go straight to the stored winner. We only re-walk when the winner breaks (self-heal),
when a cheaper rung we hadn't tried appears, or when a target has gone stale enough to re-check. The
result is a system that gets cheaper and faster the longer it runs, and repairs itself when a
platform changes underneath it — without a human editing a config.

The lesson: **persist the decision, not just the data.** Knowing *how* to reach each target is an
asset worth as much as the target itself.

### Idea 2 — "Postgres for everything"

The instinct, when you say "distributed workers," is to reach for a message broker, a cache, and a
rate-limit service. We reached for one thing: the database we already had.

- **The work queue is a query.** `SELECT … FOR UPDATE SKIP LOCKED` lets any number of workers each
  grab a different pending job atomically, with no coordinator. Two runs of the same command simply
  partition the work between them. Resumability across crashes *and* across machines falls out for
  free — stronger than a local checkpoint file, and there's no broker to run.
- **Crash recovery is three columns.** Don't delete a job when you claim it; mark it in-progress
  with a lease. A live worker extends its lease with a heartbeat; a background reaper re-queues any
  job whose lease has expired (using the same skip-locked trick so reapers don't collide). A worker
  can die mid-job and its work comes back to the pool, once, cleanly.
- **Rate limits and proxy health are just rows.** A per-host token bucket and per-proxy health live
  in the same database, updated atomically. One dependency, one backup, one mental model.

None of this is exotic. That's the point. **You almost certainly already have the distributed
primitives you need; they're spelled in SQL.**

### Idea 3 — scaling politely

Here's the load-bearing insight: the throttles that matter are **per-IP**. A host doesn't care how
many workers you run; it cares how fast a single address hits it.

That flips the scaling problem into something simple. Give **each worker its own egress IP** and a
per-IP rate limiter, and aggregate throughput against a host grows roughly **linearly with the
number of workers — with no distributed lock at all.** Ten considerate workers on ten addresses
outperform one worker overloading a single address, and they're gentler on the host doing it.

Proxies are a cost decision, not a default. Most targets don't filter by network, so cheap,
rotating addresses are plenty; residential/mobile addresses bill by the gigabyte and a menu scraper
pulls a lot of JSON. So we **tier per platform**: run a quick block-rate test, and only escalate a
platform to the expensive tier if the cheap one actually gets filtered. A surprising amount of
volume can be pulled from a plain host address for free; reserve the expensive path for the few
places that genuinely need it.

### Idea 4 — not overwhelming a host

Even considerate scrapers can overload a host by accident. The classic trap: you scrape a cohort of
targets on the same day, put a "re-check anything older than 30 days" rule on it, and 30 days later
the entire cohort goes stale on the same run and re-scrapes in one synchronized burst. To a host,
that spike is indistinguishable from an attack.

The fix borrows an old idea from network congestion control — **Random Early Detection** (Floyd &
Jacobson, 1993). Instead of a hard cutoff, admit each discretionary re-check *probabilistically*, on
a gentle ramp that rises with staleness, and pull back as pressure on any single host climbs. The
randomness scatters a same-day cohort across many future runs instead of one cliff. Nothing is
urgent; a slightly-stale record keeps serving until its turn comes up. Spreading load in time is as
important as spreading it across addresses.

### War stories

The design didn't come from a whiteboard. It came from these.

**The silent under-capture.** Our first big ingest looked fine and was quietly missing most of the
data on some platforms. A host was soft-blocking our address once its request rate crossed a
threshold — not with an error, but by returning "nothing here." The old fetch path turned that into
a legitimate-looking zero. The volume was fine at a *slower* rate; the block cleared after a short
quiet period. The fix was pacing plus a block-aware retry, and the re-ingest recovered a huge chunk
of the dataset. The real lesson wasn't about that platform — it was: **a zero that could mean
"blocked" must never be recorded as "empty."**

**The "datacenter IP" myth.** We blamed several failures on IP reputation and nearly bought our way
out with expensive residential addresses. The actual cause was a client that wasn't presenting a
believable browser fingerprint — a TLS problem, not an IP problem. Fixing the fingerprint fixed the
blocks, for free. **Diagnose the layer that's actually failing before you spend money on a different
one.**

**The test suite that ate itself.** For a while our tests would occasionally hang for what felt like
forever. The cause: a killed test run left database locks on its scratch schemas, and the *next*
run's cleanup step blocked trying to drop them — which we'd then kill, leaving more locks. A
self-reinforcing wedge. The fix was to make teardown robust: release every connection *before*
dropping anything, and cap how long any cleanup will wait for a lock. **Make your teardown as
crash-safe as your runtime; a killed run must not poison the next one.**

### Takeaways

If you take nothing else:

1. Model *how to reach* a target as first-class state, cost-ranked and self-healing — not a
   hard-coded fetch.
2. You probably don't need a broker. `SKIP LOCKED` + leases + a reaper is a crash-safe distributed
   queue in the database you already run.
3. Scale with **more addresses, not a faster address.** Per-IP is the unit that matters.
4. Buy the expensive proxy tier only where a test proves you need it.
5. Spread load in **time** (jittered, RED-style) as well as across addresses.
6. Record uncertainty loudly. A silent zero is a lie.
7. Your test teardown deserves the same crash-safety as your runtime.

### Use it

The generic engine — the access-method registry, the Postgres work queue, the persistence and CLI,
and the plugin seam that lets a private overlay carry the platform-specific parts — is open source
under the MIT license. Clone it, point it at your own domain, and build your own ladder of methods.
The per-platform recipes aren't bundled in — they depend on each platform and change often — but the
framework is here so you don't have to reinvent the *shape* of the problem, and we'd genuinely love
to hear what you build with it.

Go build something. Be polite about it.

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
