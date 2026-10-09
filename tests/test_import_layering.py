"""Guard tests: the public/private package boundary + the public package's internal layering.

After the Phase-3b carve-out the proprietary modules live in the sibling ``rung_intel``
package. The load-bearing contract is now a **package boundary**: the public ``rung``
core must import NOTHING from that overlay — that's what lets the open-source core ship and run on
its own (its proprietary stages then resolve to registry stubs). Within the public package the
original tier layering still holds: the base layer carries no upward coupling, the foundation
(base + db/queue + access) never imports the upper band, the graph is acyclic, and nothing imports
the CLI. This parses both packages with :mod:`ast` (no imports executed) and fails with the
offending edge, mirroring ``test_http.py``. See docs/publish_split_design.md.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = REPO_ROOT / "rung"
INTEL_DIR = REPO_ROOT / "rung_intel" / "rung_intel"
INTEL_PKG = "rung_intel"

# ── Public-package tiers (by import direction; see ARCHITECTURE.md) ───────────────────────────
# `static_source` is a tier-0 leaf: a self-contained DuckDB-over-Parquet adapter with ZERO internal
# imports, which `db.get_connection` delegates to when RUNG_DATA_SOURCE=static. It sits below db (db
# imports it), so it belongs in the foundation band — see rung/static_source.py, docs it enables the
# Galaxy / outside-researcher reproducibility path off the clean dataset.
# `brands` carries zero internal imports; the two offline geocoders import only base (`http` +
# `addresses`) — so all three are base-shaped leaves and belong in the enforced BASE set (they were
# in PUBLIC_MODULES but no tier, i.e. their layering was unguarded — the same "a tier the guard does
# not know about is not a tier" gap the overlay note below calls out).
# `licensing` and `operators` are the same shape as `brands`: YAML crosswalk readers with zero
# internal imports. `virtual_display` (an X display for a head-full browser) imports nothing at all.
BASE = frozenset({"models", "http", "html", "browser", "text", "addresses", "normalize", "static_source",
                  "brands", "licensing", "operators", "geocode_rnf", "geocode_points",
                  "virtual_display"})  # tier 0
TIER1 = frozenset({"db", "queue"})            # tier 1 — persistence + work queue
TIER2 = frozenset({"access"})                 # tier 2 — access-method engine
FOUNDATION = BASE | TIER1 | TIER2
CLI = "cli"

# The complete public-core module set — the carve-out is "done" when the public package holds
# exactly these and nothing proprietary leaks back in.
PUBLIC_MODULES = frozenset({
    "models", "http", "html", "browser", "fx", "text", "brands", "normalize", "addresses", "static_source",
    "db", "reference_db", "queue", "access", "rate_limit", "rate_gate", "registry", "cli",
    "seed_companies",
    "state_search", "state_lists", "extract", "ai_fallback", "homepage_discovery", "dedupe",
    # Offline Canadian geocoding — two rungs of one ladder, both public on purpose: generic
    # address-range interpolation / address-point lookup over open government data, no cannabis
    # domain logic, no import of the overlay. Neither is wired into the pipeline yet; both are
    # measured against ground truth by scripts/validate_rnf_geocoder.py, so an unused-looking
    # module here is deliberate, not dead code (docs/geocoding_design.md).
    "geocode_rnf", "geocode_points",
    # The licence-regime coding: public because it is assembled entirely from public law (statutes
    # and one regulator's page), carries nothing about our targets or our methods, and is the kind of
    # jurisdiction reference data the public core already ships in `states.yml`.
    "licensing",
    # The cross-state operator key: retail-banner ownership assembled from SEC filings and one trade
    # report, i.e. public-record corporate structure, exactly like `brand_parent.yml` beside it.
    "operators",
    # An Xvfb / Xorg+dummy display for a browser that must run with a window on a display-less box —
    # Chromium's own test-harness recipe, re-implemented. Moved here from the private overlay on
    # 2026-10-06: it names no target and imports nothing internal.
    "virtual_display",
})

# ── Overlay tiers (the proprietary modules left PUBLIC_DIR in the carve-out; their internal layering
# is enforced HERE, the INTEL_DIR analogue of the public checks below). ───────────────────────────
# Pure platform helpers: per-platform fetch/parse recipes that import NOTHING internal (they read
# their data via importlib.resources string args, not import edges — see docs/publish_split_design.md).
# `jane` was MISSING from this set for six days after it was extracted (#115) — it has zero internal
# imports and ARCHITECTURE.md documents it as `none (pure)`, but nothing stopped the next commit from
# making that false. A tier the guard does not know about is not a tier.
# The six single-store live-locator helpers (canna_cabana/delta9/storerocket/shopify/woocommerce/
# hybris_occ) are the same shape — per-platform fetch/parse recipes that take a session arg and import
# NOTHING internal. Each briefly carried a dead `from rung.http import make_session` re-export (noqa
# F401, no consumer — every caller imports make_session from rung.http directly); removing it made them
# true zero-import pure helpers, so they join this set instead of forming an unguarded "http-only" tier.
#
# ⚠ ONE EXCEPTION, ADDED 2026-09-24, AND IT IS THE PUBLIC TIER-0 `http` ONLY. A pure helper may import
# `rung.http` for `raise_for_refusal` / `refused_by` / `FetchRefused` — the shared classification of a
# failed request that every one of these modules used to hand-write as `if status != 200: return
# None`, which is how a 403 and an empty menu came to be the same row. `http` itself imports nothing
# internal (it is a BASE leaf), so the graph stays trivially acyclic, and the outcome vocabulary stays
# where it was: `rung.access` translates `FetchRefused.kind`, the helpers never see `Blocked`. This is
# NOT the dead re-export the paragraph above retired — that import had no consumer; this one is the
# consumer. Anything wider (db, the overlay, `access` itself) is still refused below.
#
# A SECOND, ADDED 2026-10-06 (audit P-37): `rung.html`, the tier-0 leaf for the page-structure and
# value primitives the helpers used to copy (two identical `_float`s folded into `html.as_float`
# first; the scanners and the embedded-state extractor follow a publish decision). It is admitted on
# the same argument as `http` — a BASE leaf that imports nothing internal, so the graph stays
# trivially acyclic — and for the same reason: a primitive each helper hand-writes is one that drifts
# (dispense's brace scanner is not string-aware; sweedpos's is). Still nothing wider than these two.
_PURE_ALLOWED_CORE = frozenset({"http", "html"})
PURE_HELPERS = frozenset({"cresco", "curaleaf", "dutchie", "dutchie_plus", "fluent", "hytiva",
                          "jane", "sweedpos", "trulieve",
                          "canna_cabana", "delta9", "storerocket", "shopify", "woocommerce",
                          "hybris_occ", "sqdc", "cannabis_nb", "shopapps_locator", "tymber", "waio",
                          "breadstack", "hifyre", "flowhub", "treez", "tendy", "dispense",
                          "barnet"})
# The two aggregator sweeps stay lean: they import only the overlay's `aggregator_http` (the private
# anti-throttle machinery) and at most the public base-layer `http`
# (the honest `make_session`) — never the heavier catalogs/extractors. Acyclic, just not zero-import.
AGGREGATOR_HTTP_ONLY = frozenset({"weedmaps", "leafly"})
# `html` joined on 2026-10-06 (audit P-37): leafly reads a page's embedded state through the
# shared `html.script_json` instead of its own attribute-order-fragile regex. Same argument as for
# the pure helpers: a zero-import BASE leaf, so the sweep stays exactly as lean as before.
_AGG_ALLOWED_CORE = frozenset({"http", "html"})    # the honest session factory + the page parsers
_AGG_ALLOWED_OVERLAY = frozenset({"aggregator_http"})  # the private anti-throttle module
# The Stage-3 routing table is its own module (`routing`) so Stage 2 can ask "does any menu rung
# serve this handle?" without importing Stage 3 (audit P-30). Two bounds keep that true. The first
# is DERIVED, not a list of Stage-2 modules: NO overlay module may import `menus` except the ones
# named here, so the next module that reaches for Stage 3 is caught without this file being told
# about it. `intel_plugin` is the plugin entry point — it registers every stage and must import
# all of them.
MENUS_IMPORTERS_ALLOWED = frozenset({"intel_plugin"})
# The second keeps `routing` from growing back into `menus`: the three operator registries it
# loads, and the two core persistence modules its predicate reads through.
_ROUTING_ALLOWED_OVERLAY = frozenset({"shopify", "tymber", "waio"})
_ROUTING_ALLOWED_CORE = frozenset({"db", "reference_db"})


def _py_files(root: Path, *, skip_init: bool = True) -> list[Path]:
    """Every module under ``root``.

    ⚠ `skip_init=False` EXISTS BECAUSE THE LEAK GUARD MUST SEE `__init__.py`. Excluding it hid the
    one file that executes on `import rung` from the public/private contract: a scratch tree whose
    `rung/__init__.py` read `from rung_intel import tymber` passed all 15 tests, leak guard
    included. `tests/test_http.py`'s own scanner never excluded it, so the two disagreed about what
    "every public module" means. Package `__init__.py` files are still skipped for the tier and
    cycle checks, where a re-export is not a dependency edge.
    """
    return [p for p in root.rglob("*.py")
            if (not skip_init or p.name != "__init__.py") and "__pycache__" not in p.parts]


def _public_stems() -> set[str]:
    return {p.stem for p in _py_files(PUBLIC_DIR)}


def _internal_edges() -> dict[str, set[str]]:
    """public module stem -> the set of public module stems it imports."""
    stems = _public_stems()
    edges: dict[str, set[str]] = {}
    for path in _py_files(PUBLIC_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level:  # relative import
                    targets = ([mod.split(".")[-1]] if mod else [a.name for a in node.names])
                elif mod in ("rung", "rung.sources"):
                    targets = [a.name for a in node.names]          # names are submodules
                elif mod.startswith("rung."):
                    targets = [mod.split(".")[-1]]                  # deeper path: names are symbols
                else:
                    continue                                        # third-party / the overlay
            elif isinstance(node, ast.Import):
                # plain `import rung.db` / `import rung.sources.dedupe` — the submodule stem is the
                # trailing component (mirrors _imported_roots so this form can't slip the guard).
                targets = [a.name.split(".")[-1] for a in node.names
                           if a.name.split(".")[0] == "rung" and "." in a.name]
            else:
                continue
            deps.update(t for t in targets if t in stems)
        deps.discard(path.stem)
        edges[path.stem] = deps
    # The engine `db` references `reference_db` ONLY via its lazy back-compat `__getattr__` shim (a
    # function-local import) and a `TYPE_CHECKING` re-export — neither is a module-load dependency
    # (`import rung.db` loads no reference_db/models/text; asserted by
    # test_importing_the_engine_loads_no_cannabis below). So it is not a real edge for the layering /
    # acyclicity contracts; drop it. `reference_db -> db` stays (a genuine load-time edge).
    edges.get("db", set()).discard("reference_db")
    return edges


def _overlay_stems() -> set[str]:
    return {p.stem for p in _py_files(INTEL_DIR)}


def _overlay_imports() -> dict[str, dict[str, set[str]]]:
    """overlay module stem -> {'core': {public stems imported}, 'overlay': {overlay stems imported}}.

    The overlay reaches the core via ``from rung[.sources] import …`` and its siblings via
    ``from rung_intel import …``; ``importlib.resources.files("rung")`` is a
    string arg, not an import edge, so the pure helpers stay zero-internal-import."""
    pub, ov = _public_stems(), _overlay_stems()
    out: dict[str, dict[str, set[str]]] = {}
    for path in _py_files(INTEL_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        core: set[str] = set()
        overlay: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                # ⚠ A RELATIVE IMPORT IS AN EDGE. This branch read `and not node.level`, so
                # `from . import aggregator_http` was dropped on the floor while the identical
                # absolute form was caught — and BOTH guards below rest on this reader, so a
                # `PURE_HELPERS` member could take an internal import, and a genuine two-module
                # cycle could exist, with 15 tests green. The PUBLIC twin `_internal_edges` has
                # always handled `node.level`; only the overlay reader was asymmetric. The overlay
                # happens to use no relative imports today, which is exactly why nobody noticed:
                # the hole was invisible for the same reason it was harmless.
                if node.level:
                    # `from . import x` names the modules; `from .x import y` names the module in `mod`.
                    overlay.update(
                        n for n in ([mod.split(".")[-1]] if mod else [a.name for a in node.names])
                        if n in ov)
                    continue
                root = mod.split(".")[0]
                if root == "rung":
                    names = ([a.name for a in node.names]
                             if mod in ("rung", "rung.sources")
                             else [mod.split(".")[-1]])
                    core.update(n for n in names if n in pub)
                elif root == INTEL_PKG:
                    names = ([a.name for a in node.names] if mod == INTEL_PKG else [mod.split(".")[-1]])
                    overlay.update(n for n in names if n in ov)
            elif isinstance(node, ast.Import):
                # plain `import rung.X` / `import rung_intel.X` — trailing component is the module stem.
                for alias in node.names:
                    parts = alias.name.split(".")
                    if len(parts) < 2:
                        continue
                    if parts[0] == "rung" and parts[-1] in pub:
                        core.add(parts[-1])
                    elif parts[0] == INTEL_PKG and parts[-1] in ov:
                        overlay.add(parts[-1])
        overlay.discard(path.stem)
        out[path.stem] = {"core": core, "overlay": overlay}
    return out


def test_no_overlay_module_but_the_plugin_imports_the_stage3_menus_module() -> None:
    """Stage 2 imported `menus` to reach one predicate, which loaded the whole Stage-3 module graph
    into every Stage-2 process — an edge `ARCHITECTURE.md` did not even record (audit P-30)."""
    if not INTEL_DIR.exists():
        pytest.skip("overlay absent — public-repo build")
    imports = _overlay_imports()
    offenders = sorted(
        m for m, edges in imports.items()
        if "menus" in edges["overlay"] and m not in MENUS_IMPORTERS_ALLOWED)
    assert not offenders, (
        f"{offenders} import `rung_intel.menus`. Only {sorted(MENUS_IMPORTERS_ALLOWED)} may: the "
        "routing table and its predicate live in `rung_intel.routing` — import from there.")
    # An allowlisted module that no longer imports it is an exemption that outlived its reason.
    stale = sorted(m for m in MENUS_IMPORTERS_ALLOWED if "menus" not in imports[m]["overlay"])
    assert not stale, f"{stale} no longer import `menus` — drop them from MENUS_IMPORTERS_ALLOWED"


def test_the_routing_module_imports_only_its_registries_and_persistence() -> None:
    if not INTEL_DIR.exists():
        pytest.skip("overlay absent — public-repo build")
    edges = _overlay_imports()["routing"]
    wide = {"core": sorted(edges["core"] - _ROUTING_ALLOWED_CORE),
            "overlay": sorted(edges["overlay"] - _ROUTING_ALLOWED_OVERLAY)}
    assert not wide["core"] and not wide["overlay"], (
        f"`routing` must stay the small module Stage 2 can afford to import; it now also imports {wide}")


def _first_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    """Return one import cycle as a node path, or None if the graph is acyclic (DFS three-colouring)."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(edges, WHITE)

    def walk(node: str, stack: list[str]) -> list[str] | None:
        color[node] = GREY
        for dep in sorted(edges.get(node, set())):
            if color.get(dep) == GREY:
                return [*stack[stack.index(dep):], dep]   # the cycle
            if color.get(dep) == WHITE:
                cyc = walk(dep, [*stack, dep])
                if cyc:
                    return cyc
        color[node] = BLACK
        return None

    for start in sorted(edges):
        if color[start] == WHITE:
            cycle = walk(start, [start])
            if cycle is not None:
                return cycle
    return None


