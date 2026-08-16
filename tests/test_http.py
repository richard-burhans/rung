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
INTEL_DIR: Path = REPO_ROOT / "rung_intel"
# NOT the library. `biblio` stopped routing through `rung.http` on 2026-07-30 and vendors its own
# honest session, because `make_sync_session`'s honesty depended on a module global the private
# overlay sets — so the librarian could impersonate on Unpaywall without asking to. It has its own
# chokepoint guard, `tests/test_library_http.py`. Two guards, deliberately: the cost of the split is
# that no single test proves both, and both docstrings say so.

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
    """Every ``.py`` file this guard covers — the two packages + ``scripts/`` — sans caches."""
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


# Network clients reachable as a SUBPROCESS. The two guards above are import- and
# constructor-shaped, so a module that shells out fetches the web while importing nothing and
# constructing nothing — it passes both while routing around the chokepoint completely.
NETWORK_BINARIES: frozenset[str] = frozenset({"curl", "wget", "httpie", "http", "aria2c"})
# subprocess entry points plus os.system; `_callee_name` reduces `subprocess.run` to `run`.
SUBPROCESS_CALLS: frozenset[str] = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "system"}
)
# The one module that shells out on purpose, and it is DEBT rather than design.
#
# `supplement_fetcher.py` is an operator-run acquisition tool (not a pipeline stage) whose
# docstring states the choice: half its rungs need a specific browser User-Agent and Referer to
# get a byte out of a publisher, and it obtained 25 of 29 supplements that way. Round 42 examined
# it and ruled the defect was the GUARD'S SELF-DESCRIPTION, not this module — and said explicitly:
# do NOT resolve it by routing the fetcher through `make_session` without measuring first, because
# the docstring's claim that these rungs need that impersonation is testable and untested.
#
# So it is exempted rather than rewritten, and the exemption is the record that the measurement is
# still owed. Anything ADDED here needs the same argument; a new pipeline fetch does not qualify.
SHELL_FETCH_ALLOWED: frozenset[str] = frozenset({"supplement_fetcher.py"})


def _shelled_binary(node: ast.Call) -> str | None:
    """The network binary this call shells out to, or ``None``.

    Handles both argv forms — ``run(["curl", url])`` and ``system("curl " + url)`` — and strips a
    path prefix so ``/usr/bin/curl`` is caught too.
    """
    if _callee_name(node) not in SUBPROCESS_CALLS or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
        first = first.elts[0]
    if isinstance(first, ast.JoinedStr) and first.values:  # an f-string command line
        first = first.values[0]
    while isinstance(first, ast.BinOp):  # `"curl " + url`, possibly nested
        first = first.left
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    binary = first.value.split()[0].rsplit("/", 1)[-1] if first.value.strip() else ""
    return binary if binary in NETWORK_BINARIES else None


def test_no_module_shells_out_to_a_network_binary() -> None:
    """The chokepoint must not be bypassable with a subprocess.

    THE HOLE THIS CLOSES. ``test_session_only_constructed_in_http`` looks for session
    CONSTRUCTORS and ``test_no_non_impersonating_http_clients`` for banned IMPORTS. A file whose
    every fetch is ``subprocess.run(["curl", url])`` has neither, so it sails through both while
    sending curl's own TLS fingerprint and none of the impersonation the whole design rests on —
    and it inherits no proxy, no host rate limit and no retry policy either.

    ``test_http.py``'s own comment has claimed ``scripts/`` is covered "too" since the guard was
    written; it was covered only on the two axes above. Filed by adversarial round 42.
    """
    offenders: list[str] = []
    for path in _gated_sources():
        if path.name in SHELL_FETCH_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (binary := _shelled_binary(node)):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} ({binary})")
    assert not offenders, (
        "Network binary invoked as a subprocess, bypassing rung.http.make_session(): "
        f"{offenders}"
    )


def test_the_shell_fetch_allowlist_names_only_files_that_exist() -> None:
    """An allowlist entry for a deleted file silently widens the guard for a future namesake."""
    names = {p.name for p in _gated_sources()}
    assert names >= SHELL_FETCH_ALLOWED, f"stale entries: {SHELL_FETCH_ALLOWED - names}"


def test_the_allowlisted_fetcher_still_actually_shells_out() -> None:
    """If it were rerouted through `make_session`, the exemption should GO, not linger.

    An allowlist that outlives its reason is how a guard quietly stops guarding — and this
    entry exists to carry an owed measurement, so it must not become decoration.
    """
    path = next(p for p in _gated_sources() if p.name in SHELL_FETCH_ALLOWED)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    shelled = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _shelled_binary(n)]
    assert shelled, (
        f"{path.name} no longer shells out — remove it from SHELL_FETCH_ALLOWED "
        "and record that the impersonation measurement round 42 asked for was done."
    )


def test_the_subprocess_guard_actually_fires() -> None:
    """A guard nobody has seen fail is a guard nobody knows works.

    This corpus's recorded dominant defect is a check that could not see its inputs reporting
    that it found nothing wrong, so the detector is exercised against the argv forms it must
    catch — and against the ones it must NOT, since `run(["git", ...])` is everywhere.
    """
    def binaries(src: str) -> list[str]:
        return [
            b
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call) and (b := _shelled_binary(node))
        ]

    assert binaries('subprocess.run(["curl", "-s", url])') == ["curl"]
    assert binaries('subprocess.run(["/usr/bin/curl", url])') == ["curl"]
    assert binaries('subprocess.check_output(("wget", "-q", url))') == ["wget"]
    assert binaries('os.system("curl " + url)') == ["curl"]
    assert binaries('subprocess.run(f"curl {url}", shell=True)') == ["curl"]
    # Not network calls: the guard must not fire on the subprocess use that is everywhere.
    assert binaries('subprocess.run(["git", "status"])') == []
    assert binaries('subprocess.run(["uv", "run", "pytest"])') == []
    assert binaries('run(["pdftotext", path])') == []


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
