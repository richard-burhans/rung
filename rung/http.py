import os

from curl_cffi.requests import AsyncSession, Session

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
