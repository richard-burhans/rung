"""Tests for homepage_discovery — pure helpers + mocked orchestration (no network)."""

import asyncio

import pytest

from rung.models import CompanyReconRecord
from rung.sources import homepage_discovery as hd, state_search
from rung.sources.homepage_discovery import (
    _MIN_ACCEPT_SCORE,
    _candidate_domains,
    rank_candidates,
)

# ── pure helpers ──────────────────────────────────────────────────────────────

def test_the_state_is_in_the_FIRST_query_not_the_second() -> None:
    """The caller stops at the first query that returns anything, so the state has to lead.

    It used to sit in query #2, behind an unqualified `"<name>" cannabis dispensary official website`
    — which almost always returned *something* and short-circuited the rest. So we were searching the
    whole country for a Nevada shop, and SOCIETY came back as `societycoffeebar.com`.
    """
    queries = hd.build_discovery_queries("Zen Leaf", "PA")
    assert "PA" in queries[0], f"the state must be in the query we actually run: {queries[0]!r}"
    assert '"Zen Leaf"' in queries[0]

    no_state = hd.build_discovery_queries("Zen Leaf", None)
    assert all("None" not in q for q in no_state)


def test_the_query_uses_the_jurisdictions_own_word_for_the_trade() -> None:
    """Michigan says "provisioning center"; Florida "medical marijuana treatment center". Searching
    for "dispensary" where the word is not used is searching for the wrong thing (`states.yml`)."""
    queries = hd.build_discovery_queries("Some Shop", "FL", "medical marijuana")
    assert "medical marijuana" in queries[0]


def test_candidate_domains_drops_aggregators_social_noise_and_dupes() -> None:
    out = hd._candidate_domains([
        "https://www.zenleafdispensaries.com/locations",
        "https://weedmaps.com/dispensaries/zen-leaf",        # aggregator
        "https://www.facebook.com/zenleaf",                   # social
        "https://en.wikipedia.org/wiki/Zen_Leaf",             # noise
        "ftp://nope",                                          # non-http
        "https://www.zenleafdispensaries.com/locations#x",   # dup (fragment stripped)
    ])
    assert out == ["https://www.zenleafdispensaries.com/locations"]


def test_rank_candidates_orders_by_brand_match() -> None:
    ranked = hd.rank_candidates("Zen Leaf", [
        "https://competitor.com/",
        "https://zenleafdispensaries.com/",
        "https://zenleaf.org/menu/store",
    ])
    # Brand-matching hosts top (exact/⊆ brand key); the bare-path .com wins the tie.
    assert ranked[0][0] == "https://zenleafdispensaries.com/" and ranked[0][1] >= 100
    # Zero-overlap competitor ranks last with score 0.
    assert ranked[-1] == ("https://competitor.com/", 0)


# ── orchestration (mocked backend + injected probe) ───────────────────────────

class _FakeBackend(state_search._Backend):
    name = "fake"

    def __init__(self, hrefs: list[str]) -> None:
        self.hrefs = hrefs
        self.blocked = False

    async def search(self, query, filter_fn=state_search._filter_gov_urls):
        return filter_fn(self.hrefs)


def _live_for(live_url: str):
    async def _probe(session, company_id, canonical_name, url):
        if url == live_url:
            return CompanyReconRecord(company_id=company_id, canonical_name=canonical_name,
                                      homepage_url=url, platform="jane", http_status=200)
        return CompanyReconRecord(company_id=company_id, canonical_name=canonical_name,
                                  homepage_url=url, http_status=404, error="http_404")
    return _probe


def _run(coro):
    return asyncio.run(coro)


def test_discover_picks_brand_match_and_validates() -> None:
    backend = _FakeBackend([
        "https://weedmaps.com/x",            # filtered out
        "https://risecannabis.com/",         # brand match + live
        "https://random.com/",               # zero overlap
    ])
    rec = _run(hd.discover_homepage(
        [backend], None, 1, "RISE", "PA", _live_for("https://risecannabis.com/")))
    assert rec.error is None
    assert rec.homepage_url == "https://risecannabis.com/" and rec.platform == "jane"


def test_discover_no_candidates_after_filtering() -> None:
    backend = _FakeBackend(["https://weedmaps.com/x", "https://facebook.com/y"])
    rec = _run(hd.discover_homepage([backend], None, 1, "RISE", "PA", _live_for("x")))
    assert rec.error == "no_homepage_found"


def test_discover_all_candidates_fail_probe() -> None:
    backend = _FakeBackend(["https://risecannabis.com/"])
    rec = _run(hd.discover_homepage([backend], None, 1, "RISE", "PA", _live_for("none-live")))
    assert rec.error == "discovery_unverified"


def test_discover_REFUSES_a_zero_overlap_winner() -> None:
    """This test used to assert the opposite, and in doing so it DEFENDED THE BUG.

    It read: `assert rec.error is None and rec.confidence == "low"` — i.e. a candidate with no brand
    overlap at all was accepted as the homepage, merely tagged low-confidence. But a `confidence` tag
    is not a verdict: the record still carried `error=None` and a `homepage_url`, so it overwrote the
    honest `no_url` and fed Stage 2 a stranger's website. Live on Nevada that produced
    `BATTLE BORN -> battle.net` and `ZEN LEAF -> zensushibar.com`, among 19 fabrications.

    A green test asserting the wrong invariant is worse than no test: it defends the defect. The
    invariant is now the honest one — no evidence means we did not find it.
    """
    backend = _FakeBackend(["https://totallyunrelated.com/"])
    rec = _run(hd.discover_homepage(
        [backend], None, 1, "RISE", "PA", _live_for("https://totallyunrelated.com/")))
    assert rec.homepage_url is None, "a zero-overlap domain must never be recorded as a homepage"
    assert rec.error == "discovery_unverified"


