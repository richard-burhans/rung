"""Tests for state_lists pure helpers (no network): link classify / score / harvest."""

from rung.sources import state_lists as sl


def test_classify_maps_url_to_resource_type() -> None:
    assert sl._classify("https://x.gov/list.pdf") == "pdf"
    assert sl._classify("https://x.gov/a.PDF?v=1") == "pdf"        # query after ext
    assert sl._classify("https://x.gov/data.csv") == "csv"
    assert sl._classify("https://x.gov/roster.xlsx") == "csv"
    assert sl._classify("https://x.gov/layer.kml") == "kml"
    assert sl._classify("https://www.google.com/maps/d/u/0/edit") == "kml"  # /maps/d/
    assert sl._classify("https://maps.arcgis.com/apps/view") == "arcgis"     # map hint
    assert sl._classify("https://search.dos.gov/") == "lookup"               # search. host
    assert sl._classify("https://x.gov/licensee-lookup") == "lookup"
    assert sl._classify("https://x.gov/dispensaries") == "html"              # default


def test_score_link_deny_and_linkless() -> None:
    assert sl._score_link("https://x.gov/staff", "Staff Directory") == -1   # deny phrase
    assert sl._score_link("https://x.gov/list.pdf", "") == 0                 # no text → skip


def test_score_link_strong_outscores_medium_outscores_zero() -> None:
    strong = sl._score_link("https://x.gov/find", "Find a Dispensary")
    medium = sl._score_link("https://x.gov/loc", "Store Locations")
    none = sl._score_link("https://x.gov/about", "About the Agency")
    assert strong > medium > 0
    assert none == 0


def test_score_link_doc_bonus_requires_a_keyword() -> None:
    # A PDF *with* a dispensary keyword earns the document bonus.
    with_kw = sl._score_link("https://x.gov/dispensaries.pdf", "Licensed Dispensaries")
    # A generic report PDF with no keyword stays at 0 (so it can't outscore the real locator).
    no_kw = sl._score_link("https://x.gov/annual-report.pdf", "Annual Report")
    assert with_kw > 0 and no_kw == 0


def test_harvest_filters_sorts_and_classifies() -> None:
    html = """
      <a href="/find-a-dispensary">Find a Dispensary</a>
      <a href="/staff">Staff Directory</a>
      <a href="#top">jump</a>
      <a href="mailto:x@y.gov">email</a>
      <a href="/store-locations">Store Locations</a>
      <a href="/find-a-dispensary#section">Find a Dispensary</a>
    """
    cands = sl._harvest(html, "https://health.pa.gov/")
    urls = [c.url for c in cands]

    assert "https://health.pa.gov/find-a-dispensary" in urls
    assert "https://health.pa.gov/staff" not in urls          # deny phrase dropped
    assert all(u.startswith("http") and "#" not in u for u in urls)  # anchors/mailto skipped
    # Fragment-variant collapses onto the same URL (deduped, not double-counted).
    assert urls.count("https://health.pa.gov/find-a-dispensary") == 1
    # Sorted best-first: the strong locator outranks the weaker "locations" link.
    assert cands[0].url == "https://health.pa.gov/find-a-dispensary"
    assert cands[0].list_type == "html"
