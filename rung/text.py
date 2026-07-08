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
_ARTICLES = frozenset({"the", "a", "an"})
# A single trailing generic descriptor, stripped once so "Swade Cannabis Dispensary" and
# "Swade Cannabis" collapse to the same operator. Order matters (longest first).
_TRAILING_GENERIC_RE = re.compile(
    r"\s+(?:cannabis\s+dispensary|marijuana\s+dispensary|cannabis\s+shop|dispensaries|"
    r"dispensary|marijuana|cannabis|marketplace)$",
    re.IGNORECASE,
)


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
    # Drop one trailing generic descriptor, but never reduce the name to nothing or to a bare
    # article ("The Dispensary" must stay "The Dispensary", not collapse to "The").
    stripped = _TRAILING_GENERIC_RE.sub("", normalized).strip()
    brand = stripped if stripped and stripped.lower() not in _ARTICLES else normalized
    return _strip_storefront_city(brand, city)
