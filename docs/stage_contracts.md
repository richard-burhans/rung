# Stage Contracts

**Status:** contracts formalized 2026-06-10 as part of the decoupling effort. The claim keys in §5 are implemented by the `jobs` work
queue (see `rung/queue.py`).

Each pipeline stage is an independent CLI command (`rung/cli.py`). **The database is
the only interface between stages** — no in-memory state crosses a command boundary. This document
is the contract: which tables each stage reads and writes, with what semantics. A stage may be
rewritten, re-run, or re-scheduled freely as long as its contract holds.

## 1. Read/write matrix

`R` = reads, `W` = writes, `W-cols` = writes only specific columns (see §4).

| Stage | state_programs | dispensaries | companies | company_recon | company_stores | store_products | access_methods | jobs |
|---|---|---|---|---|---|---|---|---|
| search-states | W | | | | | | | |
| find-lists | R, W-cols (`list_*`) | | | | | | | |
| scrape-states | R | W (replace-by-state) | | | | | | |
| seed-companies | | R | **W (owner)** | | | | | |
| recon | | R | R | W | | | | |
| scrape-company-stores | R | | R | R | W (keep-the-best) | | W | W |
| dedupe-stores | | | R | | W-cols (`canonical_company_id`, `storefront_name`, coords) | W-col (`company_id` realign) | | W |
| compare-stores | | R | R | | R | | | |
| scrape-menus | | | | | R | W (replace-by-store) | W | W |

YAML inputs (curated, read-only; under `rung/data/`): `states.yml` (search-states,
find-lists, scrape-states), `companies.yml` (seed-companies, compare-stores),
`company_homepages.yml` (recon), `dutchie_chains.yml` / `dutchie_plus_tokens.yml` /
`state_geo_anchors.yml` / `weedmaps_slugs.yml` / `leafly_slugs.yml` / `jane_store_ids.yml`
(scrape-company-stores), `grower_brands.yml` (compare-stores).

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
  `db.set_state_list` (db.py). Status: `list_status` found|override|none.
- **Re-run:** skips states that already have a list unless `--force`; commits per state.

