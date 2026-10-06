# Stage Contracts

**Status:** contracts formalized 2026-06-10 as part of the decoupling effort. The claim keys in §5 are implemented by the `jobs` work
queue (see `rung/queue.py`).

Each pipeline stage is an independent CLI command (`rung/cli.py`). **The database is
the only interface between stages** — no in-memory state crosses a command boundary. This document
is the contract: which tables each stage reads and writes, with what semantics. A stage may be
rewritten, re-run, or re-scheduled freely as long as its contract holds.

## 1. Read/write matrix

`R` = reads, `W` = writes, `W-cols` = writes only specific columns (see §4).

| Stage | state_programs | dispensaries | geocode_cache | companies | company_recon | company_stores | store_products | access_methods | jobs |
|---|---|---|---|---|---|---|---|---|---|
| search-states | W | | | | | | | | |
| find-lists | R, W-cols (`list_*`) | | | | | | | | |
| scrape-states | R | W (replace-by-state) + W-cols (geo restore) | R | | | | | | |
| seed-companies | | R | | **W (owner)** | | | | | |
| recon | | R | | R | W | | | | |
| scrape-company-stores | R | | | R | R | W (keep-the-best) | | W | W |
| dedupe-stores | | | | R | | W-cols (`canonical_company_id`, `storefront_name`, coords) | W-col (`company_id` realign) | | W |
| compare-stores | | R | | R | | R | | | |
| store-lifecycle | | | | R | | R | | | |
| scrape-menus | | | | | | R | W (replace-by-store) | W | W |

`store-lifecycle` additionally reads the append-only history tables `store_locations` +
`store_observations` and `store_capture_attempts` (not in the matrix — they are written only under
`--record-history`, see §2/§3).
It writes nothing unless given `--write`, which replaces its state's rows in the derived
`store_lifecycle_events` (Phase 2).

YAML inputs (curated, read-only; under `rung/data/`): `states.yml` (search-states,
find-lists, scrape-states), `companies.yml` (seed-companies, compare-stores),
`company_homepages.yml` (recon), `state_geo_anchors.yml` (scrape-company-stores).

The private overlay adds its own curated catalogs, read by the same stages but shipped with the
overlay rather than the core: per-operator platform handle maps and per-platform API tokens
(scrape-company-stores), and a grower/processor brand exclusion list (compare-stores). Naming them
here would advertise how the proprietary stages reach each target, so this doc describes their role
and not their contents.

## 2. Per-stage contracts

### search-states — `sources/state_search.py`
- **In:** `states.yml` (57 jurisdictions — 50 states + DC + PR + 5 Canadian provinces; `known_url` seeds).
- **Out:** `state_programs` upsert — all columns EXCEPT `list_*` (`db.upsert_state_program`,
  db.py). Status: `check_status` ok|failed|never, `error`, `searched_at`.
- **Re-run:** per-state upserts, committed in batches; `--failed-only` re-processes
  `check_status != 'ok'`. Crash mid-run leaves later states untouched.

### find-lists — `sources/state_lists.py`
- **In:** `state_programs.best_url` (from search-states); `states.yml` `list_url:` overrides.
- **Out:** `state_programs.list_url/list_type/list_found_at/list_status` ONLY, via
  `db.set_state_list` (reference_db.py). Status: `list_status` found|override|none.
- **Re-run:** skips states that already have a list unless `--force`; commits per state. A landing
  page that cannot be fetched raises rather than reading as "no list", so `--force` keeps the stored
  `list_url` instead of writing NULL and dropping the state from extraction (2026-10-06).

