"""The paper-fetcher example wires the rung engine (access ladder + queue + honest HTTP) to a second,
non-cannabis domain: fetching open-access paper PDFs by DOI, cheapest working host first. The network
fetch itself isn't exercised here (it hits live OA hosts); these cover the pure host-routing +
DOI-resolution logic. See examples/paper_fetcher.py.
"""

import asyncio

import pytest

from examples import paper_fetcher
from rung import access


def test_url_routing_matches_doi_prefix_to_host() -> None:
    # Each host only claims a DOI it can actually serve.
    assert "journals.plos.org" in paper_fetcher._url_for("plos", "10.1371/journal.pone.0282396")
    assert paper_fetcher._url_for("plos", "10.1371/journal.pone.0282396").endswith("printable")
    assert "nature.com/articles/s41598-018-22755-2.pdf" in paper_fetcher._url_for("nature", "10.1038/s41598-018-22755-2")
    assert "frontiersin.org" in paper_fetcher._url_for("frontiers", "10.3389/fpls.2021.699530")
    assert "biomedcentral.com" in paper_fetcher._url_for("bmc", "10.1186/s42238-019-0001-1")
    assert "arxiv.org/pdf/2606.14525" in paper_fetcher._url_for("arxiv", "10.48550/arXiv.2606.14525")


def test_url_routing_returns_none_for_wrong_host() -> None:
    # A PLOS DOI is not fetchable via the Nature/Frontiers/BMC/arXiv rungs — they decline (None),
    # which is what makes the ladder try the next rung until the right host succeeds.
    assert paper_fetcher._url_for("nature", "10.1371/journal.pone.0282396") is None
    assert paper_fetcher._url_for("frontiers", "10.1038/s41598-018-22755-2") is None
    assert paper_fetcher._url_for("arxiv", "10.1371/journal.pone.0282396") is None


def test_resolve_doi_passes_through_a_bare_doi() -> None:
    # A bare DOI needs no Crossref call.
    doi, container = paper_fetcher.resolve_doi("10.1371/journal.pone.0282396")
    assert doi == "10.1371/journal.pone.0282396"
    assert container == ""


def test_ploscompbiol_journal_is_routed() -> None:
    assert "ploscompbiol" in paper_fetcher._url_for("plos", "10.1371/journal.pcbi.1004333")


# ── the PMC OA rung: "not open access" is a verdict, not a failure ────────────────────────────────
# A rung that cannot tell "this paper carries no redistribution licence" from "my fetch broke" reports
# both as "paywalled". That is how two rungs here rotted unnoticed, so pin the distinction.

_NOT_OA_XML = (
    '<OA><request id="PMC4913118"/>'
    '<error code="idIsNotOpenAccess">identifier \'PMC4913118\' is not Open Access</error></OA>'
)
_OA_TGZ_ONLY_XML = (
    '<OA><records><record id="PMC5438553" license="CC BY">'
    '<link format="tgz" href="ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_package/d1/78/PMC5438553.tar.gz" />'
    "</record></records></OA>"
)
_OA_PDF_XML = (
    '<OA><records><record id="PMC999" license="CC BY">'
    '<link format="pdf" href="ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_pdf/aa/bb/PMC999.pdf" />'
    "</record></records></OA>"
)


class _FakeResponse:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _stub_oa_service(monkeypatch, xml: str) -> None:
    monkeypatch.setattr(paper_fetcher.urllib.request, "urlopen", lambda *_a, **_k: _FakeResponse(xml))


def test_oa_service_raises_the_engine_unavailable_signal(monkeypatch) -> None:
    # Free-to-read on PMC is NOT membership of the OA subset; Unpaywall's `is_oa` doesn't imply it.
    # The rung says so in the ENGINE's vocabulary, so run_target persists 'unavailable' — distinctly
    # from a rung that merely broke. That distinction is the whole point (rung/access.py).
    _stub_oa_service(monkeypatch, _NOT_OA_XML)
    with pytest.raises(access.Unavailable, match="open-access subset"):
        paper_fetcher._oa_pdf_url("PMC4913118")


def test_oa_service_declines_a_package_only_record(monkeypatch) -> None:
    # Most OA records advertise only a `tgz` on the legacy FTP tree that NCBI deletes in Aug 2026, so
    # the rung declines rather than depending on it. Declining is not a not-open-access verdict.
    _stub_oa_service(monkeypatch, _OA_TGZ_ONLY_XML)
    assert paper_fetcher._oa_pdf_url("PMC5438553") is None


