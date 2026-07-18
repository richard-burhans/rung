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

import re
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

# A brand fragment shorter than this matches unrelated domains by coincidence ("sol" is inside
# "solana"), so it cannot earn a match on its own.
_MIN_MATCH_CHARS = 5

# What an operator appends to its own brand to make a domain. This is what separates a REAL short
# brand from a coincidence, and a length rule alone cannot: RISE's site is `risecannabis.com`, while
# SOL was handed `solana.com` — BOTH hosts begin with the brand. The difference is entirely in what
# follows it. "cannabis" is a qualifier; "ana" is the rest of somebody else's word.
_DOMAIN_SUFFIXES = frozenset({
    "", "cannabis", "cannabisco", "cannabiscompany", "dispensary", "dispensaries", "marijuana",
    "weed", "wellness", "shop", "store", "farms", "farm", "co", "collective", "cannabisdispensary",
    "dispensarynv", "nv", "mt", "co2", "colorado", "montana", "nevada", "gardens", "provisions",
    "botanicals", "organics", "remedies", "therapeutics", "labs", "brands", "group", "holdings",
})

# The floor for ACCEPTING a discovered homepage. 100+ = the whole brand appears in the host (the
# only really trustworthy signal); 2 = at least two substantial identity tokens do. Below that we
# have no evidence, and the honest answer is that we did not find it.
_MIN_ACCEPT_SCORE = 2

# THE PAGE MUST SAY IT SELLS CANNABIS. A domain name cannot establish identity, and no scoring rule
# fixes that: the search query already asks for a "cannabis dispensary", and the engine still returned
# `societycoffeebar.com` for SOCIETY and `cultivateconnecticut.com` for CULTIVATE — the brand really IS
# in the host, it just belongs to somebody else. The only evidence that a site is THIS operator's
# dispensary is that it is a dispensary at all.
#
# The vocabulary spans what the trade is actually called, which VARIES BY JURISDICTION: a "dispensary"
# in Nevada is a "provisioning center" in Michigan, a "medical marijuana treatment center" in Florida,
# and a "retail store" in Ontario. `states.yml` carries each jurisdiction's own `program_term`, and the
# caller passes it in; these are the cross-jurisdiction terms no real menu page omits.
_CANNABIS_SIGNALS = (
    "cannabis", "marijuana", "dispensary", "dispensaries", "provisioning center",
    "treatment center", "cannabis retailer", "retail cannabis", "cannabinoid",
    "thc", "cbd", "budtender", "pre-roll", "preroll", "indica", "sativa", "edibles",
)


def looks_like_cannabis(text: str, program_term: str | None = None) -> bool:
    """True when a fetched page says, anywhere, that it sells cannabis.

    **Matched on WORD BOUNDARIES, and that is not a nicety.** The first version of this check tested
    `"thc" in text` — and `heal·thc·are` contains "thc". So it passed `johnsoncontrols.com` (building
    controls "for healthcare"), `mcmurraymed.com` and `elitelearning.com`, which is how a crossbow
    manufacturer and a lobster franchise reached the Montana review list. A three-letter acronym is a
    substring of ordinary English; it must be matched as a word.

    Deliberately generous otherwise — a false NEGATIVE here just leaves an operator at `no_url`, which
    is the honest state anyway, while a false POSITIVE puts a stranger's website forward as this
    operator's homepage. Cheap to be strict; expensive to be wrong.
    """
    terms = (*_CANNABIS_SIGNALS, *((program_term.lower(),) if program_term else ()))
    pattern = "|".join(re.escape(term) for term in terms)
    return re.search(rf"\b(?:{pattern})\b", text.lower()) is not None

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
    # A SEARCH ENGINE'S OWN CHROME. `go.microsoft.com/fwlink/?LinkID=246338` is Bing's
    # "your browser is unsupported" link: when the Bing backend is degraded (blocked, or its
    # markup moved) its error page is scraped and THIS is the href that survives the filter. It
    # was then ranked, probed (it answers 200) and written as the homepage of 19 Nevada
    # operators. A dead backend must contribute NOTHING, never a plausible-looking URL.
    "microsoft.com", "live.com", "msn.com", "office.com", "windows.com",
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


