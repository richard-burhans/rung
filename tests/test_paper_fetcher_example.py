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
    # ACS: Unpaywall lists only the DOI landing page (HTML), so the direct /doi/pdf/ route is its own rung.
    assert paper_fetcher._url_for("acs", "10.1021/acs.jnatprod.9b01200") == \
        "https://pubs.acs.org/doi/pdf/10.1021/acs.jnatprod.9b01200"
    assert paper_fetcher._url_for("acs", "10.1371/journal.pone.0282396") is None


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


# ── the dspace rung: Unpaywall's repository 'url' is an HTML landing page; follow it to the bitstream ──
# A DSpace-7 repository (Harvard DASH, RiuNet/UPV, …) answers its OA URL with an HTML record page, so the
# unpaywall rung fetches HTML and fails; this HOST-AGNOSTIC rung extracts the /bitstreams/<uuid>/download
# PATH inside and absolutizes it against the host the landing resolved to — otherwise the paper looks
# paywalled even though it is OA.

_DASH_LANDING_HTML = (
    '<html><body><h1>Ecometrics in the Age of Big Data</h1>'
    '<a href="https://dash.harvard.edu/bitstreams/7312037d-953b-6bd4-e053-0100007fdf3b/download">PDF</a>'
    "</body></html>"
)


def test_bitstream_path_extracts_the_download_path_from_landing_html() -> None:
    assert paper_fetcher._bitstream_path(_DASH_LANDING_HTML) == \
        "/bitstreams/7312037d-953b-6bd4-e053-0100007fdf3b/download"


def test_bitstream_path_extracts_a_relative_href() -> None:
    # RiuNet (UPV) and other DSpace-7 repos carry the link relative; the extractor keys on the PATH.
    assert paper_fetcher._bitstream_path('<a href="/bitstreams/abc-123/download">download</a>') == \
        "/bitstreams/abc-123/download"


def test_bitstream_path_returns_none_without_a_bitstream() -> None:
    assert paper_fetcher._bitstream_path("<html>no pdf here</html>") is None


def test_absolutize_uses_the_resolved_host_not_a_hardcoded_one() -> None:
    # The generalization: the origin comes from the host the landing resolved to, so a non-Harvard
    # repository (RiuNet) absolutizes correctly — the Harvard-only rung could not do this.
    path = "/bitstreams/abc-123/download"
    assert paper_fetcher._absolutize(path, "https://dash.harvard.edu/handle/1/2") == \
        "https://dash.harvard.edu/bitstreams/abc-123/download"
    assert paper_fetcher._absolutize(path, "https://riunet.upv.es/entities/publication/xyz") == \
        "https://riunet.upv.es/bitstreams/abc-123/download"
    assert paper_fetcher._absolutize(path, "not-a-url") is None


def test_repo_landings_finds_any_repository_not_just_harvard(monkeypatch) -> None:
    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    # A non-Harvard repository copy (RiuNet handle, host_type=repository) IS now this rung's job —
    # the whole point of the generalization from the Harvard-only `dash` rung.
    riunet = ('{"best_oa_location": {"host_type": "repository", "url_for_pdf": null, '
              '"url": "http://hdl.handle.net/10251/103269"}, "oa_locations": '
              '[{"host_type": "repository", "url": "http://hdl.handle.net/10251/103269"}]}')
    _stub_oa_service(monkeypatch, riunet)
    assert "http://hdl.handle.net/10251/103269" in \
        paper_fetcher._repo_landings_for_doi("10.1016/j.indcrop.2017.04.043")

    # A publisher (non-repository) PDF location is NOT a dspace landing — left to the other rungs.
    publisher = '{"best_oa_location": {"host_type": "publisher", "url_for_pdf": "https://ex.org/x.pdf"}, "oa_locations": []}'
    _stub_oa_service(monkeypatch, publisher)
    assert paper_fetcher._repo_landings_for_doi("10.1/other") == []


# ── what counts as a fetch: a PDF, or a JATS full text — never a block page ───────────────────────
# `fetched_plausible` is the gate that stops an HTML interstitial or a reCAPTCHA from being recorded as
# a successful fetch. It also decides what the epmc_fulltext rung is allowed to return.

def test_fetched_plausible_accepts_a_real_pdf(tmp_path) -> None:
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.7" + b"x" * 30_000)
    assert paper_fetcher.fetched_plausible(paper_fetcher.Fetched("10.1/a", "plos", str(p)))


def test_fetched_plausible_rejects_a_block_page(tmp_path) -> None:
    # MDPI's bot wall and Wiley's 403 body are both HTML — and both would otherwise land on disk.
    p = tmp_path / "b.pdf"
    p.write_bytes(b"<!DOCTYPE html>" + b"x" * 30_000)
    assert not paper_fetcher.fetched_plausible(paper_fetcher.Fetched("10.1/b", "mdpi", str(p)))


def test_fetched_plausible_accepts_a_jats_full_text(tmp_path) -> None:
    # A gold-OA paper the publisher bot-walls is still fetchable as full-text XML from Europe PMC.
    p = tmp_path / "c.xml"
    p.write_bytes(b'<?xml version="1.0"?><article><body>' + b"x" * 30_000 + b"</body></article>")
    assert paper_fetcher.fetched_plausible(paper_fetcher.Fetched("10.1/c", "epmc_fulltext", str(p)))


def test_fetched_plausible_rejects_an_abstract_only_record(tmp_path) -> None:
    # Europe PMC serves a bodyless record for anything outside the OA subset. An abstract is not a full
    # text, and accepting one would let the rung claim a paper we cannot actually read.
    p = tmp_path / "d.xml"
    p.write_bytes(b'<?xml version="1.0"?><article><front>' + b"x" * 30_000 + b"</front></article>")
    assert not paper_fetcher.fetched_plausible(paper_fetcher.Fetched("10.1/d", "epmc_fulltext", str(p)))


def test_epmc_fulltext_rung_declines_when_there_is_no_pmc_record(monkeypatch) -> None:
    # No PMCID is an ordinary "this rung can't serve you" — it must not raise a licence verdict.
    monkeypatch.setattr(paper_fetcher, "_pmcid_for_doi", lambda _doi: None)
    records, _url, _hint = asyncio.run(paper_fetcher._fetch_epmc_fulltext(None, "10.1/none", None))
    assert records == []


def test_epmc_fulltext_is_the_last_rung_tried() -> None:
    # It returns XML, not the archival PDF, so every PDF route must be tried first. Pin the ordering:
    # a cheaper rung regressing below it would silently start preferring XML over a fetchable PDF.
    catalog = {m.name: m.cost_rank for m in paper_fetcher.CATALOG}
    assert catalog["epmc_fulltext"] == max(catalog.values())
    assert catalog["epmc_fulltext"] > catalog["unpaywall"]
