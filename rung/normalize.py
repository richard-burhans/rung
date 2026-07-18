"""Product-data normalization: sizes -> grams, terpenes -> canonical %, the combined standard.

The scraped menu rows preserve each platform's published shape (raw `category`, the
per-variant `option` strings inside `variants`, the mixed-unit `terpenes` list). This module
derives the cross-platform *standard* values that back the `products_normalized` view, the same
way `text.normalize_category` derives `category_std`:

- ``size_to_grams`` / ``enrich_variants`` — a variant's ``option`` label ("3.5g", "eighth_ounce",
  "1/8 oz") to grams, plus a price-per-gram, and a representative top-level ``size_g``. Only
  weight-sold categories (flower/pre-roll/vape/concentrate) are sized; a dosed/unit product
  (edible, tincture, accessory) is left unsized so its grams can't yield a nonsense $/g.
- ``normalize_terpenes`` — fold a product's raw terpene list to canonical ``{Name: percent}``
  (alias names collapsed, α+β-pinene summed, ``mg/g`` converted to ``%``, an impossible total
  repaired — a lone spike dropped or an unlabeled mg/g row rescaled) plus a total.
- ``enrich_record`` — stamp all of the above onto a StoreProductRecord (called from the menu
  extractors' ``_record`` choke point; idempotent so it can re-run after deferred variants).

Numbers, not names: the name/category helpers live in ``text.py`` (this imports
``normalize_terpene`` from there for the terpene-name fold).
"""

import math
import re

from rung.models import StoreProductRecord
from rung.text import name_of, normalize_terpene

# A potency tagged as a percent but over 100% is a mislabeled mg dose — Dutchie tags some edibles'
# mg total as PERCENTAGE (e.g. 396 for a 396 mg gummy pack). Route those to mg rather than store an
# impossible percentage (100 is left as a percent; the boundary is ambiguous). Shared by the
# menu_extractors potency splitters and the backfill_normalization potency guard, so the boundary
# is defined once.
PERCENT_MAX = 100.0

# ── size -> grams ──────────────────────────────────────────────────────────────

# Retail cannabis "ounce" is the conventional 28 g (not 28.35) — matches how the platforms
# bucket their weights (Jane's `ounce` price tier, an "eighth" = 3.5 g). A pound follows from it.
_GRAMS_PER_OUNCE = 28.0
_UNIT_TO_GRAMS = {
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "mg": 0.001, "milligram": 0.001, "milligrams": 0.001,
    "oz": _GRAMS_PER_OUNCE, "ounce": _GRAMS_PER_OUNCE, "ounces": _GRAMS_PER_OUNCE,
    "lb": _GRAMS_PER_OUNCE * 16, "lbs": _GRAMS_PER_OUNCE * 16, "pound": _GRAMS_PER_OUNCE * 16,
}

# Discrete weight labels the platforms emit as words/slugs (Jane uses the slug form). "each" is
# a unit count, not a weight, so it maps to None (no grams) rather than being left unparseable.
_LABEL_TO_GRAMS: dict[str, float | None] = {
    "each": None,
    "half gram": 0.5, "halfgram": 0.5,
    "gram": 1.0,
    "two gram": 2.0, "twogram": 2.0,
    "eighth": 3.5, "eighth ounce": 3.5, "eighthounce": 3.5,
    "quarter": 7.0, "quarter ounce": 7.0, "quarterounce": 7.0,
    "half ounce": 14.0, "halfounce": 14.0,
    "ounce": 28.0,
}

_NUMERIC_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(milligrams|milligram|grams|gram|ounces|ounce|lbs|lb|pound|mg|oz|g)\b",
    re.IGNORECASE,
)
# A fraction of an ounce: "1/8 oz", "1/4 ounce".
_FRACTION_OZ_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*(?:oz|ounce|ounces)\b", re.IGNORECASE)
# A multipack count: "5pk", "5 pack", "3 x 0.5g", "10x". Bounded so a stray id can't multiply.
_MULTIPACK_RE = re.compile(r"(\d{1,3})\s*(?:x|×|pk|pack)\b", re.IGNORECASE)


