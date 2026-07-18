"""Shared text helpers: dispensary/company name normalization, the product taxonomy/terpene
normalizers, and the master-product identity hash (`product_fingerprint`)."""

import hashlib
import re
from pathlib import Path

import yaml


def as_dict(value: object) -> dict:
    """Narrow a maybe-dict (e.g. an ``obj.get(...)`` over untrusted JSON) to a dict for safe ``.get``."""
    return value if isinstance(value, dict) else {}


def name_of(value: object) -> str | None:
    """A platform "name-ish" field — sometimes a bare string, sometimes ``{"name": …}`` — as a
    stripped non-empty string, or None."""
    if isinstance(value, dict):
        value = value.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else None

# Some sources separate the brand from a location with an en or em dash where a hyphen is meant
# ("AYR Dispensary — Columbus"); both fold to a plain hyphen so the " - " split strips the suffix.
EN_DASH = "–"
_EM_DASH = "—"

# Multi-alias rosters (arcgis MO/OR/WA) cram several names into one field, joined by ';' or ','
# ("Good Day Farm Dispensary; Gdf Dispensary; Good Day Farm", "TODAY'S HERBAL CHOICE, INC.",
# "Lucid Auburn, 21+ Cannabis"). The first segment is the operative brand; the rest are
# abbreviations, legal suffixes, locations, or restatements. (No clean-source canonical_name
# contains ';', so this never affects PA/IL/OH/MD/FL.)
_ALIAS_DELIM_RE = re.compile(r"\s*[;,]\s*")
# " at " as a storefront-location separator ("Value Buds at Baseline Village"), folded like " - "
# but guarded (see extract_brand) because "at" is a common word.
_AT_SEPARATOR_RE = re.compile(r"\s+at\s+", re.IGNORECASE)
_ARTICLES = frozenset({"the", "a", "an"})
# A single trailing generic descriptor, stripped once so "Swade Cannabis Dispensary" and
# "Swade Cannabis" collapse to the same operator. Order matters (longest first).
# Longest alternatives FIRST — regex alternation is first-match, so "medical marijuana dispensary"
# must precede "marijuana dispensary", which must precede "dispensary". Get this order wrong and
# "RISE Medical Marijuana Dispensary" strips to "RISE Medical" instead of "RISE".
_TRAILING_GENERIC_RE = re.compile(
    r"\s+(?:medical\s+marijuana\s+dispensary|medical\s+cannabis\s+dispensary|medical\s+dispensary|"
    r"cannabis\s+dispensary|marijuana\s+dispensary|cannabis\s+shop|dispensaries|"
    r"dispensary|marijuana|cannabis|marketplace)$",
    re.IGNORECASE,
)

# A trailing LEGAL-ENTITY suffix — the licensee/storefront gap, and the reason a state roster and an
# operator's own site describe the same shop under two names.
#
# A licence is issued to a legal person ("ADEGOKE HOLDINGS LLC", "Patriot Care Corp", "Smacked LLC");
# the shop trades under a brand ("Adegoke", "Patriot Care", "Smacked"). The roster files the former and
# the company's own site publishes the latter, so the two never key together and BOTH sides report a
# phantom — ours as `site_only` ("the state list is missing this store"), the roster's as `state_only`
# ("possible closure"). Measured on the live DB, 2026-07-14: stripping this recovers **324 roster rows
# across 18 jurisdictions** (MT 96, NY 76, MA 44, CO 26, CA 23, OR 23, …).
#
# Stripped REPEATEDLY and BEFORE the generic descriptor, because both stack in one name:
# "FLOYD'S CANNABIS COMPANY" -> (legal) "FLOYD'S CANNABIS" -> (generic) "FLOYD'S", which is what the
# company's own site calls it. Repeat because "Aurora Cannabis Enterprises Inc." carries two.
#
# Every strip is guarded by `_keeps_a_brand`, and that guard is load-bearing here: "Elixir Holdings"
# folds to "Elixir", so a strip that ate the whole name would collapse unrelated licensees into one
# phantom operator — the same failure the numeric guard exists to prevent.
_LEGAL_ENTITY_RE = re.compile(
    r"[,\s]+(?:l\.?l\.?c\.?|inc\.?|incorporated|corp\.?|corporation|co\.|company|holdings?|group|"
    r"enterprises?|ventures?|partners|l\.?l\.?p\.?|lp|ltd\.?|limited|management|properties|"
    r"investments?)\.?$",
    re.IGNORECASE,
)


def _is_bare_generic(value: str) -> bool:
    """True when `value` is *only* a generic descriptor — "Cannabis", "Dispensary", "Marijuana".

    Such a string is not a brand; it is the word every brand contains. Keying on it folds unrelated
    operators into one phantom company — the same failure `_keeps_a_brand` refuses for bare numbers.
    `_TRAILING_GENERIC_RE` needs leading whitespace, so probe it against a padded copy.
    """
    return not _TRAILING_GENERIC_RE.sub("", f" {value}").strip()


