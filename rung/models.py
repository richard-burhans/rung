from dataclasses import dataclass, field


@dataclass
class DispensaryRecord:
    """One dispensary location from any scrape source."""

    source: str  # pdf | csv | kml | arcgis | html | lookup | ca_dcc | az_dhs | co_med |
    # ma_ccc | ab_aglc | sk_slga | bc_lcrb (static list handlers; on_agco delegates to arcgis) ·
    # ai (AI fallback)
    name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    phone: str | None = None
    website: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class CompanyReconRecord:
    """One recon probe result for a cannabis company."""

    company_id: int
    canonical_name: str
    homepage_url: str | None = None
    platform: str | None = None
    confidence: str | None = None
    http_status: int | None = None
    error: str | None = None


@dataclass
class CompanyStoreRecord:
    """One store scraped from a company's OWN website/API.

    The company's own site is the trusted source of truth; these rows are compared
    against the state-published dispensaries to surface where the state list lags.
    """

    company_id: int
    canonical_name: str
    state: str
    source: str  # discovery-mechanism label (the rung's `source`, not always its method
    # name): next_data | jsonld | embedded_json | address_blocks | sibling_blocks |
    # line_blocks | jane | dutchie_directory | dutchie_plus | curaleaf_api |
    # fluent_locations | sweed_stores | weedmaps_directory | leafly_directory |
    # sitemap | browser | ai_llm (illustrative, not exhaustive)
    name: str | None = None
    address: str | None = None
    city: str | None = None
    zip_code: str | None = None
    phone: str | None = None
    website: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # Scrape handle — what the NEXT stage needs to pull this store's menu/products.
    platform: str | None = None      # menu platform for Stage 3 (jane | dutchie |
    # dutchie_plus | sweedpos | unknown | the recon-detected platform | ...)
    external_id: str | None = None   # the store's id on that platform
    store_url: str | None = None     # per-store page / menu URL


@dataclass
class StoreProductRecord:
    """One menu product scraped from a store's menu platform (Stage 3).

    A snapshot row: each scrape pass replaces the store's products wholesale
    (menus churn daily, so there is nothing to merge). ``store_key`` is the
    stable store handle — ``company_stores.id`` is regenerated on every
    keep-the-best replace, so products key on ``{platform}:{external_id}``.
    """

    company_id: int
    state: str
    store_key: str       # stable store handle: "{platform}:{external_id}"
    platform: str        # menu platform the products came from
    external_id: str     # the store's id on that platform
    source: str          # winning menu rung: jane_algolia | dutchie_products | dutchie_plus_menu
    # | trulieve_rest | cresco_api | sweedpos_ssr | hytiva_api | weedmaps_menu | leafly_menu
    # (illustrative, not exhaustive)
    external_product_id: str | None = None
    name: str | None = None
    brand: str | None = None
    category: str | None = None          # the platform's raw category string (preserved)
    category_std: str | None = None      # canonical cross-platform category (text.normalize_category)
    strain_type: str | None = None      # the platform's raw strain string (preserved)
    strain_type_std: str | None = None  # canonical lineage facet (text.normalize_strain_type):
    # Indica | Sativa | Hybrid | CBD | None — None when the raw value is a category word or a
    # bare strain/brand name (the raw column is polluted), not a forced bucket.
    product_type_std: str | None = None  # canonical 2nd-level product type within category_std
    # (text.normalize_product_type from the name + raw category): e.g. Concentrate -> Live Resin,
    # Vape -> Cartridge, Edible -> Gummies. "Unspecified" within a covered category whose name
    # carries no form; None for categories not yet covered (see docs/product_type_hierarchy.md).
    price: float | None = None          # lowest current shelf price across variants
    size_g: float | None = None         # representative weight in grams (normalize.enrich_record):
    # the smallest variant size — None for count-priced products (edibles sold "each"). Per-variant
    # size_g/price_per_g are stamped inside the `variants` JSONB.
    thc: float | None = None            # potency as published (percent)
    cbd: float | None = None
    # Per-dose milligram potency for mg-dosed products (edibles, tinctures, beverages)
    # where the platform publishes mg instead of a percent. Kept separate from thc/cbd so
    # the percent columns stay a single clean unit; a product carries one form or the other
    # — enforced in the DB by store_products_potency_unit_check (a cannabinoid is percent OR mg).
    thc_mg: float | None = None
    cbd_mg: float | None = None
    terpenes: list[dict] | None = None  # [{"name": ..., "value": ...}] as published
    # Canonical cross-platform terpenes (normalize.normalize_terpenes): {Name: percent} with
    # alias names folded, α+β-pinene summed, mg/g converted to %; terp_total = their sum.
    terpenes_std: dict | None = None
    terp_total: float | None = None
    # Minor cannabinoids beyond the thc/cbd headline, as a canonical {NAME: percent} map
    # (e.g. {"CBG": 0.37, "CBN": 0.1, "CBC": 0.2}); percent-only, only the entries a platform
    # publishes. Captured where the menu exposes per-cannabinoid values (Jane lab_results,
    # Hytiva/Cresco potency blocks, Weedmaps aggregates); None when none are published.
    cannabinoids_std: dict | None = None
    # Per-size detail as published: [{option, price, ...}] — rec/med split, promo
    # prices etc. stay here, platform-shaped; `price` above is the cross-variant low.
    # normalize.enrich_variants also stamps each variant's size_g/price_per_g here.
    variants: list[dict] | None = None


