import json
import os
from typing import Literal, overload

from curl_cffi.requests import AsyncSession, Session
from curl_cffi.requests.errors import RequestsError

# Browser TLS/JA3 impersonation is OPT-IN, and the public default is OFF: the open-source core
# makes no attempt to defeat a target's bot detection, so running the published code with defaults
# does not circumvent an access control (see docs/publish_split_design.md, "no target + no
# evasion"). The private overlay enables it at plugin load (intel_plugin.register_all ->
# set_impersonation), and a public user may opt in explicitly via the RUNG_IMPERSONATE env var
# (legacy DISPENSARY_IMPERSONATE still honored), then health-check the profile against Cloudflare
# with the private check_impersonation tool.
# When off, make_session sends an honest, self-identifying User-Agent.
#
# The anti-throttle machinery (the adaptive 406 cooldown + the 406/429 retry + per-request proxy
# rotation) is NOT here — it is private evasion know-how and lives in
# rung_intel.aggregator_http (+ the overlay proxy pool). This module is the honest,
# generic session chokepoint only.
HONEST_USER_AGENT = (
    "rung/0.1 (+https://github.com/richard-burhans/rung)"
)
_impersonate: str | None = (
    os.environ.get("RUNG_IMPERSONATE") or os.environ.get("DISPENSARY_IMPERSONATE") or None
)


def set_impersonation(profile: str | None) -> None:
    """Opt into (``profile`` = a curl_cffi browser profile) or out of (``None``) TLS impersonation.

    Process-wide. The private overlay calls this at plugin load so the real scraping pipeline keeps
    its browser fingerprint; the public default leaves it unset (honest, non-impersonating).
    """
    global _impersonate
    _impersonate = profile


def current_impersonation() -> str | None:
    """The active impersonation profile, or ``None`` when off (the public default)."""
    return _impersonate


def make_session(
    proxy: str | None = None,
    *,
    cookies: dict[str, str] | None = None,
    impersonate: str | None = None,
) -> AsyncSession:
    """Return an ``AsyncSession``: impersonating when a profile is opted in, else honest.

    With impersonation opted in (see :func:`set_impersonation`) the session carries that browser's
    TLS/JA3 + HTTP-2 fingerprint; otherwise it sends the honest :data:`HONEST_USER_AGENT` and
    curl_cffi's plain client fingerprint — no evasion. This is the single session chokepoint
    (enforced by ``tests/test_http.py``) so the impersonation decision is made in exactly one place.

    ``proxy`` is an optional **CONNECT-tunnel** proxy URL (e.g. ``http://user:pass@host:port``);
    ``None`` (the default) goes direct. A tunnelling proxy composes with ``impersonate`` — the
    fingerprint travels end-to-end — but a TLS-terminating (MITM) proxy would defeat it. Forwarding a
    URL is generic; the pool that *picks/rotates/benches* URLs is private
    (``rung_intel.proxy``).

    ``cookies`` seeds the session's cookie jar — most usefully a Cloudflare ``cf_clearance`` token that
    a real browser minted for a challenge this client cannot solve headless (``rung_intel.cf_clearance``).
    ``impersonate`` overrides the opted-in profile for this one session; it MUST match the browser that
    minted such a token, because ``cf_clearance`` is bound to the exact IP **and** the UA/TLS
    fingerprint that solved the challenge — so a mismatched profile (or a different egress) is rejected.

    Usage::

        async with make_session(proxy=pool.acquire(host)) as session:
            response = await session.get(url)
    """
    profile = impersonate or _impersonate
    if profile:
        # curl_cffi types `impersonate` as a fixed Literal; we pass a runtime str (the
        # opted-in profile) on purpose, so the stub can't verify it.
        return AsyncSession(impersonate=profile, proxy=proxy, cookies=cookies)  # ty: ignore[invalid-argument-type, invalid-return-type]
    return AsyncSession(headers={"User-Agent": HONEST_USER_AGENT}, proxy=proxy, cookies=cookies)


