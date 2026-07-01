"""Batch search for cannabis dispensary coverage across all US states + DC.

Run order per state:
  1. If a URL is stored in the DB (or curated in states.yml), verify it with a HEAD
     request; if it still responds, mark ok and skip search.
  2. Otherwise rotate search backends: DuckDuckGo → Bing (both curl_cffi, no
     browser) → Google (a real Chrome via pydoll, shared across all states, so
     Google cannot detect automation).
  3. Save the result to the state_programs table.
"""

import asyncio
import base64
import datetime
import html as html_mod
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import yaml
from pydoll.browser.chromium import Chrome
from selectolax.parser import HTMLParser

from rung import db
from rung.browser import get_script_value, make_browser_options
from rung.http import make_session
from rung.models import StateProgramRecord

_YAML_PATH = Path(__file__).parent.parent / "data" / "states.yml"
_PAGE_LOAD_WAIT = 3.0   # seconds for JS-rendered results to appear
_BETWEEN_SEARCHES = 2.5  # seconds between requests (human-like pacing)

# Search backend URLs
_DDG_URL = "https://html.duckduckgo.com/html/?q={query}"
_BING_URL = "https://www.bing.com/search?q={query}&cc=US&setlang=en&count=10"
_GOOGLE_URL = "https://www.google.com/search?q={query}&num=10&gl=us&hl=en"

_GOV_SUFFIXES = (".gov", ".us")
_SKIP_DOMAINS = {
    "google.com", "duckduckgo.com", "bing.com", "yahoo.com",
    "youtube.com", "wikipedia.org", "reddit.com",
}


@dataclass
class StateInfo:
    abbr: str
    name: str
    programs: str  # 'none' | 'cbd_only' | 'medical' | 'recreational' | 'both'
    program_term: str
    agency: str
    known_url: str = ""  # curated agency landing page; HEAD-verified before search
    list_url: str = ""   # optional curated dispensary-list URL; overrides crawl discovery
    list_type: str = ""  # optional explicit type for list_url (e.g. ca_dcc); else inferred


@dataclass
class StateCoverage:
    state: StateInfo
    gov_urls: list[str] = field(default_factory=list)
    best_url: str | None = None
    source_type: str | None = None  # 'pdf' | 'map' | 'html' | 'api'
    queries_tried: list[str] = field(default_factory=list)
    error: str | None = None


def load_states() -> list[StateInfo]:
    with _YAML_PATH.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return [
        StateInfo(
            abbr=s["abbr"],
            name=s["name"],
            programs=s["programs"],
            program_term=s.get("program_term", ""),
            agency=s.get("agency", ""),
            known_url=s.get("known_url", ""),
            list_url=s.get("list_url", ""),
            list_type=s.get("list_type", ""),
        )
        for s in raw
    ]


def _build_queries(state: StateInfo) -> list[str]:
    name = state.name
    term = state.program_term
    agency = state.agency

    queries = [
        f"{name} {term} dispensary list site:gov",
        f"{name} {term} licensed dispensaries",
    ]
    if agency:
        queries.append(f"{agency} dispensary list")
    queries.append(f"{name} cannabis dispensary licensed site:gov")
    return queries


def _classify_url(url: str) -> str:
    lower = url.lower()
    if lower.endswith(".pdf") or ".pdf?" in lower:
        return "pdf"
    if "maps.google" in lower or "/maps/d/" in lower or "kml" in lower:
        return "map"
    if ".json" in lower or "/api/" in lower or "api." in lower:
        return "api"
    return "html"


def _filter_gov_urls(hrefs: list[str]) -> list[str]:
    """Return deduplicated .gov URLs from a list of hrefs, skipping noise domains."""
    seen: set[str] = set()
    results: list[str] = []
    for href in hrefs:
        if not href or not href.startswith("http"):
            continue
        parsed = urlparse(href)
        netloc = parsed.netloc.lower()
        if any(skip in netloc for skip in _SKIP_DOMAINS):
            continue
        if not any(netloc.endswith(suf) for suf in _GOV_SUFFIXES):
            continue
        page = parsed._replace(fragment="").geturl()
        if page not in seen:
            seen.add(page)
            results.append(page)
    return results


# ── Search backends ────────────────────────────────────────────────────────────

class _Backend(ABC):
    name: str
    blocked: bool = False

    @abstractmethod
    async def search(
        self, query: str, filter_fn: Callable[[list[str]], list[str]] = _filter_gov_urls
    ) -> list[str]:
        """Run one query; return result URLs kept by ``filter_fn`` (may set self.blocked).

        ``filter_fn`` defaults to the .gov filter for state-coverage search; other callers
        (e.g. operator-homepage discovery) pass their own URL filter.
        """


