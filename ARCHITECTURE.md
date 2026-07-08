# Architecture

A conceptual map of `rung` — the abstractions, the contracts between
them, and the dependency direction. A *map*, not a rationale dump; it exists so an
audit can test concrete invariants instead of gut feel.

**Two packages.** The codebase is split into the public open-source core **`rung`**
and a private plugin overlay (the domain catalogs and per-platform recipes, the
roster-comparison logic, recon, bootstrap, and the curated datasets). The core ships and runs on its own: its proprietary
stages resolve to registry stubs until the overlay plugs in via the `rung.plugins`
entry point. The boundary is enforced — the core imports **nothing** from the overlay, and no
proprietary data lives in the core (`tests/test_import_layering.py`). The overlay is a uv-workspace
member that depends on the core (never the reverse). Full design: `docs/publish_split_design.md`.
Modules below are tagged **[core]** or **[overlay]**; unmarked base/persistence modules are core.

The tool runs **three pipelines** over one Postgres database (`dispensaries`; dev
container, URL via `DATABASE_URL`). Stage contracts —
which stage reads/writes which table, and with what semantics — are formalized
in `docs/stage_contracts.md`:

- **Stage 1 — rosters.** Discover each authority's list resource and
  extract the official roster into `dispensaries`.
- **Stage 2 — entity sites.** For each entity derived from those rosters, scrape
  its *own* website for its locations (the **trusted** source of truth), dedupe by
  physical address, and compare against the roster.
- **Stage 3 — listings.** Scrape each handled location's live catalog into per-store
  `store_products` snapshots, routed by the platform that
  minted the store's Stage-2 scrape handle.

Stages 2 and 3 route through a generic **access-method registry** (`access.py`): a
cost-ranked ladder of extraction methods per target, with a persisted winner and
self-healing re-exploration.

## Reusable engine vs the reference pipeline

Within the public core, two concerns coexist — worth distinguishing for anyone reusing `rung`
in a different domain:

- **Reusable engine (domain-agnostic).** `access` (the cost-ranked method ladder), `queue` (the
  `FOR UPDATE SKIP LOCKED` work queue), `registry` (the plugin seam), `rate_limit` (the cross-worker
  token bucket), `http` (the honest session chokepoint), `browser` (the pydoll primitives), plus the
  **generic-infra tables** in `db` (`jobs`, `access_methods`, `token_buckets`, `proxies`,
  `proxy_tiers`). Reuse these for any scraping domain.
- **Reference pipeline (the dispensary application that exercises the engine).** The Stage-1 roster
  extractors (`sources/{extract, state_lists, state_search, homepage_discovery, ai_fallback,
  dedupe}`), the **domain schema + records** (`db`'s `dispensaries`/`company_stores`/`store_products`/
  `state_programs` tables + `models`), `seed_companies`, and the `cli` front-end.

The boundary is **conceptual, not a clean package edge**: `db`/`models`/`text`/`normalize` are shared
and domain-flavored — the engine imports `db` for its generic tables even though `db` also defines the
domain ones. A physical `rung.engine`/`rung.pipeline` split would first have to decompose those shared
modules (and would break public import paths), so it is deliberately deferred; the split lives as this
documented grouping.

## Layers & abstractions

**Base layer** (no *upward* deps — each imports only third-party libs or lower base modules:
`addresses`→`models`, `normalize`→`models`/`text`; enforced by `test_import_layering.py`, which
forbids importing outside the base layer, not base→base edges):

