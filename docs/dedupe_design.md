# Deduplication design (`sources/dedupe.py`)

> **Reference application (dispensary dataset).** This document describes the reference pipeline that ships with `rung`, not the generic engine. If you are building your own domain, see [`build-your-own-domain.md`](build-your-own-domain.md) — you define your own equivalents.

## Why
The dataset captures each store from several angles — a company's own site, the Dutchie pool, the
Weedmaps/Leafly directories — and the same operator often appears under multiple **legal entities**
(Delta 9 Pittsburgh and Keystone Integrated Care both redirect to sunnyside.shop). Left alone, one
physical dispensary is counted many times, which (a) inflates the dataset and (b) triple-counts
locations in the company-vs-roster comparison. Dedup collapses every capture of one physical store
into a single canonical row, and folds a multi-entity operator into one company.

`dedupe-stores --state XX` runs after Stage 2 and before `compare-stores`. It is **per-state and
exclusive** (one dedupe claim per state), reads `company_stores`, and **commits**.

## Physical-store identity — the keys
Two `company_stores` rows are the **same physical store** if they share ANY of three keys
(`run_dedupe` ties a row's keys together in a key union-find; rows sharing a key land in one group):

1. **Address key** — `store_key(address, zip)` = `normalize_address` (street number + first
   non-directional word + zip; unit/suite designators stripped). Folds casing / `St`↔`Street` /
   legal-suffix variants of the same address.
2. **Coordinate cell** — `geo_key(lat, lng, zip)` = a **tight ~11 m grid** (4 decimal places) + zip5.
   Recovers the same rooftop scraped with divergent address text (`US-50` vs `U.S. 50`). Deliberately
   tight: at 11 m only the same rooftop collides, so it never merges two *different* dispensaries that
   cluster in one commercial "green zone". (Measured: 3 decimals / ~110 m wrongly merged neighbouring
   competitors, so the cross-row geo key stays at 11 m.)
3. **Platform handle** — `platform:external_id`. A platform store id is unique to one rooftop, so it
   folds a store captured a second time under the same handle even with no address (e.g. Cresco's
   address-less custom duplicates).

### Same-operator coarse-geo merge (~100 m)
The tight 11 m geo key misses one case: the **same store geocoded > 11 m apart by two platforms**
(Weedmaps vs Dutchie drift). For rows of **one operator** (same `canonical_name`), `run_dedupe`
additionally unions any two whose coordinates are within **`_SAME_OP_MERGE_M` = 100 m**.

This is safe *only within one operator*. Measured nationwide, a store's nearest same-operator
cross-platform neighbour is **bimodal**: either `< ~100 m` (the same rooftop, geocoded differently —
~419 stores) or **km+ away** (a genuinely distinct store of the chain — median 12.6 km), with a wide
empty valley between. An operator essentially never has two of its own stores within 100 m, so
same-operator + within-100 m ⇒ the same store. The same 100 m applied *across* operators would fuse
real neighbouring competitors (why key #2 stays at 11 m), so the coarse radius is scoped to one
`canonical_name`. Effect at introduction: **−254 canonical stores nationwide** (12,077 → 11,823),
all geocode-drift duplicates; the largest, most geographically spread chain (CA STIIIZY) kept all 46
distinct locations — zero false merges.

## Operator clustering + canonical choice
Companies that share any physical store are unioned into **operator clusters** (a second union-find
over `company_id`). `pick_canonical` chooses the cluster's canonical company — the one whose brand
appears in the scraped *store names* (Sunnyside's stores are named "Sunnyside …"). Non-canonical
companies' stores get `canonical_company_id` set to the canonical; the operator (canonical) name is
the scrape/dedup key, the storefront alias is the display label.

## Keep-the-best (which row survives a rooftop)
One row per physical store stays canonical (`canonical_company_id IS NULL`); the rest are folded.
The survivor is chosen by `_menu_target_rank` — **prefer the richest-menu handle** (Dutchie /
first-party > Weedmaps / Leafly), then the canonical company, then a stable id — so a first-party
store is never demoted to its empty Weedmaps/Leafly twin at the same rooftop. If the kept row won
without coordinates but a folded sibling has them, the sibling's coords (and a blank address/zip) are
copied onto the survivor so it still maps. `storefront_name` is stamped from the alias.

## Realign menus
`store_products.company_id` is realigned onto each handle's kept row, so a menu scraped under a
since-folded alias re-attributes to the operator (fixes e.g. the Cresco triplication, where PA menus
were attributed to a now-folded legal entity).

## What dedup deliberately does NOT do
- Merge **different operators** at one rooftop — co-located distinct dispensaries, or a store that
  re-branded (the data shows ~72 CA rooftops shared by >1 operator name: real co-locations / brand
  changes), so a cross-operator geo merge is never applied.
- Loosen the cross-row geo key below 11 m precision (it would fuse competitors).
- Remove empty-aggregator-tail stubs — that is the comparison's concern (`compare.run_compare`
  excludes own-side listings with no address AND no menu; see `roster_comparison_findings.md`), not
  dedup's.

## Verify
- After a re-dedupe, total canonical stores per state (`canonical_company_id IS NULL`) and a large
  chain's distinct-location count are the sanity gauges (a chain count that *drops* under a re-dedupe
  is the signature of an over-merge).
- `tests/test_dedupe.py` pins the key/union/keep-the-best behaviour.
