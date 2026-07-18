# Publishable split — public core + private `intel` overlay

**Status:** Phases 1–4a + 5 done on branch `refactor/publish-split-seam`. The two-package split is
functionally complete and test-enforced: seam + CLI routing (1–2), honest-by-default HTTP (3a), the
private overlay carved into `rung_intel` (3b), the dataset partitioned with a leak
guard (4a), and the ARCHITECTURE/README/CLAUDE docs swept to the two-package structure (5).
**Publishing — tooled (done).** `scripts/build_public_repo.py` assembles the public framework repo
(the `rung` package + `examples/` + the framework tests + core docs, with a public-only
`pyproject` — no uv workspace / overlay dep) and **leak-guards the output**: no file imports the
overlay, no overlay directory, no proprietary data, no overlay reference in the pyproject, and a
generic secret-content/secret-filename scan over the assembled tree. It auto-excludes anything
importing the overlay (the private tests) or `scripts/`, and treats the analysis scripts / research
reports / recipe docs (`scraping_techniques`, `access_methods_design`) / the bring-up + data-source
docs (`multi_state_expansion`, `data_sources`) / the dataset as private intel (tunable via the
script's exclude-lists). `--check` is test-gated (`tests/test_build_public_repo.py`). Tokens are untracked + gitignored; both packages
bundle their `data/*.yml` in a wheel (hatchling default — no extra config). **To publish:**
`python3 scripts/build_public_repo.py --out ../dispensary-scraper-public`, review the output, then
`git init` + push to a public repo. **Goal:** make it cheap and
safe to keep the proprietary *intel* + *access catalogs* + *dataset* private while publishing the
rest of the framework as open source, with the public half **runnable on its own** against
stub/example plugins.

This is the design + phased TDD plan. It records the decisions taken on 2026-06-29 and the
boundary it enforces.

## Decisions (confirmed)

| Question | Answer |
|---|---|
| What is private? | **All four**: the access-method *catalogs*, the per-platform scraping know-how, the comparison/analysis *intel*, **and** the curated dataset (`data/*.yml`, DB snapshots). |
| Separation mechanism | **Plugin seam (registry injection)** — public core defines the seam; a separate private package plugs in via an entry-point group. |
| Must the public half run alone? | **Yes** — runnable end-to-end against stub/example plugins + sample data. |

## The one reconciliation: the access *engine* is public, the *catalogs* are private

`access.py` was nominated as "private," but it is **pure generic mechanism** — `run_target` already
takes the `catalog` as a *parameter* (`access.run_target(conn, target_type, target_key, catalog,
…)`). The ladder-walker, the `ReExploreGovernor`, the plausibility gate, and the `access_methods`
persistence carry **zero** proprietary knowledge. The secret sauce is the **catalogs** built by
`rung_intel/company_stores._company_catalog` and `rung_intel/menus._store_catalog`, which wire the
private per-platform runners.

Therefore the publishable boundary is the **catalog/stage boundary**, not `access.py`:

- **Public:** the `access` engine (the empty socket), and the registry that stages plug into.
- **Private:** the catalog *providers* and the platform runners they wire (the plug).

This is exactly what "plugin seam" means, and it is the natural reading of "keep the access layer
private": the public ships the socket, the private ships the plug. (If you'd rather the engine ship
privately too, that breaks runnable-with-stubs — flag it and we'll revisit.)

## Module partition

Everything under `rung/` classified. The crucial empirical fact: **the only
public→private import edges that exist today are the lazy imports inside `cli.py`.** Every other
edge already points the right way (private→public is fine; public→public is fine). So the code
move is localized.

### Public core (the open-source framework)

| Module | Why public |
|---|---|
| `models.py` | Generic persisted-record dataclasses. |
| `http.py` | Generic curl_cffi session / Chrome impersonation / rate-limiter. |
| `browser.py` | Generic pydoll/Chrome primitives. |
| `text.py` | Generic normalizers + identity hash (the *keyword data* it reads is private). |
| `normalize.py` | Generic numeric/size/terpene normalizers. |
| `addresses.py` | Generic address-extraction primitives. |
| `proxy.py` | Generic health-aware proxy pool. |
| `db.py` | Postgres schema + CRUD (generic persistence). |
| `static_source.py` | DuckDB-over-Parquet adapter `db.get_connection` delegates to under `RUNG_DATA_SOURCE=static` — runs the analysis off a frozen clean dataset (the Galaxy / outside-researcher reproducibility path); takes a file path, never a credential. |
| `queue.py` | Generic Postgres work queue (`FOR UPDATE SKIP LOCKED`). |
| `access.py` | The generic access-method **engine** (the plugin socket). |
| `registry.py` | **NEW** — the plugin seam (stage/catalog registration + entry-point discovery). |
| `cli.py` | The pipeline shell (resolves proprietary stages through `registry`). |
| `seed_companies.py` | Generic company derivation. |
| `sources/state_search.py` | Generic per-state program coverage. |
| `sources/state_lists.py` | Generic list-resource discovery. |
| `sources/extract.py` | Generic HTML/PDF/CSV/ArcGIS/Socrata extractors. |
| `sources/ai_fallback.py` | Generic scrapegraphai+Ollama wrapper. |
| `sources/homepage_discovery.py` | Generic web-search homepage finder. |
| `sources/dedupe.py` | Generic address/geo dedup algorithm. |

### Private overlay (`rung_intel`)

| Module | Why private |
|---|---|
| `rung_intel/company_stores.py` | Stage-2 catalogs + per-platform runners (scraping know-how). |
| `rung_intel/company_store_fetch.py` | Discovery/fetch/render helpers (know-how). |
| `rung_intel/company_store_extractors.py` | Payload→record mappers (know-how). |
| `rung_intel/menus.py` | Stage-3 menu catalogs (know-how). |
| `rung_intel/menu_extractors.py` | Per-platform menu mappers (know-how). |
| `rung_intel/compare.py` | Roster-gap comparison — **the intel deliverable**. |
| `rung_intel/recon.py` | Operator→platform detection (first step of the know-how). |
| `bootstrap.py` | Dutchie/Weedmaps/Leafly pool bootstraps (know-how). |
| `rung_intel/dutchie.py`, `dutchie_plus.py`, `weedmaps.py`, `leafly.py`, `sweedpos.py`, `trulieve.py`, `cresco.py`, `curaleaf.py`, `fluent.py`, `hytiva.py` | Per-platform scraping recipes. |
| `dev/analyze.py` | Dev-only AI inspector. |

> `recon.py` and `homepage_discovery.py` are the two debatable ones. `recon` is classified private
> (knowing which platform an operator runs *is* the first move of the scraping playbook);
> `homepage_discovery` stays public (a generic web-search finder). Adjust if you disagree.

> **Per-store `platform:external_id` in the published search/maps shards is classified PUBLIC-by-design.**
> The product-search export ships `store_key = platform:external_id` (`export_products.store_index`)
> to the public maps/search repos. An escalated leak audit (2026-06-29; `docs/publish_topology_audit.md`)
> adjudicated this: each platform's `external_id` is *already* public (it sits in the store's own
> consumer menu URL / public store API), and the only incremental bit — the operator→platform mapping
> — is exactly what the **public** `recon`/`homepage_discovery` modules infer from a homepage, so it
> leaks nothing an adversary couldn't derive publicly. It is NOT the private *catalog* (the
> `dutchie_chains.yml` / `*_slugs.yml` / `jane_store_ids.yml` mappings stay in the overlay). Recorded
> here so it isn't re-flagged each audit; if ever reclassified, swap `store_key` for an opaque
> per-store id in the shards (the UI only needs a stable key, not a meaningful one).