| Module | Defines | Role |
|---|---|---|
| `models.py` | `DispensaryRecord`, `CompanyReconRecord`, `CompanyStoreRecord`, `StoreProductRecord`, `StateProgramRecord` | Canonical home of the **persisted** record dataclasses. |
| `http.py` | `make_session()`, `set_impersonation()`/`current_impersonation()`, `HONEST_USER_AGENT` | The honest curl_cffi `AsyncSession` factory — the single session chokepoint (enforced by `tests/test_http.py`), nothing else. **Browser TLS impersonation is opt-in; the public default is honest** (a self-identifying `HONEST_USER_AGENT`, no fingerprint spoofing) so the published core circumvents nothing by default (`docs/publish_split_design.md`). The private overlay calls `set_impersonation(...)` at plugin load (the Chrome-profile choice + `RUNG_IMPERSONATE` pin + the `check_impersonation` health-check live on the private side); a public user may opt in via `RUNG_IMPERSONATE` (legacy `DISPENSARY_IMPERSONATE` still honored). `make_session(proxy=…)` forwards a CONNECT-tunnel URL (generic); the pool that picks/rotates it is private. **The anti-throttle machinery is NOT here** — it is private (in the overlay); imports only third-party (no internal deps). |
| `browser.py` | `make_browser_options()`, `render_html()`, `get_script_value()` | pydoll/Chrome primitives (Playwright-installed Chromium). |
| `text.py` | `extract_brand()`, `normalize_brand()`, `load_company_aliases()`, `normalize_category()`, `normalize_product_type()`, `normalize_strain_type()`, `is_placeholder_name()`, `readability_key()`, `normalize_terpene()`, `TERPENE_COLUMNS`, `terpene_floats()`, `dominant_terpene()`, `product_fingerprint()`, `as_dict()`, `name_of()` | Brand splitter + the spelling-insensitive operator key (`normalize_brand` folds "Zen Leaf"/"ZenLeaf"/"NuEra"/"nuEra" — the variants companies.yml doesn't alias) + the one companies.yml alias loader. All shared by seed + compare so folding is consistent. Also the three product-taxonomy normalizers, each a substring-keyword matcher over its own `data/*.yml` (alnum-normalized keys, YAML order = match priority): `normalize_category(raw, name)` → the canonical cross-platform product category (`data/category_aliases.yml` ordered keyword rules on the raw category + `data/category_name_overrides.yml` name-keyword overrides that correct platform-mislabeled forms — a capsule sold as an "edible"; no-match→`"Other"`; see `docs/category_taxonomy.md`); `normalize_product_type(name, category, category_std)` → the **2nd-level** product type *within* a `category_std` (`data/product_type_aliases.yml`, nested per category, matched off the name + raw category; per-category `_defaults` label else `"Unspecified"`, `None` for an uncovered category; see `docs/product_type_hierarchy.md`); and `normalize_strain_type` → the canonical lineage facet `Indica/Sativa/Hybrid/CBD` or `None` (`data/strain_aliases.yml`; conservative lineage-only keywords, no-match→`None`, *not* "Other"). Also `is_placeholder_name` — the one shared junk-row predicate (test/demo/equity-tag/no-data/bare-license/header) used by `extract`+`seed_companies`+`compare` so junk never enters `dispensaries`/`companies`. Beyond names, `text.py` also hosts the cross-platform **terpene** helpers (`normalize_terpene` canonicalization, `TERPENE_COLUMNS`, `terpene_floats`/`dominant_terpene` jsonb coercion) and the master-product **identity hash** `product_fingerprint` (brand+name+size+type, +mg dose for mg-dosed products), plus `readability_key` (shared by seed + compare). (`EN_DASH` is an internal constant.) |
| `normalize.py` | `size_to_grams()`, `grams_to_label()`, `enrich_variants()`, `normalize_terpenes()`, `enrich_record()`, `PERCENT_MAX` | Product-data numeric normalizers (sizes/potency/terpene totals; the canonical terpene-name + identity helpers live in `text.py`); `grams_to_label` is the display inverse of `size_to_grams` (grams → "3.5g") used by the search export so one weight reads the same everywhere: a variant size label → grams (+ per-variant `price_per_g`) and a representative `size_g`, sized only for weight-sold categories (flower/pre-roll/vape/concentrate) so a dosed product's `mg` label can't yield a nonsense `$/g`; a raw terpene list → canonical `{Name: percent}` + `terp_total` (folds `text.normalize_terpene`, sums α+β-pinene, converts `mg/g`→`%`, repairs an impossible >40% total by dropping a lone spike or rescaling an unlabeled `mg/g` row). `enrich_record` stamps these onto a `StoreProductRecord` from the `menu_extractors._record` choke point (idempotent). Backs the `products_normalized` view. |
| `addresses.py` | `clean()`, `extract_address_blocks()`, `BLOCK_ADDRESS_RE`/`PHONE_RE`/… | Shared address/text-extraction primitives (imports only `models`); used by both `extract` and `company_stores` so neither reaches into the other. |

**Persistence:** `db.py` — Postgres access (psycopg3, raw SQL; `DBConn` is the
connection type alias every signature uses). Tables it creates: `dispensaries`,
`company_recon`, `company_stores`, `store_products`, `access_methods`,
`state_programs`, `jobs`, the distributed-scraping infra `token_buckets`
(cross-worker per-host rate-limit buckets — policy in `rate_limit.py`) + `proxies`
(per proxy×host health — policy in the overlay `proxy_store.py`) + `proxy_tiers`
(per-platform egress tier: direct/datacenter/residential — policy in the overlay `proxy_tiers.py`),
and the master-product
DB `products` + `product_observations`
(append-only price/potency/terpene history — the longitudinal substrate, written by
`record_observations`), and the store-lifecycle `store_locations` + `store_observations`
(append-only open/close/acquired history keyed by a physical-location identity — the shared
engine `record_location_observations` consumes `LocationObservation`s whose keys the callers
compute, driven by the overlay `company_stores.record_store_observations` (`company_site` leg)
and the core `extract.record_roster_observations` (`state_roster` leg); see
`docs/store_history_design.md`)
(+ the `products_normalized` VIEW — one row per product exposing the standardized
fields: `category_std` (as `category`), `product_type_std` (as `product_type`), `strain_type_std`
(as `strain_type`), `size_g`, derived
`price_per_g`, percent/mg potency, `terp_total`, `terpenes_std`, `cannabinoids_std` (minor
cannabinoids `{NAME: %}` — CBN/CBG/CBC from Jane/Hytiva/Cresco/Weedmaps), and `currency`
(derived via a `LEFT JOIN state_programs` — `CAD` for CA-province stores, else `USD`; prices
stay numeric in native currency so cross-country analyses partition, D2) — alongside the
identity/price/timestamp passthrough columns
(`id`/`company_id`/`state`/`store_key`/`platform`/`source`/`name`/`brand`/`price`/`scraped_at`);
the combined cross-platform surface)
(+ `ADD COLUMN IF NOT EXISTS` in-place column migrations for `company_stores`,
`state_programs`, `store_products`, `product_observations`, and `jobs` (`_migrate_jobs` adds
`lease_until`/`last_heartbeat`); `store_locations`/`store_observations`
are additive, no migration). CRUD helpers for each.
A companion **materialized view `product_latest`** (one row per distinct product — its latest
observation — over `product_observations` ⋈ `products`, same measurement column names as
`store_products`) backs the `--source current|history` dual-view analyses. Unlike the
`products_normalized` VIEW it is **script-owned** (built + `REFRESH`ed `CONCURRENTLY` out-of-band by
the daily history sweep — a `REFRESH` can't run inside `create_tables`' transaction), **not** created
by `db.create_tables`; it is a read-path analysis cache, not pipeline-persisted truth. Being a
matview (like `products_normalized`), it is outside Contract 3's table-ownership rule.
**Does not create `companies`** (owned by `seed_companies.py`). Imports `models` + `text`
(the latter for `product_fingerprint`/`normalize_brand` in `record_observations`). The legacy `dispensaries.db` SQLite
file is kept as the one-time migration source.

**Work queue:** `queue.py` — transient per-run jobs over the `jobs` table
(`enqueue`/`claim_next`/`claim_target`/`complete`/`bump_heartbeat`/`bump_worker_heartbeat`/
`heartbeat_forever`/`reap_expired`/`requeue_stale`/`prune_completed`/`live_claim_holder`; claims via
`FOR UPDATE SKIP LOCKED`, a partial unique index dedupes live jobs + a partial `jobs_pending_claim`
index over `status='pending'` keeps the claim scan index-only). `enqueue` also takes an opt-in
`spread_seconds=N` that hashes `target_key` to a deterministic `scheduled_at` offset across a window
(Google-SRE distributed-cron, for a future scheduled/cron enqueue; the default keeps `now()`, so the
self-feeding callers stay immediately claimable), and `requeue_stale`/`reap_expired` jitter each
requeued-to-`pending` retry ~0–30s (not the →failed branch) so a requeue wave doesn't stampede.
`complete()` is
**worker-scoped** — it takes the holding `worker` and returns `False` (a no-op) when a
stale-reclaim has reassigned the job, so the orphaned worker rolls back its redundant write
instead of clobbering the reclaimer's (the partitioned Stage-2/3 consumers check the return;
see `docs/stage_contracts.md`). Every claim stamps a `lease_until` window + `last_heartbeat`;
each Stage-2/3 runner keeps its process's in-flight leases fresh with `heartbeat_forever`
(a per-worker keep-alive on a dedicated connection — `bump_worker_heartbeat` extends all of the
worker's claims in one statement so it never interleaves with a consumer's `run_target`
transaction), reaps at startup, and the `reap-jobs` CLI is the standalone reaper (the `worker` CLI packages
reap-at-start + both Stage-2/3 consumers + an optional poll loop as the single fleet entrypoint —
one process per egress IP); `reap_expired`
re-queues an expired-lease (dead-worker) claim through a `FOR UPDATE SKIP LOCKED` subquery so
concurrent reapers don't collide (`requeue_stale` is the coarser claimed_at-age fallback) — see
 §4-5. The per-run companion to the durable `access_methods` registry;
