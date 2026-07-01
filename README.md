# rung

> **run the cheapest rung that works** — a broker-free, Postgres-centric framework for
> resilient, polite, distributed scraping of fragmented consumer-data platforms.

An open-source framework for building an authoritative, multi-state dataset of licensed
cannabis dispensaries — and for checking what each operator publishes against the state's
official roster. The engine is domain-agnostic; cannabis is its first, demonstrated application.

It runs three pipelines over one Postgres database (`dispensaries`):

- **State rosters** — discover each state agency's dispensary-list resource
  (HTML / PDF / CSV / ArcGIS / Socrata) and extract the official roster.
- **Company sites** — scrape each operator's *own* website for its store list (the trusted
  source of truth) through a cost-ranked access-method engine, dedupe by physical address,
  and diff it against the state roster to surface where the state list lags — closures it
  still shows, open stores it's missing.
- **Menus** — snapshot each store's live menu: products, prices, potency, terpenes.

**Circumvents nothing by default.** The published code makes no attempt to defeat a site's
bot protection: the HTTP client sends an honest, self-identifying User-Agent with no
fingerprint spoofing (browser TLS impersonation is opt-in, off by default), and Stage 1
reads **public government records**. Respect each site's terms and `robots.txt`; you are
responsible for how you use this.

## Why

I built this to help Pennsylvania medical-cannabis patients find accurate store and product
information, and I'm releasing the framework in the tradition of open scraping education —
the techniques are worth sharing even when the dataset stays private. If it saves you time,
that's the point.

— Richard Burhans

## Architecture

The codebase is **two packages**: the public open-source core **`rung`** (Stage-1
roster extraction, the cost-ranked access-method *engine*, the work queue, persistence, the CLI, and
the plugin seam) and the private overlay **`dispensary_scraper_intel`** (the proprietary Stage-2/3
scraping catalogs and per-platform recipes, the roster-comparison intel, recon, bootstrap, and the
curated platform datasets). The core ships and runs on its own — its proprietary stages resolve to
registry stubs until the overlay plugs in via the `rung.plugins` entry point. The
overlay is a `uv`-workspace member that depends on the core; the boundary is test-enforced
(`tests/test_import_layering.py`). See [`docs/publish_split_design.md`](docs/publish_split_design.md).

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the abstraction map, dependency
direction, and cross-cutting contracts.

## Setup

```bash
# Start a local Postgres matching the default DSN (or set DATABASE_URL to your own):
docker run -d --name dispensaries-pg \
  -e POSTGRES_USER=dispensary -e POSTGRES_PASSWORD=dispensary -e POSTGRES_DB=dispensaries \
  -p 5432:5432 postgres:16
```

The connection URL defaults to the dev container
(`postgresql://dispensary:dispensary@localhost:5432/dispensaries`); override with
`DATABASE_URL`.

Stage table contracts — which command reads/writes which table, and the work-queue
claims that make concurrent runs safe — are in
[`docs/stage_contracts.md`](docs/stage_contracts.md).

## CLI

Commands (see `pyproject.toml [project.scripts]`), run via `uv run <command>`:

| Stage | Commands |
|---|---|
| 1 · State lists | `search-states`, `find-lists`, `scrape-states` (`--render`, `--ai`), `show-states` |
| 2 · Company sites | `seed-companies`, `recon` (`--discover`), `scrape-company-stores` (`--ai`), `dedupe-stores`, `compare-stores` |
| 3 · Menus | `scrape-menus` |
| Dev | `analyze <url>` |

- **Stage 1 flow** (every state, incl. PA): `search-states → find-lists → scrape-states`.
- **Stage 2 flow:** `seed-companies → recon → scrape-company-stores → dedupe-stores →
  compare-stores`. Dedupe collapses cross- and intra-company duplicates by physical
  address (folding operator aliases, e.g. Harvest of Whitehall → Trulieve) and stamps each
  store's `storefront_name`; compare diffs each operator's own stores against the state
  roster, excluding grower/processor brands.
  For 0%-website states, `recon --discover` (opt-in, network) web-searches operators with no
  derivable homepage, ranks candidates by brand↔domain match, validates the best via the same
  probe, and prints a review list to promote into `data/company_homepages.yml`.