def build_discovery_queries(
    canonical_name: str, state: str | None, program_term: str | None = None
) -> list[str]:
    """A small, most-specific-first set of search queries for an operator.

    **The caller stops at the FIRST query that returns anything**, so whatever is most specific has to
    come first — and the state did not. It sat in query #2, behind an unqualified
    `"<name>" cannabis dispensary official website`, which almost always returned *something* and
    short-circuited the rest. That is how SOCIETY (Nevada) came back as `societycoffeebar.com`: we
    never told the engine which state we meant.

    `program_term` is the jurisdiction's own word for the trade (`states.yml`) — Michigan says
    "provisioning center", Florida "medical marijuana treatment center". Searching for "dispensary" in
    a state that does not use the word is searching for the wrong thing.
    """
    name = canonical_name.strip()
    term = (program_term or "cannabis").strip()
    queries: list[str] = []
    if state:
        queries.append(f'"{name}" {term} dispensary {state} official website')
        queries.append(f'"{name}" dispensary {state}')
    queries.append(f'"{name}" {term} dispensary official website')
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


def _host_is_the_brand(brand_key: str, host: str) -> bool:
    """True when `host` is this brand's own domain, not a stranger's that happens to contain it.

    Three ways to be sure, and the middle one is the whole point:

    * the host IS the brand (``curaleaf`` -> ``curaleaf.com``);
    * the host is the brand plus a QUALIFIER it chose (``rise`` -> ``risecannabis``). A bare length
      floor cannot do this: ``rise``/``risecannabis`` and ``sol``/``solana`` both begin with the
      brand, and only the remainder tells them apart — "cannabis" is a qualifier, "ana" is the rest
      of somebody else's word;
    * the brand appears anywhere in a host long enough that coincidence is implausible
      (``zenleaf`` -> ``zenleafdispensaries``).
    """
    if not brand_key or not host:
        return False
    if host == brand_key:
        return True
    if host.startswith(brand_key) and host[len(brand_key):] in _DOMAIN_SUFFIXES:
        return True
    return len(brand_key) >= _MIN_MATCH_CHARS and brand_key in host


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
        if not host or not brand_key:
            return 0
        # THE BRAND MUST APPEAR IN THE HOST. The reverse — `host in brand_key` — was accepted as a
        # match and is not evidence of anything: "silver" is a fragment of SILVER SAGE WELLNESS, and
        # matching it handed that operator `silver.com`. Likewise "battle" -> battle.net for BATTLE
        # BORN, "stash" -> stash.com, "sol" -> solana.com. A piece of our own name found in a
        # stranger's domain says nothing about whose domain it is.
        #
        # And the containment must be SUBSTANTIAL: a 3-character key matches half the web ("sol" is
        # inside "solana"), so a short brand cannot earn the strong score by containment alone.
        if _host_is_the_brand(brand_key, host):
            return 100 + len(tokens)
        return sum(1 for token in tokens if len(token) >= _MIN_MATCH_CHARS and token in host)

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
    program_term: str | None = None,
) -> CompanyReconRecord:
    """Search → filter → rank → validate. Returns the validated recon record for the best
    live candidate, or a record with an ``error`` describing why none was used.

    ``probe`` is recon._probe_one (injected to avoid a circular import); the first ranked
    candidate it confirms live (error None, http_status < 400) wins. A zero-overlap winner
    is downgraded to ``confidence="low"`` for the human-review summary.
    """
    urls: list[str] = []
    for query in build_discovery_queries(canonical_name, state, program_term):
        urls = await _search_with_rotation(backends, query, _candidate_domains)
        if urls:
            break
    ranked = rank_candidates(canonical_name, urls)
    if not ranked:
        return CompanyReconRecord(
            company_id=company_id, canonical_name=canonical_name,
            error="no_homepage_found",
        )
    # A LIVE URL IS NOT THE RIGHT URL, AND `no_url` IS A BETTER ANSWER THAN A CONFIDENT WRONG ONE.
    #
    # This loop used to accept the best-ranked candidate that merely ANSWERED — flagging a
    # zero-overlap winner `confidence="low"` and returning it anyway. A warning is not a verdict:
    # the record still carried `error=None` and a `homepage_url`, so it REPLACED the honest `no_url`
    # and would have sent Stage 2 to scrape battle.net for BATTLE BORN's menu. Every candidate
    # answers 200; that is what a homepage does. Requiring the domain to actually match the brand is
    # the only thing standing between "we could not find it" and a fabricated fact about the operator.
    for url, score in ranked[:_PROBE_CAP]:
        if score < _MIN_ACCEPT_SCORE:
            break                                    # ranked best-first: nothing below will qualify
        record = await probe(session, company_id, canonical_name, url)
        if record.error is None and (record.http_status is None or record.http_status < 400):
            return record
    return CompanyReconRecord(
        company_id=company_id, canonical_name=canonical_name,
        error="discovery_unverified",
    )
