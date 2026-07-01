"""Tests for the shared text helpers (used by recon.py and seed_companies.py)."""

from rung.text import (
    dominant_terpene,
    extract_brand,
    is_placeholder_name,
    normalize_brand,
    normalize_category,
    normalize_product_type,
    normalize_strain_type,
    terpene_floats,
)


def test_terpene_floats_coerces_and_filters_non_numeric():
    assert terpene_floats({"Myrcene": 1.2, "Limonene": 3, "Pinene": None, "X": "0.5"}) == {
        "Myrcene": 1.2, "Limonene": 3.0,  # None and the string "0.5" are dropped
    }
    assert terpene_floats(None) == {}
    assert terpene_floats("not a dict") == {}


def test_dominant_terpene_picks_highest_value():
    assert dominant_terpene({"Myrcene": 1.2, "Limonene": 0.8}) == "Myrcene"
    assert dominant_terpene({"Caryophyllene": 0.5, "Limonene": 2.0}) == "Limonene"
    assert dominant_terpene({}) is None
    assert dominant_terpene(None) is None


def test_is_placeholder_name() -> None:
    for junk in [
        "[Equity Retailer]", "Cookies Brentwood [Equity Retailer]", "Treez Demo",
        "Test Toker", "DO NOT USE Serra", "DNU Jungle Boys", "Data Not Available",
        "TBD", "284.000141-CL", "284.CL.4445088", "Calma Weho (Duplicate", "", None,
    ]:
        assert is_placeholder_name(junk), junk
    for real in ["Curaleaf", "Zen Leaf", "Trulieve", "Cookies Florida", "RISE", "Sunnyside*"]:
        assert not is_placeholder_name(real), real


def test_normalize_brand_folds_spacing_case_and_punctuation():
    assert normalize_brand("Zen Leaf") == normalize_brand("ZenLeaf") == "zenleaf"
    assert normalize_brand("zen-leaf") == "zenleaf"
    assert normalize_brand("NuEra") == normalize_brand("nuEra") == "nuera"
    assert normalize_brand("EarthMed") == normalize_brand("Earthmed") == "earthmed"
    assert normalize_brand("Beyond / Hello") == normalize_brand("Beyond Hello") == "beyondhello"


def test_normalize_brand_keeps_distinct_brands_distinct():
    assert normalize_brand("Green") != normalize_brand("Greenhouse")
    assert normalize_brand("Cure") != normalize_brand("Curaleaf")


def test_normalize_brand_falls_back_when_all_punctuation():
    assert normalize_brand("!!!") == "!!!"  # stripping leaves nothing → lowered original


def test_extract_brand_hyphen():
    assert extract_brand("Trulieve - Pittsburgh") == "Trulieve"


def test_extract_brand_folds_storefront_city_with_own_city():
    # The bare "<brand> <city>" suffix (no separator) folds when the row's OWN city is passed, so a
    # multi-store operator's per-city names collapse to one brand.
    assert extract_brand("Zen Leaf Dayton", "Dayton") == "Zen Leaf"
    assert extract_brand("Cookies Modesto", "Modesto") == "Cookies"
    assert extract_brand("Native Roots Grand Junction", "Grand Junction") == "Native Roots"  # multi-token
    # Without the city it's unchanged (default), so non-seed callers are unaffected.
    assert extract_brand("Zen Leaf Dayton") == "Zen Leaf Dayton"


def test_extract_brand_keeps_name_when_city_is_not_a_suffix_or_unsafe():
    # City isn't the trailing token → no strip.
    assert extract_brand("Dayton Wellness", "Dayton") == "Dayton Wellness"
    # Dangling connector → the city is part of the operator's name (Harvest of Whitehall).
    assert extract_brand("Harvest of Whitehall", "Whitehall") == "Harvest of Whitehall"
    # A ≤2-char stub would be a risky coincidental prefix → keep the full name.
    assert extract_brand("JO Gardner", "Gardner") == "JO Gardner"
    # The brand IS the city → keep (don't empty it).
    assert extract_brand("Modesto", "Modesto") == "Modesto"