def strip_legal_entity(name: str) -> str:
    """Drop trailing legal-entity suffixes ("Adegoke Holdings LLC" -> "Adegoke"), repeatedly.

    **Never eats the name.** Two strips are refused, and the second was found by measuring the fold
    against the live roster rather than by imagining it:

    * one that leaves nothing usable (`_keeps_a_brand`) — "Holdings LLC" stays as it is;
    * one that leaves a **bare generic** — BC's *"Cannabis Co."* would strip to *"Cannabis"* and
      collide with *"Cannabis 247"*, merging two unrelated licensees into one phantom operator. The
      suffix is only noise when a real brand survives it.
    """
    current = name.strip()
    while True:
        stripped = _LEGAL_ENTITY_RE.sub("", current).strip(" ,.")
        if stripped == current or not _keeps_a_brand(stripped) or _is_bare_generic(stripped):
            return current
        current = stripped


def load_company_aliases(path: Path, *, strict: bool = False) -> dict[str, str]:
    """Flat ``{alias: canonical}`` map from a companies.yml-shaped file.

    The one loader for company-brand aliases, shared by the seeding step and the
    read-only consumers (compare). ``strict=True`` (seeding) raises on a
    missing/malformed file; ``strict=False`` returns ``{}`` and skips bad entries.
    """
    if not path.is_file():
        if strict:
            raise FileNotFoundError(f"Canonicalization file not found: {path}")
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"Expected a mapping at the top level of {path}")
        return {}
    aliases: dict[str, str] = {}
    for canonical, names in data.items():
        if not isinstance(names, list):
            if strict:
                raise ValueError(
                    f"Aliases for '{canonical}' must be a list, got {type(names).__name__}"
                )
            continue
        for alias in names:
            aliases[str(alias)] = str(canonical)

    # Index every alias under its FOLDED form too, because that is the key it will be looked up by.
    #
    # Callers look aliases up with `extract_brand(name)` as the key. Many aliases here are written in
    # the licence roster's own words — "Diamond Star Group inc." folds to Dankley, "Delta 9 Pittsburgh"
    # to Sunnyside — so once `extract_brand` learned to strip the legal-entity suffix, it started
    # handing the lookup "Diamond Star" while this map was still keyed on "Diamond Star Group inc.".
    # The alias silently stopped folding and the operator split back into two companies. Registering
    # both spellings keeps a legal-entity-shaped alias working, and costs nothing when the alias is
    # already a bare brand.
    #
    # `setdefault`: an explicit alias always wins over a derived one, so a fold can never quietly
    # re-point an alias that companies.yml states outright.
    for alias, canonical in list(aliases.items()):
        folded = extract_brand(alias)
        if folded and folded != alias:
            aliases.setdefault(folded, canonical)
    return aliases


_BRAND_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_brand(brand: str) -> str:
    """A spelling-insensitive grouping key for a brand.

    Lower-cases and strips all whitespace and punctuation, so spacing/case/punctuation
    variants of one operator collapse to a single key — "Zen Leaf" / "ZenLeaf" / "zen-leaf"
    -> "zenleaf", "NuEra" / "nuEra" -> "nuera", "EarthMed" / "Earthmed" -> "earthmed". This
    folds the variants that `extract_brand` + the companies.yml aliases miss (those handle
    generic descriptors and known aliases, not bare spelling drift). Returns the lower-cased
    trimmed original if stripping would leave nothing (a name that is all punctuation).
    """
    key = _BRAND_NORMALIZE_RE.sub("", brand.lower())
    return key or brand.lower().strip()


