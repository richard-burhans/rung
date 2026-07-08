"""Tests for the product-data normalizers (sizes -> grams, terpenes -> canonical %)."""

from rung.models import StoreProductRecord
from rung.normalize import (
    enrich_record,
    enrich_variants,
    grams_to_label,
    normalize_terpenes,
    size_to_grams,
)


def test_size_to_grams_numeric_units():
    assert size_to_grams("3.5g") == 3.5
    assert size_to_grams("1 g") == 1.0
    assert size_to_grams("100mg") == 0.1
    assert size_to_grams("1oz") == 28.0
    assert size_to_grams("0.5g") == 0.5


def test_size_to_grams_discrete_labels():
    # Jane emits slug labels; words and slugs both fold.
    assert size_to_grams("eighth_ounce") == 3.5
    assert size_to_grams("half_gram") == 0.5
    assert size_to_grams("two_gram") == 2.0
    assert size_to_grams("ounce") == 28.0
    assert size_to_grams("Quarter Ounce") == 7.0


def test_size_to_grams_fraction_ounce():
    assert size_to_grams("1/8 oz") == 3.5
    assert size_to_grams("1/4 ounce") == 7.0
    assert size_to_grams("1/2 oz") == 14.0


def test_size_to_grams_multipack():
    assert size_to_grams("5pk 0.5g") == 2.5
    assert size_to_grams("3 x 1g") == 3.0
    assert size_to_grams("10 x 0.7g") == 7.0


def test_size_to_grams_count_priced_and_junk_are_none():
    assert size_to_grams("each") is None       # a unit count carries no weight
    assert size_to_grams("1 each") is None      # no weight unit
    assert size_to_grams("") is None
    assert size_to_grams(None) is None
    assert size_to_grams("Large") is None       # unitless words don't guess


def test_grams_to_label_canonicalizes_weights():
    # Every platform spelling of one weight normalizes through size_to_grams to the same label.
    assert grams_to_label(size_to_grams("eighth_ounce")) == "3.5g"
    assert grams_to_label(size_to_grams("3.5g")) == "3.5g"
    assert grams_to_label(size_to_grams("1/8oz")) == "3.5g"
    assert grams_to_label(1.0) == "1g"      # trailing .0 dropped
    assert grams_to_label(0.5) == "0.5g"
    assert grams_to_label(28.0) == "28g"


def test_grams_to_label_none_for_unsized():
    assert grams_to_label(None) is None     # a unit/dosed product carries no weight
    assert grams_to_label(0) is None
    assert grams_to_label(-1) is None
    assert grams_to_label(True) is None     # a bool is not a weight
    assert grams_to_label("3.5") is None


def test_normalize_terpenes_folds_aliases_and_sums_pinene():
    terpenes = [
        {"name": "Beta Myrcene", "value": 0.8},
        {"name": "b_myrcene", "value": 0.2},   # same canonical -> summed
        {"name": "Alpha Pinene", "value": 0.3},
        {"name": "Beta-Pinene", "value": 0.1},  # α+β fold into one Pinene
        {"name": "d-Limonene", "value": 1.0},
    ]
    std, total = normalize_terpenes(terpenes)
    assert std == {"Limonene": 1.0, "Myrcene": 1.0, "Pinene": 0.4}  # sorted desc
    assert total == 2.4


def test_normalize_terpenes_tracks_sesquiterpene_alcohols():
    # Guaiol/eudesmol (Watts 2021 Indica markers) are tracked; isomers fold to one canonical (#75).
    std, _ = normalize_terpenes([
        {"name": "Guaiol", "value": 0.3},
        {"name": "Beta-Eudesmol", "value": 0.2},
        {"name": "alpha eudesmol", "value": 0.1},  # folds into Eudesmol
    ])
    assert std == {"Guaiol": 0.3, "Eudesmol": 0.3}  # eudesmol isomers summed; sorted desc


def test_normalize_terpenes_converts_mg_per_gram():
    # Trulieve syringes publish terpenes in mg/g; mg/g ÷ 10 = %.
    std, total = normalize_terpenes([{"name": "Limonene", "value": 20.0, "unit": "mg/g"}])
    assert std == {"Limonene": 2.0}
    assert total == 2.0