### scrape-states — `sources/extract.py`
- **In:** `state_programs.list_url/list_type` (states with a found/override list).
- **Out:** `dispensaries`, **replace-by-state**: `DELETE WHERE state = ?` then inserts — and the
  delete runs ONLY when extraction yielded ≥1 record, so a transient zero-yield never wipes prior
  good rows. Append via `db.insert_dispensary` (db.py); commit per state.
  - `store_locations` + `store_observations` via `extract.record_roster_observations` **only under
    `--record-history`** — the `state_roster` leg of the store-lifecycle history (same shared engine,
    `db.record_location_observations`, as Stage 2's `company_site` leg), appended inside the same
    non-empty-replace commit. A failed extraction records nothing, so observed absence stays a real
    signal. See `docs/store_history_design.md`.
- **Re-run:** idempotent per state.

### seed-companies — `seed_companies.py`
- **In:** `dispensaries.name/state`; `companies.yml` aliases.
- **Out:** `companies` — **this module owns the table** (creates it; `db.create_tables` does not).
  Insert-if-absent on `(canonical_name, state)`; never deletes.
- **Re-run:** additive only.

### recon — `rung_intel/recon.py`
- **In:** `companies.id/canonical_name` (per state); `dispensaries.name/website` (homepage
  derivation); `company_homepages.yml` overrides.
- **Out:** `company_recon` full-row upsert per company (`db.upsert_recon`, db.py). Failure is a
  row with `error` set; success has `error IS NULL` + `platform`/`confidence`.
- **Re-run:** re-probes and overwrites; safe.

### scrape-company-stores — `rung_intel/company_stores.py`
- **In:** `db.get_recon_companies_for_state` (db.py): recon rows with `error IS NULL AND
  homepage_url IS NOT NULL`; `access_methods` per-target winner + hints; platform YAMLs.
- **Out:**
  - `company_stores` via `db.replace_company_stores` (db.py) — **keep-the-best replace**:
    per-company delete+insert happens only if the new result covers **at least as many DISTINCT
    physical stores** (by address; raw row counts let a double-counting extractor entrench
    itself), OR new adds `external_id` handles where stored had none AND retains ≥0.8 of the
    distinct count. Zero-yield re-runs keep prior data.
  - `access_methods` via `db.record_access_attempt` (db.py) — one upsert **per method attempt,
    committed immediately** (access.py). Load-bearing for crash recovery: a killed run
    leaves a frozen ladder snapshot; the next run resumes from the stored winner.
  - `jobs`: enqueues one `company_stores` job per company, then claims them (see §5).
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
- **Re-run:** cached winner skips re-discovery; failure triggers a ladder re-walk (see
  `access_methods_design.md`).

### scrape-menus — `rung_intel/menus.py` (Stage 3)
- **In:** `db.get_menu_stores_for_state` — canonical `company_stores` rows carrying a Stage-2
  scrape handle (`platform` + `external_id`), DISTINCT ON the handle; the discovery `source`
  column routes the menu rung (it says which platform minted the external_id); the company's
  Stage-2 `access_methods` params supply the Dutchie Plus token.
- **Out:**
  - `store_products` via `db.replace_store_products` — **wholesale snapshot replace per
    store_key** (`{platform}:{external_id}` — stable across company_stores re-scrapes, which
    regenerate ids). Menus churn daily so any non-empty result is the new truth; an EMPTY
    result keeps the prior snapshot. Each row carries both the platform-shaped raw fields
    (`category`/`terpenes`/`variants`) and the normalized standard fields stamped at write time
    (`category_std`, `product_type_std`, `strain_type_std`, and via `normalize.enrich_record`
    `size_g`, `terpenes_std`, `terp_total`, plus per-variant `size_g`/`price_per_g` inside the
    `variants` JSONB); the `products_normalized`
    VIEW projects just the standard fields (+ a derived top-level `price_per_g`).
    `scripts/backfill_normalization.py` recomputes these over existing rows.
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
- **Rungs live (2026-06-15, all verified live; PA = 175/175 handled stores, ~125k product
  rows):** `jane_algolia` (public Algolia index), `dutchie_products` (consumer
  persisted product query, med-then-rec), `trulieve_rest` (operator REST wrapper,
  routed by trulieve.com store_url; menu id discovered from the store page), `cresco_api`
  (Sunnyside + white-labels like Verilife; captured ids validated via /p/stores and
  re-resolved by address from the state directory when wrong-namespace), `sweedpos_ssr`
  (SSR menu pages — Curaleaf/Apothecarium — with a flight `?page=N` fallback for custom
  Next.js front-ends like Zen Leaf), `hytiva_api` (api.hytiva.com/v1/menu/{businessId} —
  whole menu in one no-auth GET; Restore), `dutchie_plus_menu` (no Plus-stamped stores
  currently). Sweed handles come from the Stage-2 `curaleaf_api` + `sweed_stores` rungs (the
  latter parses an operator's flight store directory or crawls its own shop.* store bases);
  Hytiva handles come from the Stage-2 menu-embed (`jane_api`) rung, which harvests
  `<hytiva-menu>` businessIds and dedupes dual-platform stores to the dominant platform; the
  same rung also harvests a Dutchie embedded-menu id off an operator's menu subpages and
  resolves it against the `dutchie_directory` sweep, so an off-name Dutchie operator gets a
  `dutchie` handle instead of an addressed-only `custom` row.
- **Re-run:** idempotent per store; cached winner replays.

### dedupe-stores — `sources/dedupe.py`
- **In:** `company_stores` rows for the state; `companies.yml` aliases.
- **Out:** `company_stores.canonical_company_id` + `storefront_name` (clear-then-mark: reset to
  NULL for the state, re-cluster by normalized address / coordinate cell / platform handle, mark
  duplicates; a kept row that lacks coordinates inherits a folded sibling's), then a second commit
  realigns `store_products.company_id` for the state onto each handle's kept (canonical) row
  (`db.realign_store_products_company`) so a snapshot scraped under a since-folded alias is
  re-attributed to the operator a fresh `scrape-menus` would file it under. A crash between the two
  commits leaves a consistent dedupe with stale snapshot ids — the next run re-realigns. Claims the
  per-state `dedupe` job (§5).
- **Re-run:** fully idempotent when serial.

### compare-stores — `sources/compare.py`
- **In:** `dispensaries`, deduped `company_stores` (`canonical_company_id IS NULL`), `companies`,
  alias + grower-brand YAMLs.
- **Out:** stdout report only. Read-only; always safe.

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
`requeue_stale` is wall-clock only, so it can't tell a *crashed* worker from a *slow-but-alive*
one — it would re-`pending` a >60-min job another worker then reclaims. `queue.complete` guards
that: it is scoped to the holding worker (`claimed_by = worker AND status = 'claimed'`) and returns
whether it still held the claim, so the orphaned slow worker's completion is a no-op and it rolls
back its redundant write rather than clobbering the reclaimer's. The two partitioned data-write
consumers (`scrape-company-stores`, `scrape-menus`) check the return and roll back on `False`; the
`dedupe-stores` consumer (one exclusive claim per state, `run_dedupe` self-commits before
`complete`) is race-safe by exclusivity + idempotence and does not — and cannot — roll back, so it
ignores the return.

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