class _DDGBackend(_Backend):
    """DuckDuckGo HTML endpoint via curl_cffi — fastest, no browser needed."""

    name = "ddg"

    async def search(
        self, query: str, filter_fn: Callable[[list[str]], list[str]] = _filter_gov_urls
    ) -> list[str]:
        url = _DDG_URL.format(query=quote_plus(query))
        async with make_session() as session:
            resp = await session.get(url, timeout=20)
        if resp.status_code == 403:
            self.blocked = True
            return []
        if resp.status_code >= 400:
            return []
        tree = HTMLParser(resp.text)
        hrefs = []
        for node in tree.css(".result__a"):
            href = node.attributes.get("href", "") or ""
            if "duckduckgo.com/l/" in href:
                qs = parse_qs(urlparse(href).query)
                real = qs.get("uddg", [None])[0]
                if real:
                    href = unquote(real)
            if href:
                hrefs.append(href)
        results = filter_fn(hrefs)
        if not results:
            # DDG returned a page but no useful results — may be bot-detected
            all_links = [n.attributes.get("href", "") or "" for n in tree.css("a[href]")]
            if len(all_links) < 5:
                self.blocked = True
        return results


class _BingBackend(_Backend):
    """Bing search via curl_cffi with base64-decoded redirect URLs."""

    name = "bing"

    async def search(
        self, query: str, filter_fn: Callable[[list[str]], list[str]] = _filter_gov_urls
    ) -> list[str]:
        url = _BING_URL.format(query=quote_plus(query))
        async with make_session() as session:
            resp = await session.get(url, timeout=20)
        if resp.status_code >= 400:
            self.blocked = True
            return []

        decoded_html = html_mod.unescape(resp.text)
        u_params = re.findall(r"[?&]u=(a1[A-Za-z0-9_-]+)", decoded_html)
        hrefs = []
        for up in u_params:
            b64 = up[2:]
            pad = 4 - len(b64) % 4
            if pad != 4:
                b64 += "=" * pad
            try:
                href = base64.urlsafe_b64decode(b64).decode("utf-8", errors="replace")
                if href.startswith("http"):
                    hrefs.append(href)
            except ValueError:
                # Only malformed base64 is expected here (binascii.Error ⊂ ValueError);
                # let anything else (a real bug) surface rather than swallowing it.
                pass

        results = filter_fn(hrefs)
        if not hrefs:
            self.blocked = True
        return results


class _GoogleBackend(_Backend):
    """Google search via pydoll real Chrome — stealth, needs browser tab."""

    name = "google"

    def __init__(self, tab=None) -> None:
        self.tab = tab
        self.blocked = False

    async def search(
        self, query: str, filter_fn: Callable[[list[str]], list[str]] = _filter_gov_urls
    ) -> list[str]:
        if self.tab is None:
            return []
        url = _GOOGLE_URL.format(query=quote_plus(query))
        try:
            await self.tab.go_to(url)
            await asyncio.sleep(_PAGE_LOAD_WAIT)
            result = await self.tab.execute_script(
                """
                Array.from(document.querySelectorAll('a[href]'))
                  .map(a => a.href)
                  .filter(h => h && h.indexOf('http') === 0)
                  .join('|')
                """
            )
            raw = get_script_value(result) or ""
        except Exception:
            self.blocked = True
            return []

        hrefs = [h for h in raw.split("|") if h]
        results = filter_fn(hrefs)
        if not results and len(hrefs) < 5:
            # Likely reCAPTCHA or consent page
            self.blocked = True
        return results


async def _search_with_rotation(
    backends: list[_Backend], query: str,
    filter_fn: Callable[[list[str]], list[str]] = _filter_gov_urls,
) -> list[str]:
    """Try each backend in order, skipping blocked ones. Returns URLs kept by filter_fn."""
    for backend in backends:
        if backend.blocked:
            continue
        try:
            urls = await backend.search(query, filter_fn)
        except Exception:
            backend.blocked = True
            continue
        if urls:
            return urls
        await asyncio.sleep(_BETWEEN_SEARCHES)
    return []


async def _search_state(
    backends: list[_Backend], state: StateInfo
) -> StateCoverage:
    coverage = StateCoverage(state=state)

    if state.programs in ("none", "cbd_only"):
        return coverage

    queries = _build_queries(state)
    coverage.queries_tried = queries

    for query in queries:
        gov_urls = await _search_with_rotation(backends, query)
        for u in gov_urls:
            if u not in coverage.gov_urls:
                coverage.gov_urls.append(u)
        if coverage.gov_urls:
            break

    if coverage.gov_urls:
        for u in coverage.gov_urls:
            t = _classify_url(u)
            if t in ("pdf", "map"):
                coverage.best_url = u
                coverage.source_type = t
                break
        if coverage.best_url is None:
            coverage.best_url = coverage.gov_urls[0]
            coverage.source_type = _classify_url(coverage.best_url)

    return coverage