def test_extract_brand_en_dash():
    assert extract_brand("Trulieve – Pittsburgh") == "Trulieve"


def test_extract_brand_em_dash():
    # OH rosters separate the storefront city with an em dash; it must fold like the en dash so
    # "AYR Dispensary — Columbus" / "— Cincinnati" collapse to one operator instead of per-store.
    assert extract_brand("AYR Dispensary — Columbus") == extract_brand("AYR Dispensary — Cincinnati")
    assert extract_brand("The Botanist — Cleveland") == "The Botanist"


def test_extract_brand_no_separator():
    assert extract_brand("Cresco") == "Cresco"


def test_extract_brand_tight_hyphen():
    assert extract_brand("Ayr- Boca Raton") == "Ayr"


def test_extract_brand_semicolon_aliases():
    # arcgis multi-alias field -> first segment, trailing descriptor stripped.
    assert extract_brand("Good Day Farm Dispensary; Gdf Dispensary; Good Day Farm") == "Good Day Farm"
    assert extract_brand("Fresh Karma Dispensaries; Fresh Karma") == "Fresh Karma"
    assert extract_brand("High Profile Cannabis Shop; High Profile") == "High Profile"
    # both spellings of the same operator collapse together
    assert extract_brand("Swade Cannabis Dispensary; Swade Cannabis") == "Swade"
    assert extract_brand("Swade Cannabis") == "Swade"


def test_extract_brand_comma_legal_and_location():
    assert extract_brand("TODAY'S HERBAL CHOICE, INC.") == "TODAY'S HERBAL CHOICE"
    assert extract_brand("Lucid Auburn, 21+ Cannabis, 21+ Marijuana") == "Lucid Auburn"
    assert extract_brand("Shangri-La, Shangri-La Dispensary") == "Shangri-La"


def test_extract_brand_trailing_generic_not_overstripped():
    # a single bare generic word stays (never reduce to empty)
    assert extract_brand("Cannabis") == "Cannabis"
    assert extract_brand("Curaleaf") == "Curaleaf"
    # an article + generic must not collapse to a bare article
    assert extract_brand("The Dispensary") == "The Dispensary"
    assert extract_brand("The Marijuana Shop; The Shop") == "The Marijuana Shop"


# ── normalize_category (cross-platform product taxonomy) ──────────────────────

def test_normalize_category_maps_each_platform_raw_form():
    # The same canonical from every platform's own vocabulary.
    assert normalize_category("Vaporizers") == "Vape"          # dutchie
    assert normalize_category("vape") == "Vape"                # jane
    assert normalize_category("Vape Pens") == "Vape"           # weedmaps
    assert normalize_category("Cartridge") == "Vape"           # leafly
    assert normalize_category("Pre-Rolls") == "Pre-Roll"       # dutchie/weedmaps
    assert normalize_category("preroll") == "Pre-Roll"         # leafly
    assert normalize_category("Infused Pre-rolls") == "Pre-Roll"
    assert normalize_category("Flower") == "Flower"
    assert normalize_category("Edible") == normalize_category("Edibles") == "Edible"
    assert normalize_category("Concentrate") == normalize_category("extract") == "Concentrate"
    assert normalize_category("Tincture") == "Tincture"
    assert normalize_category("Topicals") == "Topical"
    assert normalize_category("Drinks") == "Beverage"
    assert normalize_category("Capsules") == "Capsule"
    assert normalize_category("Gear") == normalize_category("Accessories") == "Accessory"


def test_normalize_category_spacing_case_punctuation_collapse():
    assert normalize_category("Pre-Rolls") == normalize_category("pre rolls") \
        == normalize_category("PreRoll") == "Pre-Roll"


