"""Tests for homepage_discovery — pure helpers + mocked orchestration (no network)."""

import asyncio

from rung.models import CompanyReconRecord
from rung.sources import homepage_discovery as hd, state_search

# ── pure helpers ──────────────────────────────────────────────────────────────

def test_build_discovery_queries() -> None:
    queries = hd.build_discovery_queries("Zen Leaf", "PA")
    assert queries[0] == '"Zen Leaf" cannabis dispensary official website'
    assert any("PA" in q for q in queries) and len(queries) == 3
    no_state = hd.build_discovery_queries("Zen Leaf", None)
    assert len(no_state) == 2 and all("None" not in q for q in no_state)


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


def test_discover_zero_overlap_winner_flagged_low() -> None:
    backend = _FakeBackend(["https://totallyunrelated.com/"])
    rec = _run(hd.discover_homepage(
        [backend], None, 1, "RISE", "PA", _live_for("https://totallyunrelated.com/")))
    assert rec.error is None and rec.confidence == "low"