def make_sync_session(
    proxy: str | None = None,
    *,
    cookies: dict[str, str] | None = None,
    impersonate: str | None = None,
) -> Session:
    """The synchronous sibling of :func:`make_session` — same impersonation chokepoint, a blocking
    ``Session`` instead of an ``AsyncSession``.

    For the sequential, non-async tools (the librarian's Crossref/DataCite/Unpaywall fetchers) that
    would gain nothing from async but must still route HTTP through the one place the impersonation
    decision is made, rather than reaching for raw ``urllib``/``requests`` (banned by
    ``tests/test_http.py``). Standalone scripts never opt in, so they stay honest by default.

    Usage::

        with make_sync_session() as session:
            response = session.get(url, timeout=25)
    """
    profile = impersonate or _impersonate
    if profile:
        return Session(impersonate=profile, proxy=proxy, cookies=cookies)  # ty: ignore[invalid-argument-type, invalid-return-type]
    return Session(headers={"User-Agent": HONEST_USER_AGENT}, proxy=proxy, cookies=cookies)


# ── Refusals: the one place a fetch decides it may NOT read a response as "no data" ─────────────
#
# Every per-platform fetcher in this project used to end a failed request the same way: `return None`
# or `return []`. A 403, a 429, a 404, a 5xx, a timeout and a Cloudflare page answering 200 all reached
# the rung as the same nothing, the rung returned an empty list, and the registry recorded
# `no_plausible_records` — the bucket the dashboard writes off as "this store simply has no menu".
# On 2026-09-15 every failing menu target in the database sat there: 6,883 of 6,883. A rung that had
# stopped working would have joined them and nothing would have said so.
#
# The access engine (`rung.access`) has the vocabulary to say what happened — `Blocked`, `Unavailable`,
# `Broken` — but a pure platform helper may not import it (the layering rule keeps those modules
# leaf-shaped), so nine modules each grew a hand-written `if response.status_code != 200: return None`.
# This helper is the shared classification they could not share: it lives at tier 0, imports nothing
# internal, and raises a plain `FetchRefused` that carries the KIND of failure; `access._attempt`
# translates the kind into the outcome vocabulary at the one boundary that may name it. One rule, one
# home, and a fetcher that wants to say why it produced nothing calls one function.
#
# The kinds are the engine's, spelled here so the two files cannot disagree:
#   blocked      — we were refused: 401/403/406/429, or an HTML interstitial answering 200 where JSON
#                  was expected (Cloudflare, Incapsula, a captcha), or an edge challenge that NAMES
#                  ITSELF in a header (`x-amzn-waf-action` on AWS WAF's 202, `cf-mitigated` on
#                  Cloudflare's 403 or its older 503) — a header-named challenge is a wall on any
#                  status; an unnamed non-200 is not (P-33's open question, ruled 2026-10-06). The
#                  data exists; this egress may not have it. `refused_statuses` is the status set.
#   unavailable  — the source says no: 404/410 at the address we asked for. Re-trying will not change
#                  it. A caller PROBING candidate addresses catches a refusal per candidate and moves
#                  on, because "not here" is the expected answer for a guess — and maps its host's
#                  OWN "not here" when it is not a 404: Trulieve's Magento answers a wrong store-view
#                  code with a 400 whose body says "Requested store is not found", which
#                  `trulieve.store_not_found` reads as `unavailable` before this generic rule would
#                  call it `broken`. (This comment said the host sent a 404 for sixteen weeks; it
#                  never did — measured 2026-10-06.)
#   broken       — we are wrong, or the host is: any other non-200, a body that is not the payload
#                  shape, a connection error or a timeout. The engine's default for anything a rung
#                  did not explain, and the same word it uses for an unlabelled crash.
#
# What this deliberately does NOT do: decide when to raise. A paged walk that has already taken a
# full page and then fails owes its caller a TRUNCATION signal (`TruncatedMenu`, below), not a
# refusal, because the question there is "may I return what I hold?" and the answer is no either
# way. This helper is for the request whose failure would otherwise be read as an empty result.