def test_normalize_product_type_within_category():
    npt = normalize_product_type
    # Vape forms.
    assert npt("Sundae Driver 1g Cartridge", "vape", "Vape") == "Cartridge"
    assert npt("Runtz All-In-One", "vape", "Vape") == "Disposable"
    assert npt("PAX Era Pod", "vape", "Vape") == "Pod"
    # Concentrate priority: method (Live Rosin) leads over consistency (Badder).
    assert npt("Blue Dream Live Rosin Badder", "concentrate", "Concentrate") == "Live Rosin"
    assert npt("GMO Live Resin", "extract", "Concentrate") == "Live Resin"
    assert npt("Gelato Diamonds & Sauce", "concentrate", "Concentrate") == "Diamonds"
    assert npt("Wedding Cake Shatter", "concentrate", "Concentrate") == "Shatter"
    # Edible forms.
    assert npt("Watermelon Gummies 100mg", "edible", "Edible") == "Gummies"
    assert npt("Dark Chocolate Bar", "edible", "Edible") == "Chocolate"
    # Flower + Pre-Roll use a per-category `_defaults` label (not "Unspecified") for the unqualified
    # product, which genuinely IS a known form: whole Bud / a Single joint.
    assert npt("OG Kush 3.5g", "flower", "Flower") == "Bud"
    assert npt("Blue Dream Smalls", "flower", "Flower") == "Smalls"
    assert npt("Sour Diesel Pre-Roll", "pre-rolls", "Pre-Roll") == "Single"
    assert npt("GMO Infused Pre-Roll 5-pack", "infused pre-rolls", "Pre-Roll") == "Infused"
    # Other categories.
    assert npt("Strawberry Seltzer 10mg", "beverage", "Beverage") == "Soda/Seltzer"
    assert npt("Relief Balm 1:1", "topical", "Topical") == "Balm/Salve"
    assert npt("Sleep Softgels", "capsule", "Capsule") == "Softgel"
    # Honest fallbacks: a covered category with no default + no form -> Unspecified; a category not
    # in the YAML -> None.
    assert npt("Mystery Concentrate", "concentrate", "Concentrate") == "Unspecified"
    assert npt("Pax Battery", "accessory", "Other") is None
    # The raw category carries the form when the name doesn't (Leafly "Cartridge").
    assert npt("House Blend", "Cartridge", "Vape") == "Cartridge"


def test_normalize_category_priority_for_multi_keyword_strings():
    # A vape cart of live resin is a Vape (cart) before a Concentrate (resin).
    assert normalize_category("Live Resin Cart") == "Vape"
    # Rosin softgels are a Capsule (gel) before a Concentrate.
    assert normalize_category("Rosin Gels") == "Capsule"
    # Ice cream is an Edible before a Topical ("cream").
    assert normalize_category("Ice Creams") == "Edible"
    # A topical cream stays Topical (no edible keyword).
    assert normalize_category("Topical Cream") == "Topical"
    # RSO is a concentrate even in syringe form.
    assert normalize_category("RSO Syringe") == "Concentrate"
    # Plain live resin (no cart/gel) is a Concentrate.
    assert normalize_category("Live Resin") == "Concentrate"


def test_normalize_category_name_overrides_correct_mislabeled_forms():
    # The platform's raw category mislabels a dosed/finished form; the NAME corrects it.
    assert normalize_category("edibles", "Indica RSO Capsules 30ct") == "Capsule"
    assert normalize_category("Orally Administered", "Raz Lemonade Distillate Troches") == "Edible"
    assert normalize_category("topicals", "THC Suppository 5pk") == "Capsule"
    assert normalize_category("concentrates", "Chimera Junky Infused Flower") == "Flower"
    assert normalize_category("concentrates", "Live Resin Distillate Cartridge 1g") == "Vape"
    assert normalize_category("concentrates", "Legacy Liquid Live Resin All In One Disposable") \
        == "Vape"
    # Override fires even with a blank/absent raw category (name is the only signal).
    assert normalize_category(None, "Full Spectrum Softgels 10pk") == "Capsule"


