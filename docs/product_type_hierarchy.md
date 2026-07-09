# Product-type hierarchy (`product_type_std`)

> **Reference application (dispensary dataset).** This document describes the reference pipeline that ships with `rung`, not the generic engine. If you are building your own domain, see [`build-your-own-domain.md`](build-your-own-domain.md) — you define your own equivalents.

**Status:** BUILT 2026-06-24 (leaf-level, name-driven) for **all categories** — Vape, Concentrate,
Edible, Flower, Pre-Roll, Beverage, Tincture, Topical, Capsule, Accessory.
`store_products.product_type_std` is stamped at scrape time (`menu_extractors._record` →
`text.normalize_product_type` reading `data/product_type_aliases.yml`), backfilled over existing
rows by an idempotent script, surfaced in `products_normalized` (`product_type`) and the
search-page dropdown. Companion to `category_taxonomy.md` (the top-level `category_std`); the
investigation that led here is below.

**Live distribution (national, by category):**
- **Vape**: Cartridge 480k · Disposable 467k · Pod 63k · Unspecified 109k.
- **Concentrate**: Live Resin 72k · Live Rosin 64k · Badder 45k · Rosin 44k · Sugar 30k · RSO 20k ·
  Hash 19k · Wax 18k · Diamonds 15k · Shatter 14k · (Sauce/Crumble/Kief/Distillate/Moonrock/Isolate
  smaller) · Unspecified 99k.
