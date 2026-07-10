# Changelog

All notable changes to `rung` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Provenance of this history

`rung` is the open-source core of a larger private monorepo. The engine was developed there from
**2026-06-05**, and the core was extracted into this repository on **2026-07-01**; it has been
developed in the open since.

Entries dated before 2026-07-01 are **reconstructed** from that monorepo history — 125 commits
across 24 distinct days touching the modules that ship here. They are summarized rather than
replayed as individual commits, because those same commits also carry the private plugin overlay
(the per-platform recipes and curated catalogs) that this repository deliberately does not
distribute. The summary is offered so that readers and reviewers can see the real shape of the
work; nothing here is backdated or reconstructed to imply activity that did not occur.

## [Unreleased]

### Changed

- `examples/paper_fetcher.py`: replaced the Europe PMC rung with a **`pmc_oa`** rung built on PMC's
  OA Web Service. The old rung fetched an endpoint that now returns 404 for every article — including
  papers that *are* in the open-access subset — so it had been silently dead rather than correctly
  declining.

### Fixed

- `examples/paper_fetcher.py` now reports **"not open access"** as a verdict distinct from a fetch
  failure. A paper that PMC indexes but places outside the OA subset is free to read and carries no
  redistribution licence; reporting it as "paywalled" was indistinguishable from a broken rung, which
  is how two rungs rotted unnoticed. A ladder should be able to say *"I can't get this"* and *"you may
  not have this"* in different words.

## [0.1.0] — 2026-07-10

First tagged release. The public core runs standalone: Stage-1 roster extraction, the access
engine, the queue, persistence, normalization, and the CLI all work with no overlay installed —
plug-in stages resolve to registry stubs until an overlay registers via the `rung.plugins`
entry point.

### Added

- **Cost-ranked access-method engine** (`access.py`). Every target is reachable several ways at
  different cost; `run_target` runs the cheapest method that works, persists the winning method
  per target, and re-walks only on failure, on a cheaper untried rung, or on a governed staleness
  re-explore. The re-exploration governor is RED-inspired (Floyd & Jacobson 1993): discretionary
  re-walks are admitted probabilistically so a same-day cohort cannot re-scrape in one
  synchronized burst.
- **Broker-free work queue** (`queue.py`, `jobs` table). Concurrent workers claim targets with
  `SELECT … FOR UPDATE SKIP LOCKED`, so runs partition work with no message broker. Lease +
  heartbeat + a lease-aware reaper recover the work of crashed workers. Retry jitter, a `run_at`
  spread, and a partial index keep the claim path cheap; `prune-jobs` is the maintenance command.
- **Per-host rate limiting** (`rate_limit.py`) — a durable token bucket, shared across workers
  through the same database.
- **`worker`** — the distributed entrypoint, one process per egress IP.
- **Honest-by-default HTTP** (`http.py`). All HTTP flows through `make_session()`, enforced by an
  AST guard in `tests/test_http.py`. Browser TLS impersonation is **opt-in and off by default**;
  a public user may opt in explicitly via `RUNG_IMPERSONATE`.
- **Plugin seam** (`registry.py`) and the `rung.plugins` entry point, with a worked
  `examples/example_plugin.py` proving the public core is extensible standalone. The core imports
  nothing from any overlay; the boundary is test-enforced (`tests/test_import_layering.py`).
- **Stage-1 generic extractors** — HTML tables, address-block and card/list pages, prose rosters,
  PDF, CSV, ArcGIS (direct feature-service resolution and where-clause layer filtering), and
  Socrata; an opt-in `--render` browser tier (`browser.py`) and an opt-in `--ai` LLM fallback.
- **Stage-2 / Stage-3 CLI surface** — `scrape-company-stores`, `dedupe-stores`, `compare-stores`,
  `scrape-menus`, with `--only` targeting for a focused re-scrape and `--max-age-hours` to
  freshness-gate re-scrapes for a daily-cron cadence.
- **`recon --discover`** — operator homepage discovery via web search.
- **Normalization** (`normalize.py`, `text.py`) — a canonical product-category taxonomy
  (`category_std`), a second-level product-type hierarchy (`product_type_std`), a lineage facet
  (`strain_type_std`), variant size → grams and price-per-unit, canonical terpene percentages,
  minor cannabinoids (`cannabinoids_std`), discount capture into `original_price`, and the
  combined `products_normalized` view with a derived `currency`.
- **Append-only history** — `products` / `product_observations` (a dose-aware fingerprint for
  mg-dosed categories) and store-lifecycle capture, so that price and potency observations
  accumulate rather than overwrite.
- **Examples** — `examples/custom_domain.py` (a farmers-market domain) and
  `examples/paper_fetcher.py` (fetch open-access papers through the same cost-ranked ladder:
  arXiv → direct journal hosts → Unpaywall → Europe PMC, with `--from` batch mode and a
  `PAPER_FETCH_DIR` output override). Two unrelated domains on one engine.
- **Quality gate + CI** — one command runs `ruff` → `ty` → `pytest` with a coverage floor; CI runs
  the same gate against a Postgres service container on every pull request.
- **Documentation site** — MkDocs (Material) published to Read the Docs, plus a JOSS paper
  (`paper/paper.md`).

### Changed

- **Renamed** the framework and its package from `dispensary_scraper` to **`rung`**, and the
  private overlay to `rung_intel`. The tagline: *run the cheapest rung that works.*
- **Public/private split.** The generic engine ships open-source; per-platform recipes and curated
  catalogs live in a private overlay behind the plugin seam, assembled by a leak-guarded build.
  The evasion machinery moved out of the public `http.py`, leaving the public default honest and
  non-impersonating.
- **Split `db.py`** into an engine-table module and `reference_db.py` (reference tables), so the
  engine's schema is separable from any one domain's reference data.
- **De-branded** the reusable framework surface and genericized the front-door documentation.
- **`keep-the-best` replace semantics** — a re-scrape only overwrites when it yields at least as
  many distinct physical stores, so a transient low-yield run cannot clobber good data.
- **Potency contract** — a database `CHECK` constraint enforces that a cannabinoid carries a
  percent value *or* a milligram value, never both.

### Fixed

- `queue`: job completion is scoped to the holding worker, closing a stale-reclaim race; per-state
  claims are scoped to their state; pruning is wired into the sweep.
- `db`: take a chronological (not lexical) maximum of the `TEXT` `scraped_at` column.
- Stage-1 extraction: recover a street column from a misordered header; un-merge a PDF name column
  overprinted onto the address; null a bare date mis-mapped onto a record's address; fall back to
  the legal name when the DBA column sorts first.
- `text`: fold em-dash storefront suffixes, and fold bare `"<brand> <city>"` storefront names to a
  single operator.
- `addresses`: fold `Mt`/`Mount`, `Ft`/`Fort`, `St`/`Saint` street-name prefixes and hyphenated
  house numbers so the same physical location keys identically across sources.
- `compare`: surface keyless rows instead of silently dropping them.
- Test harness: a killed run could leave database locks that wedged the next run's schema sweep;
  teardown now rolls back before dropping and caps the lock wait.

### Security

- Browser TLS impersonation is off by default in the public core and must be opted into
  explicitly. The public core circumvents nothing: it ships no per-platform access recipes, no
  anti-throttle machinery, and no credentials.

[Unreleased]: https://github.com/richard-burhans/rung/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/richard-burhans/rung/releases/tag/v0.1.0
