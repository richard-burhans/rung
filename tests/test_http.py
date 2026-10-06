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

import pytest

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
        # ⚠ THE SUBMODULE SPELLING COUNTS TOO. Returning only `node.module` meant
        # `from urllib import request` bound `"urllib"` — in neither `BANNED_IMPORTS` nor the
        # root set — while `import urllib.request` and `from urllib.request import urlopen` were
        # both caught. A live, non-impersonating, un-proxied `request.urlopen` in a scraper module
        # passed all eight tests of this file. The three spellings must be one rule.
        return [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
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


#: "The allowlisted file is absent because its whole TREE is absent" is not staleness, and the two
#: tests below must not read it as such. `SHELL_FETCH_ALLOWED` names a `scripts/` module, and
#: `scripts/` does not ship: `faces/targets/rung.py` excludes it, so in the public build
#: `_gated_sources()` returns the two packages and nothing else.
#:
#: THIS FILE IS SHIPPED ON PURPOSE and the reason is recorded next to `PRIVATE_SCRIPTS_PATH`, which
#: exempts `test_http` from the private-scripts drop as a framework guard that is "vacuously empty in
#: public". That was true of the SCAN — `rglob` on a missing directory yields nothing and raises
#: nothing — and false of this CONSTANT, which is a hardcoded literal that survives the carve intact.
#: So the guard shipped naming a file that cannot exist beside it, and the public repo's suite failed
#: at `stale entries: {'supplement_fetcher.py'}` and a bare `StopIteration` — a contributor's first
#: `pytest` run reporting a defect in the private monorepo they cannot see, let alone act on.
#: Measured 2026-08-18 on a clean public checkout: 2 failed, 533 passed.
#:
#: SKIPPED, NOT SILENTLY PASSED. A bare `return` here would turn "this build cannot ask the question"
#: into a green tick, which is the one outcome a guard must never produce; the skip says which tree
#: was missing, so a public run reports honestly that the exemption went unchecked rather than
#: claiming it held. In the private tree — the only one where the allowlist HAS a subject — nothing
#: changes and both tests run exactly as before.
_NO_ALLOWLIST_TREE = "scripts/ is not in this build, so the shell-fetch allowlist has no subject here"


def test_the_shell_fetch_allowlist_names_only_files_that_exist() -> None:
    """An allowlist entry for a deleted file silently widens the guard for a future namesake."""
    if not SCRIPTS_DIR.is_dir():
        pytest.skip(_NO_ALLOWLIST_TREE)
    names = {p.name for p in _gated_sources()}
    assert names >= SHELL_FETCH_ALLOWED, f"stale entries: {SHELL_FETCH_ALLOWED - names}"


def test_the_allowlisted_fetcher_still_actually_shells_out() -> None:
    """If it were rerouted through `make_session`, the exemption should GO, not linger.

    An allowlist that outlives its reason is how a guard quietly stops guarding — and this
    entry exists to carry an owed measurement, so it must not become decoration.
    """
    if not SCRIPTS_DIR.is_dir():
        pytest.skip(_NO_ALLOWLIST_TREE)
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


#: Scripts that fetch PUBLIC data (census, geocoders, a price index, the public build) and are right
#: to send the honest client. Every other script that opens a session through `make_session` must
#: load the overlay first — its plugin registrar is what turns TLS impersonation on, and without it
#: Dutchie and Jane answer a Cloudflare "Attention Required" page from EVERY vantage, which on
#: 2026-09-25 read as a platform-wide wall until the user agent was checked.
HONEST_BY_DESIGN_SCRIPTS = frozenset({
    "backfill_geocode.py", "build_public_repo.py", "build_us_counties_geojson.py", "fetch_acs_ice.py",
    "fetch_acs_income.py", "fetch_priceofweed.py", "geocode_tracts.py", "school_geocode_error.py",
    # Its one request is to OUR OWN published status page (`_check_published_dashboard`).
    "coverage_healthcheck.py",
    # RULED 2026-10-06: the POS census stays on the honest client, as an instrument. Its NJ and PA
    # samples of 2026-08-01 were taken that way, and the cost was then measured by re-probing the
    # 31 non-Dutchie stores it recorded `blocked` with the impersonating client: 21 still refused
    # (RISE's sites answer 403 to any client from this address; 22 of the 33 blocked rows are RISE),
    # 9 answered with no platform signature, ONE became an observation — 1 of 517 sampled stores.
    # The blocks are host-level, not client-level, so switching clients would change what the
    # instrument sends mid-series for nothing it measures. `docs/analysis/pos_census.md` limitation
    # 2 carries the numbers.
    "pos_census.py",
})

#: Scripts that open a session WITHOUT loading the overlay and that nobody has ruled on. Found
#: 2026-10-04, when this guard learned to follow a session opened one call away (see
#: `_session_opening_calls`). An entry is named rather than fixed when the fix would change a
#: measurement — `pos_census.py` sat here from 2026-10-04 until its ruling on 2026-10-06 (above:
#: honest by design, the cost measured at one observation in 517). Empty means every script that
#: opens a session either loads the overlay or has a stated reason not to.
#:
#: Two-sided: a new unruled script fails, and so does an entry here that has since been fixed.
SESSION_SCRIPTS_AWAITING_A_RULING: frozenset[str] = frozenset()

_SESSION_FACTORIES = frozenset({"make_session", "make_sync_session"})


def _session_opening_calls() -> frozenset[str]:
    """Every call that hands a script an open session: the two factories, plus each PUBLIC overlay
    function whose body calls one (`cf_clearance.session_for`, `pos_census.run_census`, the stage
    runners). Derived by AST, so a new wrapper is covered the day it is written.

    The guard used to key on the literal `make_session` in the script's own source, so a script
    that reached a session one call away — `cf_clearance.session_for(...)` — was skipped
    (audit P-41i). Private helpers are left out: `_one` and `_run` are common local names in
    scripts and would match by coincidence. In the public build there is no overlay and this is
    just the two factories.
    """
    names = set(_SESSION_FACTORIES)
    overlay = REPO_ROOT / "rung_intel" / "rung_intel"
    if overlay.is_dir():
        for path in sorted(overlay.glob("*.py")):
            for function in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if function.name.startswith("_"):
                    continue
                for node in ast.walk(function):
                    callee = node.func if isinstance(node, ast.Call) else None
                    called = (callee.id if isinstance(callee, ast.Name)
                              else callee.attr if isinstance(callee, ast.Attribute) else None)
                    if called in _SESSION_FACTORIES:
                        names.add(function.name)
    return frozenset(names)


def _opens_a_session(source: str, calls: frozenset[str]) -> bool:
    import re

    return any(re.search(rf"\b{re.escape(name)}\(", source) for name in calls)


def test_a_script_that_opens_a_session_loads_the_overlay_or_is_honest_by_design() -> None:
    if not SCRIPTS_DIR.is_dir():
        pytest.skip(_NO_ALLOWLIST_TREE)
    calls = _session_opening_calls()
    unguarded = set()
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not _opens_a_session(source, calls) or path.name in HONEST_BY_DESIGN_SCRIPTS:
            continue
        if "registry.load_plugins(" not in source and "set_impersonation(" not in source:
            unguarded.add(path.name)
    new = sorted(unguarded - SESSION_SCRIPTS_AWAITING_A_RULING)
    assert not new, (
        f"scripts opening a session without registry.load_plugins(): {new} — add the call at the "
        "top of main(), or name the script in HONEST_BY_DESIGN_SCRIPTS if it fetches public data")
    fixed = sorted(SESSION_SCRIPTS_AWAITING_A_RULING - unguarded)
    assert not fixed, f"{fixed} no longer need a ruling — drop them from SESSION_SCRIPTS_AWAITING_A_RULING"
    stale = sorted(name for name in HONEST_BY_DESIGN_SCRIPTS if not (SCRIPTS_DIR / name).exists())
    assert not stale, f"HONEST_BY_DESIGN_SCRIPTS names scripts that no longer exist: {stale}"


def test_the_session_guard_follows_a_session_opened_one_call_away() -> None:
    """ANTI-VACUITY for the derivation, on synthetic source: the literal-only key passed this."""
    calls = frozenset({"make_session", "session_for"})
    assert _opens_a_session("session = cf_clearance.session_for(url)", calls)
    assert _opens_a_session("async with make_session() as s:", calls)
    assert not _opens_a_session("# see make_session in the docs\nrows = load()", calls)
    if (REPO_ROOT / "rung_intel" / "rung_intel").is_dir():
        derived = _session_opening_calls()
        assert {"session_for", "cleared_session", "run_store_menus"} <= derived, sorted(derived)
        assert not any(name.startswith("_") for name in derived)


# ── the shared fetch (P-37): one request, one refusal rule, one parse ──────────────────────────────
# Seven pure helpers carried a private copy of these twelve lines until 2026-10-06; two had drifted.
# `tests/test_fetcher_shape.py` keeps the next copy out. These pin the shared one's verdicts.


class _FetchResponse:
    def __init__(self, status: int, text: str, headers: dict | None = None) -> None:
        self.status_code, self.text, self.headers = status, text, headers or {}


class _FetchSession:
    def __init__(self, response: object = None, *, raises: BaseException | None = None) -> None:
        self.response, self.raises, self.calls = response, raises, []

    async def get(self, url: str, **kwargs: object) -> object:
        self.calls.append((url, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.response


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_get_json_returns_the_parsed_payload_and_forwards_the_request_arguments() -> None:
    from rung import http

    session = _FetchSession(_FetchResponse(200, '{"a": [1, 2]}'))
    payload = _run(http.get_json(
        session, "https://x.test/api", params={"p": 1}, headers={"Accept": "application/json"},
        timeout=7, expect="object"))
    assert payload == {"a": [1, 2]}
    url, kwargs = session.calls[0]
    assert url == "https://x.test/api"
    assert kwargs == {"params": {"p": 1}, "headers": {"Accept": "application/json"}, "timeout": 7,
                      "allow_redirects": True}
    assert _run(http.get_json(_FetchSession(_FetchResponse(200, "[1]")), "u", timeout=1)) == [1]


@pytest.mark.parametrize(
    ("session", "kind", "why"),
    [
        (_FetchSession(_FetchResponse(403, "nope")), "blocked", "refused by the host"),
        (_FetchSession(_FetchResponse(404, "{}")), "unavailable", "no record at this address"),
        (_FetchSession(_FetchResponse(200, "<!doctype html><html>Just a moment")), "blocked", "interstitial"),
        (_FetchSession(_FetchResponse(200, "not json at all")), "broken", "non-JSON body"),
        (_FetchSession(_FetchResponse(200, "[1, 2")), "broken", "JSONDecodeError"),
        (_FetchSession(raises=OSError("connection reset")), "broken", "OSError"),
        (_FetchSession(raises=ValueError("bad url")), "broken", "ValueError"),
        (_FetchSession(_FetchResponse(200, "[1]")), "broken", "non-object JSON"),
    ],
)
def test_get_json_refuses_with_the_shared_vocabulary(session, kind: str, why: str) -> None:
    """403 → blocked, 404 → unavailable, HTML-200 → blocked, non-JSON → broken, transport → broken,
    and `expect="object"` refuses a list — the verdicts the seven private copies each re-derived."""
    from rung import http

    with pytest.raises(http.FetchRefused) as refused:
        _run(http.get_json(session, "https://x.test/api", timeout=1, expect="object"))
    assert refused.value.kind == kind
    assert why in str(refused.value)


def test_get_text_judges_the_status_only_and_returns_the_page() -> None:
    from rung import http

    page = "<!doctype html><html><body>a server-rendered menu</body></html>"
    assert _run(http.get_text(_FetchSession(_FetchResponse(200, page)), "u", timeout=1)) == page
    with pytest.raises(http.FetchRefused) as refused:
        _run(http.get_text(_FetchSession(_FetchResponse(429, page)), "u", timeout=1))
    assert refused.value.kind == "blocked"
    with pytest.raises(http.FetchRefused) as refused:
        _run(http.get_text(_FetchSession(raises=OSError("reset")), "u", timeout=1))
    assert refused.value.kind == "broken"
