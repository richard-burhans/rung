"""Tests for state_search pure helpers (no network): URL filter, classifier, queries."""

from pathlib import Path

from rung.sources import state_search
from rung.sources.state_search import (
    StateInfo,
    _build_queries,
    _classify_url,
    _filter_gov_urls,
)


def test_filter_gov_urls_keeps_gov_drops_noise_and_dupes() -> None:
    out = _filter_gov_urls([
        "https://health.pa.gov/list",
        "https://dispensary.example.com/",      # non-gov → drop
        "https://www.google.com/search?q=x",     # skip domain → drop
        "https://mmp.dhss.mo.gov/page#frag",     # gov; fragment stripped
        "ftp://files.state.gov/x",               # non-http → drop
        "https://health.pa.gov/list",            # duplicate
    ])
    assert out == ["https://health.pa.gov/list", "https://mmp.dhss.mo.gov/page"]


def test_classify_url() -> None:
    assert _classify_url("https://x.gov/roster.pdf") == "pdf"
    assert _classify_url("https://x.gov/a.PDF?v=2") == "pdf"            # query after .pdf
    assert _classify_url("https://maps.google.com/d/abc") == "map"
    assert _classify_url("https://x.gov/data.kml") == "map"
    assert _classify_url("https://api.x.gov/list") == "api"            # api. host
    assert _classify_url("https://x.gov/api/list") == "api"            # /api/ path
    assert _classify_url("https://x.gov/data.json") == "api"
    assert _classify_url("https://x.gov/licensees.html") == "html"     # default


def _info(agency: str) -> StateInfo:
    return StateInfo(
        abbr="PA", name="Pennsylvania", programs="medical",
        program_term="medical marijuana", agency=agency,
    )


def test_build_queries_includes_name_term_and_optional_agency() -> None:
    queries = _build_queries(_info("DOH"))
    assert len(queries) == 4
    assert any("Pennsylvania" in q and "site:gov" in q for q in queries)
    assert any("medical marijuana" in q for q in queries)
    assert any("DOH" in q for q in queries)                            # agency query added
    # No agency → the agency query is dropped.
    assert len(_build_queries(_info(""))) == 3


# ── DDG's anti-bot challenge answers HTTP 202, which is a SUCCESS code ───────────────────────────────
# After one or two queries from an IP, DuckDuckGo serves "Unfortunately, bots use DuckDuckGo too.
# Please complete the following challenge" — 0 results, HTTP **202**. The backend only treated 403 as
# blocked and anything under 400 as fine, so the challenge read as "the search worked and found
# nothing". The <5-links fallback never fired either: the challenge page is a full page.
#
# So the search silently died two queries into a 56-company run, Bing's boilerplate was all that
# remained, and `recon --discover` fabricated homepages for 19 Nevada operators (BATTLE BORN ->
# battle.net). A dead instrument answering 202 is the most comfortable sentence in the codebase: it
# does not raise, it does not warn, and it looks like an answer.

class _Resp:
    def __init__(self, status: int, text: str) -> None:
        self.status_code, self.text = status, text


def test_ddg_challenge_marks_the_backend_blocked_not_empty(monkeypatch) -> None:
    import asyncio
    import contextlib

    from rung.sources import state_search as ss

    challenge = (
        "<html><body><h1>DuckDuckGo</h1>"
        "<p>Unfortunately, bots use DuckDuckGo too. Please complete the following challenge.</p>"
        # a full page: the old "<5 links => blocked" fallback would NOT have fired
        + "".join(f'<a href="/x{i}">l{i}</a>' for i in range(12))
        + "</body></html>"
    )

    class _Session:
        async def get(self, *_a, **_k):
            return _Resp(202, challenge)

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _Session()

    monkeypatch.setattr(ss, "make_session", _fake_session)

    backend = ss._DDGBackend()
    results = asyncio.run(backend.search("anything"))

    assert results == []
    assert backend.blocked is True, (
        "a 202 challenge page is a DEAD BACKEND, not an empty result set. Reporting it as 'no results' "
        "is how discovery came to invent homepages from whatever the other engine returned."
    )


# ── Bing: organic results only ───────────────────────────────────────────────────────────────────
#
# Driven by `tests/fixtures/bing_results.html`, a real results page saved 2026-09-12. The query was
# `"Greenery Spot" cannabis dispensary NY official website`, chosen because the operator's own site
# (greeneryspot.com) does NOT appear on it — so the fixture also pins the honest negative: this
# backend returning a page is not the same as it returning the answer.

_BING_FIXTURE = Path(__file__).parent / "fixtures" / "bing_results.html"


def _fixture_html() -> str:
    import html as html_mod

    return html_mod.unescape(_BING_FIXTURE.read_text(encoding="utf-8"))


def test_only_organic_results_are_read_not_the_whole_page() -> None:
    """The bug this replaces: a whole-page `u=a1…` regex ranks ADVERTISEMENTS as search results.

    Measured on the live page this fixture came from — 30 tracker links against 10 organic ones —
    and for `"Milligrams" cannabis dispensary NJ official website` the first six were walmart.com.
    `homepage_discovery` probes these and `state_lists` follows them, so an ad here is an ad those
    stages treat as evidence.
    """
    import re

    html = _fixture_html()
    whole_page = len(re.findall(r"[?&]u=(a1[A-Za-z0-9_-]+)", html))
    organic = state_search.bing_organic_hrefs(html)
    assert organic, "the fixture has organic blocks; the selector must find them"
    assert len(organic) < whole_page, (
        f"the whole-page scan sees {whole_page} links and the organic one {len(organic)}; if these "
        "are equal the selector is not actually scoping anything"
    )


def test_every_returned_href_is_an_absolute_destination_not_a_tracker() -> None:
    """A caller probes these. A bing.com/ck/a tracker would be probed as if it were the operator."""
    for href in state_search.bing_organic_hrefs(_fixture_html()):
        assert href.startswith("http"), href
        assert "bing.com/ck/" not in href, f"undecoded tracker leaked through: {href}"


def test_a_tracker_decodes_to_its_destination() -> None:
    import base64

    encoded = base64.urlsafe_b64encode(b"https://greeneryspot.com/").decode().rstrip("=")
    assert state_search.bing_destination(f"https://www.bing.com/ck/a?u=a1{encoded}&x=1") \
        == "https://greeneryspot.com/"


def test_a_direct_href_passes_through_and_junk_does_not() -> None:
    assert state_search.bing_destination("https://example.com/x") == "https://example.com/x"
    assert state_search.bing_destination("/relative/path") is None
    assert state_search.bing_destination("https://www.bing.com/ck/a?u=a1!!!not-base64!!!") is None


def test_a_page_with_no_organic_block_blocks_the_backend_rather_than_answering(monkeypatch) -> None:
    """No organic block = a challenge, an error, or markup we no longer read — never 'no results'.

    Falling back to the whole-page scan here is what would answer with advertisements, and a caller
    cannot tell a bad answer from a good one. `blocked` is the only honest verdict.
    """
    import asyncio

    class _Resp:
        status_code = 200
        text = "<html><body><a href='https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9hZC5jb20'>ad</a></body></html>"

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(state_search, "make_session", lambda *a, **k: _Session())
    backend = state_search._BingBackend()
    assert asyncio.run(backend.search("anything", lambda hrefs: hrefs)) == []
    assert backend.blocked, "a page we cannot read must mark the backend blocked"