closes the two concurrency hazards in `docs/stage_contracts.md` §5 (concurrent
`scrape-company-stores` runs partition companies; `dedupe-stores` is exclusive per state).

**Rate limiting:** `rate_limit.py` — the public cross-worker per-host token-bucket primitive
(`try_acquire(conn, host, *, rate_per_sec, burst, cost)`) over the `token_buckets` table: one
atomic `INSERT … ON CONFLICT` refills from elapsed time then deducts iff enough remains, with
Postgres `now()` as the authoritative clock. Generic infra (imports `db` only) for the shared-IP
case where per-worker in-process limiters would multiply; the aggregator throttle *policy* (the
adaptive 406 cooldown + the `backoff_delay` retry governor) stays private in the overlay's
`aggregator_http`. See §3-4.

**Generic mechanism:** `access.py` — the access-method registry.
- `AccessMethod(name, cost_rank, run)` — one extraction method; `cost_rank` is
  **try-priority** (lower = tried first; cost-informed, confidence-overridden).
- `run_target(conn, target_type, target_key, catalog, *, governor)` — try the stored
  winner first; on failure, admitted staleness, or an UNTRIED method cheaper than the
  winner (a newly added rung gets one shot instead of being shadowed forever), re-walk
  the ladder cheapest-first; first success becomes the new winner. **Commits per attempt.**