def _imported_roots(path: Path) -> set[str]:
    """Top-level package names imported by a file (for the cross-package boundary check)."""
    roots: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
    return roots


# ── The publishable boundary ──────────────────────────────────────────────────────────────────

def test_public_core_imports_nothing_from_the_private_overlay() -> None:
    """No public-core module may import ``rung_intel`` — the contract that lets the
    open-source core ship and run without the overlay (proprietary stages → registry stubs). The
    CLI reaches the overlay's stages through ``registry.resolve`` (a runtime lookup) and the overlay
    is discovered via the ``rung.plugins`` entry point — neither is a static import."""
    # `skip_init=False`: `rung/__init__.py` is the one file that executes on `import rung`, so it is
    # the file this contract most needs to cover — and it was the one file excluded.
    offenders = sorted(
        str(p.relative_to(REPO_ROOT)) for p in _py_files(PUBLIC_DIR, skip_init=False)
        if INTEL_PKG in _imported_roots(p)
    )
    assert not offenders, f"public core statically imports the private overlay: {offenders}"


def test_public_package_contains_exactly_the_public_modules() -> None:
    """The carve-out is complete: the public package holds the public set, nothing proprietary."""
    stems = _public_stems()
    leaked = stems - PUBLIC_MODULES
    assert not leaked, f"unexpected (proprietary?) modules in the public package: {sorted(leaked)}"
    missing = PUBLIC_MODULES - stems
    assert not missing, f"expected public modules missing from the package: {sorted(missing)}"


