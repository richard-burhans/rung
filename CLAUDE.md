# rung — working guide

`rung` is a cost-ranked web-scraping framework: extract authoritative rosters, scrape each entity's own
site for its locations, diff those against the roster, and snapshot each location's live catalog.
Domain-specific catalogs plug in via the `rung.plugins` entry point; this repo is the generic core.

**Start with `ARCHITECTURE.md`** for the module map and dependency edges.

## The pipeline (CLI = `[project.scripts]`)

- **Stage 1 — rosters:** `search-states` → `find-lists` → `scrape-states` (generic
  HTML/PDF/CSV/ArcGIS/Socrata extractors; opt-in `--render` browser and `--ai` LLM fallbacks).
  `seed-companies` derives entities; `recon` detects each one's platform + homepage.
- **Stage 2 — entity sites:** `scrape-company-stores` routes each entity through the access engine
  (`access.py`) → `company_stores`; then `dedupe-stores` → `compare-stores`.
- **Stage 3 — listings:** `scrape-menus` snapshots each handled location's catalog into
  `store_products`, routed to a per-platform rung supplied by the plugin overlay. `--max-age-hours N`
  freshness-gates re-scrapes.
- **Fleet:** `worker` is the distributed entrypoint (one process per egress IP). See
  `docs/worker_fleet_deployment.md`.

## Core concepts

- **Two packages (public core + private plugin).** The public core ships Stage-1 extraction, the
  access engine, the queue, persistence, normalization, the CLI, and the plugin seam, and runs on its
  own — plug-in stages resolve to registry stubs until an overlay registers via `rung.plugins`. The
  core imports nothing from any overlay; the boundary is test-enforced
  (`tests/test_import_layering.py`). See `docs/publish_split_design.md`.
- **Access-method engine (`access.py`):** every target is reachable several ways at different cost;
  `run_target` runs the cheapest that works, persists the winner per target, and re-walks only on
  failure / a cheaper untried rung / a governed staleness re-explore.
- **Work queue (`queue.py`, `jobs` table):** stages enqueue then claim their own work
  (`FOR UPDATE SKIP LOCKED`), so concurrent runs partition targets.

## Dev workflow

- **Python ≥ 3.13**, deps via **`uv`**: `uv run <cmd>`, `uv run pytest`, `uv tool run ruff`.
- **Postgres** (psycopg3 raw SQL, no ORM). `DATABASE_URL` env; default = a local Postgres dev
  container. Run `ruff check` → `ty` → `pytest` (coverage floor) before every commit; the same gate
  runs in CI (`.github/workflows/ci.yml`).

## Conventions

- **Coding standards:** modern type syntax (`X | None`, `list[…]`); LBYL over `try/except` except at
  the CLI boundary and untrusted-external-data boundaries; `pathlib.Path` with explicit
  `encoding="utf-8"`. CLI commands use `print()` for user-facing output.
- **All HTTP goes through `http.make_session()`** (an AST guard in `tests/test_http.py` enforces it —
  never construct a bare session).
- **Normalized fields:** each `store_products` row keeps its raw platform shape and standardized fields
  stamped at scrape time (canonical category/type, lineage facet, per-variant size→grams and
  price-per-unit, folded attribute maps); the `products_normalized` VIEW is the cross-platform surface.
- Keep `ARCHITECTURE.md` / `README.md` / `docs/` in sync with code changes.