- **Edible**: Gummies 383k · Chocolate 64k · Hard Candy 17k · Drink Mix 15k · Baked 11k · Unspecified 240k.
- **Flower**: **Bud 664k (default)** · Smalls 56k (incl. "small flower") · Pre-Pack 40k · Infused 23k · Shake/Trim 21k · Ground 15k.
- **Pre-Roll**: Infused 475k (incl. the platforms' "Infused Pre-Rolls" category) · **Single 406k
  (default)** · Pack 71k · Blunt 17k.
- **Beverage**: Soda/Seltzer 19k · Lemonade/Juice 3k · Tea/Tonic-Shot/Coffee/Mix smaller · Unspecified 13k.
- **Tincture**: Drops/Oral 14k · RSO/FECO/Sublingual/Spray smaller · Unspecified 20k.
- **Topical**: Lotion/Cream 5k · Balm/Salve 5k · Patch 4k · Bath/Roll-On/Stick/Lube smaller · Unspecified 7k.
- **Capsule**: **Capsule 15k (default)** · Softgel 5k · Tablet 2k · Suppository smaller. (Capsule rose
  to ~22k total once `category_name_overrides.yml` routed name-identified capsules/softgels/suppositories
  out of Edible/Tincture/Topical; see `docs/category_taxonomy.md`.)
- **Accessory**: Paper/Cone 66k · Pipe/Bong 60k · Apparel 45k · Battery 28k · Lighter/Grinder/Storage
  smaller · Unspecified 174k.

**Defaults vs Unspecified:** a no-keyword match returns the category's `_defaults` label where the
unqualified product genuinely IS a known form (Flower→**Bud**, Pre-Roll→**Single**, Capsule→**Capsule**),
else the honest **`Unspecified`** (Vape/Edible/Beverage/Tincture/Topical/Accessory — where the form is
truly unknown without the platform sub-category fields). Extend the same way as `category_std`: edit
`product_type_aliases.yml` (mind the per-category priority order and that keywords are alnum-stripped,
so avoid bare numerics / strain names), add a `tests/test_text.py` case, re-run the backfill.

## Goal

Add a **second level** under `category_std`: a canonical **product type** (e.g. `Concentrate →
Live Resin`, `Vape → Cartridge`, `Edible → Gummies`). The hierarchy is **ours** — every platform's
classification is an *input signal* mapped into it, not the taxonomy itself (exactly how
`category_std` already folds Dutchie/Jane/Weedmaps/etc. raw categories into 11 canonical buckets).

## Why our own, and why name-driven

Each platform classifies differently, and — critically — the rich platform sub-category fields are
**only in ~40% of the data and are not stored in our DB** (we kept only the top category). The one
field that is **universal across all ~5.5M rows and every platform is the product `name`.** So the
hierarchy is built as **keyword rules over the name** (like `category_aliases.yml`), which gives
**uniform national coverage including Dutchie** (46% of data, no sub-category field). The platform
sub-category fields become **ground-truth validation** and an optional precision boost if captured
later.

### Where the sub-category signal lives (national, by share of ~5.5M products)

| Platform | Share | Field | Use |
|---|---|---|---|
| Dutchie | 46% | none (`type` only) | name-derive |
| Weedmaps | 36% | `edge_category` + `ancestors` (hierarchy) | ground truth + capture later |
| Leafly | 10% | `productCategory` (top only) | name-derive |
| Jane | 2% | `root_subtype` / `kind_subtype` | ground truth |
| Trulieve / SweedPOS / Cresco | ~5% | `subcategory` / `kind_subtype` / `sub_category` | capture later |

## Empirical sizing (name keywords over all ~5.5M rows)

- **Vape:** Cartridge ≈ 426k · Disposable/AIO ≈ 372k · Pod ≈ small · **~215k name no form** (~25%).
- **Edible:** **Gummies ≈ 384k (dominant)** · Chocolate ≈ 64k · Baked ≈ 19k · other (mints/troches/
  drink-mix) ≈ 66k.
- **Concentrate:** Badder/Budder ≈ 76k · Live Resin ≈ 71k · Live Rosin ≈ 64k · Sauce/Sugar ≈ 55k ·
  Rosin ≈ 43k · Hash ≈ 41k · Wax/Crumble ≈ 36k · Diamonds ≈ 20k · Shatter ≈ 15k · Distillate/Kief/
  RSO ≈ small. (Counts overlap — a "Live Rosin Badder" hits both — so priority order matters, below.)

These line up with the platform ground truth (Weedmaps `edge_category`, Jane `root_subtype`).

## The hierarchy (`category_std` → `product_type_std`)

Built for **all categories**. The canonical leaves below are the source of truth in
`data/product_type_aliases.yml` (the Concentrate/Vape/Edible set shown here in full; the rest —
Flower, Pre-Roll, Beverage, Tincture, Topical, Capsule, Accessory — are in the YAML and summarized
in the distribution above).

```
Vape
  Cartridge          # 510-thread carts (incl. live-resin/distillate carts)
  Disposable         # disposables / all-in-one / AIO
  Pod                # proprietary pods (PAX, etc.)

Concentrate
  Live Resin
  Live Rosin
  Rosin              # non-"live" rosin (press/cold-cure)
  Badder/Budder
  Sugar              # sugar / sauce / terp sauce / HTFSE
  Diamonds           # diamonds / THCa crystalline (+ sauce = "diamonds & sauce")
  Shatter
  Wax                # wax / crumble / honeycomb
  Hash               # hash / bubble / ice-water / temple ball
  Kief               # kief / dry sift
  Distillate         # bulk distillate (non-cart)
  RSO/Oil            # RSO / FECO / applicator / syringe
  Isolate
  Moonrock

Edible
  Gummies            # gummies / fruit chews / sours / pastilles
  Chocolate          # chocolate / truffle / bar
  Baked Goods        # cookie / brownie / krispie / biscuit
  Hard Candy         # hard candy / lozenge / lollipop
  Mints              # mints / tablets (oral)
  Capsule            # softgel / capsule (oral)        [also a Capsule top-category]
  Troche             # PA-medical heavy
  Drink Mix          # powder/shot mix-ins             [or a future Beverage top-category]
```

## How it's derived (as built)

- `data/product_type_aliases.yml` maps **ordered keyword rules per category** → `product_type_std`,
  evaluated against the product `name` + the raw `category` (which on some platforms carries the form).
- **Priority order matters**, same as `category_std`: within Concentrate, `Live Rosin` before `Rosin`
  before `Badder` (so a "Live Rosin Badder" is `Live Rosin`); a vape "Live Resin Cartridge" never
  reaches the Concentrate rules — its `category_std` is already Vape, so it matches Vape → `Cartridge`.
  The category is decided first (`normalize_category`), then the type within it
  (`normalize_product_type(name, category, category_std)`).
- Stamped at the `menu_extractors._record` choke point into the `product_type_std` column; an
  idempotent backfill populated existing rows; surfaced in
  `products_normalized` and the search-page dropdown.
- An explicit **`Unspecified`** bucket where a covered category's name carries no form — honest, not
  faked. `None` for categories not yet in the YAML.

## Resolved / open decisions

1. **Granularity — DECIDED: leaf level** (the ~16 concentrate types as built).
2. **Capsule / Troche / Drink-mix — RESOLVED via name overrides.** A name-identified capsule/softgel/
   suppository now routes to the **Capsule top-category** (not Edible/Tincture), and `troche` is
   pinned to **Edible/Troche** — both via `data/category_name_overrides.yml`. Edible keeps `Capsule`
   and `Troche` as subtype labels only for rows a platform genuinely tags Edible without a name cue.
3. **Capture the platform fields too? (follow-up, not done)** — name-only is uniform across all
   platforms but leaves ~20% `Unspecified` (higher in medical states whose vape names omit the form).
   Capturing Weedmaps `edge_category` + Jane `root_subtype` going forward would lift precision on
   ~40% of rows; it needs a menu re-scrape since those fields aren't stored today.

## The other categories (built)

```
Flower      → Bud (default) · Smalls · Shake/Trim · Ground · Infused · Pre-Pack
Pre-Roll    → Single (default) · Infused · Blunt · Pack
Beverage    → Soda/Seltzer · Tea · Coffee · Lemonade/Juice · Tonic/Shot · Mix
Tincture    → Spray · Sublingual · Drops/Oral · RSO/FECO
Topical     → Lotion/Cream · Balm/Salve · Patch · Lube/Intimate · Roll-On · Bath · Stick
Capsule     → Capsule (default) · Softgel · Tablet · Suppository
Accessory   → Battery · Grinder · Paper/Cone · Pipe/Bong · Lighter · Storage · Apparel
```
