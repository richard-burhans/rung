"""Tests for the shared text helpers (used by recon.py and seed_companies.py)."""

from rung.text import (
    category_overridden,
    dominant_terpene,
    extract_brand,
    is_placeholder_name,
    normalize_brand,
    normalize_category,
    normalize_obtention,
    normalize_product_type,
    normalize_strain_type,
    product_type_defaulted,
    strip_legal_entity,
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


def test_extract_brand_folds_at_storefront_separator():
    # " at <location>" is a storefront separator (the AGLC roster's form where the own site uses
    # " - "), so both sides fold to one operator — the AB compare-lag fix.
    assert extract_brand("Value Buds at Baseline Village") == "Value Buds"
    assert extract_brand("Value Buds - Baseline Village") == "Value Buds"  # symmetric with the dash
    assert extract_brand("The Frosted Nug at Red Bank") == "The Frosted Nug"
    # Guarded: a bare-generic prefix must NOT collapse into the mega-generic "Cannabis" bucket.
    assert extract_brand("Cannabis at the Green Brier") == "Cannabis at the Green Brier"


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


def test_category_overridden_flags_a_name_driven_correction():
    # True exactly when the NAME overrides the raw-category bucket — OUR correction, not the platform's
    # label. Same cases as test_normalize_category_name_overrides_correct_mislabeled_forms.
    assert category_overridden("edibles", "Indica RSO Capsules 30ct") is True
    assert category_overridden("concentrates", "Chimera Junky Infused Flower") is True
    assert category_overridden(None, "Full Spectrum Softgels 10pk") is True   # name-only signal
    # False when the raw category already decides the bucket (no override fired) …
    assert category_overridden("edibles", "Sour Gummies 10pk") is False
    assert category_overridden("Vaporizers", "Capsule Collection Cart") is False  # protected higher format
    assert category_overridden("Pre-Rolls", "Strawberry Infused Flower Pre-Roll") is False
    # … and False when there is no name to drive an override at all.
    assert category_overridden("edibles", None) is False


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


def test_extract_brand_strips_a_trailing_store_address_or_parenthetical() -> None:
    """A state roster names the LICENSEE OF EACH STORE, so one operator arrives as many names.

    `seed-companies` derives `companies` from that roster, so "ONE PLANT 3003 DANFORTH" and
    "ONE PLANT BARRIE (CUNDLES)" each became their OWN company — and Stage 2 then scraped the
    operator's single homepage once per company, giving each the FULL store list. 6,906 of 22,074
    store rows (31%) are a redundant copy of a rooftop another "company" already holds.
    """
    assert extract_brand("ONE PLANT 3003 DANFORTH") == "ONE PLANT"
    assert extract_brand("SPIRITLEAF 1550 HURON CHURCH ROAD") == "SPIRITLEAF"
    assert extract_brand("HIGH CANNABIS 12467") == "HIGH CANNABIS"
    assert extract_brand("GREEN ROOM (ALBANY)") == "GREEN ROOM"


def test_extract_brand_store_suffix_does_not_cascade_into_the_generic_strip() -> None:
    """The parenthetical is the STORE; "Cannabis" is part of the BRAND. Strip one, keep the other.

    Order matters: stripping the store tail first would expose "Ziggyz Cannabis" to the trailing-
    generic rule and over-fold it to "Ziggyz".
    """
    assert extract_brand("Ziggyz Cannabis (MacArthur)") == "Ziggyz Cannabis"


def test_extract_brand_keeps_a_number_that_is_part_of_the_brand() -> None:
    """Only a 3+-digit trailing run is a store address. A brand's own small number survives."""
    assert extract_brand("Cloud 9") == "Cloud 9"
    assert extract_brand("Green 2 Go") == "Green 2 Go"


# ── The city can hide the descriptor behind it (the RISE / Ontario fragmentation) ───────────────────
# `extract_brand` stripped the trailing generic descriptor BEFORE it stripped the storefront city, so a
# descriptor sitting *behind* the city was invisible to it. One ordering bug, ten phantom companies.

def test_a_descriptor_hidden_behind_the_city_is_stripped() -> None:
    """The bug that fragmented ONE Virginia operator into TEN companies.

    "RISE Dispensaries Abingdon" -> (city) -> "RISE Dispensaries", with the descriptor now exposed and
    never re-examined. Each spelling seeded its own row in `companies` — and the W2 event study clusters
    on the operator, so ten phantom firms is not a cosmetic problem.
    """
    variants = [
        ("RISE Dispensaries Abingdon", "Abingdon"),
        ("RISE Dispensary Abingdon", "Abingdon"),
        ("RISE Medical Marijuana Dispensary Salem", "Salem"),
        ("Rise Dispensary Bristol", "Bristol"),
        ("RISE Abingdon", "Abingdon"),
    ]
    brands = {normalize_brand(extract_brand(n, c)) for n, c in variants}
    assert brands == {"rise"}, f"one operator must fold to one brand, got {brands}"


def test_the_longest_descriptor_wins() -> None:
    """Regex alternation is first-match: "medical marijuana dispensary" must precede "marijuana
    dispensary", or the strip leaves "RISE Medical" and the fold still fails."""
    assert extract_brand("RISE Medical Marijuana Dispensary Salem", "Salem") == "RISE"


def test_the_re_strip_does_not_cascade_when_no_city_was_removed() -> None:
    """THE GUARD THAT MAKES THE FIX SAFE, and the cascade the store-suffix work already warned about.

    Re-stripping unconditionally turns "Ziggyz Cannabis (MacArthur)" into "Ziggyz" — but the brand really
    is "Ziggyz Cannabis"; the parenthetical is the store. No city was removed there, so nothing was
    hidden, so the first pass's verdict must stand. We re-examine only what the city strip newly exposed.
    """
    assert extract_brand("Ziggyz Cannabis (MacArthur)", "Oklahoma City") == "Ziggyz Cannabis"


def test_a_strip_may_not_leave_a_name_without_letters() -> None:
    """"123 Cannabis" must stay "123 Cannabis", not become "123".

    A brand that is a bare number folds every unrelated numeric name in the state into one phantom
    company — the very failure this area exists to prevent. (This hole pre-dated the fix; the fix would
    have widened it.)
    """
    assert extract_brand("123 Cannabis Carstairs", "Carstairs") == "123 Cannabis"
    assert extract_brand("123 Cannabis") == "123 Cannabis"


def test_a_bare_article_is_still_protected() -> None:
    assert extract_brand("The Dispensary", "Denver") == "The Dispensary"
    assert extract_brand("The Dispensary Denver", "Denver") == "The Dispensary"


# ── A defaulted label is a PRIOR, not an observation ────────────────────────────────────────────────
# Weedmaps shipped a *defaulted* lineage field (98.2% "Indica"); we read it as an observation and had to
# retract a finding (E1). `product_type_aliases.yml`'s `_defaults` does the same thing on OUR side of the
# wire: no keyword match -> Flower becomes "Bud". 759,927 flower rows carry a Bud no name ever said.
# `bowker-star_1999_sorting-things-out` names it: a residual category promoted into a positive assertion.

def test_a_manufactured_label_is_flagged() -> None:
    """No keyword matched, so the category's `_defaults` fired. The label is real; the observation is not."""
    assert normalize_product_type("Blue Dream 3.5g", None, "Flower") == "Bud"
    assert product_type_defaulted("Blue Dream 3.5g", None, "Flower") is True
    assert normalize_product_type("OG Kush Pre-Roll", None, "Pre-Roll") == "Single"
    assert product_type_defaulted("OG Kush Pre-Roll", None, "Pre-Roll") is True


def test_an_observed_label_is_not_flagged() -> None:
    """A keyword matched — the name actually said it."""
    for name, cat, want in [("Blue Dream Smalls", "Flower", "Smalls"),
                            ("Pineapple Shake", "Flower", "Shake/Trim"),
                            ("Gummies 10mg", "Edible", "Gummies")]:
        assert normalize_product_type(name, None, cat) == want
        assert product_type_defaulted(name, None, cat) is False, name


def test_an_honest_unspecified_is_not_a_manufactured_label() -> None:
    """A category with NO `_defaults` entry emits "Unspecified" — which is an admission, not an assertion.
    Flagging it as `defaulted` would confuse the residual category with the manufactured one."""
    assert normalize_product_type("Mystery Item", None, "Edible") == "Unspecified"
    assert product_type_defaulted("Mystery Item", None, "Edible") is False


def test_an_uncovered_category_is_not_flagged() -> None:
    assert normalize_product_type("Thing", None, "Apparel") is None
    assert product_type_defaulted("Thing", None, "Apparel") is False


# ── The licensee/storefront gap: a licence is issued to a legal person, a shop trades under a brand ──
#
# The roster files "ADEGOKE HOLDINGS LLC"; the operator's own site publishes "Adegoke". The two never
# keyed together, so BOTH sides reported a phantom — ours as `site_only` ("the state list is missing this
# store"), the roster's as `state_only` ("possible closure"). Measured on the live DB (2026-07-14),
# folding the suffix moves compare: matched 9,415 -> 9,687 and site_only 8,469 -> 8,193.


def test_strip_legal_entity_drops_the_licence_holder_suffix() -> None:
    assert strip_legal_entity("Adegoke Holdings LLC") == "Adegoke"
    assert strip_legal_entity("Patriot Care Corp") == "Patriot Care"
    assert strip_legal_entity("Smacked LLC") == "Smacked"
    assert strip_legal_entity("Cannabis Hut Ltd") == "Cannabis Hut"
    assert strip_legal_entity("Old Growth Cannabis Ltd") == "Old Growth Cannabis"


def test_strip_legal_entity_is_repeated_because_names_stack_them() -> None:
    """"Aurora Cannabis Enterprises Inc." carries two suffixes; one pass would leave "Enterprises"."""
    assert strip_legal_entity("Aurora Cannabis Enterprises Inc.") == "Aurora Cannabis"


def test_strip_legal_entity_never_eats_the_whole_name() -> None:
    """A name that IS the suffix keeps it — an empty brand folds every unrelated name into one phantom."""
    assert strip_legal_entity("Holdings LLC") == "Holdings"
    assert strip_legal_entity("LLC") == "LLC"


def test_strip_legal_entity_refuses_to_leave_a_BARE_GENERIC() -> None:
    """The guard that measurement found, not imagination.

    BC's "Cannabis Co." strips to "Cannabis" — the word every brand contains — which then collides with
    "Cannabis 247", merging two unrelated licensees into one phantom operator. The suffix is only noise
    when a REAL brand survives it.
    """
    assert strip_legal_entity("Cannabis Co.") == "Cannabis Co."
    assert strip_legal_entity("Dispensary LLC") == "Dispensary LLC"
    # And the two stay distinct operators through the full brand key.
    assert normalize_brand(extract_brand("Cannabis Co.")) != normalize_brand(extract_brand("Cannabis 247"))


def test_extract_brand_folds_the_legal_suffix_before_the_generic_one() -> None:
    """Both stack: "FLOYD\'S CANNABIS COMPANY". The generic strip only sees the END of the name, so a
    "Cannabis" hiding behind a "Company" is invisible to it unless the legal strip runs first."""
    assert extract_brand("FLOYD'S CANNABIS COMPANY") == "FLOYD'S"
    assert extract_brand("Adegoke Holdings LLC") == "Adegoke"
    # The roster row and the storefront now key as one operator — the whole point.
    assert normalize_brand(extract_brand("Adegoke Holdings LLC")) == normalize_brand(extract_brand("Adegoke"))
    assert normalize_brand(extract_brand("Patriot Care Corp")) == normalize_brand(extract_brand("Patriot Care"))


def test_a_legal_entity_alias_still_folds_after_the_suffix_strip() -> None:
    """The regression the legal-entity fold caused, and the reason `load_company_aliases` folds its keys.

    Aliases are looked up with `extract_brand(name)` as the key, and many are written in the licence
    roster's own words ("Diamond Star Group inc." -> Dankley). Once `extract_brand` learned to strip the
    suffix it handed the lookup "Diamond Star" while the map was still keyed on the full legal name, so
    the alias silently stopped folding and one operator split back into two companies. Nothing crashed —
    which is exactly why this is a test and not a comment.
    """
    import tempfile
    from pathlib import Path

    from rung.text import load_company_aliases

    with tempfile.TemporaryDirectory() as tmp:
        yml = Path(tmp) / "companies.yml"
        yml.write_text('Dankley:\n  - "Diamond Star Group inc."\n', encoding="utf-8")
        aliases = load_company_aliases(yml)

    assert aliases["Diamond Star Group inc."] == "Dankley"   # the spelling as written
    assert aliases[extract_brand("Diamond Star Group inc.")] == "Dankley"  # the key it is looked up by


# ── obtention_std ───────────────────────────────────────────────────────────────────────────────────
#
# This facet replaced a regex inlined in NATURAL_FLOWER_WHERE. Every case below is one the LIVE DATA
# produced while it was being built — none is hypothetical, and each one was a bug at some point during
# the port. They are pinned because the two failure modes here are opposite and both silent: a keyword
# that is too loose drops real flower out of the chemovar analyses, and one that is too tight lets
# infused flower back in (which is what inflated a producer->THC effect 0.226 -> 0.372 in the first
# place).


def test_normalize_obtention_reads_the_raw_category_not_just_the_name():
    # 267 rows carry the signal ONLY in the platform's raw category; a name-only rule missed every one.
    assert normalize_obtention("Baby Yoda", "Infused Flower") == "Infused"
    assert normalize_obtention("Pineapple Donutz", "Moonrocks") == "Infused"


def test_normalize_obtention_alnum_normalization_catches_split_words():
    # `Sherb Cherry THCA In fused preroll` is a real product name. The retired regex looked for `infus`
    # and saw "In fused" — with a space — so it passed an infused product through as natural flower.
    assert normalize_obtention("Sherb Cherry THCA In fused preroll", "Flower") == "Infused"
    assert normalize_obtention("Moon Rock Nugs", "Flower") == "Infused"


def test_normalize_obtention_matches_the_infus_prefix_not_the_whole_word():
    # The retired regex used the PREFIX `infus`. Spelling it `infused` here missed three real products,
    # found only because backfill_obtention.py --verify prints its disagreements instead of counting them.
    for name in ("Good Time 11 Infuse Flower", "Pineapple Express OZ Shake | Infuse Trim",
                 "Revert - Galactic Jack - 14g - Infusd Ground Flower"):
        assert normalize_obtention(name, "Flower") == "Infused", name


def test_normalize_obtention_short_keywords_do_not_match_inside_a_word():
    # THE trap. Alnum-normalization JOINS words and manufactures substrings that were never in the name,
    # so a short keyword matched as a substring silently excludes real flower. Each of these is a live
    # product name, and each matched a keyword across a joined word boundary during the port:
    #   "Panda: Blue Sugar"  -> "pan-DAB-luesugar"        "3 Bros | Indoor" -> "3b-ROSIN-door"
    #   "Cap Junkie" + Flower -> "jun-KIEF-lower"          "Gunpowder"       -> "powder"
    for name in ("Panda: Blue Sugar", "Candy Cartel", "Beezwax Biscotti", "Poddy Mouth",
                 "3 Bros | Indoor Flower | 3.5g | Lemon Diesel", "Tier 1 - Cap Junkie -",
                 "BLACK PEARL - Gunpowder - Cereal Milk"):
        assert normalize_obtention(name, "Flower") is None, name
    # …while the same words, as WORDS, still match.
    assert normalize_obtention("Kief Dusted Nugs", "Flower") == "Infused"
    assert normalize_obtention("Live Rosin Coated Bud", "Flower") == "Infused"
    assert normalize_obtention("Wax Melts", "Flower") == "Infused"


def test_normalize_obtention_is_silent_when_the_name_is_silent():
    # 96.35% of Flower rows say nothing. NULL means "neither the name nor the category declares a
    # method" — NOT "we determined this is natural". There is no `Natural` value precisely so that this
    # cannot become 903,819 manufactured observations (cf. product_type_defaulted, and the E1 retraction).
    for name in ("Blue Dream 3.5g", "Black Diamond", "Wedding Cake | 1/8oz"):
        assert normalize_obtention(name, "Flower") is None, name
    assert normalize_obtention(None, None) is None
    assert normalize_obtention("", "") is None


def test_normalize_obtention_flags_a_mislabelled_extract():
    # A cartridge in the Flower category is a STORE's mislabel on the platform; the raw category really
    # does say Flower and we record it faithfully. The name is what gives it away.
    assert normalize_obtention("Live Resin Cartridge", "Flower") == "Extracted"
    assert normalize_obtention("Runtz Pods", "Flower") == "Extracted"


def test_normalize_obtention_infused_wins_when_a_name_says_both():
    # `Distillate Syringe` names an adulterant (Infused) AND a vessel (Extracted). Infused wins, because
    # it is first in the YAML and YAML order IS the priority.
    #
    # Worth being honest about: the retired regex had ONE exclusion list, so Infused-vs-Extracted is a
    # distinction this facet INTRODUCED and the old data cannot adjudicate. It does not affect the guard
    # — `obtention_std IS NULL` treats both identically, which is all NATURAL_FLOWER_WHERE ever asked —
    # so the ordering is pinned here to make the choice deliberate rather than incidental. If a future
    # analysis ever splits on the two values, THAT is when this needs evidence rather than a convention.
    assert normalize_obtention("Distillate Syringe", "Flower") == "Infused"