### Private non-code assets

- **Private** `rung/data/*.yml` — the curated platform catalogs + tokens that encode the
  scraping playbook: `dutchie_chains.yml`, `dutchie_plus_tokens.yml`, `grower_brands.yml`,
  `jane_store_ids.yml`, `leafly_slugs.yml`, `weedmaps_slugs.yml` (moved to the overlay; see
  `PRIVATE_DATA`). **Public** (intentionally): the operational seeds the public CLI + homepage
  discovery need — `companies.yml`, `company_homepages.yml`, `states.yml`, `state_geo_anchors.yml`,
  the taxonomy/brand alias maps, and `brand_parent.yml` (the brand→parent-MSO crosswalk — public-record
  ownership, powers the analysis's parent-collapse and the clean-dataset `brand_parent` table). These are operator rosters + own-site homepages (public-record,
  not evasion); the public build strips the inline platform/override annotations from
  `company_homepages.yml`.
- `scripts/` — the analysis/conference/backfill scripts (intel); a small infra subset
  (`dev_pg.sh`, `qa_gate.sh`, `migrate_*`, geocode) may stay public — TBD in Phase 4.
- `reports/`, `docs/analysis/`, `docs/scraping_techniques.md`, `docs/access_methods_design.md`,
  the literature bibliography — intel/know-how.
- `dispensaries.db`, any DB snapshot — the dataset.

## The seam (`registry.py`)

A string-keyed registry of **proprietary stage implementations**, discovered at startup via the
`rung.plugins` entry-point group.

- `register(name, impl, *, override=True)` — register/override a stage callable.
- `resolve(name) -> Callable` — return the registered impl, else the stub (which raises
  `StageNotAvailable` with an install hint).
- `load_plugins() -> list[str]` — idempotent; iterate the entry-point group, import+invoke each
  registrar (side-effect registration). Called once at CLI startup.
