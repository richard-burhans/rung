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
