"""Guard tests: the public/private package boundary + the public package's internal layering.

After the Phase-3b carve-out the proprietary modules live in the sibling ``dispensary_scraper_intel``
package. The load-bearing contract is now a **package boundary**: the public ``rung``
core must import NOTHING from that overlay — that's what lets the open-source core ship and run on
its own (its proprietary stages then resolve to registry stubs). Within the public package the
original tier layering still holds: the base layer carries no upward coupling, the foundation
(base + db/queue + access) never imports the upper band, the graph is acyclic, and nothing imports
the CLI. This parses both packages with :mod:`ast` (no imports executed) and fails with the
offending edge, mirroring ``test_http.py``. See docs/publish_split_design.md.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = REPO_ROOT / "rung"
INTEL_DIR = REPO_ROOT / "dispensary_scraper_intel" / "dispensary_scraper_intel"
INTEL_PKG = "dispensary_scraper_intel"

# ── Public-package tiers (by import direction; see ARCHITECTURE.md) ───────────────────────────
BASE = frozenset({"models", "http", "browser", "text", "addresses", "normalize"})  # tier 0
TIER1 = frozenset({"db", "queue"})            # tier 1 — persistence + work queue
TIER2 = frozenset({"access"})                 # tier 2 — access-method engine
FOUNDATION = BASE | TIER1 | TIER2
CLI = "cli"

# The complete public-core module set — the carve-out is "done" when the public package holds
# exactly these and nothing proprietary leaks back in.
PUBLIC_MODULES = frozenset({
    "models", "http", "browser", "text", "normalize", "addresses",
    "db", "queue", "access", "rate_limit", "registry", "cli", "seed_companies",
    "state_search", "state_lists", "extract", "ai_fallback", "homepage_discovery", "dedupe",
})

# ── Overlay tiers (the proprietary modules left PUBLIC_DIR in the carve-out; their internal layering
# is enforced HERE, the INTEL_DIR analogue of the public checks below). ───────────────────────────
# Pure platform helpers: per-platform fetch/parse recipes that import NOTHING internal (they read
# their data via importlib.resources string args, not import edges — see docs/publish_split_design.md).
PURE_HELPERS = frozenset({"cresco", "curaleaf", "dutchie", "dutchie_plus",
                          "fluent", "hytiva", "sweedpos", "trulieve"})
# The two aggregator sweeps stay lean: they import only the overlay's `aggregator_http` (the private
# anti-throttle machinery) and at most the public base-layer `http`
# (the honest `make_session`) — never the heavier catalogs/extractors. Acyclic, just not zero-import.
AGGREGATOR_HTTP_ONLY = frozenset({"weedmaps", "leafly"})
_AGG_ALLOWED_CORE = frozenset({"http"})            # the honest session factory (if used)
_AGG_ALLOWED_OVERLAY = frozenset({"aggregator_http"})  # the private anti-throttle module


def _py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if p.name != "__init__.py" and "__pycache__" not in p.parts]


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
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = node.module or ""
            if node.level:  # relative import
                targets = ([mod.split(".")[-1]] if mod else [a.name for a in node.names])
            elif mod in ("rung", "rung.sources"):
                targets = [a.name for a in node.names]          # names are submodules
            elif mod.startswith("rung."):
                targets = [mod.split(".")[-1]]                  # deeper path: names are symbols
            else:
                continue                                        # third-party / the overlay
            deps.update(t for t in targets if t in stems)
        deps.discard(path.stem)
        edges[path.stem] = deps
    return edges


def _overlay_stems() -> set[str]:
    return {p.stem for p in _py_files(INTEL_DIR)}


def _overlay_imports() -> dict[str, dict[str, set[str]]]:
    """overlay module stem -> {'core': {public stems imported}, 'overlay': {overlay stems imported}}.

    The overlay reaches the core via ``from rung[.sources] import …`` and its siblings via
    ``from dispensary_scraper_intel import …``; ``importlib.resources.files("rung")`` is a
    string arg, not an import edge, so the pure helpers stay zero-internal-import."""
    pub, ov = _public_stems(), _overlay_stems()
    out: dict[str, dict[str, set[str]]] = {}
    for path in _py_files(INTEL_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        core: set[str] = set()
        overlay: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue
            mod = node.module or ""
            root = mod.split(".")[0]
            if root == "rung":
                names = ([a.name for a in node.names]
                         if mod in ("rung", "rung.sources")
                         else [mod.split(".")[-1]])
                core.update(n for n in names if n in pub)
            elif root == INTEL_PKG:
                names = ([a.name for a in node.names] if mod == INTEL_PKG else [mod.split(".")[-1]])
                overlay.update(n for n in names if n in ov)
        overlay.discard(path.stem)
        out[path.stem] = {"core": core, "overlay": overlay}
    return out


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
    """No public-core module may import ``dispensary_scraper_intel`` — the contract that lets the
    open-source core ship and run without the overlay (proprietary stages → registry stubs). The
    CLI reaches the overlay's stages through ``registry.resolve`` (a runtime lookup) and the overlay
    is discovered via the ``rung.plugins`` entry point — neither is a static import."""
    offenders = sorted(
        str(p.relative_to(REPO_ROOT)) for p in _py_files(PUBLIC_DIR)
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
    "strain_aliases.yml",
})
# Keep PRIVATE_DATA in lockstep with scripts/build_public_repo.py:PRIVATE_DATA (the publish leak
# guard) and the actual files in dispensary_scraper_intel/.../data/ — the two are independent copies.
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
        m: sorted(imports[m]["core"] | imports[m]["overlay"])
        for m in PURE_HELPERS if imports.get(m, {}).get("core") or imports.get(m, {}).get("overlay")
    }
    assert not offenders, f"pure platform helpers must carry zero internal imports: {offenders}"


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
        "aggregator sweeps may import only public `http` + overlay `aggregator_http`: " f"{offenders}"
    )


def test_overlay_internal_import_graph_is_acyclic() -> None:
    if not INTEL_DIR.exists():
        pytest.skip("overlay absent — public-repo build")
    edges = {m: d["overlay"] for m, d in _overlay_imports().items()}
    cycle = _first_cycle(edges)
    assert cycle is None, f"overlay import cycle: {' -> '.join(cycle or [])}"
