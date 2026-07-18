"""Fetch academic-paper PDFs with the `rung` engine — a real, non-cannabis second example.

Fetching a paper is exactly what rung is for: one target (a paper) is reachable several ways at
different cost/success — arXiv, a PLOS "printable" endpoint, a Nature-OA `.pdf`, a BMC `counter/pdf`,
a Frontiers `/pdf`, an ACS `/doi/pdf/`, Unpaywall's locator, a DSpace-7 repository bitstream, the PMC OA
subset, or Europe PMC's full-text XML. So the access ladder is: **resolve the DOI (Crossref), then run
the cheapest rung that returns a real full text, and persist the winning host per paper** so a re-run
skips straight to it.

This is the seed of "paper-rung" — the productionized `acquire` step of the research-librarian loop and a
component of the central knowledge base — and it doubles as evidence that the engine is domain-agnostic
(same `access.run_target` + queue as `examples/custom_domain.py`, a completely different domain).

All HTTP goes through `http.make_session()` (the honest chokepoint). Be polite: this fetches only
open-access papers, one at a time.

    DATABASE_URL=postgresql://rung:rung@localhost:5432/rung \
      uv run python examples/paper_fetcher.py 10.1371/journal.pone.0282396 10.1038/s41598-018-22755-2

    # or a batch — one DOI/title per line:
    uv run python examples/paper_fetcher.py --from dois.txt

TWO THINGS THAT MAKE THIS LADDER LIE, BOTH LEARNED THE HARD WAY
---------------------------------------------------------------
**1. Set `UNPAYWALL_EMAIL`.** Unpaywall requires a real contact address and returns HTTP 422 for a
placeholder, which disables the single most productive rung (it locates an OA copy of ANY DOI on any
repository, preprint server or publisher). Without it the ladder reports fetchable papers as paywalled.

**2. Call `registry.load_plugins()` before ANY fetch.** `http.make_session()` impersonates a browser's
TLS fingerprint only when a profile has been opted into, and the private overlay opts in at plugin load.
Skip the load and every rung runs with the public honest fingerprint — which BMJ, Taylor & Francis and
Wiley answer with a 403. A caller that forgets this does not merely fail; it **attributes its own
misconfiguration to the paper** and reports a free-to-read article as paywalled. `verify_library.py` did
exactly that for five papers. `main()` loads them; so must any script importing this as a library.

The general rule behind both: **a self-inflicted fetch failure must never be recorded as a fact about
the paper.** A rung that cannot tell "I am broken" from "this is paywalled" is worse than no rung.

THE HOST MATRIX (verified 2026-07-13, from the sandbox, through the impersonating session)
------------------------------------------------------------------------------------------
WORKS, direct PDF:  arXiv · PLOS (`article/file?...&type=printable`) · Nature-OA · BMC (`counter/pdf`)
                    · Frontiers · **ACS (`pubs.acs.org/doi/pdf/<doi>`)** · publisher "bronze" PDFs that
                    Unpaywall's `url_for_pdf` points at (BMJ and Taylor & Francis both serve fine).
BOT-WALLED, no PDF: Wiley (`/doi/pdfdirect/` → 403) · MDPI (`/pdf` → a 2 KB interstitial) ·
                    PMC (`/articles/<id>/pdf/` → HTTP 200 + a reCAPTCHA — `fetched_plausible` is what
                    saves us) · `europepmc.org/articles/<id>?pdf=render` → 404 for every article now.
THE ESCAPE HATCH:   for a GOLD-OA paper whose publisher bot-walls the PDF, Europe PMC serves the full
                    text as **JATS XML** (`/rest/<pmcid>/fullTextXML`), free and licensed. That is the
                    `epmc_fulltext` rung, and it is why a Wiley or MDPI article is not lost. It is not a
                    PDF, so it is ranked last and the caller converts it, recording the provenance.
LANDING-PAGE TRAP:  `is_oa=true` does NOT mean fetchable. Unpaywall's `url_for_pdf` is often a
                    repository *landing page* answering HTML (Harvard DASH, RiuNet, … — hence the host-agnostic `dspace` rung; and
                    bepress/Digital Commons institutional repositories, which hide the PDF behind a
                    `viewcontent.cgi` link). Always check the `%PDF` header, never the status code.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from rung import access, db, http, queue, registry

TASK_TYPE = "paper_fetch"
TARGET_TYPE = "paper_pdf"
# Where fetched PDFs land; override with PAPER_FETCH_DIR (e.g. a bibliography drop-zone).
OUT_DIR = Path(os.environ.get("PAPER_FETCH_DIR", "fetched_papers"))


@dataclass
class Fetched:
    doi: str
    host: str
    path: str


def fetched_plausible(record: object) -> bool:
    """A fetch counts only if a real, substantial full text landed on disk.

    Two accepted shapes, because a paper's full text is not always reachable as a PDF: a **PDF**
    (>20 KB, `%PDF-` header) or a **JATS XML** full text (>20 KB, an XML declaration, and an actual
    `<body>` — an abstract-only record is not a full text and must not pass). The header check is what
    stops a block page or a reCAPTCHA from being recorded as a success; see :func:`_oa_pdf_url`.
    """
    path = getattr(record, "path", None)
    if not path:
        return False
    p = Path(path)
    if not p.is_file() or p.stat().st_size <= 20_000:
        return False
    body = p.read_bytes()
    if body[:5] == b"%PDF-":
        return True
    # JATS may arrive with or without an XML declaration, and with leading whitespace: Europe PMC
    # serves some articles as "\n<!DOCTYPE article PUBLIC ...". Demanding a literal "<?xml" prefix
    # rejected perfectly good licensed full text and the ladder then reported the paper PAYWALLED —
    # our defect recorded as a fact about the world. Sniff the shape, don't assume one serialization.
    head = body[:200].lstrip()
    if head.startswith((b"<?xml", b"<!DOCTYPE", b"<article")):
        return b"<body" in body
    return False


# ── DOI resolution (Crossref) — turns a title into a DOI + host, if you only have a title ───────────
def resolve_doi(query: str) -> tuple[str | None, str]:
    """Return (doi, container) for a title via the Crossref API, or (None, '') if unresolved.

    A bare DOI (contains '/') is returned as-is. Crossref fuzzy-matches, so reject obvious
    non-matches ('Correction to', 'Faculty Opinions')."""
    if "/" in query and " " not in query:
        return query, ""
    url = "https://api.crossref.org/works?rows=1&query.bibliographic=" + urllib.parse.quote(query)
    try:
        import json
        with urllib.request.urlopen(url, timeout=25) as resp:  # Crossref: public read-only API
            item = json.load(resp)["message"]["items"][0]
    except Exception:
        return None, ""
    title = (item.get("title") or [""])[0].lower()
    if title.startswith(("correction to", "faculty opinions")):
        return None, ""
    return item.get("DOI"), (item.get("container-title") or [""])[0]


# ── The host fetchers — each is an AccessMethod: build its URL, GET via make_session, verify PDF ────
def _url_for(host: str, doi: str) -> str | None:
    """The OA-PDF URL pattern for a host, or None if the DOI clearly isn't that host's."""
    if host == "arxiv" and doi.startswith("10.48550/arXiv."):
        return f"https://arxiv.org/pdf/{doi.split('arXiv.')[-1]}"
    if host == "plos" and doi.startswith("10.1371/"):
        journal = "ploscompbiol" if "pcbi" in doi else "plosone"
        return f"https://journals.plos.org/{journal}/article/file?id={doi}&type=printable"
    if host == "nature" and doi.startswith("10.1038/"):
        return f"https://www.nature.com/articles/{doi.split('/', 1)[1]}.pdf"
    if host == "bmc" and doi.startswith(("10.1186/", "10.1007/s42238")):
        return f"https://jcannabisresearch.biomedcentral.com/counter/pdf/{doi}.pdf"
    if host == "frontiers" and doi.startswith("10.3389/"):
        return f"https://www.frontiersin.org/articles/{doi}/pdf"
    # ACS serves an open/hybrid article's PDF at /doi/pdf/<doi> while Unpaywall lists only the DOI
    # landing page (which answers HTML) — so the general Unpaywall rung fetches the landing and fails.
    if host == "acs" and doi.startswith("10.1021/"):
        return f"https://pubs.acs.org/doi/pdf/{doi}"
    return None


async def _download_pdf(url: str, doi: str, host: str) -> access.MethodResult:
    """GET a URL via the honest session; succeed only if a real PDF comes back."""
    async with http.make_session() as session:
        try:
            resp = await session.get(url)
        except Exception:
            return [], None, None
    content = resp.content if hasattr(resp, "content") else b""
    if content[:5] != b"%PDF-" or len(content) < 20_000:
        return [], None, None           # HTML block page / gated / missing → this rung "fails"
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / (doi.replace("/", "_") + ".pdf")
    out.write_bytes(content)
    return [Fetched(doi=doi, host=host, path=str(out))], url, None


def _make_fetcher(host: str):
    async def _fetch(_conn: db.DBConn, doi: str, _hint: access.MethodHint) -> access.MethodResult:
        url = _url_for(host, doi)
        if url is None:
            return [], None, None
        return await _download_pdf(url, doi, host)
    return _fetch


def _pmcid_for_doi(doi: str) -> str | None:
    """Ask Europe PMC for the PMCID backing a DOI (any PMC-indexed OA paper), or None."""
    import json
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?format=json&pageSize=1"
           "&query=" + urllib.parse.quote(f'DOI:"{doi}"'))
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:  # Europe PMC: public read-only API
            r = json.load(resp)["resultList"]["result"][0]
    except Exception:
        return None
    return r.get("pmcid")


def not_open_access(conn: db.DBConn) -> set[str]:
    """DOIs the engine recorded as `unavailable` — PMC indexes them, outside the OA subset.

    Read from `access_methods`, not from a module-level set: the engine already persists *why* each
    rung stopped, and a second copy of that fact would drift from the first.
    """
    rows = conn.execute(
        "SELECT target_key FROM access_methods WHERE target_type = %s AND status = 'unavailable'",
        (TARGET_TYPE,),
    ).fetchall()
    return {row[0] for row in rows}


def _oa_pdf_url(pmcid: str) -> str | None:
    """A directly-fetchable OA PDF URL for a PMCID, or None. Records an explicit not-open-access verdict.

    Asks the PMC OA Web Service — the *documented* route, and the only endpoint that separates "this
    paper is not open access" from "our fetch broke". That distinction is the whole point of this rung:
    without it a dead rung is indistinguishable from a paywall, which is how two rungs here rotted
    unnoticed. Free-to-read on PMC is NOT membership of the PMC OA subset, and Unpaywall's `is_oa` does
    not imply it either — a free-to-read deposit carries no redistribution licence.

    Both direct-PDF routes are dead ends, verified 2026-07-10 against OA-subset controls (PMC5438553,
    PMC9831296) and not merely against paywalled papers:

    * ``pmc.ncbi.nlm.nih.gov/articles/<id>/pdf/`` answers **HTTP 200 with a Google reCAPTCHA page** —
      a naive status-code check would record that as a success (``fetched_plausible`` is what stops it).
    * ``europepmc.org/articles/<id>?pdf=render`` now 404s for *every* article.

    Most OA records advertise only a ``tgz`` package on the legacy FTP tree, which NCBI moved under
    ``deprecated/`` and **deletes in August 2026** (``ftp.ncbi.nlm.nih.gov/pub/pmc/readme.txt``), so we
    deliberately do not fetch through it. In practice this rung therefore classifies far more often than
    it downloads; the OA papers it would have fetched are already caught by the cheaper journal rungs.
    """
    url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:  # PMC: public read-only API
            xml = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    if "idIsNotOpenAccess" in xml:
        # The engine's vocabulary, not a bespoke flag: the WORLD says no. `run_target` persists this
        # as 'unavailable', distinctly from a rung that merely broke. See rung/access.py.
        raise access.Unavailable("not in the PMC open-access subset")
    links = re.findall(r'<link format="([^"]+)"[^>]*href="([^"]+)"', xml)
    pdf = next((h for fmt, h in links if fmt == "pdf"), None)
    return pdf.replace("ftp://ftp.ncbi.nlm.nih.gov", "https://ftp.ncbi.nlm.nih.gov") if pdf else None


async def _fetch_pmc_oa(_conn: db.DBConn, doi: str, _hint: access.MethodHint) -> access.MethodResult:
    """Costly last rung: resolve the DOI to a PMCID and fetch the PDF from the PMC OA subset.

    Records a not-open-access verdict rather than reporting a failure, so `main` can tell the user the
    paper is theirs to download by hand instead of implying our ladder is broken.
    """
    pmcid = _pmcid_for_doi(doi)
    if not pmcid:
        return [], None, None
    url = _oa_pdf_url(pmcid)          # raises access.Unavailable when PMC says it is not OA
    if url is None:
        return [], None, None
    return await _download_pdf(url, doi, "pmc_oa")


def _unpaywall_json(doi: str) -> dict | None:
    """Unpaywall's record for a DOI, or None. Centralizes the email gate + honest failure handling so
    every Unpaywall-driven rung (the general locator AND the DASH resolver) shares one story.

    Unpaywall requires a contact email (UNPAYWALL_EMAIL) and REJECTS a placeholder with HTTP 422. The
    old default ("unpaywall@example.com") made this fail on every DOI, and because the failure was
    swallowed it was indistinguishable from "this DOI has no OA copy anywhere" — the rung was silently
    dead, and fetchable papers were reported as paywalled. A non-404 HTTP / network error is
    inconclusive (OUR fault), never a "closed access" verdict.
    """
    import json
    email = os.environ.get("UNPAYWALL_EMAIL")
    if not email:
        if not _unpaywall_json._warned:  # type: ignore[attr-defined]
            print("  note: UNPAYWALL_EMAIL is unset — the Unpaywall rungs are DISABLED "
                  "(the API 422s on a placeholder address). Set it to enable OA lookup.")
            _unpaywall_json._warned = True  # type: ignore[attr-defined]
        return None
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={urllib.parse.quote(email)}"
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:  # Unpaywall: public read-only API
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:  # 404 = DOI unknown (a real "no OA"); anything else is inconclusive
            print(f"  warn: Unpaywall HTTP {exc.code} for {doi} — rung inconclusive, not 'closed'")
        return None
    except Exception as exc:  # network/JSON — likewise inconclusive
        print(f"  warn: Unpaywall lookup failed for {doi}: {type(exc).__name__} — rung inconclusive")
        return None


_unpaywall_json._warned = False  # type: ignore[attr-defined]


def _unpaywall_pdf_urls(doi: str) -> list[str]:
    """EVERY open-access PDF URL Unpaywall knows for a DOI, best location first (deduped).

    Reads `best_oa_location` FIRST but falls through every entry of `oa_locations`. `best_oa_location`
    frequently carries `url_for_pdf: null` — typically a repository landing page — while another
    location (often the publisher, for "bronze" OA) has the direct PDF. Taking only the best location
    silently lost three fetchable papers in one round (Doggett 2024 JAMA Netw Open, Bonn-Miller 2017
    JAMA, Dryburgh 2018 BJCP), each of which reported `is_oa=true`.

    It returns a LIST, not the first hit, because a URL existing is not a PDF arriving: Unpaywall's
    first location is often a publisher URL behind a bot wall (Wiley `pdfdirect` → 403) while a *later*
    location is a clean institutional-repository PDF (e.g. eScholarship) that fetches fine. Trying only
    the first one made us report such papers as paywalled — a block on one host is not a fact about the
    paper. The caller walks the list until a real PDF lands.
    """
    data = _unpaywall_json(doi)
    if not data:
        return []
    best = data.get("best_oa_location") or {}
    urls: list[str] = []
    for loc in (best, *(data.get("oa_locations") or [])):
        pdf = (loc or {}).get("url_for_pdf")
        if pdf and pdf not in urls:
            urls.append(pdf)
    return urls


# ── DSpace-7 repositories — the OA 'url' is an HTML landing page whose real PDF is a
#    /bitstreams/<uuid>/download link inside it. Harvard DASH is the original case, but the SAME DSpace-7
#    pattern serves many institutional repositories (RiuNet/UPV, eScholarship, …), so this rung is
#    HOST-AGNOSTIC: it follows ANY Unpaywall repository landing (a handle.net URL redirects to the real
#    host) and absolutizes the bitstream link against the host the landing actually resolved to — never a
#    hard-coded one. (Generalized from the Harvard-only `dash` rung after a manual RiuNet fetch, 2026-07-17.)
_BITSTREAM = re.compile(r"/bitstreams/[0-9a-fA-F-]+/download")


def _bitstream_path(landing_html: str) -> str | None:
    """The `/bitstreams/<uuid>/download` PATH inside a DSpace-7 landing page, or None.

    A DSpace-7 repository answers its OA URL with an HTML *record* page even where Unpaywall lists it as
    the `url_for_pdf`, so a naive fetch gets HTML and reports the paper paywalled even though its PDF is one
    hop away. The link appears either absolute (`https://host/bitstreams/<uuid>/download`) or relative
    (`/bitstreams/<uuid>/download`); we key on the PATH so both forms parse, then absolutize against the host
    the landing resolved to (`_absolutize`). Take the first — the article PDF is first in practice; a licence
    or thumbnail bitstream, if present, follows it.
    """
    m = _BITSTREAM.search(landing_html)
    return m.group(0) if m else None


def _absolutize(path: str, base_url: str) -> str | None:
    """Join a repository bitstream PATH to the ORIGIN (scheme://host) of the landing URL it was found on."""
    origin = re.match(r"https?://[^/]+", base_url or "")
    return f"{origin.group(0)}{path}" if origin else None


def _repo_landings_for_doi(doi: str) -> list[str]:
    """Every Unpaywall *repository* landing URL for a DOI, best location first (deduped).

    Generalizes the old Harvard-only lookup: a DSpace-7 bitstream can sit on any institutional repository,
    so accept every OA location whose `host_type` is `repository` (and any `handle.net`/`/bitstreams/` URL
    regardless of host_type), rather than filtering to `dash.harvard.edu`. `best_oa_location` often carries
    only a landing URL, so scan every location's URL fields.
    """
    data = _unpaywall_json(doi)
    if not data:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for loc in ((data.get("best_oa_location") or {}), *(data.get("oa_locations") or [])):
        if not loc:
            continue
        is_repo = loc.get("host_type") == "repository"
        for key in ("url_for_pdf", "url", "url_for_landing_page"):
            u = (loc or {}).get(key)
            if not u or u in seen:
                continue
            if is_repo or "handle.net" in u or "/bitstreams/" in u:
                seen.add(u)
                out.append(u)
    return out


async def _get_text_and_url(url: str) -> tuple[str | None, str]:
    """GET a URL via the honest session (following redirects); return (decoded body, final resolved URL).

    The final URL matters: a handle.net landing redirects to the repository host, and the bitstream link is
    absolutized against *that* host, not the handle resolver."""
    async with http.make_session() as session:
        try:
            resp = await session.get(url)
        except Exception:
            return None, url
    content = resp.content if hasattr(resp, "content") else b""
    final = str(getattr(resp, "url", "") or url)
    return (content.decode("utf-8", "replace") if content else None), final


async def _fetch_dspace(_conn: db.DBConn, doi: str, _hint: access.MethodHint) -> access.MethodResult:
    """Repository rung for DSpace-7 landing pages (Harvard DASH, RiuNet/UPV, …): follow each Unpaywall
    repository landing (a handle.net URL redirects to the real host), extract the bitstream link, absolutize
    it against the host the landing resolved to, and fetch the PDF. Complements the Unpaywall rung, which
    returns the landing URL and fails on its HTML. Host-agnostic — the DASH special-case, generalized."""
    for landing in _repo_landings_for_doi(doi):
        html, final_url = await _get_text_and_url(landing)
        if not html:
            continue
        path = _bitstream_path(html)
        if not path:
            continue
        pdf_url = _absolutize(path, final_url)
        if not pdf_url:
            continue
        result = await _download_pdf(pdf_url, doi, "dspace")
        if result[0]:  # a real PDF landed
            return result
    return [], None, None


async def _fetch_epmc_fulltext(_conn: db.DBConn, doi: str, _hint: access.MethodHint) -> access.MethodResult:
    """Last rung: Europe PMC's **full-text XML** for an OA paper the publisher's site won't serve.

    The gap this closes is real and common. A paper can be *gold* open access and still be unfetchable
    as a PDF, because the publisher fronts its PDF with a bot wall — Wiley (`pdfdirect`) and MDPI
    (`/pdf`) both answer our session with a 403 or a 2 KB interstitial, and PMC's own PDF routes are
    dead ends (see :func:`_oa_pdf_url`: reCAPTCHA on one, HTTP 404 on the other). Every earlier rung is
    PDF-only, so those papers were reported "OA but blocked" and pushed onto the human want-list —
    a *free, licensed, machine-readable* full text that we simply never asked for.

    Europe PMC serves it: `.../{pmcid}/fullTextXML` returns the JATS XML for anything in the OA subset.
    It is not a PDF, so it does not satisfy the archival-PDF convention — the caller converts the XML to
    the paper's markdown and records the provenance. It IS the full text, which is what a summary needs.

    Ranked last (cost 3): it is a second API round-trip, and where a real PDF is reachable we want the
    PDF. A DOI outside the OA subset returns no `<body>` and this rung fails cleanly rather than
    inventing a verdict about the paper.
    """
    pmcid = _pmcid_for_doi(doi)
    if not pmcid:
        return [], None, None
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    async with http.make_session() as session:
        try:
            resp = await session.get(url)
        except Exception:
            return [], None, None
    content = resp.content if hasattr(resp, "content") else b""
    # Abstract-only records and error pages both come back small and bodyless — neither is a full text.
    # Accept any JATS serialization: Europe PMC serves some articles as "\n<!DOCTYPE article PUBLIC ..."
    # with no XML declaration at all. Requiring a literal "<?xml" prefix made this rung throw away good
    # licensed full text, and the ladder then called those papers PAYWALLED — our bug, recorded as a fact
    # about the paper. That is the one failure this file exists to prevent.
    if not content[:200].lstrip().startswith((b"<?xml", b"<!DOCTYPE", b"<article")):
        return [], None, None
    if b"<body" not in content or len(content) < 20_000:
        return [], None, None
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / (doi.replace("/", "_") + ".xml")
    out.write_bytes(content)
    return [Fetched(doi=doi, host="epmc_fulltext", path=str(out))], url, None


async def _fetch_unpaywall(_conn: db.DBConn, doi: str, _hint: access.MethodHint) -> access.MethodResult:
    """General OA-locator rung: ask Unpaywall for *any* open-access PDF of this DOI and fetch it.
    Subsumes the hard-coded host rungs for anything OA on a repository/preprint we don't special-case.

    Walks EVERY location Unpaywall lists, not just the first: one host bot-walling its PDF says nothing
    about whether another host serves the same paper freely (see :func:`_unpaywall_pdf_urls`).
    """
    last: access.MethodResult = ([], None, None)
    for pdf_url in _unpaywall_pdf_urls(doi):
        last = await _download_pdf(pdf_url, doi, "unpaywall")
        if last[0]:                       # a real PDF landed — stop; otherwise try the next location
            return last
    return last


def _crossref_pdf_url(doi: str) -> str | None:
    """The publisher's OWN declared full-text PDF link, from Crossref's `message.link[]`.

    Unpaywall only reports what it judges *open access*, so a paper the publisher serves freely but
    has not registered as OA is invisible to it — and the DOI then looks paywalled to us. Crossref
    carries the publisher's own `application/pdf` link regardless of OA status, which is exactly that
    blind spot. This rung found a free-to-read paper the ladder had already written off as PAYWALLED.
    """
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    try:
        import json
        with urllib.request.urlopen(url, timeout=25) as resp:  # Crossref: public read-only API
            links = json.load(resp)["message"].get("link") or []
    except Exception:
        return None
    pdfs = [ln["URL"] for ln in links if ln.get("content-type") == "application/pdf" and ln.get("URL")]
    return pdfs[0] if pdfs else None


async def _fetch_crossref_link(_conn: db.DBConn, doi: str, _hint: access.MethodHint) -> access.MethodResult:
    pdf_url = _crossref_pdf_url(doi)
    if not pdf_url:
        return [], None, None
    return await _download_pdf(pdf_url, doi, "crossref_link")


# arXiv is cost 0 (cheapest); the OA journals are cost 1; PMC/publisher would be higher (omitted here
# because they gate/block direct PDF fetches — a real deployment would add them as costly last rungs).
CATALOG = [
    access.AccessMethod("arxiv", cost_rank=0, run=_make_fetcher("arxiv")),
    access.AccessMethod("plos", cost_rank=1, run=_make_fetcher("plos")),
    access.AccessMethod("nature", cost_rank=1, run=_make_fetcher("nature")),
    access.AccessMethod("bmc", cost_rank=1, run=_make_fetcher("bmc")),
    access.AccessMethod("frontiers", cost_rank=1, run=_make_fetcher("frontiers")),
    access.AccessMethod("acs", cost_rank=1, run=_make_fetcher("acs")),
    # Unpaywall (an API round-trip) locates an OA copy of ANY DOI on ANY host/repository/preprint, so
    # it's the general fallback after the fast direct-journal rungs; the PMC OA subset is a further biomed-OA
    # backstop for the DOI→PMCID case Unpaywall occasionally misses.
    access.AccessMethod("unpaywall", cost_rank=2, run=_fetch_unpaywall),
    # Crossref's own `link[]` carries the PUBLISHER'S declared application/pdf URL, whatever the OA
    # status. Unpaywall reports only what it judges open access, so a paper the publisher serves freely
    # but never registered as OA is invisible to it and reads as paywalled — this rung covers exactly
    # that blind spot, and it rescued a free-to-read paper the ladder had already written off.
    access.AccessMethod("crossref_link", cost_rank=2, run=_fetch_crossref_link),
    # A DSpace-7 repository lists a landing page as its OA URL, so the unpaywall rung above fetches HTML and
    # fails; this rung follows the landing to its bitstream. Same cost tier, tried after unpaywall.
    access.AccessMethod("dspace", cost_rank=2, run=_fetch_dspace),
    access.AccessMethod("pmc_oa", cost_rank=3, run=_fetch_pmc_oa),
    # Last: the full text as JATS XML, for gold-OA papers whose publisher bot-walls the PDF (Wiley,
    # MDPI). Not a PDF, so it is tried only after every PDF route has failed — but a licensed full text
    # beats reporting a free paper as "blocked", which is what we did before this rung existed.
    access.AccessMethod("epmc_fulltext", cost_rank=4, run=_fetch_epmc_fulltext),
]


async def fetch_one(conn: db.DBConn, doi: str) -> tuple[str | None, list[Fetched]]:
    return await access.run_target(conn, TARGET_TYPE, doi, CATALOG, plausible=fetched_plausible)


async def run(conn: db.DBConn, queries: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """Resolve each query to a DOI, then drain the queue fetching each via the host ladder."""
    db.create_engine_tables(conn)   # only the generic infra (no cannabis reference tables)
    dois = {}
    for q in queries:
        doi, _container = resolve_doi(q)
        if doi:
            dois[doi] = q
            queue.enqueue(conn, TASK_TYPE, doi)
        else:
            print(f"  unresolved: {q}")
    conn.commit()

    worker = queue.worker_id()
    results: dict[str, tuple[str | None, str | None]] = {}
    while (job := queue.claim_next(conn, TASK_TYPE, worker)) is not None:
        winner, records = await fetch_one(conn, job.target_key)
        queue.complete(conn, job.id, "done", worker=worker)
        conn.commit()
        results[job.target_key] = (winner, records[0].path if records else None)
    return results


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--from":
        # batch mode: one DOI/title per line (blank lines + '#' comments ignored)
        queries = [ln.strip() for ln in Path(args[1]).read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]
    else:
        queries = args or [
            "10.1371/journal.pone.0282396",   # "Uncomfortably high" — PLOS
            "10.1038/s41598-018-22755-2",     # Jikomes & Zoorob — Nature Sci Reports
            "10.3389/fpls.2021.699530",       # chemotypic markers — Frontiers
        ]
    # Load the plugin overlay before any fetch. A no-op for a public install (no overlay on the entry
    # point) — but where one IS installed it opts the session into TLS impersonation, and without it the
    # publisher hosts (BMJ, Taylor & Francis) 403 on fingerprint alone and the ladder reports a
    # free-to-read paper as paywalled. The rung must not be able to lie about a paper because of how
    # its own session was configured.
    registry.load_plugins()
    conn = db.get_connection()
    results = asyncio.run(run(conn, queries))
    unavailable = not_open_access(conn)   # the engine's own record of WHY, not a second copy
    got = sum(1 for _w, p in results.values() if p)
    print(f"Fetched {got}/{len(results)} (doi: winning host → file):")
    for doi, (winner, path) in results.items():
        if path:
            outcome = path
        elif doi in unavailable:
            # Distinct from a fetch failure: PMC indexes it but it is outside the OA subset. There is
            # nothing for the ladder to fix — the paper carries no redistribution licence.
            outcome = "(not open access — free to read, fetch it by hand)"
        else:
            outcome = "(no OA host worked — paywalled, or a rung is broken)"
        print(f"  {doi}: {winner or 'NONE'} -> {outcome}")
    if unavailable:
        print(f"\n{len(unavailable)} DOI(s) are not in the PMC OA subset. `is_oa` from Unpaywall does "
              "not imply a redistribution licence; these are yours to download by hand.")


if __name__ == "__main__":
    main()