def test_normalize_terpenes_drops_unvalued_and_untracked():
    # Strain-reference names (no value) and terpenes outside the tracked set are dropped.
    assert normalize_terpenes([{"name": "Myrcene"}, {"name": "Limonene"}]) == (None, None)
    assert normalize_terpenes([{"name": "Farnesene", "value": 0.5}]) == (None, None)
    assert normalize_terpenes(None) == (None, None)


def test_normalize_terpenes_drops_lone_spike_and_negative():
    # Dutchie occasionally emits one corrupt value (here 6102) alongside sane siblings; the lone
    # spike (it exceeds the sum of the rest) is dropped, leaving the real terpenes intact.
    terpenes = [
        {"name": "Limonene", "value": 6102.0},   # lone spike -> dropped
        {"name": "Caryophyllene", "value": 0.22},
        {"name": "Linalool", "value": -0.5},      # negative -> dropped
    ]
    std, total = normalize_terpenes(terpenes)
    assert std == {"Caryophyllene": 0.22}
    assert total == 0.22


def test_normalize_terpenes_rescales_unlabeled_mg_per_gram_row():
    # A whole row published in mg/g without a unit label reads ~10x too high (total > 40% is
    # physically impossible); no single value dominates, so the row is rescaled by ÷10.
    terpenes = [
        {"name": "Beta Caryophyllene", "value": 34.59},
        {"name": "Beta Myrcene", "value": 30.21},
        {"name": "Alpha Pinene", "value": 6.32},
    ]
    std, total = normalize_terpenes(terpenes)
    assert std == {"Caryophyllene": 3.459, "Myrcene": 3.021, "Pinene": 0.632}
    assert total == 7.112


def test_normalize_terpenes_repairs_spike_and_mg_per_gram_together():
    # A row can carry both corruptions: a lone spike (210) on top of a uniformly mg/g-scaled
    # body. The repair drops the spike, then rescales the still-impossible remainder.
    terpenes = [
        {"name": "Beta Caryophyllene", "value": 210.58},  # spike -> dropped
        {"name": "Alpha Pinene", "value": 49.89},
        {"name": "Linalool", "value": 40.37},
        {"name": "Bisabolol", "value": 43.05},
    ]
    std, total = normalize_terpenes(terpenes)
    assert std == {"Bisabolol": 4.305, "Pinene": 4.989, "Linalool": 4.037}
    assert total == 13.331


def test_normalize_terpenes_keeps_plausible_concentrate_total():
    # A real live-resin profile summing to ~12% is below the impossible threshold and untouched.
    terpenes = [
        {"name": "Limonene", "value": 5.0},
        {"name": "Myrcene", "value": 4.0},
        {"name": "Caryophyllene", "value": 3.0},
    ]
    std, total = normalize_terpenes(terpenes)
    assert std == {"Limonene": 5.0, "Myrcene": 4.0, "Caryophyllene": 3.0}
    assert total == 12.0


def test_normalize_terpenes_drops_single_terpene_over_per_terpene_ceiling():
    # One terpene at 32% among ~1% siblings totals ~34% — under the 40% total ceiling, so
    # _repair_total leaves it — but no single terpene is plausibly 32% (it's a misparse, e.g. an mg
    # dose or cannabinoid value). The per-terpene cap drops it and keeps the real siblings.
    terpenes = [
        {"name": "Linalool", "value": 32.0},       # impossible single value -> dropped
        {"name": "Caryophyllene", "value": 1.2},
        {"name": "Limonene", "value": 0.8},
    ]
    std, total = normalize_terpenes(terpenes)
    assert std == {"Caryophyllene": 1.2, "Limonene": 0.8}
    assert total == 2.0


def test_normalize_terpenes_keeps_high_but_plausible_single_terpene():
    # A dominant 20% terpene in a rich concentrate is below the per-terpene ceiling — untouched.
    std, total = normalize_terpenes([{"name": "Limonene", "value": 20.0}, {"name": "Myrcene", "value": 2.0}])
    assert std == {"Limonene": 20.0, "Myrcene": 2.0}
    assert total == 22.0


def test_enrich_variants_stamps_size_and_price_per_gram():
    variants = [
        {"option": "gram", "price": 10.0},
        {"option": "eighth_ounce", "price": 35.0},
        {"option": "each", "price": 5.0},   # no weight -> no size_g/price_per_g
    ]
    representative = enrich_variants(variants, "Flower")
    assert representative == 1.0  # smallest weighted variant
    assert variants[0] == {"option": "gram", "price": 10.0, "size_g": 1.0, "price_per_g": 10.0}
    assert variants[1]["size_g"] == 3.5
    assert variants[1]["price_per_g"] == 10.0  # 35 / 3.5
    assert "size_g" not in variants[2] and "price_per_g" not in variants[2]