def test_normalize_category_name_overrides_protect_correct_higher_format():
    # An override only fires from its allowed `from` categories — it never clobbers a correct
    # higher-format bucket. "Infused Flower" in a pre-roll stays Pre-Roll (an infused pre-roll).
    assert normalize_category("Pre-Rolls", "Strawberry Infused Flower Pre-Roll") == "Pre-Roll"
    # A vape whose name happens to say "capsule" is not demoted to Capsule.
    assert normalize_category("Vaporizers", "Capsule Collection Cart") == "Vape"
    # With no name supplied the override layer is inert (back-compatible).
    assert normalize_category("edibles") == "Edible"


def test_normalize_product_type_small_flower_is_smalls():
    # "Small Flower" is the smalls grade, not whole Bud.
    assert normalize_product_type("Banana Shack Small Flower 7g", "flower", "Flower") == "Smalls"


def test_normalize_category_other_and_blank():
    # Genuinely ambiguous → Other (never a silent mis-bucket into a real category).
    assert normalize_category("Wellness") == "Other"
    assert normalize_category("CBD") == "Other"
    assert normalize_category("Other") == "Other"
    assert normalize_category("Seeds") == "Other"
    # Blank/None → None.
    assert normalize_category(None) is None
    assert normalize_category("   ") is None


# ── normalize_strain_type (cross-platform lineage facet) ──────────────────────

def test_normalize_strain_type_maps_lineage_across_case_and_punctuation():
    for raw in ("Indica", "indica", "INDICA", "(Indica)", "Indica (I)", "Vape Indica"):
        assert normalize_strain_type(raw) == "Indica", raw
    for raw in ("Sativa", "sativa", "SATIVA", "(Sativa)"):
        assert normalize_strain_type(raw) == "Sativa", raw
    for raw in ("Hybrid", "hybrid", "HYBRID", "(Hybrid)", "Hybrid Blend"):
        assert normalize_strain_type(raw) == "Hybrid", raw


def test_normalize_strain_type_explicit_hybrid_token_wins():
    # "…-Hybrid" carries the explicit hybrid token → Hybrid (Hybrid is checked before lineage).
    assert normalize_strain_type("Indica-Hybrid") == "Hybrid"
    assert normalize_strain_type("Sativa-Hybrid") == "Hybrid"
    assert normalize_strain_type("Edible Hybrid") == "Hybrid"


def test_normalize_strain_type_dominant_maps_to_its_literal_lineage():
    # No "hybrid" token → classify by the literal lineage word present, not a forced Hybrid.
    assert normalize_strain_type("Indica Dominant") == "Indica"
    assert normalize_strain_type("Sativa Dominant") == "Sativa"
    assert normalize_strain_type("Indica Blend") == "Indica"
    assert normalize_strain_type("Sativa Mix") == "Sativa"


def test_normalize_strain_type_cbd_and_ratio():
    assert normalize_strain_type("High CBD") == "CBD"
    assert normalize_strain_type("CBD") == normalize_strain_type("cbd") == "CBD"
    assert normalize_strain_type("1 to 1") == "CBD"     # word-form ratio → "1to1"
    assert normalize_strain_type("20 to 1") == "CBD"
    # CBD wins over hybrid when both present (the CBD trait is the defining one).
    assert normalize_strain_type("CBD Hybrid") == "CBD"


def test_normalize_strain_type_none_for_pollution_and_traps():
    # Category words wrongly stamped into strain_type → None, not a forced bucket.
    for raw in ("Concentrate", "Edible", "Gear", "Drink", "Tincture", "THC", "N/A", "No Strain"):
        assert normalize_strain_type(raw) is None, raw
    # Bare strain / brand / flavor names whose substrings would trap a broad keyword → None.
    for raw in ("Blueberry Muffin", "Dragon's Blend", "Cake Mix", "Mixed Berry",
                "Body Dominant", "Celebration", "Expiration Date", "Mixed Strains"):
        assert normalize_strain_type(raw) is None, raw
    # Blank / None → None.
    assert normalize_strain_type(None) is None
    assert normalize_strain_type("   ") is None