def size_to_grams(raw: object) -> float | None:
    """Grams for a variant size label, or None when it carries no weight / can't be parsed.

    Handles discrete labels ("eighth_ounce", "gram", "each"→None), fractions ("1/8 oz" → 3.5),
    and numeric+unit ("3.5g", "100mg" → 0.1, "1 oz" → 28) with a ``N x``/``Npk`` multipack
    multiplier ("5pk 0.5g" → 2.5). Unitless or unrecognized input → None (never a bare guess).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().lower()
    label_key = re.sub(r"[\s_]+", " ", text).strip()
    if label_key in _LABEL_TO_GRAMS:
        return _LABEL_TO_GRAMS[label_key]
    count_match = _MULTIPACK_RE.search(text)
    count = int(count_match.group(1)) if count_match else 1
    if count < 1:
        count = 1
    fraction = _FRACTION_OZ_RE.search(text)
    if fraction:
        numerator, denominator = float(fraction.group(1)), float(fraction.group(2))
        if denominator:
            return _plausible_g((numerator / denominator) * _GRAMS_PER_OUNCE * count)
    match = _NUMERIC_UNIT_RE.search(text)
    if match:
        grams = float(match.group(1)) * _UNIT_TO_GRAMS[match.group(2).lower()]
        return _plausible_g(grams * count)
    return None


# A retail cannabis unit tops out near a pound (454 g bulk flower); a "size" past ~1 kg is a unit-parse
# error (a 10,416 g "flower", a 2,000 g vape — mg read as g, or a runaway multipack), not a sellable
# weight. Reject it rather than stamp a nonsense size the shelf never sold.
_MAX_PLAUSIBLE_G = 1000.0


def _plausible_g(grams: float) -> float | None:
    return round(grams, 4) if 0 < grams <= _MAX_PLAUSIBLE_G else None


def grams_to_label(grams: object) -> str | None:
    """Canonical compact size label for a weight in grams: ``3.5`` → "3.5g", ``1.0`` → "1g".

    The display counterpart of ``size_to_grams``: every platform spelling of one weight
    ("eighth_ounce", "1/8 oz", "3.5g Flower") goes through ``size_to_grams`` to the same number
    and back out here to a single label, so the same size reads the same everywhere. Returns None
    for a missing/non-positive weight — a unit/dosed product that ``size_to_grams`` left unsized.
    """
    if (
        not isinstance(grams, (int, float))
        or isinstance(grams, bool)
        or not math.isfinite(grams)
        or grams <= 0
    ):
        return None
    text = f"{float(grams):.4f}".rstrip("0").rstrip(".")
    return f"{text}g"


# Variant price keys, across platforms (med/rec split, promos, sales). The variant's "current"
# price is the lowest of whatever it carries — same rule as the product-level `price` the mappers
# compute (a marketing "original"/"promo" high price never wins a min).
_VARIANT_PRICE_KEYS = (
    "price", "price_med", "price_rec", "special_price_med", "special_price_rec",
    "sale_price", "discounted_price", "unit_price", "original_price",
)


# Sale-price field -> the channel regular it discounts. The variant's lowest price winning via one
# of these is a genuine same-channel discount (Dutchie special_price_{med,rec} undercut price_{med,
# rec}; others' discounted_price/sale_price undercut price), so the regular is the pre-discount
# original. Keyed per channel to avoid mistaking the med-vs-rec price spread for a discount.
_SALE_TO_REGULAR = {
    "special_price_med": "price_med",
    "special_price_rec": "price_rec",
    "discounted_price": "price",
    "sale_price": "price",
}


def _variant_pricing(variant: dict) -> tuple[float | None, float | None]:
    """(effective current price, pre-discount original or None) across a variant's price fields.

    Effective = the lowest price field (the shelf price). Original is recovered when that lowest is a
    SALE field undercutting its own channel's regular (`_SALE_TO_REGULAR`) — Dutchie
    `special_price_{med,rec}`, Jane `discounted_price` — or when the variant already carries an
    explicit higher `original_price` (Weedmaps/Hytiva). Unifies discount capture across platforms.
    """
    priced = {
        key: float(value)
        for key in _VARIANT_PRICE_KEYS
        if isinstance(value := variant.get(key), (int, float)) and not isinstance(value, bool)
    }
    if not priced:
        return None, None
    winner = min(priced, key=lambda key: priced[key])
    effective = priced[winner]
    original = None
    regular = priced.get(_SALE_TO_REGULAR.get(winner, ""))
    if regular is not None and regular > effective:
        original = regular
    explicit = priced.get("original_price")  # Weedmaps/Hytiva stamp the regular directly
    if explicit is not None and explicit > effective:
        original = max(original, explicit) if original is not None else explicit
    return effective, original


def _variant_price(variant: dict) -> float | None:
    """Lowest current price across a variant's known price fields (None if it has none)."""
    return _variant_pricing(variant)[0]