@dataclass(frozen=True)
class LocationObservation:
    """One store-lifecycle observation: what was seen at a physical location in one pass.

    The unit the shared history engine (``db.record_location_observations``) consumes.
    ``location_key`` is the stable physical-location identity (``dedupe.geo_key`` /
    ``address_key``) — computed by the CALLER, since the key builders live above ``db``
    in the layering. ``operator`` is the raw scraped name (canonicalized at read — see
    docs/store_history_design.md); ``platform``/``external_id`` are the menu handle when
    the source carries one (the state roster doesn't).
    """

    location_key: str
    state: str
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    city: str | None = None
    zip_code: str | None = None
    operator: str | None = None
    storefront_name: str | None = None
    platform: str | None = None
    external_id: str | None = None


@dataclass
class StateProgramRecord:
    """Coverage record for one state's (or Canadian province's) cannabis dispensary program."""

    abbr: str            # two-letter state/province code (USPS and CA codes don't collide)
    name: str
    programs: str        # 'none' | 'cbd_only' | 'medical' | 'recreational' | 'both'
    program_term: str
    agency: str
    best_url: str | None = None         # best discovered source URL (evidence)
    source_type: str | None = None      # 'pdf' | 'html' | 'map' | 'api'
    all_gov_urls: list[str] = field(default_factory=list)  # all .gov URLs found
    last_checked: str | None = None     # ISO-8601 UTC — when best_url was last verified
    check_status: str = "never"         # 'ok' | 'failed' | 'never'
    searched_at: str | None = None      # ISO-8601 UTC — when last search was run
    error: str | None = None
    # Dispensary-list resource discovered by crawling the landing page (best_url).
    list_url: str | None = None         # URL of the dispensary list/locator resource
    list_type: str | None = None        # the extract.ListType vocabulary: html|pdf|csv|kml|arcgis|lookup
    # (from state_lists._classify) OR a per-state custom-handler type from states.yml
    # (az_dhs/ca_dcc/co_med/ma_ccc/on_agco/ab_aglc/bc_lcrb/sk_slga)
    list_found_at: str | None = None    # ISO-8601 UTC — when the list URL was discovered
    list_status: str | None = None      # 'found' | 'override' | 'none'
    country: str = "US"                 # ISO-3166 alpha-2 (US | CA) — lets exports/analyses
    # partition by country / derive currency without a hardcoded province set (canada_expansion D1/D2)
