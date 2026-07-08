"""The paper-fetcher example wires the rung engine (access ladder + queue + honest HTTP) to a second,
non-cannabis domain: fetching open-access paper PDFs by DOI, cheapest working host first. The network
fetch itself isn't exercised here (it hits live OA hosts); these cover the pure host-routing +
DOI-resolution logic. See examples/paper_fetcher.py.
"""

from examples import paper_fetcher


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