def test_oa_service_returns_an_https_pdf_when_one_is_advertised(monkeypatch) -> None:
    # The service still advertises ftp:// hrefs; the same paths serve over HTTPS.
    _stub_oa_service(monkeypatch, _OA_PDF_XML)
    url = paper_fetcher._oa_pdf_url("PMC999")
    assert url == "https://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_pdf/aa/bb/PMC999.pdf"


def test_pmc_oa_rung_raises_rather_than_returning_a_silent_empty(monkeypatch) -> None:
    # The rung yields nothing, but it does not go quiet: it raises the signal that tells `run_target`
    # to persist 'unavailable'. A silent empty return would be recorded as 'failed' — unknown — which
    # is the honest default but the wrong answer here.
    doi = "10.1016/j.jcm.2016.02.012"
    monkeypatch.setattr(paper_fetcher, "_pmcid_for_doi", lambda _doi: "PMC4913118")
    _stub_oa_service(monkeypatch, _NOT_OA_XML)

    with pytest.raises(access.Unavailable):
        asyncio.run(paper_fetcher._fetch_pmc_oa(None, doi, None))


def test_pmc_oa_rung_stays_silent_when_there_is_no_pmc_record(monkeypatch) -> None:
    # No PMC record at all is an ordinary "this rung can't serve you", not a licence verdict. It must
    # NOT raise Unavailable — that would assert something about the world it has not checked.
    doi = "10.48550/arXiv.2305.14325"
    monkeypatch.setattr(paper_fetcher, "_pmcid_for_doi", lambda _doi: None)
    records, _url, _hint = asyncio.run(paper_fetcher._fetch_pmc_oa(None, doi, None))
    assert records == []


# ── the DASH rung: Unpaywall's 'pdf' URL is an HTML landing page; follow it to the bitstream ───────
# DASH answers its OA URL with an HTML record page, so the unpaywall rung fetches HTML and fails; this
# rung extracts the bitstream PDF link inside — otherwise the paper looks paywalled even though it's OA.

_DASH_LANDING_HTML = (
    '<html><body><h1>Ecometrics in the Age of Big Data</h1>'
    '<a href="https://dash.harvard.edu/bitstreams/7312037d-953b-6bd4-e053-0100007fdf3b/download">PDF</a>'
    "</body></html>"
)


def test_dash_bitstream_url_extracts_the_download_link_from_landing_html() -> None:
    url = paper_fetcher._dash_bitstream_url(_DASH_LANDING_HTML)
    assert url == "https://dash.harvard.edu/bitstreams/7312037d-953b-6bd4-e053-0100007fdf3b/download"


def test_dash_bitstream_url_absolutizes_a_relative_href() -> None:
    # DASH pages carry the link both absolute and relative; the extractor keys on the path so both work.
    html = '<a href="/bitstreams/abc-123/download">download</a>'
    assert paper_fetcher._dash_bitstream_url(html) == "https://dash.harvard.edu/bitstreams/abc-123/download"


def test_dash_bitstream_url_returns_none_without_a_bitstream() -> None:
    assert paper_fetcher._dash_bitstream_url("<html>no pdf here</html>") is None


def test_dash_landing_is_found_only_for_a_dash_hosted_oa_copy(monkeypatch) -> None:
    # A DASH-hosted OA copy is recognized by host; a non-DASH repository copy is not this rung's job.
    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")  # or _unpaywall_json short-circuits to None
    dash_json = ('{"best_oa_location": {"url_for_pdf": '
                 '"http://nrs.harvard.edu/urn-3:HUL.InstRepos:17692600"}, "oa_locations": []}')
    _stub_oa_service(monkeypatch, dash_json)
    assert paper_fetcher._dash_landing_for_doi("10.1177/0081175015576601") == \
        "http://nrs.harvard.edu/urn-3:HUL.InstRepos:17692600"

    other_json = '{"best_oa_location": {"url_for_pdf": "https://example.org/x.pdf"}, "oa_locations": []}'
    _stub_oa_service(monkeypatch, other_json)
    assert paper_fetcher._dash_landing_for_doi("10.1/other") is None