async def _check_url(url: str) -> tuple[bool, int | None]:
    """HEAD-request a URL; returns (is_ok, status_code).

    Uses curl_cffi — lightweight, no browser needed for a simple liveness check.
    """
    try:
        async with make_session() as session:
            resp = await session.head(url, timeout=15, allow_redirects=True)
        ok = resp.status_code < 400
        return ok, resp.status_code
    except Exception:
        return False, None


def _coverage_to_record(
    cov: StateCoverage,
    now: str,
    check_status: str,
    last_checked: str | None,
) -> StateProgramRecord:
    return StateProgramRecord(
        abbr=cov.state.abbr,
        name=cov.state.name,
        programs=cov.state.programs,
        program_term=cov.state.program_term,
        agency=cov.state.agency,
        best_url=cov.best_url,
        source_type=cov.source_type,
        all_gov_urls=cov.gov_urls,
        last_checked=last_checked,
        check_status=check_status,
        searched_at=now if cov.queries_tried else None,
        error=cov.error,
    )


async def _verify_candidates(
    state: StateInfo, candidates: list[str]
) -> StateCoverage | None:
    """HEAD-check candidate URLs in order; return coverage for the first live one.

    Candidates are ordered most-trusted first (a previously stored best_url, then the
    curated known_url). The first URL that responds < 400 becomes best_url, so no search
    engine is needed for that state.
    """
    for url in candidates:
        is_ok, _status = await _check_url(url)
        if is_ok:
            return StateCoverage(
                state=state,
                gov_urls=[url],
                best_url=url,
                source_type=_classify_url(url),
            )
    return None


async def run_state_coverage(
    conn: db.DBConn,
    failed_only: bool = False,
    force: bool = False,
) -> list[StateCoverage]:
    """Establish dispensary-program coverage for all states + DC.

    Each program state is first verified against curated/stored URLs with a fast HEAD
    request (no browser). Only states with no live URL fall back to search-engine
    discovery (DDG → Bing → Google), which shares a single real Chrome session.

    Args:
        conn: Open DB connection; results are written to the state_programs table.
        failed_only: Only process states where stored URL check failed or was never run.
        force: Skip URL verification and search every state regardless of stored data.
    """
    from rung.db import get_state_program, upsert_state_program

    states = load_states()
    results: list[StateCoverage] = []
    now = datetime.datetime.now(datetime.UTC).isoformat()

    # States that need search-engine discovery (no live candidate URL).
    to_search: list[tuple[StateInfo, StateProgramRecord | None]] = []
    # States to HEAD-verify: (state, stored, ordered candidate URLs).
    verify_jobs: list[tuple[StateInfo, StateProgramRecord | None, list[str]]] = []

    for state in states:
        if state.programs in ("none", "cbd_only"):
            cov = StateCoverage(state=state)
            results.append(cov)
            # Persist skip states too so the table is complete
            rec = _coverage_to_record(cov, now, "skip", None)
            upsert_state_program(conn, rec)
            continue

        stored = get_state_program(conn, state.abbr)

        if not force and failed_only and stored is not None \
                and stored.check_status == "ok" and stored.best_url:
            # Already ok — reuse without re-checking.
            cov = StateCoverage(
                state=state,
                gov_urls=stored.all_gov_urls,
                best_url=stored.best_url,
                source_type=stored.source_type,
            )
            results.append(cov)
            continue

        if force:
            to_search.append((state, stored))
            continue

        candidates: list[str] = []
        if stored is not None and stored.best_url:
            candidates.append(stored.best_url)
        if state.known_url and state.known_url not in candidates:
            candidates.append(state.known_url)

        if candidates:
            verify_jobs.append((state, stored, candidates))
        else:
            to_search.append((state, stored))

    conn.commit()

    # HEAD-verify all candidate URLs concurrently — no browser, finishes in seconds.
    if verify_jobs:
        print(f"  Verifying {len(verify_jobs)} states via HEAD requests…", flush=True)
        coverages = await asyncio.gather(
            *(_verify_candidates(s, c) for s, _stored, c in verify_jobs)
        )
        for (state, stored, _candidates), cov in zip(verify_jobs, coverages, strict=True):
            if cov is not None:
                last_checked = datetime.datetime.now(datetime.UTC).isoformat()
                rec = _coverage_to_record(cov, now, "ok", last_checked)
                upsert_state_program(conn, rec)
                results.append(cov)
                print(f"  [ok ] {state.name:<22} {cov.best_url}", flush=True)
            else:
                print(
                    f"  [!  ] {state.name:<22} no live known URL — re-searching",
                    flush=True,
                )
                to_search.append((state, stored))
        conn.commit()

    if not to_search:
        return results

    print(f"  Searching {len(to_search)} states (DDG → Bing → Google fallback)…", flush=True)

    # Start backends — Google needs a real browser, others use curl_cffi
    google_backend = _GoogleBackend()
    backends: list[_Backend] = [_DDGBackend(), _BingBackend(), google_backend]

    async with Chrome(options=make_browser_options()) as browser:
        google_backend.tab = await browser.start()
        for i, (state, _stored) in enumerate(to_search, 1):
            cov = await _search_state(backends, state)
            results.append(cov)

            last_checked = datetime.datetime.now(datetime.UTC).isoformat() if cov.best_url else None
            check_status = "ok" if cov.best_url else "failed"
            rec = _coverage_to_record(cov, now, check_status, last_checked)
            upsert_state_program(conn, rec)
            conn.commit()

            label = "✓" if cov.best_url else "?"
            print(f"  [{i:>2}/{len(to_search)}] {state.name:<22} {label}", flush=True)

    return results