def product_fingerprint(
    brand: str | None, name: str | None, size_g: float | None, product_type_std: str | None,
    *, thc_mg: float | None = None, cbd_mg: float | None = None,
) -> str | None:
    """A stable identity for a product across stores and time (master-product DB).

    Hashes the IDENTITY tuple — normalized brand + collapsed lower-cased name + size + 2nd-level
    type. Percent potency and terpenes are deliberately EXCLUDED: they vary harvest-to-harvest, so
    they are the *observed* values (`product_observations`), not the product's identity.

    For mg-dosed products (edibles/tinctures/beverages/capsules) the manufactured **dose**
    (``thc_mg``/``cbd_mg``) IS part of identity — a 10 mg vs 100 mg gummy of the same name are
    different products, and weight-sold categories carry no ``size_g`` to tell them apart. The dose is
    folded in ONLY when present, so weight-sold products (flower/vape/concentrate, mg=None) keep their
    original v1 hash (backward-compatible). Returns ``None`` when there is no name to key on.
    """
    clean_name = " ".join((name or "").lower().split())
    if not clean_name:
        return None
    size = f"{size_g:.2f}" if size_g is not None else ""
    parts = [normalize_brand(brand or ""), clean_name, size, product_type_std or ""]
    if thc_mg is not None or cbd_mg is not None:
        parts += [f"thc_mg={thc_mg or 0:.2f}", f"cbd_mg={cbd_mg or 0:.2f}"]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def terpene_floats(value: object) -> dict[str, float]:
    """Coerce a stored ``terpenes_std`` jsonb value to a typed ``{name: percent}`` dict, dropping
    non-numeric/None entries. Centralizes the dynamic-jsonb→typed-dict step so analysis scripts get a
    properly-typed mapping (no per-script ``isinstance``/``object`` juggling)."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for name, percent in value.items():
        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
            out[str(name)] = float(percent)
    return out


def dominant_terpene(value: object) -> str | None:
    """The highest-valued terpene in a ``terpenes_std`` jsonb value, or None if empty/non-numeric."""
    floats = terpene_floats(value)
    return max(floats, key=lambda name: floats[name]) if floats else None


def readability_key(spelling: str) -> tuple[int, int, str]:
    """The deterministic "most readable spelling" sort key for a folded brand group: more spaces,
    then longer, then alphabetical — so "Zen Leaf" beats "ZenLeaf" on a tie. Shared so the seed
    (``seed_companies.dominant_spelling``, frequency-first then this) and the report
    (``compare._better_display``) break ties the same way and name an operator consistently."""
    return (spelling.count(" "), len(spelling), spelling)


# ── placeholder / junk-row detection (shared by extract, seed_companies, compare) ──
# Dirty Stage-1 rosters (license PDFs/CSVs) carry non-operator rows: header labels
# ("Dispensary Name"), test/demo/sandbox entries, [Equity Retailer] tags, "Data Not Available"/
# "TBD" stubs, and bare license numbers ("284.000141-CL"). One predicate so they are dropped at
# extraction, never seeded as a company, and never matched in compare.
_PLACEHOLDER_RE = re.compile(
    r"\[equity retailer\]|\bdnu\b|\bdo not use\b|\bsandbox\b|\bdemo\b|\btest\b|\btbd\b"
    r"|data not available|integrations lab|\(flagged|\bdispensary name\b"
    r"|conditional license number|\bduplicate\b",
    re.IGNORECASE,
)
_LICENSE_RE = re.compile(r"^\d{2,}[\d.\-]*(-cl)?$|\.cl\.\d|^\d{3}\.\d{3}", re.IGNORECASE)


def is_placeholder_name(name: str | None) -> bool:
    """True for a non-operator junk row — a test/demo/equity-tag/no-data stub, a roster header
    label, or a bare license number — that must not become a dispensary or a company."""
    if not name or not name.strip():
        return True
    return bool(_PLACEHOLDER_RE.search(name) or _LICENSE_RE.search(name.strip()))


# ── product-category normalization (the cross-platform `category_std` standard) ──

_CATEGORY_ALIASES_PATH = Path(__file__).parent / "data" / "category_aliases.yml"
_category_rules_cache: list[tuple[str, tuple[str, ...]]] | None = None


def _category_key(value: str) -> str:
    """Lower-case + strip to alphanumerics, so 'Pre-Rolls'/'pre rolls'/'PreRoll' all match."""
    return _BRAND_NORMALIZE_RE.sub("", value.lower())


def load_category_rules(
    path: Path = _CATEGORY_ALIASES_PATH,
) -> list[tuple[str, tuple[str, ...]]]:
    """Ordered ``[(canonical, (keyword, ...)), ...]`` from category_aliases.yml.

    YAML key order is the match PRIORITY (first canonical with a substring hit wins). Keywords
    are alnum-normalized to match the way normalize_category normalizes its input.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at the top level of {path}")
    return [
        (str(canonical), tuple(_category_key(str(k)) for k in (keywords or [])))
        for canonical, keywords in data.items()
    ]


_CATEGORY_NAME_OVERRIDES_PATH = Path(__file__).parent / "data" / "category_name_overrides.yml"
_category_overrides_cache: list[tuple[str, tuple[str, ...], frozenset[str | None]]] | None = None