- `ReExploreGovernor` — RED-inspired admission control for discretionary staleness
  re-walks (temporal ramp × per-host limiter).
- `is_plausible`/`is_success` — the "a method worked" gate (≥1 record with name + a
  location signal). Store-shaped by default; `run_target` takes a `plausible`
  predicate override for other record shapes (menus pass `menus.menu_plausible`).
  Imports `db` (for `record_access_attempt`/`get_access_*`).

**Plugin seam:** `registry.py` — the boundary between the **public** open-source core and a
**private** plugin overlay (the Stage-2/3 scraping catalogs, the roster-comparison
logic, and recon). `cli.py` resolves those proprietary stages by name through
`registry.resolve(...)` (a runtime lookup, never a static import); the overlay registers the real
implementations via the `rung.plugins` entry-point group, discovered once by
`registry.load_plugins()`. With the overlay absent, an unplugged stage resolves to a stub that
raises `StageNotAvailable` only when invoked, so the public CLI dispatches every verb either way.
The overlay's bridge registrar (`rung_intel.intel_plugin.register_all`, the entry
point) registers the proprietary stages and enables impersonation on load. **Full design + the
public/private partition: [`docs/publish_split_design.md`](docs/publish_split_design.md).**

**Sources.** Two homes after the carve-out: the **generic, public** sources stay in
`rung/sources/` (`state_search`, `state_lists`, `extract`, `ai_fallback`,
`homepage_discovery`, `dedupe` — Stage-1 government-roster extraction + shared dedup); the
**proprietary** scrapers/catalogs/mappers/intel moved **flat into the overlay**
`rung_intel/` (`company_stores`, `company_store_fetch`, `company_store_extractors`,
`menus`, `menu_extractors`, `compare`, `recon`, `bootstrap`, and the platform recipes `dutchie`,
`dutchie_plus`, `weedmaps`, `leafly`, `sweedpos`, `trulieve`, `cresco`, `curaleaf`, `fluent`,
`hytiva`, `jane`). The table below keeps the per-module detail; **[overlay]** marks the moved ones (write
through the core's `db.py` or return records):

| Module | Problem | Key API | Writes |
|---|---|---|---|
| `state_search.py` | Per-state program coverage + verified agency URL | `run_state_coverage`, `load_states`, `StateInfo`/`StateCoverage` | `state_programs` (non-list cols) |
| `state_lists.py` | Crawl landing page → score links → find list resource | `run_find_lists`, `find_list_url`, `ListCandidate` | `state_programs.list_*` |
| `extract.py` | Extract records from a list, dispatching on `list_type` (pdf/csv/kml/arcgis/lookup/html/ca_dcc/az_dhs/co_med/ma_ccc/on_agco/ab_aglc/bc_lcrb/sk_slga — the `ListType` Literal, with `HANDLED_LIST_TYPES = frozenset(get_args(ListType))` derived from it); opt-in `--render` and `--ai` tiers; `--record-history` also appends the `state_roster` leg of the store-lifecycle history via `record_roster_observations` (physical-location identity from `dedupe.geo_key`/`address_key`, only on a non-empty extraction) | `run_extract_states`, `record_roster_observations`, `ExtractResult`, `print_extract_report`, `extract_records`, `extract_rendered`, `HANDLED_LIST_TYPES` | `dispensaries`; `store_locations`/`store_observations` via `db.record_location_observations` when `--record-history` |
| `ai_fallback.py` | scrapegraphai+Ollama extraction fallback (model via `RUNG_OLLAMA_MODEL`, legacy `DISPENSARY_OLLAMA_MODEL` honored, default `llama3.2`) | `extract_with_ai` | returns records |
| `recon.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `homepage_discovery.py` | Opt-in (`recon --discover`) web-search of a no-homepage operator → filter aggregators/social → rank by brand↔domain → validate via `recon._probe_one`. Reuses `state_search` backends; probe injected to avoid a cycle | `discover_homepage`, `build_discovery_queries`, `rank_candidates`, `make_backends` | none (caller persists via recon) |
| `company_stores.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `company_store_fetch.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `company_store_extractors.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `dutchie.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `weedmaps.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `leafly.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `aggregator_http.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `proxy.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `proxy_store.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `proxy_tiers.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `dutchie_plus.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `sweedpos.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `jane.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `menus.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `menu_extractors.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `trulieve.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `cresco.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `curaleaf.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `fluent.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `hytiva.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |
| `dedupe.py` | Collapse duplicate stores (cross- & intra-company) by physical address, coordinate cell (~11 m), **or platform handle** (`platform:external_id` — folds an address-less duplicate of the same store), **plus a same-operator ~100 m geo-merge** for cross-platform geocode drift (scoped to one `canonical_name` — never across operators); pick canonical operator; **keep the richest-menu handle per rooftop** (Dutchie/first-party > Weedmaps/Leafly) as the surviving menu-scrape row; carry a folded sibling's coords onto a kept row that lacks them; stamp `storefront_name`; **realign `store_products.company_id`** onto each handle's kept row so menus scraped under a since-folded alias re-attribute to the operator. **Full design: [`docs/dedupe_design.md`](docs/dedupe_design.md).** | `run_dedupe`, `DedupeReport`, `print_dedupe_report`, `normalize_address`, `address_key`, `geo_key`, `location_key`, `physical_key`, `pick_canonical` | `company_stores.canonical_company_id` + `storefront_name` + coords; `store_products.company_id`; **commits** |
| `compare.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). | — | — |

