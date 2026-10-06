"""A landing page that does not answer says nothing about the stored list URL, or the stored best URL.

Regression tests for the 2026-10-06 whole-tree review's findings in `rung.sources.state_lists` and
`rung.sources.state_search`.
"""

import asyncio

import pytest
from conftest import pg_conn

from rung import db
from rung.models import StateProgramRecord
from rung.sources import state_lists, state_search
from rung.sources.state_search import StateInfo

_PA = StateInfo(abbr="PA", name="Pennsylvania", programs="medical", program_term="medical marijuana",
                agency="DOH", known_url="https://doh.example/landing")


def _program(**over) -> StateProgramRecord:
    base: dict = {
        "abbr": "PA", "name": "Pennsylvania", "programs": "medical",
        "program_term": "medical marijuana", "agency": "DOH",
        "best_url": "https://doh.example/landing", "source_type": "html", "all_gov_urls": [],
        "last_checked": None, "check_status": "ok", "searched_at": None, "error": None,
        "country": "US",
    }
    base.update(over)
    return StateProgramRecord(**base)


def test_an_unreachable_landing_raises_rather_than_reading_as_no_list(monkeypatch) -> None:
    async def dead(session: object, url: str) -> None:
        return None

    monkeypatch.setattr(state_lists, "_fetch", dead)
    with pytest.raises(state_lists.LandingUnreachable):
        asyncio.run(state_lists.find_list_url("https://doh.example/landing"))


def test_find_lists_force_keeps_the_stored_list_url_when_the_landing_is_down(monkeypatch) -> None:
    """`find-lists --force` used to write list_url NULL on one transient 503, and extraction —
    which filters on the URL — silently stopped refreshing the state."""
    conn = pg_conn()
    db.create_tables(conn)
    db.upsert_state_program(conn, _program())
    db.set_state_list(conn, "PA", "https://doh.example/roster.csv", "csv", "found")
    conn.commit()

    async def unreachable(landing_url: str) -> None:
        raise state_lists.LandingUnreachable(landing_url)

    monkeypatch.setattr(state_lists, "load_states", lambda: [_PA])
    monkeypatch.setattr(state_lists, "find_list_url", unreachable)
    [(_, cand, status)] = asyncio.run(state_lists.run_find_lists(conn, force=True))
    assert (cand, status) == (None, "unreachable")
    stored = db.get_state_program(conn, "PA")
    assert stored is not None
    assert (stored.list_url, stored.list_status) == ("https://doh.example/roster.csv", "found")


# ── the coverage check ────────────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _Session:
    """HEAD refused with 405; GET serves the page."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def head(self, url: str, **_kwargs: object) -> _Resp:
        self.calls.append("HEAD")
        return _Resp(405)

    async def get(self, url: str, **_kwargs: object) -> _Resp:
        self.calls.append("GET")
        return _Resp(200)


def test_a_server_refusing_head_is_checked_with_get(monkeypatch) -> None:
    session = _Session()
    monkeypatch.setattr(state_search, "make_session", lambda: session)
    assert asyncio.run(state_search._check_url("https://doh.example/landing")) == (True, 200)
    assert session.calls == ["HEAD", "GET"]


class _Browser:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def start(self) -> object:
        return object()


def test_a_search_that_finds_nothing_does_not_erase_the_stored_best_url(monkeypatch) -> None:
    conn = pg_conn()
    db.create_tables(conn)
    db.upsert_state_program(conn, _program())
    conn.commit()

    async def nothing_found(backends: object, state: StateInfo) -> state_search.StateCoverage:
        return state_search.StateCoverage(state=state, queries_tried=["q"])

    monkeypatch.setattr(state_search, "load_states", lambda: [_PA])
    monkeypatch.setattr(state_search, "_search_state", nothing_found)
    monkeypatch.setattr(state_search, "Chrome", lambda **_kwargs: _Browser())
    asyncio.run(state_search.run_state_coverage(conn, force=True))
    stored = db.get_state_program(conn, "PA")
    assert stored is not None
    assert stored.best_url == "https://doh.example/landing"
    assert stored.check_status == "failed"