def test_overlay_depends_on_the_public_core() -> None:
    """Sanity on the dependency direction: the overlay imports the public core (never the reverse)."""
    if not INTEL_DIR.exists():
        pytest.skip("overlay absent — public-repo build")
    importers = [p for p in _py_files(INTEL_DIR) if "rung" in _imported_roots(p)]
    assert importers, "expected the private overlay to import the public core"


# ── Public-package internal layering (the original contract, scoped to the public package) ──────

def test_tier_set_names_are_real_modules() -> None:
    stems = _public_stems()
    missing = (BASE | TIER1 | TIER2 | {CLI}) - stems
    assert not missing, f"tier sets name non-existent modules (renamed/removed?): {sorted(missing)}"


def test_nothing_imports_cli() -> None:
    offenders = {m: sorted(e) for m, e in _internal_edges().items() if CLI in e}
    assert not offenders, f"modules import the CLI (cli.py is the top tier, imported by nothing): {offenders}"


def test_base_layer_imports_only_base_layer() -> None:
    edges = _internal_edges()
    offenders = {m: sorted(edges[m] - BASE) for m in BASE if edges.get(m, set()) - BASE}
    assert not offenders, f"base-layer modules must not import upward (only within the base set): {offenders}"


def test_foundation_does_not_depend_on_upper_band() -> None:
    edges = _internal_edges()
    offenders = {m: sorted(edges[m] - FOUNDATION) for m in FOUNDATION if edges.get(m, set()) - FOUNDATION}
    assert not offenders, f"foundation tiers (base/db/queue/access) import the upper band: {offenders}"