def print_report_from_db(records: list[StateProgramRecord]) -> None:
    """Print the state coverage table from stored StateProgramRecords."""
    col_state = 22
    col_prog = 11
    col_status = 8
    col_url = 50
    col_type = 6
    sep = "-" * (col_state + col_prog + col_status + col_url + col_type + 12)

    print(sep)
    print(
        f"{'State':<{col_state}} | "
        f"{'Programs':<{col_prog}} | "
        f"{'Status':<{col_status}} | "
        f"{'Best URL':<{col_url}} | "
        f"{'Type':<{col_type}}"
    )
    print(sep)

    programs_total = 0
    programs_with_data = 0

    for rec in sorted(records, key=lambda r: r.name):
        if rec.programs in ("none", "cbd_only"):
            continue

        prog_label = {
            "medical": "medical",
            "recreational": "rec",
            "both": "med+rec",
        }.get(rec.programs, rec.programs)

        programs_total += 1
        if rec.best_url:
            programs_with_data += 1

        status_label = {
            "ok": "✓ ok",
            "failed": "✗ fail",
            "never": "— never",
        }.get(rec.check_status, rec.check_status)

        url_display = rec.best_url or "-"
        if len(url_display) > col_url:
            url_display = url_display[: col_url - 1] + "…"

        checked = f"  (checked {rec.last_checked[:10]})" if rec.last_checked else ""

        print(
            f"{rec.name:<{col_state}} | "
            f"{prog_label:<{col_prog}} | "
            f"{status_label:<{col_status}} | "
            f"{url_display:<{col_url}} | "
            f"{(rec.source_type or '?'):<{col_type}}"
            f"{checked}"
        )

    print(sep)
    print(f"Full programs: {programs_total} | URLs found: {programs_with_data}/{programs_total}")
    print(sep)


def print_report(results: list[StateCoverage]) -> None:
    col_state = 22
    col_prog = 11
    col_url = 52
    col_type = 6
    sep = "-" * (col_state + col_prog + col_url + col_type + 9)

    header = (
        f"{'State':<{col_state}} | "
        f"{'Programs':<{col_prog}} | "
        f"{'Gov URL':<{col_url}} | "
        f"{'Type':<{col_type}}"
    )
    print(sep)
    print(header)
    print(sep)

    programs_total = 0
    programs_with_data = 0

    for cov in sorted(results, key=lambda c: c.state.name):
        prog_label = {
            "none": "none",
            "cbd_only": "CBD only",
            "medical": "medical",
            "recreational": "rec",
            "both": "med+rec",
        }.get(cov.state.programs, cov.state.programs)

        if cov.state.programs not in ("none", "cbd_only"):
            programs_total += 1
            if cov.best_url:
                programs_with_data += 1

        url_display = cov.best_url or "-"
        if len(url_display) > col_url:
            url_display = url_display[: col_url - 1] + "…"

        type_display = cov.source_type or (
            "-" if cov.state.programs in ("none", "cbd_only") else "?"
        )

        print(
            f"{cov.state.name:<{col_state}} | "
            f"{prog_label:<{col_prog}} | "
            f"{url_display:<{col_url}} | "
            f"{type_display:<{col_type}}"
        )

    print(sep)
    total = len(results)
    no_program = sum(1 for c in results if c.state.programs == "none")
    cbd_only = sum(1 for c in results if c.state.programs == "cbd_only")
    errors = sum(
        1 for c in results
        if c.error and c.state.programs not in ("none", "cbd_only")
    )

    print(f"Total: {total} jurisdictions")
    print(f"  No program: {no_program} | CBD-only: {cbd_only} | Full programs: {programs_total}")
    print(f"  Gov URL found: {programs_with_data}/{programs_total}")
    if errors:
        print(f"  Search errors: {errors}")
    print(sep)