# Categories sold by weight, where grams and price-per-gram are meaningful. Everything else
# (edibles, tinctures, beverages, capsules, topicals, accessories) is dosed in mg or sold per
# unit, so an ``option`` like "100mg" is a DOSE, not a sellable weight — sizing it yields a
# nonsense price-per-gram (a 5 mg gummy → 0.005 g → thousands of $/g). Those products carry
# their dose in ``thc_mg``/``cbd_mg`` instead. An absent/unknown category is treated as not
# weight-sold (conservative: don't guess a weight).
_WEIGHT_CATEGORIES = frozenset({"Flower", "Pre-Roll", "Vape", "Concentrate"})
# Smallest real sellable cannabis weight (a half-gram, with margin). A computed size below this
# is a dosing artifact — an mg label that slipped into a weight category — so it isn't sized.
_MIN_SELLABLE_GRAMS = 0.05


def enrich_variants(variants: object, category: str | None = None) -> float | None:
    """Stamp ``size_g`` + ``price_per_g`` onto each variant in place; return the representative
    top-level size_g (the smallest variant weight, the most granular sellable unit).

    Only ``_WEIGHT_CATEGORIES`` get sized — a dosed/unit product (edible, tincture, accessory…)
    leaves ``size_g``/``price_per_g`` unset because grams aren't a meaningful quantity for it.
    Idempotent: recomputes from the variant's own ``option``/price/``category`` fields,
    overwriting (or clearing) any prior stamp, so the same list can be enriched again after a
    deferred build (Cresco) or re-run by the backfill with the guards applied.
    """
    if not isinstance(variants, list):
        return None
    weight_sold = category in _WEIGHT_CATEGORIES
    sizes: list[float] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        item: dict = variant
        item.pop("price_per_g", None)
        # Unify discount capture: stamp `original_price` (pre-discount regular) when the variant is
        # on sale — recovered from per-platform price fields (Dutchie special_price_*, Jane
        # discounted_price, …). Done for ALL variants (discounts aren't weight-only) and idempotent:
        # cleared when there's no discount so a re-run/backfill can't leave a stale stamp.
        price, original = _variant_pricing(item)
        if original is not None:
            item["original_price"] = round(original, 2)
        else:
            item.pop("original_price", None)
        grams = size_to_grams(item.get("option")) if weight_sold else None
        if grams is None or grams < _MIN_SELLABLE_GRAMS:
            item.pop("size_g", None)
            continue
        item["size_g"] = grams
        sizes.append(grams)
        if price is not None:
            item["price_per_g"] = round(price / grams, 2)
    return min(sizes) if sizes else None


# ── terpenes -> canonical {Name: percent} ──────────────────────────────────────