def load_category_name_overrides(
    path: Path = _CATEGORY_NAME_OVERRIDES_PATH,
) -> list[tuple[str, tuple[str, ...], frozenset[str | None]]]:
    """Ordered ``[(to_category, (keyword, ...), from_categories), ...]`` from
    category_name_overrides.yml — name-keyword category corrections applied after the raw-category
    pass. Keywords are alnum-normalized like normalize_category's input; ``from_categories`` is the
    set of current category_std values the rule may override (``None`` = blank/uncategorized)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Expected a list of rules at the top level of {path}")
    rules: list[tuple[str, tuple[str, ...], frozenset[str | None]]] = []
    for rule in data:
        keywords = tuple(_category_key(str(k)) for k in (rule.get("keywords") or []))
        from_cats = frozenset(
            None if f is None else str(f) for f in (rule.get("from") or [])
        )
        rules.append((str(rule["to"]), keywords, from_cats))
    return rules


def _override_category(name: str, base: str | None) -> str | None:
    """A name-driven category that overrides ``base`` (the raw-category result), or None.

    The product NAME names a form the platform mis-bucketed, and ``base`` is among the rule's
    overridable ``from`` categories (so a correct higher-format bucket is never clobbered)."""
    global _category_overrides_cache
    key = _category_key(name)
    if not key:
        return None
    if _category_overrides_cache is None:
        _category_overrides_cache = load_category_name_overrides()
    for to_category, keywords, from_cats in _category_overrides_cache:
        if base in from_cats and base != to_category \
                and any(kw and kw in key for kw in keywords):
            return to_category
    return None


def normalize_category(raw: str | None, name: str | None = None) -> str | None:
    """Canonical product category for a platform's raw `category` string (None if blank).

    Cross-platform standard backing unified search: returns the first canonical (by the
    category_aliases.yml priority order) whose any keyword is a substring of the alnum-normalized
    raw value, else "Other" — never a silent mis-bucket into a real category. When ``name`` is
    given, a name-keyword override (category_name_overrides.yml) can correct a form the platform
    mislabeled in its raw category (a capsule sold as an "edible"); see ``_override_category``.
    """
    global _category_rules_cache
    base: str | None = None
    if raw and raw.strip() and (key := _category_key(raw)):
        if _category_rules_cache is None:
            _category_rules_cache = load_category_rules()
        base = "Other"
        for canonical, keywords in _category_rules_cache:
            if any(kw and kw in key for kw in keywords):
                base = canonical
                break
    if name and (override := _override_category(name, base)) is not None:
        return override
    return base


def category_overridden(raw: str | None, name: str | None) -> bool:
    """True when :func:`normalize_category` CORRECTED the category from the NAME, not the raw category.

    The same shape as :func:`product_type_defaulted`: ``category_std`` conflates two provenances. When
    the platform's raw ``category`` decides the bucket, the label is the platform's own. When
    ``category_name_overrides.yml`` fires — a capsule the platform sold as an "edible", a disposable it
    called a "cartridge" — the bucket is OUR correction from the product name, and downstream the two are
    spelled identically. The override is a defensible correction, not an observation of what the platform
    published; an analysis of platform mislabeling (or one that trusts ``category_std`` as the platform's
    voice) must be able to tell them apart. Recomputes the raw-only base and asks whether the name flips it.
    """
    base = normalize_category(raw, None)
    return bool(name) and _override_category(name, base) is not None


# ── product-type normalization (the 2nd-level `product_type_std` standard) ──────

_PRODUCT_TYPE_ALIASES_PATH = Path(__file__).parent / "data" / "product_type_aliases.yml"
# The reserved top-level YAML key holding per-category no-match fallback labels (not a category).
_PRODUCT_TYPE_DEFAULTS_KEY = "_defaults"
_product_type_rules_cache: dict[str, list[tuple[str, tuple[str, ...]]]] | None = None
_product_type_defaults_cache: dict[str, str] | None = None


def load_product_type_rules(
    path: Path = _PRODUCT_TYPE_ALIASES_PATH,
) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    """``{category_std: [(product_type, (keyword, ...)), ...]}`` from product_type_aliases.yml.

    Nested one level under category (the row's category_std selects the rule list); within a
    category the YAML key order is the match PRIORITY. Keywords are alnum-normalized like the input.
    The reserved ``_defaults`` key (per-category fallback labels) is not a category and is excluded.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at the top level of {path}")
    return {
        str(category): [
            (str(ptype), tuple(_category_key(str(k)) for k in (keywords or [])))
            for ptype, keywords in (types or {}).items()
        ]
        for category, types in data.items()
        if category != _PRODUCT_TYPE_DEFAULTS_KEY
    }


def load_product_type_defaults(path: Path = _PRODUCT_TYPE_ALIASES_PATH) -> dict[str, str]:
    """``{category_std: fallback label}`` from the YAML ``_defaults`` key — the no-match label for a
    category where the unqualified product genuinely IS a known form (Flower→Bud). Empty if absent."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults = data.get(_PRODUCT_TYPE_DEFAULTS_KEY) if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in (defaults or {}).items()}


def normalize_product_type(
    name: str | None, category: str | None, category_std: str | None
) -> str | None:
    """Canonical 2nd-level product type for a row, within its ``category_std``.

    Matches the alnum-normalized product NAME (plus the raw ``category``, which on some platforms
    carries the form) against the per-category keyword rules. Returns the first hit (priority
    order); on no match, the category's ``_defaults`` label if one is set (e.g. Flower→Bud), else
    ``"Unspecified"`` (honest, not guessed); ``None`` for a category not in product_type_aliases.yml.
    See docs/product_type_hierarchy.md.
    """
    global _product_type_rules_cache, _product_type_defaults_cache
    if _product_type_rules_cache is None:
        _product_type_rules_cache = load_product_type_rules()
        _product_type_defaults_cache = load_product_type_defaults()
    rules = _product_type_rules_cache.get(category_std or "")
    if rules is None:
        return None
    key = _category_key(f"{name or ''} {category or ''}")
    for product_type, keywords in rules:
        if any(kw and kw in key for kw in keywords):
            return product_type
    return (_product_type_defaults_cache or {}).get(category_std or "", "Unspecified")


def product_type_defaulted(
    name: str | None, category: str | None, category_std: str | None
) -> bool:
    """True when :func:`normalize_product_type` MANUFACTURED the label from absence rather than reading it.

    **This is the `strain_type` bug's shape, and it lives in our own code.** Weedmaps shipped a *defaulted*
    lineage field — 98.2% "Indica" — we read it as an observation, and we retracted a whole finding (E1)
    when it turned out to be a platform default. `_defaults` in `product_type_aliases.yml` does the same
    thing on our side of the wire: when no keyword matches, `Flower` becomes **`Bud`**, `Pre-Roll` becomes
    **`Single`**. **759,927 flower rows — 82.5% of them — carry a `Bud` that no name ever said**, and
    downstream it is indistinguishable from a `Bud` we actually read.

    The label is a defensible *prior* ("an unqualified flower is probably whole bud"). It is not an
    observation, and the two must not be spelled the same. `bowker-star_1999_sorting-things-out` names the
    failure exactly — a residual category that has been quietly promoted into a positive assertion — and
    the fix it licenses is this predicate: keep the convenient default, but let every consumer *know* it
    was a default. An analysis that reports "82% of flower is whole bud" is otherwise reporting our
    fallback rule, not the market.
    """
    global _product_type_rules_cache, _product_type_defaults_cache
    if _product_type_rules_cache is None:
        _product_type_rules_cache = load_product_type_rules()
        _product_type_defaults_cache = load_product_type_defaults()
    rules = _product_type_rules_cache.get(category_std or "")
    if rules is None:
        return False                       # no rules for this category: nothing was manufactured
    key = _category_key(f"{name or ''} {category or ''}")
    if any(kw and kw in key for product_type, keywords in rules for kw in keywords):
        return False                       # a keyword actually matched — this label was READ
    # No match. It was defaulted iff this category HAS a default (else it is an honest "Unspecified").
    return (category_std or "") in (_product_type_defaults_cache or {})


# ── strain-type normalization (the cross-platform `strain_type_std` standard) ──

_STRAIN_ALIASES_PATH = Path(__file__).parent / "data" / "strain_aliases.yml"
_strain_rules_cache: list[tuple[str, tuple[str, ...]]] | None = None


def load_strain_rules(
    path: Path = _STRAIN_ALIASES_PATH,
) -> list[tuple[str, tuple[str, ...]]]:
    """Ordered ``[(canonical, (keyword, ...)), ...]`` from strain_aliases.yml.

    YAML key order is the match PRIORITY (first canonical with a substring hit wins). Keywords
    are alnum-normalized to match the way normalize_strain_type normalizes its input.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at the top level of {path}")
    return [
        (str(canonical), tuple(_category_key(str(k)) for k in (keywords or [])))
        for canonical, keywords in data.items()
    ]


def normalize_strain_type(raw: str | None) -> str | None:
    """Canonical strain type (Indica/Sativa/Hybrid/CBD) for a raw `strain_type` string.

    Cross-platform facet backing unified search: returns the first canonical (by the
    strain_aliases.yml priority order) whose any keyword is a substring of the alnum-normalized
    raw value, else **None**. Unlike normalize_category's "Other" fallback, no-match is None —
    the raw column is polluted with product categories and bare strain names, so an unmatched
    value means "no recognizable strain type", not a residual bucket.

    **This field is a MARKETING label, not a taxon, and the field's own standard reference says so.**
    Clarke & Merlin's *Cannabis: Evolution and Ethnobotany* (the monograph the sativa/indica literature
    rests on) concludes that **C. sativa is the HEMP species and ALL drug varieties are C. indica** —
    "we now know that all drug varieties, regardless of their origin or gross phenotype, belong to
    Cannabis indica" (Ch.10, pp.300-301). A retail "sativa" is *C. indica* ssp. *indica*; a retail
    "indica" is ssp. *afghanica*. **Both poles of the contrast are one species.** The vocabulary has a
    datable, non-taxonomic origin: late-1970s growers called narrow-leaflet varieties "sativas" because
    they *resembled* NLH varieties in gross phenotype — leaflet-shape shorthand, never a determination.
    By the mid-1980s "the vast majority of all illicitly produced sinsemilla had probably received some
    portion of its genome from the BLD gene pool" (p.301) — the names lost their referent because the
    populations merged.

    So: normalize it, search on it, never treat it as lineage. Two independent reasons, and they stack —
    the vocabulary names no taxon (above), **and** the raw field is defaulted on at least one platform
    (Weedmaps: 98.2% "Indica", ~296k wrong rows; a lineage finding built on it was retracted, which is
    why ``reference_db.TRUSTED_LINEAGE_WHERE`` exists). See
    ``research/papers_md/summaries/clarke-merlin_2013_evolution-ethnobotany.md`` and
    ``reports/synthesis/identity_and_chemovars.md``.
    """
    global _strain_rules_cache
    if not raw or not raw.strip():
        return None
    key = _category_key(raw)
    if not key:
        return None
    if _strain_rules_cache is None:
        _strain_rules_cache = load_strain_rules()
    for canonical, keywords in _strain_rules_cache:
        if any(kw and kw in key for kw in keywords):
            return canonical
    return None


# ── obtention normalization (the `obtention_std` facet) ──────────────────────────────────────────

_OBTENTION_ALIASES_PATH = Path(__file__).parent / "data" / "obtention_aliases.yml"
_obtention_rules_cache: list[tuple[str, tuple[str, ...], re.Pattern[str] | None]] | None = None


def load_obtention_rules(
    path: Path = _OBTENTION_ALIASES_PATH,
) -> list[tuple[str, tuple[str, ...], re.Pattern[str] | None]]:
    """Ordered ``[(canonical, (contains_kw, ...), words_re | None), ...]`` from obtention_aliases.yml.

    YAML key order is the match PRIORITY. Two keyword kinds, and the split is not cosmetic:
    ``contains`` matches a substring of the ALNUM-NORMALIZED text (so "In fused" -> "infused");
    ``words`` matches whole words against the RAW text, because alnum-normalization joins words and
    manufactures substrings — "Panda: Blue Sugar" contains "dab".
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at the top level of {path}")
    rules: list[tuple[str, tuple[str, ...], re.Pattern[str] | None]] = []
    for canonical, spec in data.items():
        if not isinstance(spec, dict):
            raise ValueError(f"{path}: {canonical!r} must map to `contains:`/`words:` lists")
        contains = tuple(_category_key(str(k)) for k in (spec.get("contains") or []))
        words = [re.escape(str(w)) for w in (spec.get("words") or [])]
        pattern = re.compile(rf"\b(?:{'|'.join(words)})\b", re.IGNORECASE) if words else None
        rules.append((str(canonical), contains, pattern))
    return rules


def normalize_obtention(name: str | None, category: str | None) -> str | None:
    """The obtention method a product's NAME or RAW category declares — else ``None``.

    Answers one question: *did the cannabinoids in this product come from the plant as grown?*
    ``Infused`` = added to the bud; ``Extracted`` = taken out of the plant; ``None`` = **neither the
    name nor the raw category says**.

    **There is no ``Natural`` value, deliberately.** 96.35% of Flower rows (903,819 of 938,042,
    measured 2026-07-16) declare no obtention method, and stamping those ``Natural`` would manufacture
    903,819 observations nobody made. That is the failure this project has shipped twice — Weedmaps
    ``strain_type`` defaulted to "Indica" (the E1 retraction) and our own ``product_type_std``
    ``_defaults`` putting a "Bud" on 759,927 rows no name ever said. So the vocabulary is positive-only,
    and ``reference_db.NATURAL_FLOWER_WHERE`` asks ``obtention_std IS NULL`` — "the source called this
    Flower and nothing contradicts it", which is a claim we can actually defend.

    Reads the platform's **raw** ``category``, never ``category_std``: 267 Flower rows carry the signal
    only there ("Infused Flower", "Moonrocks"). Deriving this facet from ``category_std`` would make it
    a function of the conflated field it exists to disentangle (see reports/obtention_facet_design.md).
    """
    global _obtention_rules_cache
    raw = " ".join(p for p in (name, category) if p)
    if not raw.strip():
        return None
    if _obtention_rules_cache is None:
        _obtention_rules_cache = load_obtention_rules()
    key = _category_key(raw)
    for canonical, contains, words_re in _obtention_rules_cache:
        if any(kw and kw in key for kw in contains):
            return canonical
        if words_re is not None and words_re.search(raw):
            return canonical
    return None


# Canonical terpenes surfaced as search columns (the common ones). Raw names vary wildly
# (Myrcene / Beta Myrcene / b_myrcene; Alpha Pinene / Beta Pinene / b-Pinene); collapse by
# substring after the alnum-strip — alpha+beta of one terpene fold to the single canonical.
TERPENE_COLUMNS = (
    "Myrcene", "Limonene", "Caryophyllene", "Pinene", "Linalool",
    "Terpinolene", "Humulene", "Ocimene", "Bisabolol",
    # Sesquiterpene alcohols tied to the Indica label's genetics (Watts 2021); widely reported
    # (guaiol ~63k / eudesmol ~4k flower) but previously dropped — see analysis #75.
    "Guaiol", "Eudesmol",
)
_TERPENE_KEYS = tuple((canon, canon.lower()) for canon in TERPENE_COLUMNS)


def normalize_terpene(raw: str | None) -> str | None:
    """Canonical terpene name for a raw label, or None if blank / not a tracked terpene.

    Folds casing + alpha/beta prefixes (`b_myrcene`/`Beta Myrcene` -> Myrcene; alpha+beta
    pinene -> Pinene). Returns one of TERPENE_COLUMNS, or None (rarer terpenes aren't columned).
    """
    if not raw:
        return None
    key = _category_key(raw)
    for canonical, needle in _TERPENE_KEYS:
        if needle in key:
            return canonical
    return None


# Trailing connector words that mean a city is part of the brand name, not a storefront suffix
# ("Harvest of Whitehall", "Green Cross of …") — don't strip the city in that case.
_CITY_CONNECTORS = frozenset({"of", "the", "at", "on", "in", "and", "by", "a", "&"})

# ── store-level suffixes ─────────────────────────────────────────────────────────────────────────
# A state roster names the LICENSEE of each STORE, so a multi-store operator arrives as many names
# that differ only by a per-store tail: "ONE PLANT BARRIE (CUNDLES)", "ONE PLANT 3003 DANFORTH",
# "CURALEAF GROTON LLC". `seed-companies` derives `companies` from that roster, so each became its
# own COMPANY — and Stage 2 then scraped the operator's ONE homepage once per company, giving each
# the operator's FULL store list. Measured 2026-07-13: 6,906 of 22,074 company_stores rows (31%) are
# a redundant copy of a rooftop another "company" already holds, and Ontario's One Plant alone
# fragmented into ~15 companies.
#
# So the brand fold has to strip a store-level tail, not just a city. Each rule below is deliberately
# narrow — a false merge joins two REAL operators, which is far worse than leaving one fragmented.

# A trailing parenthetical: "ONE PLANT BARRIE (CUNDLES)" -> "ONE PLANT BARRIE".
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")

# NOTE: no legal-entity rule here. A trailing "LLC"/"Inc." is a per-store legal entity and DOES
# fragment an operator ("CURALEAF GROTON LLC") — but `companies.yml` aliases are the designed
# mechanism for it, and they key on the FULL name including the suffix ("Diamond Star Group inc." ->
# Dankley). Stripping it here makes those alias keys miss, which seeds the entity as its OWN company:
# the exact fragmentation we are trying to remove. Fold legal entities in companies.yml, or teach the
# alias map to match on a stripped key first — do not strip it out from under the map.

# A trailing street address or store number: "ONE PLANT 3003 DANFORTH", "HIGH CANNABIS 12467".
# Requires THREE+ digits so a number that is part of the brand survives — "Cloud 9", "Green 2 Go",
# and (crucially) "Score 420 Alamogordo", whose 420 is mid-name and never trailing anyway.
_TRAILING_ADDRESS_RE = re.compile(r"\s+\d{3,}[A-Za-z0-9 .,'\-]*$")


def _strip_store_suffix(brand: str) -> str:
    """Drop one store-level tail (a parenthetical or a trailing street address) from a brand.

    Applied longest-first and guarded: a rule only fires if what remains is still a plausible brand
    (≥3 chars, contains a letter, and does not end on a dangling connector). Never empties the name —
    leaving an operator fragmented is the safe failure; merging two real operators is not.
    """
    for rule in (_TRAILING_PAREN_RE, _TRAILING_ADDRESS_RE):
        folded = rule.sub("", brand).strip().rstrip(",")
        if folded == brand:
            continue
        last = folded.lower().rsplit(" ", 1)[-1] if folded else ""
        if len(folded) >= 3 and any(ch.isalpha() for ch in folded) and last not in _CITY_CONNECTORS:
            brand = folded
    return brand


def _strip_storefront_city(brand: str, city: str | None) -> str:
    """Drop a trailing storefront city — "Zen Leaf Dayton" (+ own city "Dayton") -> "Zen Leaf".

    Only fires when the brand ENDS with this store's OWN city on a word boundary and stripping
    leaves a real brand: never empties it, and never strips when it would leave a dangling
    connector ("Harvest of Whitehall" must NOT become "Harvest of"). City-suffix folding needs
    the row's own city, so it's a no-op when ``city`` is absent (the default).
    """
    if not city or not city.strip():
        return brand
    city_lower = city.strip().lower()
    if not brand.lower().endswith(" " + city_lower):
        return brand
    folded = brand[: len(brand) - len(city_lower)].strip()
    last_token = folded.lower().rsplit(" ", 1)[-1] if folded else ""
    # Keep the full name when stripping would leave nothing, a dangling connector ("Harvest of
    # Whitehall"), or a too-short stub (≤2 chars like "JO"/"MJ" — a coincidental prefix risks
    # merging unrelated operators; a real short brand staying per-city is the safe failure).
    if len(folded) < 3 or last_token in _CITY_CONNECTORS:
        return brand
    return folded


def _keeps_a_brand(stripped: str) -> bool:
    """True when what remains after a generic-descriptor strip is still a usable brand.

    Three ways a strip can eat the name, and all three have been observed in the roster:

    * **nothing left** — the name *was* the descriptor;
    * **a bare article** — "The Dispensary" must stay "The Dispensary", not become "The";
    * **no letters left** — "123 Cannabis" must stay "123 Cannabis", not become "123". A brand that is
      a bare number folds every unrelated numeric name in the state into one phantom company, which is
      the very failure this whole area exists to prevent.
    """
    if not stripped or stripped.lower() in _ARTICLES:
        return False
    return any(ch.isalpha() for ch in stripped)


def extract_brand(name: str, city: str | None = None) -> str:
    """Extract the company brand from a full dispensary name.

    e.g. "Trulieve - Pittsburgh" -> "Trulieve" (en/em dash normalized to a hyphen first, so
    "AYR Dispensary — Columbus" -> "AYR" too — a storefront-suffix the separator strips).
    Multi-alias roster fields collapse to their first segment with a trailing generic
    descriptor stripped once: "Good Day Farm Dispensary; Gdf Dispensary" -> "Good Day Farm";
    "TODAY'S HERBAL CHOICE, INC." -> "TODAY'S HERBAL CHOICE".

    With the store's ``city``, a trailing storefront city is also dropped ("Zen Leaf Dayton" ->
    "Zen Leaf") so a multi-store operator's per-city names fold to one brand — see
    :func:`_strip_storefront_city` for the guards.
    """
    normalized = name.replace(EN_DASH, "-").replace(_EM_DASH, "-").strip()
    # Take the first ';'/',' segment (the operative brand) before any other handling.
    first = _ALIAS_DELIM_RE.split(normalized)[0].strip()
    if first:
        normalized = first
    if " - " in normalized:
        normalized = normalized.split(" - ")[0].strip()
    elif "- " in normalized:
        normalized = normalized.split("- ")[0].strip()
    # " at <storefront location>" is the separator some rosters use where the operator's own site
    # uses " - " — the AGLC files "Value Buds at Baseline Village"; the site is "Value Buds - Baseline
    # Village". Fold it the same way so both sides bucket to one operator. BUT "at" is a common word,
    # so guard on the prefix still reading as a brand: "Cannabis at the Green Brier" must NOT collapse
    # to the bare generic "Cannabis" (measured: 64/68 " at " names corpus-wide are this AB storefront
    # pattern; the 4 others fold correctly and the guard protects the lone bare-generic case).
    elif _AT_SEPARATOR_RE.search(normalized):
        candidate = _AT_SEPARATOR_RE.split(normalized, maxsplit=1)[0].strip()
        # Guard on the prefix surviving a generic strip as a real brand — "California Street Cannabis"
        # keeps "California Street", but a bare "Cannabis" strips to nothing, so "Cannabis at the Green
        # Brier" must NOT collapse into the mega-generic "Cannabis" bucket. The leading space lets the
        # (space-anchored) generic regex also catch a candidate that is ITSELF a lone generic word.
        if candidate and _keeps_a_brand(_TRAILING_GENERIC_RE.sub("", " " + candidate).strip()):
            normalized = candidate
    # Drop the legal-entity suffix BEFORE the generic one — a licence roster stacks both
    # ("FLOYD'S CANNABIS COMPANY"), and the generic strip only ever looks at the END of the name, so
    # a "Cannabis" hiding behind a "Company" is invisible to it. Same ordering lesson as the
    # storefront-city fold below, which fragmented one Virginia operator into ten companies.
    normalized = strip_legal_entity(normalized)
    # Drop one trailing generic descriptor, subject to the guards in `_keeps_a_brand`.
    stripped = _TRAILING_GENERIC_RE.sub("", normalized).strip()
    brand = stripped if _keeps_a_brand(stripped) else normalized
    # Store-level tail LAST, so it cannot cascade into the generic strip: "Ziggyz Cannabis
    # (MacArthur)" must fold to "Ziggyz Cannabis" (the brand), not to "Ziggyz" — the parenthetical
    # is the store, "Cannabis" is part of the name.
    brand = _strip_store_suffix(brand)
    folded = _strip_storefront_city(brand, city)

    # ONLY IF THE CITY WAS ACTUALLY REMOVED, look for a descriptor it was hiding.
    #
    # The generic strip above only ever sees the END of the name, so a descriptor sitting *behind* a
    # storefront city is invisible to it: "RISE Dispensaries Abingdon" -> (city) -> "RISE Dispensaries",
    # with the descriptor now exposed and never re-examined. That single ordering bug fragmented ONE
    # operator into TEN companies in Virginia ("RISE Dispensaries Abingdon", "RISE Dispensary Abingdon",
    # "RISE Medical Marijuana Dispensary Salem", …) — the same shape as the Ontario fragmentation, and
    # the W2 event study clusters on the operator, so ten phantom firms is not a cosmetic problem.
    #
    # The `folded != brand` guard is load-bearing, and is exactly the cascade the store-suffix fix warned
    # about: re-stripping unconditionally turns "Ziggyz Cannabis (MacArthur)" into "Ziggyz", when the
    # brand really is "Ziggyz Cannabis" — no city was removed there, so nothing was hidden, so the first
    # pass's verdict stands. We re-examine only what the city strip newly exposed.
    if folded != brand:
        restripped = _TRAILING_GENERIC_RE.sub("", folded).strip()
        if _keeps_a_brand(restripped):
            folded = restripped
    return folded


def geocode_query(address: str | None, city: str | None,
                  state: str | None, zip_code: str | None) -> str | None:
    """The one-line US address to geocode, or None when the row is too sparse to match.

    Needs a street plus at least a city, a ZIP, or a state. A street + state alone is ambiguous in a
    large state ("123 Main St, TX"), so a caller only trusts such a query when the geocoder returns
    exactly ONE match. That is what lets DC's roster (street only, no city, no ZIP) resolve, DC being
    a single city.

    **This doubles as the `geocode_cache` key, and that is why it lives here rather than in the
    backfill script** — the key must be computed identically by the writer (the backfill) and the
    reader (the post-re-scrape restore), so there is exactly one definition of it.

    The invariant that makes the cache hit: both paths key a row while it carries only what its
    SOURCE published. A roster row is geocoded precisely because it arrives with no ZIP, and the
    restore runs immediately after the re-scrape re-inserts it — both times the derived columns are
    still NULL, so both compute the same string. (A second backfill over an already-enriched row
    would key it differently and write a redundant cache entry; harmless, since the restore still
    looks up the source-only key.)
    """
    if not address or not (city or zip_code or state):
        return None
    parts = [" ".join(address.split())]
    if city:
        parts.append(" ".join(city.split()))
    locality = " ".join(p for p in ((state or "").strip(), (zip_code or "").strip()) if p)
    if locality:
        parts.append(locality)
    return ", ".join(parts)
