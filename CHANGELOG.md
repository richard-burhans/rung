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

### Fixed — 2026-10-10

- **Brand folding** (`rung.text.strip_legal_entity`): a "Co." behind another legal suffix is now
  stripped too. The first pass trimmed the period along with the suffix, leaving a bare "Co" the
  pattern does not match, so "Green Leaf Wellness Co. LLC" and "Green Leaf Wellness Co." keyed as two
  operators. A "Co" with no period is still not treated as a suffix.

### Fixed — 2026-10-06 (a whole-tree review)

- **Sizes** (`rung.normalize`): a leading-dot decimal (`.5g`), a fraction of a gram or pound
  (`1/2 g`, `1/2 lb`) and a compact multipack (`5x0.5g`) are read correctly; they read 5 g, 2 g /
  896 g and 0.5 g, which put the per-gram price off by up to tenfold.
- **Effective price in SQL** (`reference_db.EFFECTIVE_VARIANT_PRICE`): an undeclared menu outside a
  medical-only jurisdiction now prices on the adult-use list, as `normalize.price_channel` does; a
  NULL comparison had sent it to the lowest of both lists.
- **State programs**: re-running `search-states` now updates a jurisdiction's `programs`, name, term
  and agency; the upsert used to keep the first values for ever.
- **Rosters** (`rung.sources`): a failed sub-box of the California sweep, or an ArcGIS layer still
  flagging rows at the page cap, is a partial roster and never replaces the stored one; one
  extractor's crash costs that state only; `find-lists --force` keeps the stored list when the
  agency page is unreachable; a spreadsheet roster is read as a sheet; header mapping matches whole
  words; a `HEAD`-refusing server is re-checked with `GET`; roster history is recorded after the
  geocode cache fills the rows.
- **Names and addresses**: brand folding never strips `420`/`710` or folds to a bare generic word;
  trailing units are stripped from street keys; a unit keyword no longer becomes the house number;
  re-seeding reuses an existing company spelling.
- **Queue**: requeue requires a lapsed lease; failed rows are stamped and prunable; a targeted claim
  takes a just-requeued job; targeted keys are claimed in the order given; the heartbeat runs off the
  event loop; a failing dedupe releases its claim; the two aggregator filters are mutually exclusive.
- **Elsewhere**: `fetch-fx` is incremental; `replace_company_stores` keeps cached coordinates;
  `pick_canonical` matches the brand as a whole word; a descending address range is no longer
  mirrored by the interpolating geocoder; a system Chromium is the binary that launches; the static
  source requires `state_programs.parquet` and reads `%%` as psycopg does.

### Added — 2026-10-06

- **`rung.virtual_display`** — an X display for a browser that must run with a window on a box
  with none (a server, a container, CI): Xvfb, or Xorg with the `dummy` video driver, re-implementing
  Chromium's own Xvfb test helper. `virtual_display("xvfb")` is a context manager that picks a free
  `DISPLAY`, waits until the server answers, neutralises a Wayland session (else the browser ignores
  `DISPLAY` and draws on the real screen), restores the environment on exit, and yields the geometry
  so the caller can verify where the browser drew. `missing_binaries()` reports which system
  binaries are absent. Previously private; it names no target and imports nothing internal.

### Changed — 2026-08-18 → 2026-10-06 (synced to the public repo 2026-10-06)

The public repo had not been synced for seven weeks; these entries cover what reached it in one
sync. Each item names the module, so a reader can find it.

- **A refusal is never an empty result** (`rung.http`). `get_json` / `get_text` are the shared
  fetch: one request, one refusal rule, one parse. A failure raises `FetchRefused` with a kind —
  `blocked` (we were refused), `broken` (we are wrong) or `unavailable` (the source says no). An
  edge network's own challenge header (`cf-mitigated`, `x-amzn-waf-action`) makes a response
  `blocked` whatever its status. A paged walk that fails after its first page raises
  `TruncatedMenu`, so a fragment is never stored as a shorter list.
- **`rung.html`** — new tier-0 module for page primitives: the string-aware brace scanner,
  `script_json`, and the RSC flight-stream reassembler (`flight_text`, with a `strict` mode for
  callers that turn the stream into a list).