# ── Data partition (the dataset leak guard) ─────────────────────────────────────────────────────
# The public package's data/ holds only public/shared curated inputs; the proprietary curated data
# (platform slugs/chains/tokens, pinned store ids, the grower list) lives in the overlay. This
# catches a private data file reappearing in the public package — a publish leak.
PUBLIC_DATA = frozenset({
    "companies.yml", "company_homepages.yml", "states.yml", "state_geo_anchors.yml",
    "category_aliases.yml", "category_name_overrides.yml", "product_type_aliases.yml",
    "strain_aliases.yml", "obtention_aliases.yml", "brand_parent.yml", "license_regime.yml",
    "operator_parent.yml",
})
# Keep PRIVATE_DATA in lockstep with faces/tier2.py:PRIVATE_DATA (the publish leak
# guard) and the actual files in rung_intel/.../data/ — the two are independent copies, pinned equal by
# test_build_public_repo.py::test_private_data_denylist_matches_build_tool (that test lives there, not
# here, because it imports `scripts` — and the build drops any shipped test that imports scripts/, which
# would silently strip THIS boundary-guard file from the public repo).
# This is the SCRAPING-INTEL denylist: platform slugs/chains/tokens/store-ids/grower brands whose
# filename must never surface in a shipped doc or the public tree. It deliberately does NOT list the
# newer overlay registries (brand_ownership / provincial_monopolies / shopify_storefronts /
# storerocket_banners): those live in the overlay (already excluded from the public build) and are
# curated-but-not-secret (brand_ownership is SEC-sourced; provincial_monopolies names public Crown
# stores and is even referenced by public states.yml, so denylisting it would be wrong). Adding one
# here is a publish-policy decision, not a mechanical sync.
PRIVATE_DATA = frozenset({
    "dutchie_chains.yml", "dutchie_plus_tokens.yml", "grower_brands.yml",
    "jane_store_ids.yml", "leafly_slugs.yml", "weedmaps_slugs.yml",
})
# A subset of PRIVATE_DATA that is gitignored (a real secret), so it is absent from a fresh checkout
# (CI / public build) and must NOT be required to physically exist — it stays in PRIVATE_DATA only for
# the leak-guard denylist + public-data exclusion.
GITIGNORED_PRIVATE_DATA = frozenset({"dutchie_plus_tokens.yml"})