- `StageNotAvailable` — raised by a stub when an unplugged proprietary stage is invoked, with a
  `pip install rung-intel` hint.

The private package declares in *its* `pyproject.toml`:

```toml
[project.entry-points."rung.plugins"]
intel = "rung_intel.intel_plugin:register_all"
```

`register_all()` calls `registry.register("company_stores.run", run_company_stores)`, etc. — the
dotted seam keys `cli.py` resolves via `_stage(...)` (distinct from the `scrape-company-stores` CLI
verb name; the as-built keys are pinned in `tests/test_intel_plugin.py`).

"Runnable with stubs" = the public framework (queue/db/access engine/generic state extractors)
runs out of the box; proprietary stages cleanly report they need the plugin; an **example plugin**
(shipped under `examples/`) registers a trivial demo access method against a fake source, proving
the mechanism end-to-end.

## Phased TDD plan

Each phase is red→green; nothing merges to `main` until the whole branch is green through the QA
gate and a `/pre-pr-audit`.

- **Phase 1 — seam + boundary contract — DONE (additive, zero behavior change):**
  - `tests/test_registry.py` (written first): register/resolve round-trip; unregistered →
    stub raises `StageNotAvailable`; `load_plugins` discovers+invokes entry points (monkeypatched);
    a plugin overrides a stub.
  - `rung/registry.py` to satisfy it.
  - Extend `tests/test_import_layering.py` with `PUBLIC`/`PRIVATE` partition sets: a
    completeness test (every module classified — green) and a **public-must-not-import-private**
    guard recording today's `cli.py` violations as `strict xfail` (documents the target; flips
    green when Phase 2 lands, and strict-xfail then forces removing the marker).
- **Phase 2 — route the CLI through the registry — DONE (behavior-preserving):** `cli.py`'s
  direct lazy imports of the private stages are replaced with `registry.resolve(...)` via the
  `_stage()` helper; the bridge registrar `intel_plugin.register_all` (discovered through the
  `rung.plugins` entry point) registers the real impls; the public→private guard
  is now a passing assertion. All existing tests still pass; `--help` stays import-light. *(Done:
  the example demo plugin ships under `examples/` so an outside clone can prove the seam without the
  real overlay.)*
- **Phase 3 — extract the private package — DONE:** the private modules moved into the sibling
  `rung_intel/` package (flat) with its own `pyproject.toml` + entry point; public core
  imports it only via the entry-point group; public tests run with the plugin **absent** (stubs)
  and **present** (real). The 3b execution map below records the as-run move.
- **Phase 4 — assets + publish tooling — DONE (4a):** the proprietary `data/*.yml` moved into the
  overlay (shared/public seeds stay in the core) with a leak guard, and `scripts/build_public_repo.py`
  produces the public repo (leak-guarded; `--check` is CI-gated by `tests/test_build_public_repo.py`).
- **Phase 5 — docs — DONE:** `ARCHITECTURE.md` (the `registry` tier + public/overlay band),
  `README.md`, and `CLAUDE.md` swept to the two-package structure.

## Phase 3b execution map (AST-inventoried 2026-06-29 — ready to run)

The 20 private modules and every reference to them, so the carve-out is mechanical, not exploratory.

**Move (20):** `company_stores`, `company_store_fetch`, `company_store_extractors`, `menus`,
`menu_extractors`, `compare`, `recon`, `bootstrap`, `intel_plugin`, `dev/analyze`, and the platform
helpers `dutchie`/`dutchie_plus`/`weedmaps`/`leafly`/`sweedpos`/`trulieve`/`cresco`/`curaleaf`/
`fluent`/`hytiva` → into `rung_intel/` (flat; recommend dropping the `sources/`/`dev/`
nesting).

**References to rewrite (from the AST scan):**
- *Package:* only the moving files cross-import each other (`bootstrap`→{company_store_extractors,
  dutchie,leafly,weedmaps}; `intel_plugin`→{analyze,bootstrap,company_stores,compare,menus,recon};
  `company_store_fetch`→{company_store_extractors,dutchie_plus,hytiva}; `company_stores`→9 helpers;
  `menus`→9 helpers). **No public module references a private one** (boundary confirmed).
- *Tests (16):* test_bootstrap, test_company_stores, test_compare, test_dutchie, test_dutchie_plus,
  test_http_retry, test_hytiva, test_intel_plugin, test_leafly, test_menu_extractors, test_menus,
  test_platform_helpers, test_recon, test_sweedpos, test_trulieve, test_weedmaps.
- *Scripts (3, themselves private-side → move in Phase 4):* `escalation_gate.py`→weedmaps,
  `pool_gap_audit.py`→{company_store_extractors,dutchie,leafly,weedmaps}, `roster_scorecard.py`→compare.

