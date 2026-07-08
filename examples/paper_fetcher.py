"""Fetch academic-paper PDFs with the `rung` engine — a real, non-cannabis second example.

Fetching a paper is exactly what rung is for: one target (a paper) is reachable several ways at
different cost/success — arXiv, a PLOS "printable" endpoint, a Nature-OA `.pdf`, a BMC `counter/pdf`,
a Frontiers `/pdf`, PMC, or the gated publisher. So the access ladder is: **resolve the DOI (Crossref),
then run the cheapest OA host that returns a real PDF, and persist the winning host per paper** so a
re-run skips straight to it.

This is the seed of "paper-rung" — the productionized `acquire` step of the deep-literature loop and a
component of the central knowledge base — and it doubles as evidence that the engine is domain-agnostic
(same `access.run_target` + queue as `examples/custom_domain.py`, a completely different domain).

All HTTP goes through `http.make_session()` (the honest chokepoint). Be polite: this fetches only
open-access PDFs, one at a time.

    DATABASE_URL=postgresql://rung:rung@localhost:5432/rung \
      uv run python examples/paper_fetcher.py 10.1371/journal.pone.0282396 10.1038/s41598-018-22755-2
"""

from __future__ import annotations

import asyncio
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from rung import access, db, http, queue

TASK_TYPE = "paper_fetch"
TARGET_TYPE = "paper_pdf"
OUT_DIR = Path("fetched_papers")


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


def _make_fetcher(host: str):
    async def _fetch(_conn: db.DBConn, doi: str, _hint: access.MethodHint) -> access.MethodResult:
        url = _url_for(host, doi)
        if url is None:
            return [], None, None
        out = OUT_DIR / (doi.replace("/", "_") + ".pdf")
        async with http.make_session() as session:
            try:
                resp = await session.get(url)
            except Exception:
                return [], None, None
        content = resp.content if hasattr(resp, "content") else b""
        if content[:5] != b"%PDF-" or len(content) < 20_000:
            return [], None, None       # HTML block page / gated / missing → this rung "fails"
        OUT_DIR.mkdir(exist_ok=True)
        out.write_bytes(content)
        return [Fetched(doi=doi, host=host, path=str(out))], url, None
    return _fetch


# arXiv is cost 0 (cheapest); the OA journals are cost 1; PMC/publisher would be higher (omitted here
# because they gate/block direct PDF fetches — a real deployment would add them as costly last rungs).
CATALOG = [
    access.AccessMethod("arxiv", cost_rank=0, run=_make_fetcher("arxiv")),
    access.AccessMethod("plos", cost_rank=1, run=_make_fetcher("plos")),
    access.AccessMethod("nature", cost_rank=1, run=_make_fetcher("nature")),
    access.AccessMethod("bmc", cost_rank=1, run=_make_fetcher("bmc")),
    access.AccessMethod("frontiers", cost_rank=1, run=_make_fetcher("frontiers")),
]


async def fetch_one(conn: db.DBConn, doi: str) -> tuple[str | None, list[Fetched]]:
    return await access.run_target(conn, TARGET_TYPE, doi, CATALOG, plausible=fetched_plausible)


async def run(conn: db.DBConn, queries: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """Resolve each query to a DOI, then drain the queue fetching each via the host ladder."""
    db.create_tables(conn)
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
    queries = sys.argv[1:] or [
        "10.1371/journal.pone.0282396",   # "Uncomfortably high" — PLOS
        "10.1038/s41598-018-22755-2",     # Jikomes & Zoorob — Nature Sci Reports
        "10.3389/fpls.2021.699530",       # chemotypic markers — Frontiers
    ]
    conn = db.get_connection()
    results = asyncio.run(run(conn, queries))
    print("Fetched (doi: winning host → file):")
    for doi, (winner, path) in results.items():
        print(f"  {doi}: {winner or 'NONE'} -> {path or '(no OA host worked — paywalled/to-get)'}")


if __name__ == "__main__":
    main()