def test_public_data_holds_no_proprietary_files() -> None:
    """The public package's data/ must contain no proprietary curated data (a publish-leak guard)."""
    public_data = {p.name for p in (PUBLIC_DIR / "data").glob("*.yml")}
    leaked = public_data & PRIVATE_DATA
    assert not leaked, f"proprietary data files in the PUBLIC package (publish leak!): {sorted(leaked)}"
    unexpected = public_data - PUBLIC_DATA
    assert not unexpected, (
        f"unclassified data files in the public package — classify in test_import_layering.py "
        f"+ docs/publish_split_design.md: {sorted(unexpected)}"
    )


def test_overlay_holds_the_proprietary_data() -> None:
    """The proprietary curated data lives in the overlay, co-located with the modules that load it."""
    if not INTEL_DIR.exists():
        pytest.skip("overlay absent — public-repo build")
    overlay_data = {p.name for p in (INTEL_DIR / "data").glob("*.yml")}
    # The gitignored secret (dutchie_plus_tokens.yml) is legitimately absent in a fresh checkout.
    missing = (PRIVATE_DATA - GITIGNORED_PRIVATE_DATA) - overlay_data
    assert not missing, f"proprietary data files missing from the overlay: {sorted(missing)}"


def test_internal_import_graph_is_acyclic() -> None:
    cycle = _first_cycle(_internal_edges())
    assert cycle is None, f"import cycle: {' -> '.join(cycle or [])}"


