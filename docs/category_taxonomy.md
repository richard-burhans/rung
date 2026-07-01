# Canonical product-category taxonomy (`category_std`)

Each menu platform reports product `category` in its own vocabulary — a vape is `Vaporizers`
(Dutchie) / `vape` (Jane) / `Vape Pens` (Weedmaps) / `Cartridge` (Leafly) / `Vape` (Sweed),
~200 distinct raw strings across the dataset. `store_products.category_std` is the **canonical
cross-platform category** derived from that raw string, so products are comparable / searchable
state-wide and USA-wide. The raw `category` is preserved alongside it.

## Canonical categories

`Flower · Pre-Roll · Vape · Concentrate · Edible · Beverage · Tincture · Topical · Capsule ·
Accessory · Other`

`Other` is the explicit catch-all for genuinely-ambiguous strings (e.g. `wellness`, `cbd`,
`cultivation`, tier labels like `premium`, genetics like `seeds`/`clones`). **A wrong bucket is
worse than an honest unknown — never silent-map an ambiguous string into a real category.**

## How it's derived

- Source of truth: [`rung/data/category_aliases.yml`](../rung/data/category_aliases.yml)
  — an ORDERED map of `canonical -> [substring keywords]`.
- `text.normalize_category(raw, name=None)` lowercases + strips the raw string to alphanumerics,
  then returns the **first** canonical (top-to-bottom = priority) whose any keyword is a substring
  of it; `Other` if none match, `None` if the raw is blank.
- Stamped in one place — `menu_extractors._record` — so all 9 platform mappers get it for free.
- Existing rows: `scripts/backfill_category_std.py` (idempotent; a distinct-category pass for the
  base classification + a per-row pass for the name overrides below).

### Name overrides (the raw category is name-blind, and platforms mislabel forms)

The base classification reads only the raw `category`, so a platform that files a **capsule** under
`edibles` or a **live-resin disposable** under `concentrates` lands in the wrong bucket. When the
product NAME unambiguously names such a form,
[`data/category_name_overrides.yml`](../rung/data/category_name_overrides.yml)
corrects it (applied after the base pass, when `name` is supplied):

- `capsule`/`softgel`/`protab` → **Capsule**, `suppositor` → **Capsule**, `troche` → **Edible**,
  `infused/ground flower` → **Flower**, `disposable`/`all in one`/`cartridge` → **Vape**.
- Each rule fires only from an allowed `from` set, so it corrects a known mislabel without
  clobbering a correct higher-format bucket: `Infused Flower` in a **Pre-Roll** name stays Pre-Roll
  (an infused pre-roll); only in a Concentrate name does it become Flower.
- Audited against the national name × category_std distribution; the backfill flipped ~39k rows
  (the biggest: capsules out of Edible → the Capsule category went 5.1k → 21.8k).

### Why priority order matters

A raw string can hit several keywords; the first canonical wins:
- `Live Resin Cart` → **Vape** (`cart`) before Concentrate (`resin`) — it's consumed as a vape.
- `Rosin Gels` → **Capsule** (`gel`) before Concentrate.
- `Ice Creams` → **Edible** (`icecream`) before Topical (`cream`).
- `RSO Syringe` → **Concentrate** (`rso`) before Tincture (`syringe`).

The current priority is: Pre-Roll, Vape, Capsule, Concentrate, Tincture, Beverage, Edible,
Topical, Flower, Accessory.

## Coverage (by product volume, ~4.8M rows)

Vape ~24% · Pre-Roll ~20% · Flower ~17% · Edible ~15% · Concentrate ~9% · Accessory ~9% ·
Other **~2.1%** · Beverage ~0.8% · Tincture ~0.6% · Topical ~0.5% · Capsule ~0.45%.

(Capsule rose from ~0.1% after the name-override pass routed mislabeled capsules/suppositories
out of Edible/Tincture/Topical into the Capsule category.)

## Extending it

1. Run `scripts/backfill_category_std.py --dry-run` (or `GROUP BY category_std`) and look at the
   **Other** bucket's top raw strings.
2. If a high-volume string is unambiguous, add a keyword to the right canonical in
   `category_aliases.yml` (mind the priority order — add a focused-test case in `tests/test_text.py`).
3. Re-run the backfill (idempotent).

## Second level (`product_type_std`)

A finer **product type** under each category (Concentrate → Live Resin, Vape → Cartridge, Edible →
Gummies, Flower → Bud) — same keyword-rule approach over the product name — is built for all
categories. See [`product_type_hierarchy.md`](product_type_hierarchy.md).

## Strain-type facet (`strain_type_std`)

`store_products.strain_type_std` applies the same keyword-rule pattern to the raw `strain_type`
column, deriving a canonical lineage facet: **`Indica · Sativa · Hybrid · CBD`** (or `None`).
`text.normalize_strain_type` reads `data/strain_aliases.yml` (ordered keyword rules, alnum
substring match, first hit wins); `menu_extractors._record` stamps it for every platform; the
`products_normalized` view surfaces it under the `strain_type` name; `scripts/backfill_strain_std.py`
applies it to existing rows. The raw `strain_type` is preserved alongside.

Two deliberate differences from `category_std`:

- **No-match → `None`, not "Other".** The raw column is heavily polluted — platforms stamp it
  with product *categories* (`Concentrate`, `Edible`, `Gear`, `Drink`) and bare *strain/brand
  names* (`Blueberry Muffin`, `Dragon's Blend`), so ~45% of rows carry no lineage at all. `None`
  is the honest answer there; a forced bucket would be wrong.
- **Conservative keywords.** Only unambiguous lineage tokens are matched (`indica`, `sativa`,
  `hybrid`, `cbd`, the common `N to N` ratio forms). The tempting broad ones — `blend` / `mix` /
  `dominant` / `ratio` — are traps that hit brand/flavor names (`Dragon's Blend`, `Cake Mix`,
  `Mixed Berry`, `Celebration`, `Expiration`) and are **not** used. Genuine lineage that also
  carries those words still classifies via its lineage token: `Indica Dominant` / `Indica Blend`
  → Indica; only `…-Hybrid` carries the explicit `hybrid` token → Hybrid. Bare `Mixed` / `Blend`
  with no lineage word → `None`.

Distribution across the dataset (~4.8M rows): **Hybrid ~29% · Indica ~16% · Sativa ~9% ·
CBD ~0.6% · None ~45%**.

Extend it the same way as the category taxonomy (dry-run `scripts/backfill_strain_std.py`, inspect
the values, add a keyword + a `tests/test_text.py` case) — but keep keywords lineage-specific so a
brand/strain name can never be mis-bucketed.