**Steps:** (1) create `rung_intel/` + its `pyproject.toml` (depends on
`rung`; **declares the `rung.plugins` entry point** — moved off the
public pyproject) and a root `[tool.uv.workspace]` with both members; (2) `git mv` the 20 modules
flat; (3) rewrite imports — `rung.sources.X` / `rung.<priv>` /
`rung.dev.analyze` (X,priv ∈ private) → `rung_intel.X`; **leave
`from rung import <public>` untouched**; (4) rewrite the 16 tests' + 3 scripts'
private imports; (5) `uv sync` (workspace) → entry point rediscovered; (6) rework
`test_import_layering.py` — its `PACKAGE_DIR` scan + PUBLIC/PRIVATE sets become a *cross-package*
check (public pkg has only public modules; intel pkg imports public, never the reverse); (7) gate +
verify public CLI dispatches stubs with the overlay **uninstalled**, real impls with it installed.

**Open calls for the supervised run:** flat vs mirrored layout (recommend flat); tests —
rewrite-in-place as one suite (lower risk, do in 3b) vs relocate into the intel package (cleaner, defer to 3c).

### Traps found on kickoff (2026-06-29) — read before executing

The move is mechanical but NOT a blanket sed. Three things the high-level map missed:

1. **Data-path break (8 refs in 7 files).** These hardcode `Path(__file__).parent.parent / "data" / …`:
   `weedmaps`(weedmaps_slugs), `leafly`(leafly_slugs), `company_store_fetch`(jane_store_ids),
   `recon`(company_homepages), `compare`(companies + grower_brands), `dutchie`(state_geo_anchors +
   dutchie_chains), `dutchie_plus`(dutchie_plus_tokens). On move, `__file__` changes and the path
   breaks. **`data/` stays in the public package for 3b** (its partition is Phase 4; and `companies.yml`
   is *shared* — public `seed_companies` + private `compare` — so it can't simply move). Fix: resolve
   the public data dir without an internal import — `Path(str(importlib.resources.files(
   "rung"))) / "data" / "X.yml"`. **Do NOT use `from rung import DATA_DIR`**:
   `dutchie`/`dutchie_plus` are in PURE_HELPERS (zero-internal-imports), and adding an internal import
   trips `test_overlay_pure_platform_helpers_have_no_internal_imports` (the overlay-scoped guard in
   `tests/test_import_layering.py`). `importlib.resources.files("…")` is a string arg, not an AST
   import edge, so the invariant holds.
2. **recon's public bare-import.** `recon.py` does `from rung.sources import
   homepage_discovery` — and homepage_discovery is **public** (stays in `sources/`). So a blanket
   `from rung.sources import → rung_intel import` is WRONG. Rewrite only the
   per-private-name dotted forms (`rung.sources.<PRIV>`), and handle the 4 all-private
   bare blocks explicitly (`bootstrap`, `company_store_fetch`, `company_stores`, `menus`); leave recon's
   homepage_discovery line alone.
3. **Selective import rewrites.** Most moved modules import only *public* modules (db/http/models/text/
   addresses/normalize/dedupe/ai_fallback/homepage_discovery/browser/access/queue/registry) which STAY
   `rung.*`. Only these have private→private edges to rewrite: `bootstrap` (dutchie,leafly,
   weedmaps,company_store_extractors), `intel_plugin` (bootstrap,analyze→`rung_intel.analyze`,
   company_stores,compare,menus,recon), `company_store_fetch` (dutchie_plus,hytiva,company_store_extractors),
   `company_stores` (the 7-module bare block + company_store_extractors + company_store_fetch), `menus`
   (the 8-module bare block + menu_extractors). `compare`/`recon`/`menu_extractors`/`analyze`/`company_store_extractors`
   and the pure platform helpers import *nothing private* → move untouched (besides the data-path fix).

**Recommendation:** because of these, run the move as a focused pass with the gate as the checkpoint
(roll back, never commit red). Suggested order: (a) workspace skeleton + `uv sync` to prove the
uv/hatchling setup builds; (b) `importlib.resources` data-path fix *in place* (behaviour-preserving,
green, de-risks the move); (c) `git mv` + the selective import rewrites + entry-point move; (d) test/
script import rewrites; (e) rework `test_import_layering.py` to a cross-package partition; (f) gate.

## Invariants the tests pin

1. The internal import graph stays acyclic — **within the public core**, and (skip-guarded) **within
   the overlay** (the pure-helper / aggregator-http-only / acyclic checks the carve-out moved out of
   the public tree are re-enforced over `INTEL_DIR`).
2. The foundation never imports upward — **within the public core**.
3. **Public modules never import private modules** (new — the publishable boundary).
4. Every package module is classified public xor private (new — catches drift on new modules).
5. The public framework runs end-to-end against the example plugin (Phase 2+).