### scrape-states — `sources/extract.py`
- **In:** `state_programs.list_url/list_type` (states with a found/override list).
- **Out:** `dispensaries`, **replace-by-state**: `DELETE WHERE state = ?` then inserts — and the
  delete runs ONLY when extraction yielded ≥1 record, so a transient zero-yield never wipes prior
  good rows. Append via `db.insert_dispensary` (reference_db.py); commit per state.
  - **Then `db.apply_geocode_cache(conn, "dispensaries", abbr)`, in the SAME commit** — reads
    `geocode_cache`, writes back the derived `latitude`/`longitude`/`zip_code`/`city`. The
    non-empty-replace guard above protects the *anticipated* failure (an empty scrape wiping good
    rows) and is blind to the real one: a **successful** scrape wiping **derived** rows, because the
    source republishes only what the source publishes. For a roster with a street but no ZIP (NV, IL,
    UT, MD) that silently un-matched the state — `compare`'s two keys BOTH carry the ZIP. Each column
    is restored only where it `IS NULL`, so a source-published value is never overwritten. Costs no
    geocoder calls. Enforced across all three delete+reinsert callers by
    `tests/test_roster_replace_restores_geocode.py`.
  - `store_capture_attempts` (one `state_roster` row per state per run: succeeded/empty, or `failed`
    with the reason when the roster was refused as a fragment — `PartialRoster`, which an ArcGIS
    layer still flagging rows at the page cap and a failed `ca_dcc` sub-box also raise since
    2026-10-06 — in which case the render and AI tiers are skipped and the prior rows stand; one
    handler's crash (`ExtractionFailed`) costs that state, never every state in the run, and is
    recorded `failed` with its reason the same way) **only under
    `--record-history`**, same commit — what was attempted, beside what was seen (Phase 3).
  - `store_locations` + `store_observations` via `extract.record_roster_observations` **only under
    `--record-history`** — the `state_roster` leg of the store-lifecycle history (same shared engine,
    `db.record_location_observations`, as Stage 2's `company_site` leg), appended inside the same
    non-empty-replace commit, from records the geocode cache has already filled — before 2026-10-06
    they were recorded from the raw rows, so a no-ZIP roster wrote no history at all. A failed extraction records nothing, so observed absence stays a real
    signal. See `docs/store_history_design.md`.
- **Re-run:** idempotent per state.

### seed-companies — `seed_companies.py`
- **In:** `dispensaries.name/state`; `companies.yml` aliases.
- **Out:** `companies` — **this module owns the table** (creates it; `db.create_tables` does not).
  Insert-if-absent on `(canonical_name, state)`; never deletes.
- **Re-run:** additive only.

### recon — `rung_intel/recon.py`
- **In:** `companies.id/canonical_name/state`; `dispensaries.name/**city**/website` (homepage
  derivation, indexed PER STATE on both the `--state` and the bare path — the bare path pooled every
  roster into one vote until 2026-10-05, so a company could derive another state's site);
  `company_homepages.yml` overrides, exact-name and scoped per state (`recon.override_for`).
- **Out:** `company_recon` full-row upsert per company (`db.upsert_recon`, reference_db.py). Failure is a
  row with `error` set; success has `error IS NULL` + `platform`/`confidence`.
- **Re-run:** re-probes and overwrites — **every row of the state, including the ones it can no
  longer derive.** A company whose homepage the override file or the roster no longer yields is
  rewritten `no_url`, and Stage 2 then walks it homeless, so a Jane/SweedPOS winner that read the
  homepage is no longer offered. `coverage_healthcheck`'s `recon_would_drop_homepage` and
  `recon_would_change_homepage` forecast exactly that set — erased, and re-pointed — from
  `recon.planned_homepage` (the run's own decision, no request made); read them before the run,
  because the run overwrites the evidence they read. `homepage_override_dead_key` names a key that
  reaches no company. It exists because the
  2026-10-05 re-run of IL and OH did this to four companies whose names the 2026-07-14 refold had
  moved out from under their override keys, and only a hand-taken snapshot showed it.
- **The website index MUST be keyed exactly as `seed_companies` keys a company** — `normalize_brand(
  extract_brand(name, city))` — **and the stored `canonical_name` must be re-passed through
  `extract_brand` before lookup, not merely normalized.** It is a string written by whatever
  `extract_brand` looked like the day that company was seeded, so comparing it raw against a
  freshly-derived key compares an old fold with a new one. Both mismatches were live: the roster row
  "Fresh Elizabeth" filed its website under a key the lookup never asked for, and recorded `no_url` —
  *our* key mismatch, persisted as a fact about the operator ("it publishes no website") while the
  roster published it.
- **`company_recon` is DERIVED FROM the roster and nothing re-derives it when the roster changes.**
  Recon is a *snapshot* of the read. NJ's roster was repaired 2026-07-09 (3 rows → 317) and re-scraped
  07-12; recon last ran **06-17**, when NJ had no roster and hence no websites, and recorded `no_url`
  for 209 of 215 companies. Re-running it took NJ from **3 → 91** homepages. `coverage_healthcheck`'s
  `recon_stale` check now fails when recon predates the roster that fed it — it deliberately stays
  silent for CO/MT/NV, whose rosters publish **no** website column at all, so their `no_url` is a
  *correct* verdict rather than an expired one.
- **Stage 2 walks every company, whether recon has probed it or not** (2026-10-05). It took its
  companies from a JOIN on `company_recon` until then, so "homeless" meant a row saying `no_url`
  and a company with NO row was walked by nothing — not on the homepage rungs, not on the
  directory sweeps. Recon is run by hand, per state, and nothing schedules it; the bootstraps
  that create companies never write a recon row. Measured that day: **5,435 of 15,585 companies
  in 41 states**, every one created after its state's last recon (or in a state never
  reconned), holding 6,213 store rows none of which had been rewritten since July. Stage 3 was
  unaffected — it walks handles — so their menus kept refreshing while their store lists froze,
  and nothing failed, because a company nobody asks about cannot fail. `recon_stale` could not
  see it either: it JOINs recon too. `get_companies_for_stage2` now returns a never-reconned
  company as a homeless one. Recon decides what a company's homepage IS; it no longer decides
  whether the company exists. What remains is reported by `coverage_healthcheck`'s
  `company_never_reconned`: a company recon never ran for, where recon HAS a homepage to read —
  the state's roster publishes websites (7 of 45 rosters do) or the override file names the
  operator. That was 27 companies in 10 states the day this landed; for the other 5,400 recon
  would record `no_url` and change nothing.
  ⚠ **What walking them does, replayed offline against the stored listings that day** (the
  directory rungs attribute by slug PREFIX, as they always have for homeless companies): about
  2,740 of those companies are rewritten with the same stores, 840 gain stores (1,640 in all),
  1,520 match nothing and keep what they hold, 260 are returned fewer stores than they hold and
  keep them, and **77 lose a store row** (82 rows). Of the stores gained, about 570 go to a
  company whose name is ONE word (`Kush`, `American`, `Elevated`) — where a prefix can attach
  another operator's listing — and the rest mostly to a name variant of the same business
  (`Atlantic Medicinal` / `Atlantic Medicinal Partners`).

### scrape-company-stores — `rung_intel/company_stores.py`
- **In:** `db.get_companies_for_stage2` (reference_db.py): EVERY company in the state, with recon's
  homepage when recon has one and `''` otherwise — a homeless company still gets the homepage-independent
  directory rungs (the Dutchie geo-sweep attributes by `chain`); `access_methods` per-target winner
  + hints; platform YAMLs.
- **Out:**
  - `company_stores` via `db.replace_company_stores` (reference_db.py) — **keep-the-best replace**:
    per-company delete+insert happens only if the new result covers **at least as many DISTINCT
    physical stores** (by address; raw row counts let a double-counting extractor entrench
    itself), OR new adds `external_id` handles where stored had none AND retains ≥0.8 of the
    distinct count — and, decided before either, MORE menu-bearing handles win at ≥0.5 retention
    while fewer never clobber (except the SAME non-aggregator source re-answering at ≥0.8 distinct
    retention: a closure or a not-yet-online store, recorded by the one list that knows), where
    "menu-bearing" means a non-aggregator handle that `routing.handle_served_predicate` (the Stage-3
    routing table) says a rung serves. Zero-yield re-runs keep prior data.
  - `access_methods` via `db.record_access_attempt` (db.py) — one upsert **per method attempt,
    committed immediately** (access.py). Load-bearing for crash recovery: a killed run
    leaves a frozen ladder snapshot; the next run resumes from the stored winner.
  - `jobs`: enqueues one `company_stores` job per company, then claims them (see §5).
  - `store_capture_attempts` (one `company_site` row per company per run: succeeded/empty/failed —
    the crash path writes `failed` in its own transaction) **only under `--record-history`** (Phase 3).
  - `store_locations` + `store_observations` via `company_stores.record_store_observations` **only
    under `--record-history`** — the store-lifecycle twin of Stage 3's `product_observations`. Reads
    back the company's just-replaced rows, resolves a physical-location identity
    (`dedupe.geo_key`/`address_key`), and APPENDS an observation (operator/storefront/handle) when it
    changed or once/day as a heartbeat. Same transaction as the replace + job completion.
    Append-only; see `docs/store_history_design.md`.
- **Scoped re-scrape:** `--only "<term>[,<term>]"` (`run_company_stores(only=…)`) narrows to companies
  whose canonical name contains a term or whose id matches. It enqueues + **`claim_target`s only
  those** targets (not `claim_next`), so a focused debug run never claims or fails a concurrent
  full run's jobs.
- **Re-run:** cached winner skips re-discovery; failure triggers a ladder re-walk (see the
  access-method design doc).

### scrape-menus — `rung_intel/menus.py` (Stage 3)
- **In:** `db.get_menu_stores_for_state` — canonical `company_stores` rows carrying a Stage-2
  scrape handle (`platform` + `external_id`), DISTINCT ON the handle; the discovery `source`
  column routes the menu rung (it says which platform minted the external_id); the company's
  Stage-2 `access_methods` params supply the Dutchie Plus token.
- **Out:**
  - `store_products` via `db.replace_store_products` — **wholesale snapshot replace per
    store_key** (`{platform}:{external_id}` — stable across company_stores re-scrapes, which
    regenerate ids). Menus churn daily so any non-empty result is the new truth; an EMPTY
    result keeps the prior snapshot — and SAYS SO, in `retained_since` (2026-09-13; on the
    `products_normalized` view too since 2026-10-06, with `current_snapshot_where()` as the one
    spelling of the predicate) — **for at
    most `_MENU_RETAIN_CEILING_DAYS` (30 d, 2026-10-06)**, after which the next empty result drops
    the snapshot; the history in `product_observations` is untouched. The ceiling exists because
    the stamp alone changed nothing downstream: 104 stores still visited daily carried a
    June–September menu as current, 12.7% of `store_products` was unreachable or retained, and
    `products_normalized` filtered none of it. The guard
    stamps that column the first time it keeps a snapshot and leaves it alone afterwards, so the
    value is the moment the store went dark rather than the moment of the latest failed attempt;
    a successful scrape replaces the rows and the stamp goes with them. Before it, a retained
    snapshot was byte-indistinguishable from a menu scraped that morning, and every reader with no
    age predicate treated a June menu as today's. ⚠ The stamp is written from the guard onward, so
    rows that predate the column were stamped by a one-off backfill using the snapshot's own
    `max(scraped_at)`, which can overstate retention by up to one sweep cycle and is byte-identical
    to a live stamp. A derived stamp is therefore an UPPER BOUND on how long a snapshot has been
    kept, and the distinction stops being recoverable once the next successful sweep replaces those
    rows. Each row carries both the platform-shaped raw fields
    (`category`/`terpenes`/`variants`) and the normalized standard fields stamped at write time
    (`category_std`, `product_type_std`, `strain_type_std`, and via `normalize.enrich_record`
    `size_g`, `terpenes_std`, `terp_total`, `terpenes_repaired`, `potency_implausible`, plus
    per-variant `size_g`/`price_per_g` inside the
    `variants` JSONB — the last two are QUALIFIERS on a stored value, never edits to it: they mark a
    terpene profile our normalizer altered, and a published THC that is not a plausible label for its
    category); the `products_normalized`
    VIEW projects just the standard fields (+ a derived top-level `price_per_g`). An idempotent
    normalization backfill recomputes these over existing rows.
  - `access_methods` per attempt (target_type `store_menu`, target_key `{state}:{store_key}`),
    with the menu-shaped `plausible` predicate.
  - `jobs`: one `store_menu` job per store handle, then claims them (§5). **Freshness
    gating:** `run_store_menus(conn, state, max_age_hours=N)` (CLI `--max-age-hours`) skips
    enqueueing a store whose latest `store_products.scraped_at` is younger than the window —
    so a daily cron only refreshes stale stores; the default (None) re-scrapes all. Stores
    stay in the claim map regardless, so a leftover prior job still resolves.
  - **Scoped re-scrape:** `--only "<term>[,<term>]"` (`run_store_menus(only=…)`) narrows to stores
    whose operator/storefront name, external_id, or company id matches; like Stage 2 it
    `claim_target`s only those targets, leaving a concurrent full run untouched.
  - **Platform-filtered runs claim only what they serve:** `--skip-aggregators` /
    `--only-aggregators` (`skip_platforms`/`only_platforms`) also switch to `claim_target` — over
    the run's whole claim map, so freshness-skipped stores' leftover jobs still resolve. A
    state-prefix drain would claim the excluded platforms' pending jobs (a concurrent
    aggregator-only worker's, or a crashed run's leftovers) and hard-fail each as "no store row
    for target". The claim KEY is unchanged; only the claim scope narrows.
  - **A leftover job whose store is gone is retired at startup.** A claim by target reaches only
    the claim map, and a handle that was folded or dropped after its job was enqueued is in no
    run's map — with both scheduled sweeps platform-filtered, nothing ever claimed it (ten such
    jobs from the crashed 2026-09-01 run stood 33 days and outlived the next monthly run). Every
    run therefore calls `queue.retire_orphans` over the state's prefix before it enqueues, against
    EVERY handle the state holds rather than the ones the run serves, so another worker's pending
    job for a held store is untouched; an orphan ends `failed`, "no store row for target".