REFUSED_STATUSES: frozenset[int] = frozenset({401, 403, 406, 429})
GONE_STATUSES: frozenset[int] = frozenset({404, 410})
#: AWS WAF names its own refusal in this response header: `challenge` (a silent JavaScript
#: proof-of-work) or `captcha`. It rides a **202**, so by status alone it was "any other non-200" —
#: `broken`, "fix the rung" — and 33 storefront menus sat under that verdict from the day they were
#: discovered (2026-10-04). A wall that names itself is believed, whatever status carries it.
WAF_ACTION_HEADER = "x-amzn-waf-action"
#: Cloudflare names ITS refusal here: `challenge` rides a 403 whose body is the "Just a moment..."
#: page. The status alone reads as the host's own answer; the header says an edge judged the
#: CLIENT ADDRESS and the host never saw the request. Measured 2026-10-05: of 40 client addresses
#: asking for the same six resources, 4 drew it for every one and 35 for none, identically on a
#: second ask — so it is a fact about who asked, and says nothing about what was asked for.
CF_MITIGATED_HEADER = "cf-mitigated"
#: Each edge-challenge header and the vendor that sends it — the one place both are named, read by
#: `edge_challenge` (which header) and `refusal` (which vendor).
_EDGE_VENDORS = {CF_MITIGATED_HEADER: "Cloudflare", WAF_ACTION_HEADER: "AWS WAF"}
_HTML_STARTS = ("<!doctype html", "<html")
_JSON_STARTS = ("{", "[")


#: The three words a refusal may carry — a `Literal` so a misspelled kind is a type error here rather
#: than a `KeyError` inside `access._attempt`'s except clause (which no sibling clause catches).
RefusalKind = Literal["blocked", "unavailable", "broken"]