def test_enrich_variants_uses_lowest_price_field():
    # price_per_g tracks the current (lowest) price, ignoring a marketing original.
    variants = [{"option": "gram", "price": 12.0, "original_price": 20.0}]
    enrich_variants(variants, "Concentrate")
    assert variants[0]["price_per_g"] == 12.0


def test_enrich_variants_stamps_original_price_on_real_discounts():
    # Dutchie: special_price_med undercuts price_med (same channel) -> original_price = the regular,
    # price_per_g tracks the special (effective) price.
    dutchie = [{"option": "eighth_ounce", "price_med": 60.0, "price_rec": 60.0, "special_price_med": 45.0}]
    enrich_variants(dutchie, "Flower")
    assert dutchie[0]["original_price"] == 60.0
    assert dutchie[0]["price_per_g"] == round(45.0 / 3.5, 2)
    # Jane: discounted_price undercuts price.
    jane = [{"option": "gram", "price": 12.0, "discounted_price": 9.0}]
    enrich_variants(jane, "Flower")
    assert jane[0]["original_price"] == 12.0 and jane[0]["price_per_g"] == 9.0


def test_enrich_variants_med_rec_spread_is_not_a_discount():
    # The med-vs-rec price spread (no special) must NOT read as a discount — only a same-channel
    # sale field does. Effective is still the lowest; no original_price is stamped.
    spread = [{"option": "gram", "price_med": 10.0, "price_rec": 12.0}]
    enrich_variants(spread, "Flower")
    assert "original_price" not in spread[0]
    assert spread[0]["price_per_g"] == 10.0
    # A no-op original_price (== current) is cleared (idempotent, no phantom discount).
    nodisc = [{"option": "gram", "price": 10.0, "original_price": 10.0}]
    enrich_variants(nodisc, "Flower")
    assert "original_price" not in nodisc[0]


def test_enrich_variants_skips_non_weight_categories():
    # An edible's "100mg" option is a DOSE, not a sellable weight: no size_g/price_per_g, and
    # any stale stamp from a prior (un-guarded) run is cleared.
    variants = [{"option": "100mg", "price": 20.0, "size_g": 0.1, "price_per_g": 200.0}]
    assert enrich_variants(variants, "Edible") is None
    assert "size_g" not in variants[0] and "price_per_g" not in variants[0]
    # An unknown/absent category is treated the same way (don't guess a weight).
    assert enrich_variants([{"option": "gram", "price": 10.0}]) is None


def test_enrich_variants_skips_sub_floor_dosing_artifact():
    # An mg dose that slips into a weight category yields a sub-half-gram "size" — a dosing
    # artifact, not a sellable weight, so it isn't sized (no thousands-of-$/g price_per_g).
    variants = [{"option": "26mg", "price": 50.0}]
    assert enrich_variants(variants, "Concentrate") is None
    assert "size_g" not in variants[0] and "price_per_g" not in variants[0]


def test_enrich_record_stamps_all_normalized_fields():
    record = StoreProductRecord(
        company_id=1, state="PA", store_key="jane:1", platform="jane",
        external_id="1", source="jane_algolia", name="Blue Dream",
        category_std="Flower",
        variants=[{"option": "eighth_ounce", "price": 40.0}],
        terpenes=[{"name": "Limonene", "value": 1.5}],
    )
    enrich_record(record)
    assert record.size_g == 3.5
    assert record.variants[0]["price_per_g"] == 11.43  # 40 / 3.5
    assert record.terpenes_std == {"Limonene": 1.5}
    assert record.terp_total == 1.5


def test_enrich_record_is_idempotent():
    # Re-running (as Cresco does after folding deferred variants) recomputes the same values
    # and doesn't accumulate or leave stale per-variant stamps.
    record = StoreProductRecord(
        company_id=1, state="PA", store_key="cresco:1", platform="cresco",
        external_id="1", source="cresco_api", name="OG Kush",
        category_std="Flower",
        variants=[{"option": "gram", "price": 10.0}],
    )
    enrich_record(record)
    enrich_record(record)
    assert record.size_g == 1.0
    assert record.variants[0]["price_per_g"] == 10.0