def test_importing_the_engine_loads_no_cannabis() -> None:
    # Genericization B1/B3: the generic engine (`db`/`queue`/`access`) must import WITHOUT dragging in
    # the cannabis record types (`models`) or terpene taxonomy (`text`) — those live in the reference
    # half (`reference_db`), reached only via db's lazy back-compat shim. A build-your-own-domain user
    # importing the engine gets none of the cannabis schema. Fresh subprocess for clean sys.modules.
    code = (
        "import rung.db, rung.queue, rung.access, sys\n"
        "cannabis = [m for m in ('rung.models', 'rung.text', 'rung.reference_db') if m in sys.modules]\n"
        "print(','.join(cannabis))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=REPO_ROOT)
    leaked = [m for m in out.stdout.strip().split(",") if m]
    assert not leaked, f"importing the engine eagerly loaded cannabis modules: {leaked}"


# ── Overlay-package internal layering (the proprietary modules left PUBLIC_DIR in the carve-out; the
# pre-split acyclic / pure-helper / aggregator-http-only contracts are re-enforced here over INTEL_DIR,
# mirroring test_http.py which already scans the overlay tree). Skip on a public-only repo build. ────

def test_overlay_tier_set_names_are_real_modules() -> None:
    if not INTEL_DIR.exists():
        pytest.skip("overlay absent — public-repo build")
    missing = (PURE_HELPERS | AGGREGATOR_HTTP_ONLY) - _overlay_stems()
    assert not missing, f"overlay tier sets name non-existent modules (renamed/removed?): {sorted(missing)}"


def test_overlay_pure_platform_helpers_have_no_internal_imports() -> None:
    if not INTEL_DIR.exists():
        pytest.skip("overlay absent — public-repo build")
    imports = _overlay_imports()
    offenders = {
        m: sorted((imports[m]["core"] - _PURE_ALLOWED_CORE) | imports[m]["overlay"])
        for m in PURE_HELPERS
        if (imports.get(m, {}).get("core", set()) - _PURE_ALLOWED_CORE)
        or imports.get(m, {}).get("overlay")
    }
    assert not offenders, (
        "pure platform helpers may import nothing internal but the tier-0 `http` refusal helper: "
        f"{offenders}")


def test_overlay_aggregator_sweeps_stay_lean() -> None:
    if not INTEL_DIR.exists():
        pytest.skip("overlay absent — public-repo build")
    imports = _overlay_imports()
    offenders = {
        m: {"core": sorted(imports.get(m, {}).get("core", set())),
            "overlay": sorted(imports.get(m, {}).get("overlay", set()))}
        for m in AGGREGATOR_HTTP_ONLY
        if (imports.get(m, {}).get("core", set()) - _AGG_ALLOWED_CORE)
        or (imports.get(m, {}).get("overlay", set()) - _AGG_ALLOWED_OVERLAY)
    }
    assert not offenders, (
        "aggregator sweeps may import only public `http` and `html` + overlay `aggregator_http`: "
        f"{offenders}"
    )


def test_overlay_internal_import_graph_is_acyclic() -> None:
    if not INTEL_DIR.exists():
        pytest.skip("overlay absent — public-repo build")
    edges = {m: d["overlay"] for m, d in _overlay_imports().items()}
    cycle = _first_cycle(edges)
    assert cycle is None, f"overlay import cycle: {' -> '.join(cycle or [])}"


# ── `db` forwards the reference API, and its typed block is the only thing keeping that honest ───

def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.TypeAlias):
            names.add(node.name.id)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def _db_forwarding() -> tuple[set[str], set[str], dict[str, set[str]]]:
    """``(names the TYPE_CHECKING block lists, names db.py defines itself, {db.<name>: files})``
    over every tree present — the overlay, scripts and examples only when this is not the public build."""
    repo = REPO_ROOT
    db_tree = ast.parse((REPO_ROOT / "rung" / "db.py").read_text(encoding="utf-8"))
    listed: set[str] = set()
    for node in db_tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom) and sub.module == "rung.reference_db":
                    listed.update(alias.asname or alias.name for alias in sub.names)
    used: dict[str, set[str]] = {}
    for root in ("rung", "rung_intel", "scripts", "tests", "examples", "faces"):
        for path in sorted((repo / root).rglob("*.py")) if (repo / root).is_dir() else []:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            binds_db = any(
                isinstance(n, ast.ImportFrom) and n.module == "rung"
                and any(a.name == "db" and a.asname in (None, "db") for a in n.names)
                for n in ast.walk(tree))
            if not binds_db:
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "db":
                    used.setdefault(n.attr, set()).add(str(path.relative_to(repo)))
    return listed, _module_level_names(db_tree), used