- **Menu type** (`rung.text.menu_type_of`, `models.*.menu_type`, `rung.sources.dedupe`). A store
  licensed for both programs lists a medical and an adult-use menu, and they are two catalogues.
  Dedupe keeps one row per rooftop AND menu type. A listing's name declares its program only in a
  bracket, after a separator, or as the final word of a name with no separator, so a brand like
  "Nature Med" declares nothing.
- **Price channel** (`rung.normalize.price_channel`). A variant's shelf price is its menu's own
  program list, not the cheaper of the two. An undeclared menu in a medical-only jurisdiction
  prices on the medical list. `reference_db.EFFECTIVE_VARIANT_PRICE` is the same rule in SQL.
- **Retained snapshots are marked and bounded** (`reference_db`). A snapshot the empty-result guard
  kept carries `retained_since`, exposed on `products_normalized`; `current_snapshot_where()` is the
  predicate for "today's menu". A snapshot kept for 30 days is dropped on the next visit that
  returns no menu; the history table is untouched.
- **`queue.retire_orphans`** — fails pending jobs whose target no longer exists, which no
  target-scoped consumer could otherwise claim.
- **Stage-1 rosters** (`rung.sources.extract`, `rung.sources.state_search`). Licence numbers and
  licensed-since dates on `DispensaryRecord`; Illinois's roster PDF read by column position;
  placeholder and screen-reader-only cell text ignored; a refused partial roster recorded as
  `failed` rather than as a short roster; Bing's advertisements no longer read as search results.

### Added

- **`rung.rate_gate` — the waiting half of the cross-worker rate limiter, and the wiring that makes
  it load-bearing.** `rate_limit.try_acquire` (the atomic `token_buckets` refill-and-deduct) had
  been correct, tested and documented since the queue-hardening work — and had **zero call sites**.
  It answers "may I go now?"; a scraper needs "wait for my turn". `HostGate` supplies that: a
  dedicated connection (the Stage-2/3 runners share ONE psycopg connection across 6-8 concurrent
  consumers, so committing a token deduction there would commit a sibling's in-flight transaction —
  the same hazard `queue.heartbeat_forever` avoids the same way), an `asyncio.Lock` around a
  `to_thread`'d round trip so the event loop keeps serving, a wait floored at `cost/rate` and
  jittered so simultaneous waiters de-synchronise, a deadline instead of an unbounded block, and a
  fail-open-**and-latch** on any database fault. It is a separate module so `rate_limit.py` remains
  a caller-commits primitive.
- **`queue.release()`** — hand a claim back UNWORKED: `pending`, claim cleared, and the claim-time
  `attempts` increment undone, because nothing was attempted. The `scheduled_at` push-out is the
  termination condition rather than politeness: the decrement removes the queue's own loop guard,
  so a shed target that stayed immediately claimable could be claimed and shed without bound.

- **A Harvard DASH rung in the paper-fetcher example.** DASH lists a repository *landing page* as an OA
  copy's `url_for_pdf`, so the Unpaywall rung fetches HTML and the paper looks paywalled; the new `dash`
  rung follows the landing to its `/bitstreams/<uuid>/download` link and fetches the real PDF. The
  Unpaywall request is now factored into a shared `_unpaywall_json` helper so both rungs share one
  honest failure story. (`examples/paper_fetcher.py`.)

- **An outcome vocabulary for the access ladder.** `access_methods.status` now distinguishes
  `ok` · `unavailable` (the world says no) · `blocked` (we were refused) · `broken` (we are wrong) ·
  `failed` (a rung returned nothing and did not say why). A runner signals with `access.Unavailable`,
  `access.Blocked` or `access.Broken`; `run_target` persists the reason and keeps walking.

  **The asymmetry is the design**: only an explicit `Unavailable` records `unavailable`. Silence
  records `failed`, because the cost of mistaking a broken rung for an empty world is that you stop
  looking. An *unexpected* exception still propagates — a rung that crashes has not told you why.

  `db.access_health()` reports `(method, status, count)` worst-first: the query a scheduled canary
  runs. Enforced twice — `record_access_attempt` rejects an unknown status, and a `CHECK` constraint
  refuses one written by any other path.