# Physically, no cannabis product's total terpenes reach this percent (premium flower ~4%, the
# richest live-resin concentrate ~15-20%). A canonical total above it means the row's values are
# corrupt one of two ways, repaired by `_repair_total`: ONE spurious spike (Dutchie sometimes
# emits a single value like 6102 among ~0.2 siblings) or a whole row published in mg/g without
# the unit label (every value ~10x). Real 10-40% concentrate profiles stay below it, untouched.
_MAX_PLAUSIBLE_TOTAL = 40.0

# Per-terpene ceiling: no SINGLE terpene plausibly exceeds ~30% by weight (the richest live-resin
# concentrate tops out near 20% for one terpene; flower is a few percent). A larger single value is
# a residual misparse the total-based repair misses — its row total can stay under
# `_MAX_PLAUSIBLE_TOTAL` (e.g. one terpene at 32% among ~1% siblings totals ~33%). Verified against
# the dataset: 0 concentrate and only a handful of flower rows carry a single terpene >30%, all
# clearly corrupt (an mg dose or cannabinoid value read as a terpene percent). Such a value is
# dropped (the rest of the profile is kept).
_MAX_PLAUSIBLE_TERPENE = 30.0


def _terpene_percent(value: object, unit: object) -> float | None:
    """A terpene value as a percent, converting ``mg/g`` (``mg/g ÷ 10 = %``) when so labelled.

    Drops non-numeric and negative values; an implausibly large positive value is left for the
    row-level `_repair_total` (which can tell a lone spike from a whole mg/g-misscaled row).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    percent = float(value)
    if "mg" in str(unit or "").lower():  # "mg/g" — the only labelled non-% terpene unit (Trulieve)
        percent /= 10.0
    return percent if percent >= 0 else None


def _repair_total(totals: dict[str, float]) -> dict[str, float]:
    """Repair an impossible canonical total (> ``_MAX_PLAUSIBLE_TOTAL``).

    No cannabis product reaches 40% total terpenes, so a larger total is unambiguously corrupt:
    a lone spike (one value exceeds the sum of all the others) is a spurious reading and is
    dropped; otherwise the whole row is uniformly ~10x (mg/g published without a unit label) and
    is rescaled by ÷10. A row can carry both (a spike *and* mg/g scaling), so this repeats until
    the total is plausible — the loop only ever admits genuinely-impossible totals, so a dominant
    *real* terpene (its total is already plausible) is never reached. Bounded against pathological
    input; a plausible total returns unchanged.
    """
    for _ in range(5):
        total = sum(totals.values())
        if total <= _MAX_PLAUSIBLE_TOTAL:
            break
        top = max(totals, key=lambda key: totals[key])
        if totals[top] > total - totals[top]:  # one value dwarfs the rest -> spurious spike
            totals = {name: value for name, value in totals.items() if name != top}
        else:  # every value inflated ~10x -> mg/g published without a unit label
            totals = {name: round(value / 10.0, 4) for name, value in totals.items()}
    return totals


def _fold_terpenes(terpenes: object) -> dict[str, float]:
    """Fold a raw terpene list to canonical ``{Name: percent}`` BEFORE any repair.

    Names collapse via ``text.normalize_terpene`` (so ``b_myrcene``/``Beta Myrcene`` → Myrcene and
    α-/β-pinene sum into one Pinene), values convert to percent, and duplicates of one canonical
    terpene are summed. Entries with no numeric value and untracked terpenes are dropped. Shared by
    ``normalize_terpenes`` and ``terpenes_repaired`` so the two cannot drift (the fold is defined once).
    """
    if not isinstance(terpenes, list):
        return {}
    totals: dict[str, float] = {}
    for terpene in terpenes:
        if not isinstance(terpene, dict):
            continue
        canonical = normalize_terpene(name_of(terpene.get("name")))
        if canonical is None:
            continue
        percent = _terpene_percent(terpene.get("value"), terpene.get("unit"))
        if percent is None:
            continue
        totals[canonical] = round(totals.get(canonical, 0.0) + percent, 4)
    return totals


def normalize_terpenes(terpenes: object) -> tuple[dict[str, float] | None, float | None]:
    """Fold a raw terpene list to canonical ``({Name: percent}, total)``.

    Names collapse via ``text.normalize_terpene`` (so ``b_myrcene``/``Beta Myrcene`` → Myrcene
    and α-/β-pinene sum into one Pinene), values convert to percent, and duplicates of one
    canonical terpene are summed. Entries with no numeric value (strain-reference names) and
    terpenes outside the tracked set are dropped, then `_repair_total` fixes an impossible total
    (lone spike dropped / unlabeled mg/g row rescaled); ``(None, None)`` when nothing
    quantifiable remains. ``total`` is the sum of the canonical percents (consistent with dict).
    ``terpenes_repaired`` reports whether the repair below actually altered the values.
    """
    totals = _fold_terpenes(terpenes)
    if not totals:
        return None, None
    totals = _repair_total(totals)  # drop a lone spike / rescale an mg/g-misscaled row
    # Per-terpene sanity: drop any single value still above the per-terpene ceiling — a residual
    # misparse whose row total stayed under _MAX_PLAUSIBLE_TOTAL, so _repair_total left it (run
    # AFTER _repair_total so a uniform mg/g row is rescaled, not gutted, first).
    totals = {name: value for name, value in totals.items() if value <= _MAX_PLAUSIBLE_TERPENE}
    if not totals:
        return None, None
    # An ALL-ZERO map is "not tested", not "tested and found to contain nothing". Several platforms
    # publish the full terpene panel with every value 0.0 for an untested product, and we were
    # faithfully storing it — 75,086 rows (10% of the observation history) carrying a plausible-looking
    # {Myrcene: 0.0, Limonene: 0.0, …} that passes every `terpenes_std IS NOT NULL` gate and then
    # contributes a real zero to any mean, quantile or ICC computed over "products with terpenes".
    # A measured zero for ONE terpene is meaningful (it is below the LOQ); a measured zero for EVERY
    # terpene is an empty panel. Store NULL, which is what "we do not know" already means everywhere.
    if all(value == 0.0 for value in totals.values()):
        return None, None
    ordered = dict(sorted(totals.items(), key=lambda item: -item[1]))
    return ordered, round(sum(totals.values()), 4)


def terpenes_repaired(terpenes: object) -> bool:
    """True when :func:`normalize_terpenes` ALTERED the raw values to produce ``terpenes_std``.

    The same shape as :func:`text.product_type_defaulted`: a repaired row's stored terpene numbers are
    OUR corrections — a lone spurious spike dropped, an unlabeled mg/g row rescaled ÷10 (both by
    ``_repair_total``), or a single terpene above the per-terpene ceiling dropped — not the profile the
    platform published, yet downstream they are byte-indistinguishable from a clean read. These numbers
    feed D1's ICC/variance flagship, so an analysis must be able to exclude the corrected rows. Returns
    False when the row yields no ``terpenes_std`` (nothing to flag): the flag qualifies a stored value.
    """
    raw = _fold_terpenes(terpenes)
    if not raw:
        return False
    repaired = _repair_total(raw)
    kept = {name: value for name, value in repaired.items() if value <= _MAX_PLAUSIBLE_TERPENE}
    if not kept or all(value == 0.0 for value in kept.values()):
        return False                       # normalize_terpenes returns (None, None) — no stored value
    # A repair happened iff _repair_total changed the map OR the per-terpene ceiling dropped a terpene.
    return repaired != raw or len(kept) != len(repaired)


# ── stamp onto a record ─────────────────────────────────────────────────────────

def enrich_record(record: StoreProductRecord) -> None:
    """Stamp the normalized fields (``size_g``, ``terpenes_std``, ``terp_total``,
    ``terpenes_repaired``, and the per-variant ``size_g``/``price_per_g``) onto a record from its own
    raw fields. Idempotent."""
    record.size_g = enrich_variants(record.variants, record.category_std)
    record.terpenes_std, record.terp_total = normalize_terpenes(record.terpenes)
    record.terpenes_repaired = terpenes_repaired(record.terpenes)