**Derived / front-end:**

| Module | Role |
|---|---|
| `seed_companies.py` | Derive `companies` from `dispensaries` brands (canonicalized via `companies.yml` + `text.normalize_brand` spelling-fold). **Owns `companies`** (creates it via `create_companies_table`). Own entry point. |
| `bootstrap.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). |
| `analyze.py` **[overlay]** | Private overlay module — not shipped in the public core; resolved via the plugin seam (Stage-2/3 catalogs + per-platform helpers; recipe withheld). |
| `cli.py` | Thin Click front-end. Orchestrates everything; owns the persistence for `recon` and the per-state dedupe claim. |

## Dependency direction (acyclic)

```
  PUBLIC CORE  (rung)            ┊   PRIVATE OVERLAY  (rung_intel)
                                               ┊
  cli.py ──registry.resolve(stage)──▶ stub ◀──┊── intel_plugin   (rung.plugins entry point)
   ┌───────┬────────────┬────────┐             ┊      company_stores · menus · compare · recon · bootstrap
 extract  state_lists  dedupe  seed_companies  ┊      company_store_{fetch,extractors} · menu_extractors
   │ └ai_fallback  state_search                ┊      dutchie · dutchie_plus · weedmaps · leafly · sweedpos
   ▼                                           ┊      trulieve · cresco · curaleaf · fluent · hytiva · analyze
 access · registry · db · queue                ┊                       │
   │                                           ┊   (every overlay module imports the core;
   ▼                                           ┊    the core imports NOTHING from the overlay)
 models · http · browser · text · normalize · addresses  (base) ◀┄┄┄┄┄┄┄┄┄┄┄┘
   (the private overlay also carries `aggregator_http` + `proxy` — the evasion machinery)
