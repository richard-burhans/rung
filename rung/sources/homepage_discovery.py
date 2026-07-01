"""Discover an operator's own homepage by web search (opt-in recon fallback).

For 0%-website states, an operator that isn't hand-seeded in ``company_homepages.yml``
and has no ``dispensaries.website`` to vote on falls to ``error="no_url"`` in recon and
never reaches the own-site Stage-2 ladder. This module web-searches such an operator by
name, drops aggregator/social/noise results, ranks the survivors by how well their domain
matches the brand, and lets the caller validate the top candidate(s) with recon's probe.

It reuses ``state_search``'s search backends (with a homepage-specific result filter) and
takes the probe as an injected callable to avoid importing recon (which imports this).
Network-bound and fuzzy, so it is wired in only behind ``recon --discover``.
"""

from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

from rung.models import CompanyReconRecord
from rung.sources.state_search import (
    _Backend,
    _BingBackend,
    _DDGBackend,
    _search_with_rotation,
)
from rung.text import normalize_brand

# Validates a candidate URL (recon._probe_one): (session, company_id, name, url) -> record.
ProbeFn = Callable[[AsyncSession, int, str, str], Awaitable[CompanyReconRecord]]

_PROBE_CAP = 3  # validate at most this many ranked candidates per company

# Domains a real operator homepage is never on: directories/aggregators, social/review,
# and search/maps/wiki/jobs noise. Substring match against the (www-stripped) netloc.
_EXCLUDED_DOMAINS = (
    # aggregators / menu directories
    "weedmaps.com", "leafly.com", "dutchie.com", "iheartjane.com", "jane.app",
    "allbud.com", "leafbuyer.com", "wikileaf.com", "cannabis.net", "where-to-buy",
    # social / review
    "facebook.com", "instagram.com", "twitter.com", "x.com", "yelp.com", "linkedin.com",
    "tripadvisor.com", "pinterest.com", "tiktok.com", "foursquare.com", "nextdoor.com",
    # search / maps / wiki / directories / jobs / news noise
    "google.", "bing.com", "duckduckgo.com", "wikipedia.org", "reddit.com", "youtube.com",
    "maps.apple.com", "mapquest.com", "yellowpages.com", "bbb.org", "indeed.com",
    "glassdoor.com", "ziprecruiter.com", "mjbizdaily.com",
)

# Brand-name words that carry no identity, so they don't count toward a domain match.
_GENERIC_TOKENS = frozenset({
    "cannabis", "dispensary", "dispensaries", "marijuana", "weed", "wellness",
    "co", "llc", "inc", "the", "company", "shop", "store", "group", "holdings",
})


def make_backends() -> list[_Backend]:
    """The curl_cffi search backends used for discovery (no browser). Create ONE list per
    recon run and share it across companies so a backend's ``blocked`` flag persists."""
    return [_DDGBackend(), _BingBackend()]


def build_discovery_queries(canonical_name: str, state: str | None) -> list[str]:
    """A small, most-specific-first set of search queries for an operator (search is
    rate-limited, so keep it short — the caller stops at the first query with results)."""
    name = canonical_name.strip()
    queries = [f'"{name}" cannabis dispensary official website']
    if state:
        queries.append(f'"{name}" dispensary {state}')
    queries.append(f"{name} marijuana dispensary")
    return queries


def _candidate_domains(hrefs: list[str]) -> list[str]:
    """A ``filter_fn`` for the search backends: keep plausible operator-homepage URLs,
    dropping aggregators/social/noise and non-http; dedupe preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for href in hrefs:
        if not href or not href.startswith("http"):
            continue
        netloc = urlparse(href).netloc.lower().removeprefix("www.")
        if not netloc or any(bad in netloc for bad in _EXCLUDED_DOMAINS):
            continue
        page = urlparse(href)._replace(fragment="").geturl()
        if page not in seen:
            seen.add(page)
            out.append(page)
    return out


def _brand_tokens(canonical_name: str) -> set[str]:
    """Identity-bearing tokens of a brand (normalized, generics dropped)."""
    raw = canonical_name.lower().replace("-", " ").replace("/", " ").split()
    return {t for t in (normalize_brand(part) for part in raw) if t and t not in _GENERIC_TOKENS}


def _host_label(url: str) -> str:
    """The registrable second-level label of a URL's host, brand-normalized.

    ``https://shop.zenleafdispensaries.com/x`` -> ``zenleafdispensaries``.
    """
    netloc = urlparse(url).netloc.lower().removeprefix("www.")
    parts = netloc.split(".")
    label = parts[-2] if len(parts) >= 2 else netloc
    return normalize_brand(label)


def rank_candidates(canonical_name: str, urls: list[str]) -> list[tuple[str, int]]:
    """Rank candidate URLs best-first as ``(url, overlap_score)``.

    Score: an exact brand⊆host (or host⊆brand) match dominates (100+); otherwise the count
    of brand tokens appearing in the host label. Ties prefer a bare path, https, then .com.
    Zero-overlap candidates rank last but are kept (the caller decides whether to accept one,
    flagging it low-confidence).
    """
    brand_key = normalize_brand(canonical_name)
    tokens = _brand_tokens(canonical_name)

    def _score(url: str) -> int:
        host = _host_label(url)
        if brand_key and host and (brand_key in host or host in brand_key):
            return 100 + len(tokens)
        return sum(1 for token in tokens if token in host)

    def _sort_key(url: str) -> tuple:
        parsed = urlparse(url)
        return (
            -_score(url),
            len(parsed.path.rstrip("/")),
            0 if parsed.scheme == "https" else 1,
            0 if parsed.netloc.lower().endswith(".com") else 1,
            url,
        )

    return [(url, _score(url)) for url in sorted(urls, key=_sort_key)]


async def discover_homepage(
    backends: list[_Backend],
    session: AsyncSession,
    company_id: int,
    canonical_name: str,
    state: str | None,
    probe: ProbeFn,
) -> CompanyReconRecord:
    """Search → filter → rank → validate. Returns the validated recon record for the best
    live candidate, or a record with an ``error`` describing why none was used.

    ``probe`` is recon._probe_one (injected to avoid a circular import); the first ranked
    candidate it confirms live (error None, http_status < 400) wins. A zero-overlap winner
    is downgraded to ``confidence="low"`` for the human-review summary.
    """
    urls: list[str] = []
    for query in build_discovery_queries(canonical_name, state):
        urls = await _search_with_rotation(backends, query, _candidate_domains)
        if urls:
            break
    ranked = rank_candidates(canonical_name, urls)
    if not ranked:
        return CompanyReconRecord(
            company_id=company_id, canonical_name=canonical_name,
            error="no_homepage_found",
        )
    for url, score in ranked[:_PROBE_CAP]:
        record = await probe(session, company_id, canonical_name, url)
        if record.error is None and (record.http_status is None or record.http_status < 400):
            if score == 0:
                record.confidence = "low"
            return record
    return CompanyReconRecord(
        company_id=company_id, canonical_name=canonical_name,
        error="discovery_unverified",
    )