- **Rungs live:** the list is `routing.serving_rungs` / `menus._store_catalog`, restated ONLY in `CLAUDE.md` where
  `tests/test_rung_documentation.py` keeps it complete — this bullet named seven while the catalog
  held twenty-two, so it now keeps only the per-rung notes worth having: `jane_algolia` (public Algolia index), `dutchie_products` (consumer
  persisted product query, med-then-rec; can re-resolve a rotated store id from the stable
  `cName` slug on an empty menu — but that self-heal is **opt-in, OFF by default**
  (`RUNG_DUTCHIE_CNAME_RESOLVE=1`) because it hits the Cloudflare-walled directory endpoint and
  tripped a box-wide IP ban 2026-07-29),
  `trulieve_rest` (operator REST wrapper,
  routed by trulieve.com store_url; menu id discovered from the store page), `cresco_api`
  (Sunnyside + white-labels like Verilife; captured ids validated via /p/stores and
  re-resolved by address from the state directory when wrong-namespace), `sweedpos_ssr`
  (SSR menu pages — Curaleaf/Apothecarium — with a flight `?page=N` fallback for custom
  Next.js front-ends like Zen Leaf), `hytiva_api` (api.hytiva.com/v1/menu/{businessId} —
  whole menu in one no-auth GET; Restore), `dutchie_plus_menu` (no Plus-stamped stores
  currently). Cresco handles come from the browser tier (a captured `location.id`, as
  `custom:<n>`) and, since 2026-10-05, from the Stage-2 `cresco_stores` rung, which reads the
  company's own held handles from `company_stores` so as to carry them over rather than re-key
  them, and mints `cresco:<store id>` only for a store nobody holds. Sweed handles come from the Stage-2 `curaleaf_api` + `sweed_stores` rungs (the
  latter parses an operator's flight store directory or crawls its own shop.* store bases);
  Hytiva handles come from the Stage-2 menu-embed (`jane_api`) rung, which harvests
  `<hytiva-menu>` businessIds and dedupes dual-platform stores to the dominant platform; the
  same rung also harvests a Dutchie embedded-menu id off an operator's menu subpages and
  resolves it against the `dutchie_directory` sweep, so an off-name Dutchie operator gets a
  `dutchie` handle instead of an addressed-only `custom` row.
- **Re-run:** idempotent per store; cached winner replays.

### dedupe-stores — `sources/dedupe.py`
- **In:** `company_stores` rows for the state; `companies.yml` aliases; `store_products`'s set of
  handles holding a current (not retained) snapshot (`db.handles_with_snapshots`).
- **Out:** `company_stores.canonical_company_id` + `storefront_name` (clear-then-mark: reset to
  NULL for the state, re-cluster by normalized address / coordinate cell / platform handle, mark
  duplicates; a kept row that lacks coordinates inherits a folded sibling's). **The kept row of a
  rooftop** is the handle-bearing one on the richest menu platform, and within a platform the one
  that has actually yielded a menu — the tie used to fall to the older row, which on 2026-10-06
  had kept a Leafly MED listing with nothing over the REC listing holding 1,475 products (eight
  rooftops' only menu sat on the folded side). A kept handle scraped daily stays kept, since its
  snapshot is the fresh one. Then a second commit
  realigns `store_products.company_id` for the state onto each handle's kept (canonical) row
  (`db.realign_store_products_company`) so a snapshot scraped under a since-folded alias is
  re-attributed to the operator a fresh `scrape-menus` would file it under. A crash between the two
  commits leaves a consistent dedupe with stale snapshot ids — the next run re-realigns. Claims the
  per-state `dedupe` job (§5).
- **Re-run:** fully idempotent when serial.
- **What a fold costs, and the open design question.** Stage 3 scrapes one handle per rooftop,
  so the folded handle's last snapshot is never refreshed again and sits in `store_products` as a
  current menu: 519 handles / 392,500 rows on 2026-10-06, most from June — one rooftop's second
  aggregator listing, a previous operator's menu after a change of hands, a delivery variant, and
  **143 named MED/REC pairs on one platform, which are two real price lists of one store**.
  The private menu-prune maintenance script takes them with its `--folded` flag (dry run, backup
  first; run after `dedupe-stores`, so the folded side is the side without the menu); the history
  keeps what was seen. **Decided and built the same day: a med+rec store keeps BOTH listings.**
  The two are two catalogues, not one menu with two prices — on 23 pairs snapshotted two days apart
  they shared 24–63% of product names, and shared names agreed on price 62–97% of the time — and
  which one dedupe had kept was arbitrary (the older row: MED folded 81 times, REC 45) with nothing
  recording it. Now `company_stores.menu_type` (medical | adult_use | NULL, `text.menu_type_of` on
  the listing's name — read only where a name declares: a bracket, after a separator, or a
  separator-less name's final word, so "Nature Med - Paducah" declares nothing — and Jane's menu
  path, stamped at persist) partitions a rooftop in dedupe
  (`_menu_partitions`): when both types are declared each keeps a row and an undeclared twin folds
  into the adult-use side; a rooftop with one type or none folds as before. Stage 3 scrapes every
  kept row, so both menus refresh; the store row's type rides each product record's ident into
  `store_products.menu_type` (on `products_normalized` too), and the Dutchie and Dutchie Plus
  mappers price on the menu's own channel (`normalize.price_channel`): the headline used to be
  min(medical, adult-use), the medical price for 54% of Oregon's rows. An undeclared menu in a
  medical-only jurisdiction (`state_programs.programs = 'medical'`) prices medical — the ident
  carries `medical_only` — while its stored `menu_type` stays NULL. `compare-stores` counts a
  two-menu rooftop once. `DedupeReport.second_menus` counts the rooftops that
  kept two; `distinct_stores` is kept rows, so rooftops = `distinct_stores - second_menus`.
  A private backfill script stamps the rows that predate the column and re-prices Dutchie.

### compare-stores — `rung_intel/compare.py`
- **In:** `dispensaries`, deduped `company_stores` (`canonical_company_id IS NULL`), `companies`,
  alias + grower-brand YAMLs.
- **Out:** stdout report only. Read-only; always safe.

### store-lifecycle — `rung_intel/store_lifecycle.py`
- **In:** `store_locations` + `store_observations` (the append-only history), `store_capture_attempts`
  (what was attempted per cycle — an absence counts only under a `succeeded` capture), `companies` +
  `company_stores` (via `compare.build_canon`, for the operator fold), `companies.yml`.
- **Out:** stdout report; with `--write`, `store_lifecycle_events` (replace-by-state — a derivation
  may legitimately shrink, so no keep-the-best guard). Needs `--record-history` sweeps to have run —
  with fewer than `--closed-after-cycles` usable cycles it reports `first seen` and nothing else.
- **Roster lag** is reported only when a roster cycle AFTER the store's last sighting on its own site
  still lists it — a roster listing older than that sighting says nothing about the closure (2026-10-06).
  Observation and attempt cycles are both keyed by their UTC date.
- **Re-run:** idempotent. Same log + same `--closed-after-cycles` → the same rows.

## 3. Commit discipline

- `db.py` helpers NEVER commit; the caller owns the transaction.
- Orchestrators commit: per state (search/lists/extract), per attempt (`access.run_target` —
  do not batch these), once at end (dedupe), at command end (recon, company-stores CLI).
- **Postgres note:** an error inside a transaction poisons the connection until `rollback()`.
  Any handler that swallows an exception and continues the loop on the same connection must
  roll back first.
- Long-running scrapes hold an open (idle) transaction between commits; harmless at this scale.

## 4. Write-isolation rules (never violate)

1. `state_programs.list_*` columns are written ONLY by `db.set_state_list`;
   `db.upsert_state_program` deliberately omits them (so search/verify re-runs never clobber
   discovered list URLs).
2. `company_stores.canonical_company_id` and `storefront_name` are written ONLY by dedupe-stores.
3. The `companies` table is created and written ONLY by `seed_companies.py`.
4. `access_methods` is written ONLY through `db.record_access_attempt` (the CASE/COALESCE upsert
   encodes the preserve-locator-on-ok / clear-on-fail rules).

## 5. Concurrency: hazards and claims

`access_methods` is **durable per-target memory** ("how do we access this target") — it is NOT a
queue. The `jobs` table (`rung/queue.py`) is its transient companion ("what is being
worked this run"): status pending|claimed|done|failed, claims via
`FOR UPDATE SKIP LOCKED`, a partial unique index dedupes live jobs per `(task_type, target_key)`,
and `requeue_stale` recovers claims from crashed workers at consuming-command startup.
`requeue_stale` re-`pending`s a claim only when it is both old AND its lease has lapsed, so a
heartbeating long job is left alone (until the 2026-10-06 whole-tree review it was wall-clock only,
and re-`pending`ed a >60-min job another worker then reclaimed). A row it or `reap_expired` fails at
the attempt cap is stamped `finished_at`, so `prune_completed` can reach it; a TARGETED claim takes
a just-requeued job at once (the requeue jitter staggers only untargeted claims), and
`make_claimer` claims targeted keys in the order given, which is how Stage 3's stalest-first order
reaches the queue. A dedupe claim that raises is completed `failed` rather than left claimed. `queue.complete` guards
that: it is scoped to the holding worker (`claimed_by = worker AND status = 'claimed'`) and returns
whether it still held the claim, so the orphaned slow worker's completion is a no-op and it rolls
back its redundant write rather than clobbering the reclaimer's. The two partitioned data-write
consumers (`scrape-company-stores`, `scrape-menus`) check the return and roll back on `False`; the
`dedupe-stores` consumer (one exclusive claim per state, `run_dedupe` self-commits before
`complete`) is race-safe by exclusivity + idempotence and does not — and cannot — roll back, so it
ignores the return.

A claim now ends **three** ways, not two. Besides `done` and `failed`, `queue.release` returns a
target to `pending` **unworked** — the load-shedding path taken when the cross-worker rate gate
(`rung.rate_gate`, `docs/distributed_scraping_design.md` §4) has no budget for that target's host.
It is worker-scoped exactly like `complete`, and it *decrements* the `attempts` that `_CLAIM`
stamped, because nothing was attempted: without that, sustained throttling would walk every
deferred target to `max_attempts` and permanently fail work that was never once tried. The
decrement removes the queue's own loop guard, so `release` also pushes `scheduled_at` out — a shed
target is not claimable again this drain, which is what makes a saturated host terminate the run
instead of spinning claim→shed→claim. The Stage-2/3 consumers pair it with a per-run rule that
stops *waiting* on a bucket already found exhausted, so the sheds cost one gate deadline rather
than one per remaining target.

Two hazards existed before claims; both are contract violations now closed:

| Hazard | Failure mode | Claim key |
|---|---|---|
| Two concurrent `scrape-company-stores` runs on the same state | Both pass the keep-the-best gate, both delete+insert the same company's rows → data loss | one job per company: `task_type='company_stores'`, `target_key='{company_id}:{state}'` — concurrent runs partition the companies |
| Two concurrent `dedupe-stores` runs on the same state | Second run reads rows before the first's single commit → stale clear-then-mark | one job per state: `task_type='dedupe'`, `target_key='{state}'` — the loser reports the live claim and exits |
| Two concurrent `scrape-menus` runs on the same state | Both replace the same store's snapshot (wasted double fetch; interleaved delete+insert) | one job per store handle: `task_type='store_menu'`, `target_key='{state}:{platform}:{external_id}'` — concurrent runs partition the stores |

Other stages have no identified hazard (per-state upserts are last-writer-wins by design;
compare is read-only) and stay unclaimed until a real consumer needs them.

Queue hygiene at higher volume (from `postgres_for_everything.md` #2): done/failed rows are kept
as run history. `scrape-menus` is the Stage-3 queue consumer (it enqueues a per-store
`store_menu` job across every state daily), so that churn would bloat the table with dead 'done'
tuples and degrade the claim scans. **Handled:** `queue.prune_completed` (CLI `prune-jobs
--older-than-hours N`, default 168 = 7 days) deletes finished (done/failed) jobs past a window,
leaving live (pending/claimed) jobs untouched — run it on a cron after the daily scrape. A
partition (pg_partman) is the next step only if a single window's volume itself grows large.
(This was once gated on a separate Scrapy menu-fetch stage; that stage was superseded, so the
hygiene work now stands on its own.)