```

`cli.py` reaches the overlay's stages through `registry.resolve(name)` — a **runtime** lookup, not a
static import — and the overlay registers them via the `rung.plugins` entry point; so
there is no `core → overlay` import edge. The per-module edges below are unchanged by the move (each
module imports the same things); the moved ones now simply live in `rung_intel/` and
import the core across the package boundary.

Edges (confirmed): `db→{models, text}` (the latter for `product_fingerprint`/`normalize_brand`
in `record_observations`); `access→db`; `company_stores→{access, db, queue, http, models,
company_store_fetch, company_store_extractors, dutchie_plus, curaleaf, dutchie, fluent,
leafly, proxy_store, proxy_tiers, sweedpos, weedmaps, dedupe (geo_key/address_key for the
store-history location identity)}` (proxy_store/proxy_tiers back the
per-company session rotation on the company's platform tier, mirroring Stage-3 `menus`);
`company_store_fetch→{models, addresses, dedupe (normalize_address), ai_fallback,
company_store_extractors, dutchie_plus, hytiva}`;
`company_store_extractors→{addresses, models, text}` (the shared `is_placeholder_name` junk filter); `menus→{access, db, queue, http, models, dedupe
(normalize_address), cresco, dutchie, dutchie_plus, hytiva, leafly, proxy_tiers, sweedpos, trulieve,
weedmaps, menu_extractors}` (proxy_tiers selects each store's platform tier pool);
`menu_extractors→{models, text, normalize}`; `normalize→{models, text}` (base-layer
number/normalization helpers, acyclic);
`dutchie`/`dutchie_plus`/`sweedpos`/`trulieve`/`cresco`/`curaleaf`/`fluent`/`hytiva` →
*nothing internal* (pure platform helpers; only third-party + `data/*.yml`); the two
aggregator-sweep helpers `weedmaps`/`leafly` → **overlay `aggregator_http` only** (private HTTP
helpers; lean — they reach no heavier catalog); `aggregator_http→proxy` (both overlay);
`dedupe→{db, models}`;
`compare→{db, dedupe, text}`; `extract→{addresses, browser, db, http, models, ai_fallback, dedupe
(geo_key/address_key for the roster store-history leg)}`;
`recon→{db (type-only: the `db.DBConn` annotation; recon stays read-only), http, models, text,
homepage_discovery (lazy, --discover only)}`;
`homepage_discovery→{models, text, state_search}` (probe injected from recon, no recon import);
`state_lists→{db, http, state_search}`;
`state_search→{browser, db, http, models}`; `seed_companies→{db, text}`. No module imports
`cli.py`; nothing in the base/`addresses` layer imports upward. Post-carve-out the intra-`sources`
(public) edges are `state_lists→state_search`, `homepage_discovery→state_search`, and
`extract→dedupe` (eager) + `extract→ai_fallback` (lazy); `compare→dedupe` is now a **cross-package**
edge (overlay `compare` → public `dedupe`), and `company_stores→{company_store_fetch,
company_store_extractors, dutchie, dutchie_plus}` are **intra-overlay** edges — the `ai_fallback` edge
moved into `company_store_fetch` (a lazy import) during the Stage-2 split, and `company_stores` no longer
reaches into `extract` (the shared primitives now live in `addresses`).

**Enforced** by `tests/test_import_layering.py` (AST guard, mirrors `test_http.py`): within the
**public core** the internal import graph is acyclic, the base layer imports only base, the
foundation tiers (base/db-queue/access) never import the upper band, and nothing imports `cli.py`.
Within the **overlay** (skip-guarded on a public-only build) the same AST walk re-enforces the
proprietary tiers the carve-out moved out of the public tree: the per-platform pure helpers
(`dutchie`/`dutchie_plus`/`sweedpos`/`trulieve`/`cresco`/`curaleaf`/`fluent`/`hytiva`) carry **zero**
internal imports, the two aggregator sweeps (`weedmaps`/`leafly`) import **only** the overlay's
`aggregator_http` (and at most public `http`), and the overlay's internal import graph is acyclic.
Across the **package boundary** (`docs/publish_split_design.md`): the public core imports **nothing**
from `rung_intel`, the public package holds **exactly** the public module set (nothing
proprietary leaks back in), and the overlay depends on the core. A companion **data leak guard**
keeps the core's `data/` to public/shared files only — the proprietary slugs/chains/tokens/store-ids
live with the overlay. Together: the contract that lets the open-source core ship and run without the
overlay (its proprietary stages then resolve to registry stubs).

## Cross-cutting contracts

1. **Source of truth.** Persisted truth = the Postgres DB; `data/*.yml` are curated
   inputs (states, company aliases, homepage overrides, grower brands). For *store
   data specifically*, a **company's own site outranks the state roster** — Stage 2
   trusts the site; `compare` frames site-only stores as the state being stale.
2. **Commit discipline (two tiers).** Low-level `db.py` write helpers
   (`insert_*`, `upsert_*`, `set_store_*`, `delete_*`, `realign_store_products_company`)
   **never commit** — the caller
   does. But `db.create_tables` and the high-level orchestrators that own a unit of
   work **do commit themselves**: `access.run_target` (per attempt), `dedupe.run_dedupe`,
   `state_*`/`extract` `run_*`, `seed_companies`, and `company_stores.run_company_stores`
   (per claimed company, atomically with the job completion). `recon.run_recon`
   reads/returns only — the CLI commits its writes. The queue's `claim_*` functions
   commit internally (a claim must be durable before work starts). Postgres note: a
   handler that swallows an exception and keeps using the connection must `rollback()`
   first (a failed statement poisons the open transaction).
3. **Table ownership.** `db.create_tables` creates every table **except `companies`**
   (owned by `seed_companies.py`). It is a thin wrapper over `create_engine_tables`
   (the domain-neutral engine tables — `jobs`/`access_methods`/`token_buckets`/`proxies`/`proxy_tiers`)
   + `create_reference_tables` (the cannabis reference schema); a build-your-own-domain plugin calls
   only `create_engine_tables` (genericization Workstream B2 — see).
4. **List columns are write-isolated.** `state_programs.list_*` are written only by
   `set_state_list`; `upsert_state_program` omits them.
5. **Non-destructive replace + keep-the-best (quality-aware).** A state's `dispensaries`
   (`run_extract_states`) is replaced only when the scrape yields data; a company's
   `company_stores` go through `db.replace_company_stores`, a layered keep-the-best:
   - **Menu-platform handles dominate count (decided first).** A *real-menu* handle count
     (Jane/Dutchie/Sweed/… — **not** the Weedmaps/Leafly directory listings, whose menus are
     usually empty) that **drops** never clobbers (no downgrading real menus to a larger
     empty-aggregator sweep); one that **rises** wins as long as it retains ≥
     `_MENU_UPGRADE_RETENTION` (0.5) of the stores (4 empty Leafly listings yield to 3 Jane
     handles, but a 15→1 collapse is still rejected).
   - **Otherwise (equal menu-handle count) decide on distinct count.** Overwrite when the new
     yield is **≥** what's stored (so a flaky low-yield run can't clobber good data; on a tie
     the fresher result wins) **or** when the new result adds Stage-3 `external_id` handles
     where the stored rows had none and retains ≥ `_HANDLE_UPGRADE_RETENTION` (0.8) of the
     count — a handled row outranks a bare address.

   Counts are DISTINCT physical stores (not raw rows). A big count loss is still rejected. Tested.
6. **Registry winner = cheapest `ok`.** In `access_methods`, the winner for a target is
   the lowest-`cost_rank` row with `status='ok'` (pure SQL). `resource_url`/`params`
   record the exact locator that won.
   - **Batch-shared resources** (a lazily-launched Chrome; the per-state platform-directory
     sweeps — Dutchie, Weedmaps, and Leafly) live in a context dict + `asyncio.Lock` threaded
     through `_company_catalog`, so the first company that reaches the rung pays the one-time
     cost and the rest reuse it. `dutchie_directory` (rank 1), `jane_api` (rank 2, ungated +
     self-gating — the menu-embed rung; also harvests a Dutchie embedded-menu id off an
     operator's own menu subpages and resolves it against the rank-1 sweep, so an operator whose
     Dutchie `cName` ≠ its name is still caught with a handle), `dutchie_plus` (rank 8), and the
     aggregator sweeps `weedmaps_directory`
     (rank 9) / `leafly_directory` (rank 10) are ranked by **try-priority** rather than raw
     cost — they're self-gating and high-recall, so they out-rank the low-yield generic
     parsers that would otherwise shadow them (`cost_rank` is try-priority — the derived
     `cost_tier` label was removed,
     audit M2). A homepage-less ("homeless") company gets a catalog of *only* the three
     directory rungs, since the homepage-based rungs would no-op anyway.
7. **Operator vs storefront naming.** The **operator** (canonical company) is the
   dedup/grouping key; the **storefront** (`storefront_name`, e.g. "Harvest of
   Whitehall") is the display label for reporting. Dedupe folds aliases into the
   operator but stamps each store's storefront brand.
8. **Two type vocabularies (intentional).** `source_type` (`pdf|map|html|api`) describes
   the agency evidence URL; `list_type` (`pdf|csv|kml|arcgis|lookup|html|ca_dcc|az_dhs|co_med|
   ma_ccc|on_agco|ab_aglc|bc_lcrb|sk_slga` — the `extract.ListType` Literal) is what `extract.py` dispatches on. The
   per-state custom handlers (`ca_dcc`/`az_dhs`/`co_med`/`ma_ccc`/`on_agco`/`ab_aglc`/`bc_lcrb`/`sk_slga`) are added to the
   Literal, not produced by `state_lists._classify`, so the contract-8 test still guards
   `_classify`'s output ⊆ `HANDLED_LIST_TYPES`.
9. **Canadian provinces are states (D1).** Province rows ride the same `state` TEXT
   column everywhere (2-letter codes don't collide with USPS); `states.yml` /
   `state_programs.country` (`US`/`CA`, default `US`) is the only marker, used to
   partition exports/analyses and derive currency — see.

## Known asymmetries (intentional)

- **The access-method registry serves Stages 2 and 3, not Stage 1.** `access.py` is
  written to be generic (the design doc envisions `state_dispensary_list`/`state_agency`
  target types), but Stage 1 (`extract.py`) keeps its own ad-hoc `list_type` dispatch +
  render/ai tiers. This is a **standing decision, not a roadmap gap**: Stage 1 is
  deliberately left off the registry (a standing decision); do not re-litigate.
- **Stage 3 picks proxies in-process (per-store rotation); Stage 2 uses the durable
  `proxy_store` (per-host health).** `menus.run_store_menus` calls `ProxyPool.acquire(host=store_key)`
  — sticky **per-store** IP rotation (one exit per store, spread across many; the VT 51/51-validated
  pattern) with in-process quarantine. `company_stores.run_company_stores` instead calls
  `proxy_store.claim_proxy(conn, host, …)` — durable cross-worker health keyed by the company's
  **network host**. The asymmetry is deliberate, for two reasons: (1) a store's proxy key is
  `platform:external_id`, not a network host — *all* of a platform's stores share one host
  (`dutchie.com`), so `proxy_store`'s per-host model would either fragment health per store_key
  (useless) or collapse every store onto the single "healthiest" IP (killing the rotation);
  (2) under the documented fleet topology — **one distinct egress
  IP per worker** for the per-store-session rungs Dutchie/Jane/Sweed, aggregators rotating
  per-request inside `get_json_retry` — workers don't share IPs, so there is no cross-worker ban to
  coordinate — the in-process quarantine suffices. Durable per-*platform-host* health would only add
  value under a shared multi-IP-pool config; if that topology is ever adopted, revisit (audit M1,
  2026-07-02).
- **`seed_companies.py` owns the `companies` table** while sharing the DB via
  `db.get_connection()`.
- **`extract.py`'s `--render`/`--ai` and `company_stores`' `browser_render`/`ai_llm`
  rungs are opt-in / last-resort** by cost.
- **Jane has no standalone platform module** (every other handled platform does —
  `dutchie`/`dutchie_plus`/`weedmaps`/`leafly`/`sweedpos`/`trulieve`/`cresco`/`curaleaf`/`fluent`/`hytiva`).
  Jane's payload is pure JSON, so its only pure surface is the `menu_extractors.jane_hit_records`
  mapper (which *is* modularized like every platform); the Algolia fetch/paging lives inline in
  `menus.py` (mirroring SweedPOS, whose menu fetch is also inline while `sweedpos.py` holds only
  pure parsers) and store discovery in `company_store_fetch._jane_stores`. Extracting a `jane.py`
  for the Algolia constants + query helpers is a possible parity refactor, not a gap. (audit 2026-07-07)
- **Two physical-store keys, each with a coordinate fallback.** `dedupe.address_key` = full
  normalized address + zip (intra-source dedup; named `address_key`, not `store_key`, to stay distinct
  from the `{platform}:{external_id}` menu handle Stage 3 calls `store_key`); `compare._match_key` = house number +
  first significant street word + zip (cross-source match, tolerant of addresses that bake in
  city/state — a leading number + the first non-directional word is effectively unique per
  operator).
  Different jobs. Both fall back to `dedupe.geo_key` — an ~11 m coordinate cell (4 decimals)
  + zip — so the same rooftop scraped with divergent address text still
  dedupes/matches where coordinates exist (company_stores broadly; rosters only in
  CA/MA/NJ). The cell is deliberately tight: measured against the dataset, 110 m wrongly
  merged neighbouring competitors while 11 m merged only genuine same-store pairs.
  `compare` adds a third, coarsest tier — a **city-level locality fallback**
  (`_locality_partition`, count-based per operator) that mops up street/geo-unmatched
  stores for rosters carrying only a city/county and no street (WA city+zip; MD
  county + city-in-name). Precise tiers run first; locality only pairs the leftovers.
  `dedupe.physical_key(record)` is the record-level convenience that layers these — coordinate
  cell → `address_key` street+zip → platform handle — used to dedupe the same physical store
  across the Dutchie/Weedmaps/Leafly bootstrap pools (`bootstrap.py`).
  `dedupe.location_key` is the distinct **store-history** identity — `geo_key` with a *guarded*
  `address_key` fallback that **rejects county-only rows** (MD's 119→23 keys) so a coarse roster
  address can't fabricate an operator-change event. It is the public symbol both store-history legs
  import (`extract.record_roster_observations` and `company_stores.record_store_observations`); the
  edge/tier annotations name `geo_key`/`address_key` (its internals) for brevity, but `location_key`
  is the actual boundary-crossing key.

## Reference index

| Abstraction | File | Notes |
|---|---|---|
| Persisted record types | `models.py` | canonical definitions |
| Engine DB (generic) | `db.py` | connection + the 5 infra tables (`jobs`/`access_methods`/`token_buckets`/`proxies`/`proxy_tiers`) + `create_engine_tables` + the access-registry CRUD; imports **no** `models`/`text` (genericization B1/B3) |
| Reference DB schema & CRUD | `reference_db.py` | the cannabis tables + `products_normalized` view + migrations + all domain CRUD + `create_reference_tables`/`create_tables` + `NATURAL_FLOWER_WHERE`; imports `db` + `models` + `text`. `db.<reference fn>` still resolves via `db.__getattr__` (back-compat shim) |
| Work queue | `queue.py` | SKIP LOCKED claims + lease/heartbeat/reaper; `docs/stage_contracts.md` §5 §4-5 |
| Cross-worker rate limit | `rate_limit.py` (`token_buckets`) | per-host token bucket; §3-4 |
| Stage contracts | `docs/stage_contracts.md` | read/write matrix, write isolation, claim keys |
| Access-method registry | `access.py` | `AccessMethod`, `run_target`, `ReExploreGovernor` |
| Dedup + storefront | `sources/dedupe.py` | physical-address clustering |
| Stage-1 extraction tiers | `sources/extract.py`, `sources/ai_fallback.py` | |
| Coverage / list discovery | `sources/state_search.py`, `sources/state_lists.py` | |
| CLI surface | `cli.py`, `pyproject.toml [project.scripts]` | |
| Product-type hierarchy | `docs/product_type_hierarchy.md` | The owned 2nd-level taxonomy design (`product_type_std`); `data/product_type_aliases.yml` |

Measured findings against this baseline are tracked in the internal architecture audit.
