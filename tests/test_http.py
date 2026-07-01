"""Guard tests: every HTTP session must come from ``http.make_session()``.

The pipeline funnels all networking through a single ``curl_cffi`` chokepoint
(:func:`rung.http.make_session`) so the TLS/JA3 impersonation decision is made in
exactly one place. The **public default is honest** (no impersonation); the private overlay opts
in at plugin load so the real scrapers carry a browser fingerprint for impersonation-gated targets
(e.g. Dutchie's Cloudflare). See docs/publish_split_design.md, "no target + no evasion". The static
checks fail if a future change constructs a session anywhere else, or pulls in a raw HTTP client
that bypasses the chokepoint; they parse source with :mod:`ast` rather than importing it, so a
regression is reported as ``file:lineno`` instead of a runtime surprise. The behaviour tests below
pin the honest-by-default / opt-in-impersonation contract.
"""

import ast
from pathlib import Path

# The package source tree: <repo>/rung/rung/. The test lives at
# <repo>/rung/tests/, so parents[1] is the repo root.
REPO_ROOT: Path = Path(__file__).resolve().parents[1]
PACKAGE_DIR: Path = REPO_ROOT / "rung"
# scripts/ is in the QA gate (ruff/ty) too, so the HTTP chokepoint guard covers it as well.
SCRIPTS_DIR: Path = REPO_ROOT / "scripts"
# The private overlay (Phase-3b carve-out) also routes all networking through make_session, so the
# chokepoint guard must cover it too.
INTEL_DIR: Path = REPO_ROOT / "dispensary_scraper_intel"

# Session factories may only be CALLED inside this module; every other module receives a
# session as a parameter.
SESSION_CHOKEPOINT: str = "http.py"
SESSION_CONSTRUCTORS: frozenset[str] = frozenset({"AsyncSession", "Session"})
# The one sanctioned raw-session site: the impersonation health check sweeps multiple
# impersonation profiles to find which passes Cloudflare, so it MUST construct sessions
# without the fixed make_session() profile. It uses curl_cffi (still an impersonating
# client), so it is exempt only from the constructor guard, not the banned-import guard.
RAW_SESSION_ALLOWED: frozenset[str] = frozenset({"check_impersonation.py"})

# Raw HTTP clients that do not impersonate a browser; banned package-wide. urllib.parse
# (URL parsing, not fetching) and curl_cffi.requests (the impersonating client itself) are
# intentionally absent, so only urllib's network submodules and rival libraries are listed.
BANNED_IMPORTS: frozenset[str] = frozenset(
    {"requests", "httpx", "aiohttp", "urllib.request", "urllib.error"}
)


def _gated_sources() -> list[Path]:
    """Every ``.py`` file the QA gate covers — the package + ``scripts/`` — sans caches."""
    return sorted(
        p
        for root in (PACKAGE_DIR, INTEL_DIR, SCRIPTS_DIR)
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _callee_name(node: ast.Call) -> str | None:
    """Return a call's bare function name, e.g. ``AsyncSession(...)`` -> ``"AsyncSession"``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _imported_modules(node: ast.stmt) -> list[str]:
    """Return the absolute module names an import statement binds (``[]`` for anything else)."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    # Only absolute `from x import ...` (level 0) names a foreign package; relative imports
    # (level > 0) are in-package and never a third-party HTTP client.
    if isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
        return [node.module]
    return []


def test_session_only_constructed_in_http() -> None:
    """Only ``http.py`` may construct a curl_cffi session; everyone else is handed one."""
    offenders: list[str] = []
    for path in _gated_sources():
        if path.name == SESSION_CHOKEPOINT or path.name in RAW_SESSION_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee_name(node) in SESSION_CONSTRUCTORS:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, (
        "Session constructed outside http.make_session() — route it through make_session() "
        f"so TLS impersonation stays on: {offenders}"
    )


def test_no_non_impersonating_http_clients() -> None:
    """No module imports a raw HTTP client that bypasses curl_cffi impersonation."""
    offenders: list[str] = []
    for path in _gated_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for module in _imported_modules(node):
                if module in BANNED_IMPORTS or module.split(".")[0] in {
                    "requests",
                    "httpx",
                    "aiohttp",
                }:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} ({module})"
                    )
    assert not offenders, (
        "Non-impersonating HTTP client imported; use rung.http instead: "
        f"{offenders}"
    )


class _SessionRecorder:
    """Captures the kwargs make_session would hand curl_cffi's AsyncSession."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


def test_make_session_is_honest_by_default(monkeypatch) -> None:
    """With impersonation unset (the public default), the session sends an honest UA, no spoofing."""
    from rung import http

    monkeypatch.setattr(http, "AsyncSession", _SessionRecorder)
    monkeypatch.setattr(http, "_impersonate", None)
    session = http.make_session()
    assert "impersonate" not in session.kwargs
    assert session.kwargs["headers"]["User-Agent"] == http.HONEST_USER_AGENT


def test_set_impersonation_opts_into_a_profile(monkeypatch) -> None:
    """Opting in (as the private overlay does) makes the chokepoint impersonate that profile."""
    from rung import http

    monkeypatch.setattr(http, "AsyncSession", _SessionRecorder)
    monkeypatch.setattr(http, "_impersonate", None)
    http.set_impersonation("chrome124")
    assert http.current_impersonation() == "chrome124"
    session = http.make_session()
    assert session.kwargs["impersonate"] == "chrome124"
    assert "headers" not in session.kwargs  # impersonation supplies the fingerprint, not an honest UA