Dutchie-backed operators resolve without a browser via two rungs: `dutchie_directory` (a per-state geo directory sweep) and `dutchie_plus` (a headless storefront API for operators on their own storefront — e.g. Curaleaf, which the consumer directory marks "closed"); Dutchie has three integration styles. The `weedmaps_directory` and `leafly_directory` rungs are parallel per-state sweeps of the two big *aggregators of independents* — both attributed by **slug prefix** since neither carries a chain field. They are the unlock for the independents that Dutchie/Jane/Sweed don't cover (the fragmentation ceiling in CA/OR/WA/NY). Weedmaps has no listing coverage in some states (PA, MO, IL, WA return 0 — those are Leafly-only); elsewhere both sweep.

> **Throttle note (resolved).** The first production ingest (2026-06-17) under-captured aggregator menus because of a **volume soft-block**: the aggregators soft-block an egress once its request rate crosses a threshold, and the old fetch path turned that into silent zero-yield (CA: 19/871 weedmaps stores). It is not empty listings — most slugs return a real menu at low volume, and the block clears after a short quiet period. **Fixed** with rate control (paced, soft-block-aware fetches) wrapping both aggregators. The **paced re-ingest** then lifted the dataset **+281,351 rows** (e.g. CA 159k→251k, WA 104k→179k, OR 173k→224k).

The `--render` (pydoll/Chrome) and `--ai` (scrapegraphai + local Ollama) tiers are opt-in
last resorts; the static / hidden-JSON / directory-sweep path is the default. Run
`dedupe-stores` after any `scrape-company-stores` (it re-stamps storefronts and re-resolves
aliases).

