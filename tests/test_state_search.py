"""Tests for state_search pure helpers (no network): URL filter, classifier, queries."""

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
