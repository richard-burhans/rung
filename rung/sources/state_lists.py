"""Discover each state's dispensary-list / locator resource.

Starting from a state's verified agency landing page (state_programs.best_url),
crawl one or two hops and score links to find the page or file that lists licensed
dispensaries. The winner's URL and a coarse resource type are stored on the
state_programs row (list_url / list_type) for the extraction stage to consume.

A curated `list_url` in states.yml always wins — use it for JS-only sites the
crawler can't read, or to correct a wrong pick.
"""

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from rung import db
from rung.db import get_state_program, set_state_list
from rung.http import make_session
from rung.sources.state_search import StateInfo, load_states

# Phrases that strongly indicate a dispensary list / locator (matched in link text).
_STRONG = (
    "find a dispensar", "dispensary locat", "dispensary list", "list of dispensar",
    "licensed dispensar", "dispensaries", "find a dispensary", "store locator",
    "find a store", "where to buy", "where can i buy", "treatment center",
    "list of licensees", "licensee lookup", "licensee information", "find a facility",
    "retail locations", "licensed retailers", "licensed retail", "dispensary locator",
    "find a pharmacy", "pharmacy locations", "dispensary information",
)
# Weaker keywords (matched in text, or in href at half weight).
_MEDIUM = (
    "dispensar", "retail", "licensee", "licensed", "locations", "locator",
    "lookup", "find a", "pharmacies", "providers", "directory",
)
# If any of these appear in the link text, the link is almost certainly NOT a list.
_DENY = (
    "phone directory", "staff directory", "agency directory", "program directory",
    "employee", "job", "career", "vapor", "foia", "complaint", "disciplinary",
    "newsletter", "press release", "contact us", "site map", "sitemap",
    "privacy", "accessibility", "feedback", "subscribe",
)
_DOC_EXT = (".pdf", ".csv", ".xlsx", ".xls")
_MAP_HINTS = ("arcgis", "tableau", "socrata", "/maps/d/", "webappviewer", "experience.arcgis")

# A link must clear this to be accepted as the state's list.
_MIN_SCORE = 4
# Below this, try descending one more hop into the best same-domain HTML candidate.
_SECOND_HOP_BELOW = 7


@dataclass
class ListCandidate:
    url: str
    text: str
    score: int
    list_type: str


def _classify(url: str) -> str:
    """Map a URL to a coarse resource type the extractor dispatches on."""
    low = url.lower()
    if low.endswith(".pdf") or ".pdf?" in low:
        return "pdf"
    if any(low.endswith(e) or f"{e}?" in low for e in (".csv", ".xlsx", ".xls")):
        return "csv"
    if "kml" in low or "/maps/d/" in low or "google.com/maps/d" in low:
        return "kml"
    if any(h in low for h in _MAP_HINTS):
        return "arcgis"
    host = urlparse(low).netloc
    if host.startswith("search.") or "lookup" in low or "verification" in low or "/search" in low:
        return "lookup"
    return "html"


def _score_link(href: str, text: str) -> int:
    h = href.lower()
    t = " ".join(text.lower().split())
    if not t:
        # Linkless anchors (icons) can't be judged; skip.
        return 0
    if any(d in t for d in _DENY):
        return -1

    score = 0
    has_kw = False
    for kw in _MEDIUM:
        if kw in t:
            score += 2
            has_kw = True
        elif kw in h:
            score += 1
            has_kw = True
    for phrase in _STRONG:
        if phrase in t:
            score += 4
            has_kw = True

    # Document / map bonuses only count alongside a dispensary keyword, so generic
    # report PDFs and unrelated maps don't outscore the real locator.
    if h.endswith(_DOC_EXT) and has_kw:
        score += 3
    if any(hint in h for hint in _MAP_HINTS):
        score += 3 if has_kw else 1
    return score