- **`attestations`** — a generic engine table (alongside `jobs` and `access_methods`) for the external
  facts an analysis stands on but cannot derive: *"brand X is owned by company Y"*, *"market Z opened in
  2018-01"*. Each row is a subject–predicate–object triple carrying the evidence for itself — source
  type, a citable reference, a URL, the **exact supporting quote**, a confidence grade
  (`verified` | `reported` | `inferred`), and the date it was retrieved. A premise with no recorded
  source is unfalsifiable; checking it means redoing the author's research.
  `db.upsert_attestation()` / `db.attestations_for()`; see `docs/provenance_design.md`.

  Two decisions worth knowing: the primary key is the triple **plus** the source, so two sources may
  attest one fact and a disagreement stays visible rather than becoming last-write-wins; and **negative
  attestations** (`not_owned_by`) are first-class, because a brand-name collision is the failure a
  brand→producer join actually hits.

- **`fx` — a foreign-exchange series fetcher** (Bank of Canada Valet) for cross-currency price
  normalization. It stores a daily quote-per-base series, forward-fills weekend/holiday gaps with an
  `is_carried` flag, and never fabricates a missing rate; a companion `*_fx` view converts each
  observation at the rate that prevailed **on its own date**, so a series in mixed currencies pools
  correctly. Spot FX, not PPP.

- **Two offline geocoding rungs** (`geocode_rnf`, `geocode_points`) — an address→coordinate ladder over
  open government data with no API and no quota: address-range interpolation over the StatCan Road
  Network File, and exact lookup in a municipality's own published address-point file. Both are measured
  against ground truth, and a **precision floor** bars a rung that cannot clear it rather than ranking it
  last. Not yet wired into any pipeline.

- **`static_source` — a DuckDB-over-Parquet data source.** Selected with `RUNG_DATA_SOURCE=static`, it
  runs the read/analysis surface against a frozen Parquet export instead of live Postgres (a
  reproducibility path for outside researchers), behind a psycopg-cursor-shaped adapter so the same
  queries run unchanged on either backend.

- **`brands` — a brand→parent-company crosswalk** over `data/brand_parent.yml`: one source of truth for
  "who owns this brand?", substring-matched in document order, with an unmapped brand treated as its own
  parent (an independent producer).

### Changed

- **The persistence layer split into a generic engine store and a reference schema.** `db` now owns
  only the domain-neutral engine tables (the work queue, the access registry, proxies, rate-limit
  buckets, attestations) and imports nothing domain-flavored; the new **`reference_db`** holds the
  reference-pipeline schema, its migrations, and the SQL analysis guards. A build-your-own-domain
  plugin creates just the engine tables; a back-compat shim keeps the old `db.<reference fn>` call
  sites resolving. This is the seam that lets the engine be reused for a non-cannabis domain.

- `examples/paper_fetcher.py`: replaced the Europe PMC rung with a **`pmc_oa`** rung built on PMC's
  OA Web Service. The old rung fetched an endpoint that now returns 404 for every article — including
  papers that *are* in the open-access subset — so it had been silently dead rather than correctly
  declining.

### Fixed

- **The test suite passes on a fresh clone.** Nine tests failed and the coverage floor could not be
  met, for anyone who cloned this repo and ran `pytest` — and none of it was a defect in the shipped
  code. Four guards sweep the source tree for a contract (*"an orchestrator commits its own unit of
  work"*, *"every roster replace restores the geocode cache"*) and each carries a floor on how many
  call sites it expects to find, because a sweep that matches nothing satisfies every assertion
  perfectly. Those floors were counted over a larger tree than this one, so the guards failed on a
  number no contributor could reach. They now scope to the directories that exist and derive the
  floor from them, keeping their teeth here rather than being deleted. Two further tests scanned a
  directory this repo does not contain — one failed loudly, the other passed over nothing at all,
  reporting a clean bill of health it had not earned; both now skip and name what they looked for.
  One test whose entire subject was two undistributed data files is removed rather than emptied.
  The coverage floor was measured over a wider codebase; measured here it is 65.79%, so the floor
  is 65 and the build records the measurement with its date.

- **`.gitignore` covers test and tooling output.** Running `pytest` leaves a `.coverage` database;
  without the entry, `git add -A` staged a binary file into a pull request. `htmlcov/`,
  `.pytest_cache/` and `.ruff_cache/` are covered too.

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
