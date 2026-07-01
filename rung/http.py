import os

from curl_cffi.requests import AsyncSession

# Browser TLS/JA3 impersonation is OPT-IN, and the public default is OFF: the open-source core
# makes no attempt to defeat a target's bot detection, so running the published code with defaults
# does not circumvent an access control (see docs/publish_split_design.md, "no target + no
# evasion"). The private overlay enables it at plugin load (intel_plugin.register_all ->
# set_impersonation), and a public user may opt in explicitly via the DISPENSARY_IMPERSONATE env
# var (then health-check the profile against Cloudflare with the private check_impersonation tool).
# When off, make_session sends an honest, self-identifying User-Agent.
#
# The anti-throttle machinery (the adaptive 406 cooldown + the 406/429 retry + per-request proxy
# rotation) is NOT here — it is private evasion know-how and lives in
# dispensary_scraper_intel.aggregator_http (+ the overlay proxy pool). This module is the honest,
# generic session chokepoint only.
HONEST_USER_AGENT = (
    "rung/0.1 (+https://github.com/richard-burhans/rung)"
)
_impersonate: str | None = os.environ.get("DISPENSARY_IMPERSONATE") or None


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


def make_session(proxy: str | None = None) -> AsyncSession:
    """Return an ``AsyncSession``: impersonating when a profile is opted in, else honest.

    With impersonation opted in (see :func:`set_impersonation`) the session carries that browser's
    TLS/JA3 + HTTP-2 fingerprint; otherwise it sends the honest :data:`HONEST_USER_AGENT` and
    curl_cffi's plain client fingerprint — no evasion. This is the single session chokepoint
    (enforced by ``tests/test_http.py``) so the impersonation decision is made in exactly one place.

    ``proxy`` is an optional **CONNECT-tunnel** proxy URL (e.g. ``http://user:pass@host:port``);
    ``None`` (the default) goes direct. A tunnelling proxy composes with ``impersonate`` — the
    fingerprint travels end-to-end — but a TLS-terminating (MITM) proxy would defeat it. Forwarding a
    URL is generic; the pool that *picks/rotates/benches* URLs is private
    (``dispensary_scraper_intel.proxy``).

    Usage::

        async with make_session(proxy=pool.acquire(host)) as session:
            response = await session.get(url)
    """
    if _impersonate:
        # curl_cffi types `impersonate` as a fixed Literal; we pass a runtime str (the
        # opted-in profile) on purpose, so the stub can't verify it.
        return AsyncSession(impersonate=_impersonate, proxy=proxy)  # ty: ignore[invalid-argument-type, invalid-return-type]
    return AsyncSession(headers={"User-Agent": HONEST_USER_AGENT}, proxy=proxy)
