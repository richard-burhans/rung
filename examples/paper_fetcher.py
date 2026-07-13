"""Fetch academic-paper PDFs with the `rung` engine — a real, non-cannabis second example.

Fetching a paper is exactly what rung is for: one target (a paper) is reachable several ways at
different cost/success — arXiv, a PLOS "printable" endpoint, a Nature-OA `.pdf`, a BMC `counter/pdf`,
a Frontiers `/pdf`, a Harvard DASH bitstream, PMC, or the gated publisher. So the access ladder is: **resolve the DOI (Crossref),
then run the cheapest OA host that returns a real PDF, and persist the winning host per paper** so a
re-run skips straight to it.

This is the seed of "paper-rung" — the productionized `acquire` step of the research-librarian loop and a
component of the central knowledge base — and it doubles as evidence that the engine is domain-agnostic
(same `access.run_target` + queue as `examples/custom_domain.py`, a completely different domain).

All HTTP goes through `http.make_session()` (the honest chokepoint). Be polite: this fetches only
open-access PDFs, one at a time.

    DATABASE_URL=postgresql://rung:rung@localhost:5432/rung \
      uv run python examples/paper_fetcher.py 10.1371/journal.pone.0282396 10.1038/s41598-018-22755-2

    # or a batch — one DOI/title per line:
    uv run python examples/paper_fetcher.py --from dois.txt

**Set `UNPAYWALL_EMAIL`.** Unpaywall requires a real contact address and returns HTTP 422 for a
placeholder, which disables the single most productive rung (it locates an OA copy of ANY DOI on any
repository, preprint server or publisher). Without it the ladder reports fetchable papers as paywalled.
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

from rung import access, db, http, queue

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
    """A fetch counts only if a real (>20 KB, %PDF-headed) file landed on disk."""
    path = getattr(record, "path", None)
    if not path:
        return False
    p = Path(path)
    return p.is_file() and p.stat().st_size > 20_000 and p.read_bytes()[:5] == b"%PDF-"


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


def _unpaywall_pdf(doi: str) -> str | None:
    """The best open-access PDF URL for a DOI from Unpaywall (any repository/preprint/journal), or
    None if the DOI has no OA copy anywhere.

    Reads `best_oa_location` FIRST but falls through every entry of `oa_locations`. `best_oa_location`
    frequently carries `url_for_pdf: null` — typically a repository landing page — while another
    location (often the publisher, for "bronze" OA) has the direct PDF. Taking only the best location
    silently lost three fetchable papers in one round (Doggett 2024 JAMA Netw Open, Bonn-Miller 2017
    JAMA, Dryburgh 2018 BJCP), each of which reported `is_oa=true`.
    """
    data = _unpaywall_json(doi)
    if not data:
        return None
    best = data.get("best_oa_location") or {}
    for loc in (best, *(data.get("oa_locations") or [])):
        pdf = (loc or {}).get("url_for_pdf")
        if pdf:
            return pdf
    return None


# ── Harvard DASH — the 'pdf' URL is an HTML landing page; the real PDF is a bitstream inside it ──────
_DASH_HOSTS = ("dash.harvard.edu", "nrs.harvard.edu")
_DASH_BITSTREAM = re.compile(r"/bitstreams/[0-9a-fA-F-]+/download")


def _dash_bitstream_url(landing_html: str) -> str | None:
    """Extract the DASH bitstream download URL from a landing page's HTML, or None.

    Harvard DASH answers its OA URL with an HTML *record* page even where Unpaywall lists it as the
    `url_for_pdf`, so a naive fetch gets HTML and reports the paper paywalled even though its PDF is one
    hop away. The real PDF is a `/bitstreams/<uuid>/download` link inside the page; take the first and
    make it absolute. (Heuristic: a record with several bitstreams — e.g. a licence file — may need the
    largest, but the article PDF is the first in practice.)
    """
    m = _DASH_BITSTREAM.search(landing_html)
    return f"https://dash.harvard.edu{m.group(0)}" if m else None


def _dash_landing_for_doi(doi: str) -> str | None:
    """A Harvard DASH landing URL for this DOI from Unpaywall (dash.harvard.edu / nrs.harvard.edu), or
    None. DASH lists its landing page under `url_for_pdf`/`url`, so scan every OA location's URLs."""
    data = _unpaywall_json(doi)
    if not data:
        return None
    for loc in ((data.get("best_oa_location") or {}), *(data.get("oa_locations") or [])):
        for key in ("url_for_pdf", "url", "url_for_landing_page"):
            u = (loc or {}).get(key)
            if u and any(host in u for host in _DASH_HOSTS):
                return u
    return None


async def _get_text(url: str) -> str | None:
    """GET a URL via the honest session and return its decoded body (for landing-page HTML), or None."""
    async with http.make_session() as session:
        try:
            resp = await session.get(url)
        except Exception:
            return None
    content = resp.content if hasattr(resp, "content") else b""
    return content.decode("utf-8", "replace") if content else None


async def _fetch_dash(_conn: db.DBConn, doi: str, _hint: access.MethodHint) -> access.MethodResult:
    """Repository rung for Harvard DASH: resolve the landing page, extract its bitstream link, fetch the
    real PDF. Complements the Unpaywall rung, which returns DASH's landing URL and fails on the HTML."""
    landing = _dash_landing_for_doi(doi)
    if not landing:
        return [], None, None
    html = await _get_text(landing)
    if not html:
        return [], None, None
    pdf_url = _dash_bitstream_url(html)
    if not pdf_url:
        return [], None, None
    return await _download_pdf(pdf_url, doi, "dash")


async def _fetch_unpaywall(_conn: db.DBConn, doi: str, _hint: access.MethodHint) -> access.MethodResult:
    """General OA-locator rung: ask Unpaywall for *any* open-access PDF of this DOI and fetch it.
    Subsumes the hard-coded host rungs for anything OA on a repository/preprint we don't special-case."""
    pdf_url = _unpaywall_pdf(doi)
    if not pdf_url:
        return [], None, None
    return await _download_pdf(pdf_url, doi, "unpaywall")


# arXiv is cost 0 (cheapest); the OA journals are cost 1; PMC/publisher would be higher (omitted here
# because they gate/block direct PDF fetches — a real deployment would add them as costly last rungs).
CATALOG = [
    access.AccessMethod("arxiv", cost_rank=0, run=_make_fetcher("arxiv")),
    access.AccessMethod("plos", cost_rank=1, run=_make_fetcher("plos")),
    access.AccessMethod("nature", cost_rank=1, run=_make_fetcher("nature")),
    access.AccessMethod("bmc", cost_rank=1, run=_make_fetcher("bmc")),
    access.AccessMethod("frontiers", cost_rank=1, run=_make_fetcher("frontiers")),
    # Unpaywall (an API round-trip) locates an OA copy of ANY DOI on ANY host/repository/preprint, so
    # it's the general fallback after the fast direct-journal rungs; the PMC OA subset is a further biomed-OA
    # backstop for the DOI→PMCID case Unpaywall occasionally misses.
    access.AccessMethod("unpaywall", cost_rank=2, run=_fetch_unpaywall),
    # DASH lists a repository landing page as its 'pdf' URL, so the unpaywall rung above fetches HTML and
    # fails; this rung follows the landing to its bitstream. Same cost tier, tried after unpaywall.
    access.AccessMethod("dash", cost_rank=2, run=_fetch_dash),
    access.AccessMethod("pmc_oa", cost_rank=3, run=_fetch_pmc_oa),
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