- **Stage 3 flow:** `scrape-menus` (after dedupe) walks every canonical store that carries a
  scrape handle (`platform` + `external_id`) and snapshots its menu into `store_products`.
  Live rungs: `jane_algolia`, `dutchie_products`, `trulieve_rest` (operator REST wrapper), `cresco_api` (Sunnyside + white-labels, captured ids validated/re-resolved by address), `sweedpos_ssr` (server-rendered menu pages — Curaleaf via its own store-directory API at Stage 2), `hytiva_api`, `weedmaps_menu` and `leafly_menu` (the two aggregators' consumer feeds, paged by slug — the independents rungs), and `dutchie_plus_menu` (reuses the Stage-2 token). **PA coverage: 189 handled stores →
  138,506 product rows** (verified ingest 2026-06-16). The 2026-06-15/16 Stage-2 fixes added
  **+14 handled stores / +12,661 rows**: Insa, MariMart, Organic Remedies (6) via the Jane
  discovery-reach upgrades; **Fluent (3)** and **Bloc Dispensary (3)** via Dutchie
  (`dutchie_directory` + curated parent-chain overrides). **Every
  PA operator is now handled** — the former low-value tail closed via the Jane discovery-reach
  upgrades and the id override: Hive (3 stores, 2,552 rows — `jane_api` reaches
  its JS-only embed on the `shop.` host and beats the addressed `line_blocks` set), Terra Pharm
  (3, 2,561 rows — `jane_api` is now the registry winner over `line_blocks`), Local Leaf (1, 647
  rows), and Maitri (3, 2,253 rows — via the id override; see Stage 2).
  Keystone ReLeaf's `companies.yml` alias to
  Apothecarium applies on a clean re-seed (the additive `seed-companies` leaves the existing row).
  Menus churn daily, so `scrape-menus --max-age-hours 24` refreshes only the stores whose
  latest snapshot has gone stale — run it on a daily cron and same-day re-runs are cheap
  no-ops; omit the flag to force a full re-scrape.
- **Multi-state (2026-06):** **39 states + DC live** — every Wikipedia operational-retail jurisdiction now has menus except the ones with no operating dispensaries on scrapeable platforms (ND/NH roster-only; AL/GA/IA limited low-THC). **~4.78M product rows**. CA 1,048 stores / 1,106,959; MI 761 / 788,648; OR 560 / 376,734; NY 533 / 334,250; OK 499 / 307,089; MA 377 / 188,790; FL 540 / 179,904; CO 462 / 180,542; WA 132 / 179,062; MO 169 / 177,969; PA 195 / 146,827; NM 294 / 139,328; IL 126 / 119,327; MT 313 / 76,428; NJ 136 / 71,445; AZ 83 / 71,306; OH 106 / 59,989; MD 78 / 61,684; ME 114 / 40,474; NV 38 / 37,042; MS 69 / 30,300; CT 31 / 14,280; AR 21 / 12,278; DC 61 / 12,024; SD 33 / 11,236; LA 24 / 11,211; VT 31 / 10,231; AK 28 / 8,543; WV 45 / 6,701; MN 28 / 4,183; VA 12 / 3,725; RI 5 / 2,595; UT 5 / 2,119; HI 16 / 1,744; DE 5 / 1,190; KY 12 / 989; IA 5 / 464; GA 12 / 126; AL 1 / 11 (ND/NH roster-only — too small to yield menus) — all via reused rungs. DC is I-71/medical retail; VA (medical) is Dutchie/Leafly-native (Ayr, Zen Leaf/Verano, Cannabist) with RISE/Beyond Hello off-platform; AL/GA/IA are limited low-THC programs (few SKUs). Per-dose **mg potency** (`thc_mg`/`cbd_mg`) is captured for mg-dosed edibles/tinctures: 282,373 thc_mg / 147,489 cbd_mg.
  The Dutchie geo-sweep attributes operators **without a homepage**, so a state needs only a few
  seeded homepages + geo-anchors + chain overrides; a state whose official roster is locked (NJ) or
  licensee-named (CA) or JS-gated (OK) can be bootstrapped from the Dutchie pool itself via
`bootstrap-dutchie --state X` (NJ, MI, CA, OK, AR, + the medical batch). **Dataset: 4,777,764 product rows across 7,008 stores.** The bring-up confirmed the real multi-state gate is
  *operator→homepage acquisition*, not scraping: PA's roster uniquely carried websites (100%),
  while IL's IDFPR PDF has none, so homepages were seeded into `company_homepages.yml`. The
  bring-up surfaced per-state website-coverage variation and open IL triage items (e.g. Zen
  Leaf's rec-vs-medical menu path).
- **Normalized standard fields (the combined cross-platform surface):** every `store_products`
  row keeps its platform-shaped raw fields *and* carries standardized ones stamped at scrape time
  at the `menu_extractors._record` choke point: `category_std` (canonical category — from the raw
  platform category, plus name-keyword overrides for forms platforms mislabel, e.g. a capsule sold
  as an "edible"; see `docs/category_taxonomy.md`),
  `product_type_std` (the 2nd-level product type within a category, from the product name —
  Vape→Cartridge / Concentrate→Live Resin / Edible→Gummies / Flower→Bud, all categories; see
  `docs/product_type_hierarchy.md`),
  `strain_type_std` (canonical lineage facet `Indica`/`Sativa`/`Hybrid`/`CBD`, or `None` when the
  raw value is a category word or bare strain name — the raw column is polluted), `size_g`
  (variant size →
  grams: `3.5g`/`eighth_ounce`/`1/8 oz` resolve, but only for weight-sold categories
  (flower/pre-roll/vape/concentrate) — an edible's `100mg` is a dose, not a sellable weight, so
  it's left unsized rather than yielding a nonsense `$/g`), per-variant `size_g` + `price_per_g`
  inside the `variants` JSONB, and `terpenes_std` / `terp_total` (raw terpenes folded to canonical
  `{Name: %}` with α+β-pinene summed, `mg/g` converted to `%`, and a physically-impossible total
  (>40%) repaired — a lone spurious spike dropped or an unlabeled `mg/g` row rescaled). A
  percent>100 potency is rerouted to mg (catches mg-dosed edibles a platform mislabels as a
  percentage). The **`products_normalized` VIEW** projects just these standard
  fields (+ a derived top-level `price_per_g`, and `strain_type_std` surfaced under the
  `strain_type` name) for apples-to-apples queries across platforms and
  states. The `backfill_*` scripts (`backfill_normalization.py [--state PA]`,
  `backfill_category_std.py`, `backfill_strain_std.py`, `backfill_product_type.py`) recompute them
  over existing rows.

## Tests

Run `ruff check` → `ty` → `pytest` (the last with a coverage floor, `--cov-fail-under`).
The same gate runs in **CI** (`.github/workflows/ci.yml`) on every PR and push to `main`,
against a Postgres service container — so the gate is enforced in the cloud, not just locally.

DB tests run against throwaway schemas in the `dispensaries_test` database; the suite
(`tests/`) covers the extractor parsing logic, the DB-replace safety invariants, and
the work-queue claim semantics; the network/browser/AI tiers are exercised manually.