# ── The Nevada incident: `--discover` fabricated a homepage for 19 of 57 operators ───────────────────
#
# Run live on 2026-07-14 against CO/MT/NV (whose rosters publish NO website, so discovery is the only
# path). It wrote, with `error=None` and full confidence:
#
#     BATTLE BORN             -> https://account.battle.net/
#     SOL                     -> https://solana.com/
#     ZEN LEAF                -> https://www.zensushibar.com/
#     DEEP ROOTS HARVEST INC  -> https://deepai.org/
#     SILVER SAGE WELLNESS    -> https://www.silver.com/
#     COOKIES LAS VEGAS       -> http://go.microsoft.com/fwlink/?LinkID=246338
#
# Every one of those REPLACED an honest `no_url`, and Stage 2 would have gone to battle.net looking for
# Battle Born's menu. Three compounding bugs, and the third is the one that matters:
#
#   1. `_score` accepted `host in brand_key` — a fragment of OUR name inside a STRANGER'S domain
#      ("silver" ⊂ SILVER SAGE WELLNESS) scored 100, the maximum.
#   2. `go.microsoft.com/fwlink` is Bing's "unsupported browser" link. When the Bing backend is
#      degraded its own error page gets scraped, and that href survived the domain filter.
#   3. A zero-evidence winner was flagged `confidence="low"` AND RETURNED ANYWAY. A warning is not a
#      verdict — the record still carried a `homepage_url` and no error. This is the same shape as
#      `verify_library._classify`, which printed "these verdicts are not trustworthy" and then
#      returned PAYWALLED.
#
# `no_url` — "we could not find it" — is a BETTER answer than a confident wrong one. These tests use
# the real strings from the incident, so the fix cannot regress into a plausible-looking fabrication.

_FABRICATIONS = [
    ("SILVER SAGE WELLNESS LLC", "https://www.silver.com/"),
    ("BATTLE BORN", "https://account.battle.net/?locale=en-us"),
    ("SOL", "https://solana.com/"),
    ("ZEN LEAF", "https://www.zensushibar.com/"),
    ("DEEP ROOTS HARVEST INC", "https://deepai.org/"),
    ("TOP NOTCH THE HEALTH CENTER", "https://topgolf.com/us/"),
    ("SAHARA WELLNESS", "https://saharareporters.com/"),
    ("STASH FINE", "https://www.stash.com/"),
]


@pytest.mark.parametrize(("name", "url"), _FABRICATIONS, ids=[n for n, _ in _FABRICATIONS])
def test_a_stranger_domain_never_reaches_the_accept_floor(name: str, url: str) -> None:
    """Each of these was WRITTEN to company_recon as this operator's homepage. None may qualify."""
    kept = _candidate_domains([url])
    score = rank_candidates(name, kept)[0][1] if kept else 0
    assert score < _MIN_ACCEPT_SCORE, (
        f"{url!r} would still be accepted as {name}'s homepage (score {score})"
    )


def test_a_search_engines_own_error_page_is_not_a_candidate() -> None:
    """Bing's "unsupported browser" link. A DEAD BACKEND MUST CONTRIBUTE NOTHING — not a live URL."""
    assert _candidate_domains(["http://go.microsoft.com/fwlink/?LinkID=246338"]) == []


@pytest.mark.parametrize(("name", "url"), [
    ("Green Dragon", "https://greendragon.com/about"),
    ("Native Roots", "https://nativerootscannabis.com/shop/"),
    ("Curaleaf", "https://curaleaf.com/"),
    ("The Grove", "https://thegrovenv.com/"),
    ("Zen Leaf", "https://zenleafdispensaries.com/"),
])
def test_a_real_homepage_is_still_accepted(name: str, url: str) -> None:
    """The guard must not be so tight that discovery stops working — these are the true positives."""
    score = rank_candidates(name, _candidate_domains([url]))[0][1]
    assert score >= _MIN_ACCEPT_SCORE, f"{url!r} is genuinely {name}'s site and scored only {score}"


# ── "healthcare" contains "thc" ─────────────────────────────────────────────────────────────────────
# The first cannabis-content check tested `"thc" in text`. A three-letter acronym is a substring of
# ordinary English: heal·THC·are. So it passed johnsoncontrols.com ("building controls for
# healthcare"), mcmurraymed.com and elitelearning.com — which is how a CROSSBOW MANUFACTURER
# (barnettcrossbows.com) and a LOBSTER FRANCHISE (cousinsmainelobster.com) reached the Montana
# homepage-review list. Match acronyms as WORDS.

@pytest.mark.parametrize("text", [
    "We provide healthcare solutions",      # heal-THC-are
    "building controls for healthcare",
    "Northcote Ltd",                        # Nor-THC-ote
    "Crossbows since 1962",
])
def test_a_word_containing_thc_is_not_a_cannabis_signal(text: str) -> None:
    assert not hd.looks_like_cannabis(text), f"{text!r} is not a dispensary"


@pytest.mark.parametrize("text", [
    "Our dispensary sells cannabis flower",
    "THC 24% | CBD 1%",
    "Shop pre-rolls and edibles",
])
def test_a_real_dispensary_page_still_reads_as_cannabis(text: str) -> None:
    assert hd.looks_like_cannabis(text)


def test_the_check_accepts_the_jurisdictions_own_term() -> None:
    """Michigan says "provisioning center"; a page may never use the word "dispensary" at all."""
    assert hd.looks_like_cannabis("Michigan provisioning center", "cannabis")
    assert hd.looks_like_cannabis("Florida medical marijuana treatment center", "medical marijuana")
