# rung — working guide

Cannabis dispensary intelligence pipeline: it builds a multi-state dataset of licensed
dispensaries, each operator's own published store list, and each store's live menu
(products, prices, potency, terpenes), then compares the company lists against the state
rosters to show where the state list lags. The company's own site is the source of truth.

> Sandbox/environment setup (persistent env, network policy, git auth) lives in the parent
> `../../../CLAUDE.md`. This file is the code-level guide; **start with `ARCHITECTURE.md`** for the
> module map and dependency edges.

## Read first

- `ARCHITECTURE.md` — module map, dependency edges, tier tables (the authoritative structure).
- `README.md` — pipeline flow + current coverage.
- — the cost-ranked access-method registry (the system's brain).
- `docs/stage_contracts.md` — per-stage read/write matrix + queue claim keys.
- — per-platform scraping recipes.
- — the consolidated literature bibliography (themed tables +
  adversarially-fact-checked per-paper summaries + a Missing-to-download list). Single source of
  truth for cited literature; the cited PDFs/full-text and the generator tooling live off-repo
  under `../../data/papers/` and `../../research/` (see the parent `../../../CLAUDE.md` for the deposit workflow).

## The pipeline (CLI = `[project.scripts]`)

- **Stage 1 — state → dispensary lists:** `search-states` → `find-lists` → `scrape-states`
  (generic HTML/PDF/CSV/ArcGIS/Socrata extractors; `states.yml` overrides; opt-in `--ai`
  Ollama fallback). `seed-companies` derives `companies`; `recon` detects platform + homepage.
- **Stage 2 — company stores:** `scrape-company-stores` routes each company through the
  registry (`access.py`) → `company_stores`; then `dedupe-stores` → `compare-stores`.
- **Stage 3 — menus:** `scrape-menus` snapshots each handled store's menu into `store_products`,
  routed to a per-platform rung (jane_algolia / dutchie_products / trulieve_rest / cresco_api /
  sweedpos_ssr / hytiva_api / weedmaps_menu / leafly_menu / dutchie_plus_menu). `--max-age-hours N`
  freshness-gates re-scrapes.
- Both Stage-2/3 commands take `--only "<term>[,<term>]"` to scope a focused re-scrape to one
  operator/store (matches company/store name or id); it claims just those targets, so it never
  disturbs a concurrent full run.

## Core concepts

- **Two packages (public core + private overlay).** Public **`rung`** (Stage-1
  extraction, the access engine, queue, persistence, CLI, plugin seam) ships and runs on its own;
  private **`dispensary_scraper_intel`** (Stage-2/3 catalogs + per-platform recipes, the comparison
  intel, recon, bootstrap, curated platform datasets) plugs in via the `rung.plugins`
  entry point. The core imports nothing from the overlay; absent it, proprietary stages resolve to
  registry stubs. Boundary test-enforced (`tests/test_import_layering.py`). See
  `docs/publish_split_design.md`. **The proprietary modules live in `dispensary_scraper_intel/`
  (flat), not `rung/sources/`** — the latter now holds only the generic public sources.
- **Access-method engine (`access.py`, public):** every target (a company's stores, a store's menu)
  is reachable several ways at different cost; `run_target` runs the cheapest that works,
  persists the winner per target in `access_methods`, and re-walks only on failure / a cheaper
  untried rung / a governed staleness re-explore. The *engine* is public/generic; the per-platform
  *catalogs* it walks (and the pure platform helpers + extractors) are in the overlay.
- **Work queue (`queue.py`, `jobs` table):** stages enqueue then claim their own work
  (`FOR UPDATE SKIP LOCKED`), so concurrent runs of one command partition the targets.
- **keep-the-best replace** (`db.replace_company_stores`): a re-scrape only overwrites when it
  yields at least as many DISTINCT physical stores (handle-bearing results get a small grace),
  so a transient low-yield run can't clobber good data.

## Dev workflow

- **Python ≥ 3.13**, deps via **`uv`**: `uv run <cmd>`, `uv run pytest`, `uv tool run ruff`.
- **Postgres** (psycopg3 raw SQL, no ORM). `DATABASE_URL` env; default = a local Postgres dev container. `dispensaries.db` is a gitignored, regenerable SQLite fallback.
- **QA gate before every commit:** `ruff` → `ty` → `pytest` (+ coverage floor). DB tests use throwaway schemas in `dispensaries_test`
  (`tests/conftest.py`). **Run `/pre-pr-audit` before opening any PR.**
- **Enforcement:** **CI** (`.github/workflows/ci.yml`) re-runs the gate on every PR + push to
  `main` against a Postgres service container — the authoritative check.

## Conventions

- **Coding standards:** `dignified-python` governs (see the skill — don't restate its rules
  here). Project deltas: EAFP is expected at the scraper's external-data boundaries (parsing
  untrusted JSON/HTML, best-effort I/O) where the operation itself is the authoritative test;
  CLI commands use `print()` for user-facing output, not logging; all HTTP goes through
  `http.make_session()` (the `test_http.py` AST guard enforces it).