class FetchRefused(Exception):
    """One request ended in something the caller may not read as "no data here".

    ``kind`` is ``blocked`` | ``unavailable`` | ``broken`` (see the block comment above);
    ``status`` is the HTTP status when there was one; ``url`` is what was asked for. The message is
    the whole explanation, written for the `access_methods.error` column a reader will meet it in.
    """

    def __init__(self, kind: RefusalKind, message: str, *, status: int | None = None,
                 url: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.url = url


class TruncatedMenu(FetchRefused):
    """A paged walk stopped AFTER it had taken a full page: what it holds is a FRAGMENT, never a
    shorter menu, and it must not be returned as one.

    Every paged fetcher used to stop on a mid-walk error and return the pages it had. The list was
    non-empty, so the engine recorded ``ok`` with the truncated count — a throttle was
    indistinguishable from a short menu — and the keep-the-best guard is no backstop: it rejects a
    re-scrape only below half the prior count, so a truncation keeping half or more replaced the
    full snapshot, and the observation log appended the fragment regardless. The kind is ``broken``
    (we stopped, the source did not run out), so the engine keeps the prior snapshot.

    One class, here at tier 0, because until 2026-09-24 the pure platform helpers could import
    nothing internal and each of fourteen modules carried its own copy of this paragraph; now they
    may import ``rung.http``, and the copies are aliases of this one.
    """

    def __init__(self, message: str, *, url: str | None = None) -> None:
        super().__init__("broken", message, url=url)


def stop_if_truncated(collected: int, where: object, why: str, *, url: str | None = None) -> None:
    """Raise ``TruncatedMenu`` when a walk that has ALREADY gathered something fails partway.

    ``collected`` is what the walk holds (items, groups, pages — the caller's unit), ``where`` the
    page or category it failed on, ``why`` the failure. Nothing gathered means the failure is the
    request's own refusal, which the caller raises instead; that branch is left to it.
    """
    if collected:
        raise TruncatedMenu(
            f"menu truncated at {where} with {collected} item(s) already gathered: {why}", url=url)


def refusal(response: object, url: str, *, expect: str = "json",
            missing_ok: bool = False) -> FetchRefused | None:
    """Classify a completed response; None means "read the body, it is the payload".

    ``expect`` is what a 200 must carry: ``"json"`` (the default — an HTML body is an interstitial
    and a non-JSON body is a shape change) or ``"html"`` (a server-rendered page; only the status is
    judged). ``missing_ok=True`` turns a 404/410 back into None for a caller for whom "no page here"
    is an ordinary answer — the second program of a one-program menu, a paging walk past its end —
    so that caller keeps its own "nothing here" branch instead of an `unavailable` verdict.
    """
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        return FetchRefused("broken", f"no HTTP status on the response from {url}", url=url)
    # THE EDGE'S OWN HEADER DECIDES, WHATEVER STATUS CARRIES IT. AWS WAF names its challenge on a
    # 202; Cloudflare's managed challenge names itself on a 403 (`cf-mitigated`) — and its older
    # "Under Attack" JavaScript challenge rode a **503**, which by status alone is "any other
    # non-200": `broken`, "fix the rung", for a wall. The audit's P-33 left "is a non-200
    # interstitial a wall?" open because nobody had measured one; the ruling (2026-10-06) is that
    # a header-named challenge is a wall on ANY status, and an unnamed non-200 is not widened into
    # one — it stays `broken`, carrying the head of its body (below) so that the day a 503
    # challenge page arrives it is in the record, not guessed at. Every edge challenge recorded so
    # far (654 refusal rows, 14 of them challenges) rode a 403 or a 202 and named itself.
    edge = edge_challenge(response)
    if edge:
        vendor = _edge_vendor(edge)
        return FetchRefused(
            "blocked", f"HTTP {status} from {url}: {vendor} answered with a challenge ({edge}), "
            "not the page", status=status, url=url)
    if status in REFUSED_STATUSES:
        return FetchRefused("blocked", f"HTTP {status} from {url}: refused by the host",
                            status=status, url=url)
    if status in GONE_STATUSES:
        if missing_ok:
            return None
        return FetchRefused(
            "unavailable", f"HTTP {status} from {url}: the source has no record at this address",
            status=status, url=url)
    if status != 200:
        # Keep the head of an HTML body: a 503 that is an origin outage and a 503 that is a
        # challenge page look identical by status, and the record used to hold only the status.
        head = str(getattr(response, "text", "") or "").lstrip()[:400]
        shape = f" ({head[:120]!r})" if head.lower().startswith(_HTML_STARTS) else ""
        return FetchRefused("broken", f"HTTP {status} from {url}{shape}", status=status, url=url)
    if expect == "json":
        head = str(getattr(response, "text", "") or "").lstrip()[:400]
        lowered = head.lower()
        if lowered.startswith(_HTML_STARTS):
            return FetchRefused(
                "blocked", f"HTML answered 200 at {url}: an interstitial, not the payload "
                f"({head[:120]!r})", status=200, url=url)
        if not lowered.startswith(_JSON_STARTS):
            return FetchRefused(
                "broken", f"non-JSON body answered 200 at {url} ({head[:120]!r})",
                status=200, url=url)
    return None


def edge_challenge(response: object) -> str | None:
    """The edge's own name for a refusal it issued (``"cf-mitigated: challenge"``), or None.

    A response an edge network answered INSTEAD of the host — Cloudflare's managed challenge, AWS
    WAF's challenge or captcha — says nothing about the resource asked for and everything about
    the address that asked. It is still a refusal (`refusal` classifies it `blocked`); this names
    WHICH refusal, so a caller does not record it as the host's verdict on the resource."""
    for name in _EDGE_VENDORS:
        value = _header(response, name)
        if value:
            return f"{name}: {value}"
    return None


def _edge_vendor(edge: str) -> str:
    """The vendor whose header an `edge_challenge` string names — read from the one table, so a third
    header cannot be labelled with another vendor's name (the 2026-10-06 ultra review)."""
    return next(vendor for name, vendor in _EDGE_VENDORS.items() if edge.startswith(f"{name}:"))


def _header(response: object, name: str) -> str | None:
    """One response header, case-insensitively, or None — for a response object that may be a real
    one (a case-insensitive mapping), a test double with a plain dict, or one with no headers."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        items = list(headers.items())
    except AttributeError:
        return None
    for key, value in items:
        if str(key).lower() == name and value:
            return str(value)
    return None


def raise_for_refusal(response: object, url: str, *, expect: str = "json",
                      missing_ok: bool = False) -> None:
    """`refusal`, raised. The one line a fetcher adds after ``await session.get(...)``."""
    refused = refusal(response, url, expect=expect, missing_ok=missing_ok)
    if refused is not None:
        raise refused


def refused_by(exc: BaseException, url: str) -> FetchRefused:
    """Wrap a transport or parse failure (a timeout, a connection reset, a `json.loads` error) as a
    `broken` refusal, so the fetcher's ``except`` clause has one spelling too:
    ``raise http.refused_by(exc, url) from exc``."""
    return FetchRefused("broken", f"{type(exc).__name__} fetching {url}: {exc}", url=url)


# ── The shared fetch: one request, one refusal rule, one parse ─────────────────────────────────
#
# Seven pure helpers each carried the same twelve lines — `session.get`, the transport `except` →
# `refused_by`, `raise_for_refusal`, parse → `refused_by` — under seven private names (`_get_json`
# ×3, `_loader`, `_get` ×3), found by the 2026-09-26 architecture audit (P-37). A copy is a place
# the rule can drift: dispense's returned `{}` for a non-object body where the others raised, and
# treez's caught `ValueError` on the transport leg where waio's did not. These two are the copy
# they share. A helper that still makes its request inline is one of the named exceptions in
# `tests/test_fetcher_shape.py`, each with its reason — a paged walk whose truncation signal must
# stay literal in the helper, a POST pair, a probing rule — and a new copy fails there by shape,
# not by name.

#: The transport failures a fetch turns into a `broken` refusal: the HTTP client's own error, the
#: socket's, and the `ValueError` curl_cffi raises for a malformed URL.
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (RequestsError, OSError, ValueError)


@overload
async def get_json(
    session: AsyncSession, url: str, *, params: dict | None = None, headers: dict | None = None,
    timeout: float, expect: Literal["object"], allow_redirects: bool = True,
) -> dict: ...


@overload
async def get_json(
    session: AsyncSession, url: str, *, params: dict | None = None, headers: dict | None = None,
    timeout: float, expect: Literal["json"] = "json", allow_redirects: bool = True,
) -> object: ...


async def get_json(
    session: AsyncSession,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float,
    expect: Literal["json", "object"] = "json",
    allow_redirects: bool = True,
) -> object:
    """GET ``url`` and return its parsed JSON, or raise `FetchRefused` saying why it could not.

    ``expect="object"`` additionally refuses (`broken`) a body that parses to anything but a dict,
    for a caller that reads keys off the result; ``"json"`` returns whatever parsed (a list is a
    payload for WAIO's walk). The refusal classes are `refusal`'s: a transport error is `broken`,
    an interstitial answering 200 is `blocked`, a non-JSON body is `broken`.
    """
    try:
        response = await session.get(
            url, params=params, headers=headers, timeout=timeout, allow_redirects=allow_redirects,
        )
    except TRANSPORT_ERRORS as exc:
        raise refused_by(exc, url) from exc
    raise_for_refusal(response, url)
    try:
        payload = json.loads(response.text)
    except ValueError as exc:
        raise refused_by(exc, url) from exc
    if expect == "object" and not isinstance(payload, dict):
        raise FetchRefused("broken", f"non-object JSON at {url}", url=url)
    return payload


async def get_text(
    session: AsyncSession,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float,
    allow_redirects: bool = True,
) -> str:
    """GET a server-rendered page and return its text, or raise `FetchRefused`. Only the status is
    judged (`expect="html"`): an HTML body is the payload here, not an interstitial."""
    try:
        response = await session.get(
            url, params=params, headers=headers, timeout=timeout, allow_redirects=allow_redirects,
        )
    except TRANSPORT_ERRORS as exc:
        raise refused_by(exc, url) from exc
    raise_for_refusal(response, url, expect="html")
    return response.text