def test_every_name_reached_through_db_is_typed_or_defined_there() -> None:
    """`db.__getattr__` forwards ANY name to `reference_db` as `Any`. For a name missing from the
    TYPE_CHECKING block that silently turns the LiteralString SQL guarantee off — the block's own
    comment says the four analysis guards were missing from it once. Nothing compared the block
    with what is actually reached through `db.` (audit P-41f)."""
    listed, defined, used = _db_forwarding()
    forwarded = {name: files for name, files in used.items() if name not in defined}
    assert forwarded, "no `db.<name>` reaches the forwarder — the scan found nothing to check"
    untyped = {n: sorted(f) for n, f in forwarded.items() if not n.startswith("_") and n not in listed}
    assert not untyped, (
        f"reached through `db.` and missing from its TYPE_CHECKING block, so typed `Any`: {untyped}")
    # A PRIVATE name has no business in that block; reach it where it lives. Four call sites did
    # until 2026-10-04, one of them a SQL fragment.
    private = {n: sorted(f) for n, f in forwarded.items() if n.startswith("_")}
    assert not private, (
        f"private `reference_db` names reached through `db`'s forwarder: {private} — import them "
        "from `rung.reference_db`")


def test_every_name_the_db_block_lists_exists_in_reference_db() -> None:
    listed, _, _ = _db_forwarding()
    ref_tree = ast.parse((REPO_ROOT / "rung" / "reference_db.py").read_text(encoding="utf-8"))
    missing = sorted(listed - _module_level_names(ref_tree))
    assert listed, "db.py's TYPE_CHECKING block lists nothing — the parse broke"
    assert not missing, f"db.py's TYPE_CHECKING block imports names reference_db does not define: {missing}"