- **Never import from `../.local/`** (reference-only clones); re-implement or copy what's useful.
  Don't follow patterns in any `orig/` reference files.
- **Naming:** the operator (canonical) name is the scrape/dedup key; the storefront alias is the
  display/reporting label.
- **Potency convention:** store the platform's DISPLAYED total (THCA-adjusted headline), not the
  Δ9-only lab assay; `thc`/`cbd` columns are percent-only. Mg-dosed products (edibles, tinctures,
  beverages) carry their per-dose milligrams in the separate `thc_mg`/`cbd_mg` columns instead —
  a product has a percent OR an mg value, routed by the platform's potency unit. A "percent" over
  100 is a mislabeled mg dose (Dutchie does this) and is rerouted to mg.
- **Normalized fields:** rows keep their raw platform shape *and* standardized fields stamped at
  the `menu_extractors._record` choke point — `category_std` (`text.normalize_category` — raw-category
  keywords + `data/category_name_overrides.yml` name overrides for platform-mislabeled forms),
  `product_type_std` (`text.normalize_product_type` → the 2nd-level type within a category from the
  product name, e.g. Vape→Cartridge / Concentrate→Live Resin / Edible→Gummies / Flower→Bud, all
  categories; a per-category `_defaults` label (Flower→Bud, Pre-Roll→Single) or `Unspecified` when
  the name names no form, `None` for an uncategorized row — see `docs/product_type_hierarchy.md`),
  `strain_type_std` (`text.normalize_strain_type` → `Indica/Sativa/Hybrid/CBD` or `None`;
  lineage-only keywords, raw column is polluted with categories/strain-names so no-match→`None`),
  and (via `normalize.enrich_record`) `size_g` (variant label → grams, for weight-sold categories
  only so a dosed `mg` label isn't mistaken for a sellable weight), per-variant `size_g`/`price_per_g`
  in the `variants` JSONB, and `terpenes_std`/`terp_total` (canonical `{Name: %}`, an impossible
  >40% total repaired). Mappers that expose per-cannabinoid values also stamp `cannabinoids_std`
  (`{NAME: %}` minor cannabinoids beyond the thc/cbd headline — CBN/CBG/CBC — from Jane lab_results,
  Hytiva/Cresco potency blocks, Weedmaps aggregates; Dutchie's consumer payload omits them). The
  `products_normalized` VIEW is the combined cross-platform surface; the `backfill_*` scripts
  (`backfill_normalization.py`, `backfill_category_std.py`, `backfill_strain_std.py`,
  `backfill_product_type.py`) apply each to existing rows.
- Keep `ARCHITECTURE.md` / `README.md` / `docs/` in sync with code changes (the pre-PR audit
  enforces this). Add new design decisions to the relevant `docs/` file.