def _harvest(html: str, base_url: str) -> list[ListCandidate]:
    """Score every link on a page; return candidates sorted best-first."""
    tree = HTMLParser(html)
    best: dict[str, ListCandidate] = {}
    for node in tree.css("a[href]"):
        href = node.attributes.get("href", "") or ""
        text = (node.text() or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        if not full.startswith("http"):
            continue
        full = full.split("#", 1)[0]
        score = _score_link(full, text)
        if score <= 0:
            continue
        existing = best.get(full)
        if existing is None or score > existing.score:
            best[full] = ListCandidate(full, text[:60], score, _classify(full))
    return sorted(best.values(), key=lambda c: -c.score)


async def _fetch(session, url: str) -> str | None:
    try:
        resp = await session.get(url, timeout=30, allow_redirects=True)
    except Exception:
        return None
    if resp.status_code >= 400:
        return None
    return resp.text


async def find_list_url(landing_url: str) -> ListCandidate | None:
    """Crawl from a landing page to find the best dispensary-list resource.

    One hop from the landing page; if the best link is only middling, descend one
    more hop into it and re-harvest, keeping the global best.
    """
    async with make_session() as session:
        html = await _fetch(session, landing_url)
        if html is None:
            return None
        candidates = _harvest(html, landing_url)
        best = candidates[0] if candidates else None

        if best is not None and best.score < _SECOND_HOP_BELOW and best.list_type == "html":
            landing_host = urlparse(landing_url).netloc
            # Descend into the top same-domain HTML candidate only.
            for cand in candidates[:2]:
                if cand.list_type != "html":
                    continue
                if urlparse(cand.url).netloc != landing_host:
                    continue
                sub_html = await _fetch(session, cand.url)
                if sub_html is None:
                    continue
                for deeper in _harvest(sub_html, cand.url):
                    if deeper.score > best.score:
                        best = deeper
                break

    if best is None or best.score < _MIN_SCORE:
        return None
    return best


def _landing_for(state: StateInfo, conn: db.DBConn) -> str | None:
    stored = get_state_program(conn, state.abbr)
    if stored is not None and stored.best_url:
        return stored.best_url
    return state.known_url or None


async def run_find_lists(
    conn: db.DBConn,
    only: set[str] | None = None,
    force: bool = False,
) -> list[tuple[StateInfo, ListCandidate | None, str]]:
    """Discover and store the dispensary-list URL for each program state.

    Args:
        conn: open DB connection; results written via set_state_list.
        only: if given, restrict to these state abbreviations.
        force: re-discover even states that already have a stored list_url.

    Returns (state, candidate|None, status) tuples for reporting.
    """
    states = [
        s for s in load_states()
        if s.programs in ("medical", "recreational", "both")
        and (only is None or s.abbr in only)
    ]

    results: list[tuple[StateInfo, ListCandidate | None, str]] = []

    # Curated overrides resolve without any network call.
    to_crawl: list[StateInfo] = []
    for state in states:
        if state.list_url:
            list_type = state.list_type or _classify(state.list_url)
            cand = ListCandidate(state.list_url, "(override)", 99, list_type)
            set_state_list(conn, state.abbr, cand.url, cand.list_type, "override")
            results.append((state, cand, "override"))
            continue
        stored = get_state_program(conn, state.abbr)
        if not force and stored is not None and stored.list_url and stored.list_status != "none":
            cand = ListCandidate(
                stored.list_url, "(stored)", 0, stored.list_type or "html"
            )
            results.append((state, cand, "stored"))
            continue
        to_crawl.append(state)
    conn.commit()

    landings = {s.abbr: _landing_for(s, conn) for s in to_crawl}

    async def _one(state: StateInfo) -> tuple[StateInfo, ListCandidate | None, str]:
        landing = landings.get(state.abbr)
        if not landing:
            return state, None, "no-landing"
        cand = await find_list_url(landing)
        return state, cand, "found" if cand else "none"

    crawled = await asyncio.gather(*(_one(s) for s in to_crawl))
    for state, cand, status in crawled:
        if cand is not None:
            set_state_list(conn, state.abbr, cand.url, cand.list_type, "found")
        else:
            set_state_list(conn, state.abbr, None, None, "none")
        results.append((state, cand, status))
    conn.commit()

    return results


def print_list_report(
    results: list[tuple[StateInfo, ListCandidate | None, str]],
) -> None:
    """Print the discovered dispensary-list URLs grouped by outcome."""
    rows = sorted(results, key=lambda r: (r[1] is None, r[0].name))
    sep = "-" * 100
    print(sep)
    print(f"{'State':<22} | {'Type':<8} | {'St':<8} | List URL")
    print(sep)
    found = 0
    for state, cand, status in rows:
        if cand is not None:
            found += 1
            url = cand.url if len(cand.url) <= 56 else cand.url[:55] + "…"
            print(f"{state.name:<22} | {cand.list_type:<8} | {status:<8} | {url}")
        else:
            print(f"{state.name:<22} | {'-':<8} | {status:<8} | -")
    print(sep)
    print(f"List URL found: {found}/{len(results)} program states")
    print(sep)
