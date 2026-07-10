"""A rung must be able to say WHY it produced nothing, and the engine must persist the difference.

Three times this project reported a failure of its own as a fact about the world — a paper called
"paywalled" because our Unpaywall email was a placeholder; a paper called "paywalled" because a Europe
PMC URL 404'd for *every* article; and an HTTP 200 that was a reCAPTCHA page. Two were invisible for
weeks, because a broken rung and an empty world produced the same row.

These tests pin the asymmetry that fixes it: an explicit `Unavailable` is the *only* thing that
records 'unavailable'. Silence records 'failed' — unknown — because mistaking a broken rung for an
empty world means you stop looking.
"""

import pytest

from rung import access, db
from tests.conftest import pg_conn


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_engine_tables(conn)
    return conn


class _Record:
    """A plausible record: `is_plausible` wants a name and a location signal."""

    name = "Somewhere"
    city = "Anytown"


def _method(name: str, cost: int, run) -> access.AccessMethod:
    return access.AccessMethod(name=name, cost_rank=cost, run=run)


def _raises(exc: Exception):
    async def _run(_conn, _key, _hint):
        raise exc
    return _run


def _yields(records: list):
    async def _run(_conn, _key, _hint):
        return records, "https://example.test/x", None
    return _run


def _status_of(conn, method: str) -> str:
    rows = {r[0]: r[2] for r in db.get_access_methods(conn, "t", "k")}
    return rows[method]


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        (access.Unavailable("no such paper"), "unavailable"),
        (access.Blocked("403 from the edge"), "blocked"),
        (access.Broken("endpoint 404s for every target"), "broken"),
    ],
)
def test_each_signal_persists_its_own_status(signal, expected) -> None:
    import asyncio
    conn = _conn()
    ladder = [_method("m", 0, _raises(signal))]
    winner, records = asyncio.run(access.run_target(conn, "t", "k", ladder))

    assert (winner, records) == (None, [])
    assert _status_of(conn, "m") == expected


def test_silence_records_failed_and_never_unavailable() -> None:
    # The whole asymmetry. A rung that returns nothing without explaining itself is UNKNOWN, not a
    # statement about the world. Calling it 'unavailable' is how you stop looking at a broken rung.
    import asyncio
    conn = _conn()
    ladder = [_method("quiet", 0, _yields([]))]
    asyncio.run(access.run_target(conn, "t", "k", ladder))

    assert _status_of(conn, "quiet") == "failed"
    assert _status_of(conn, "quiet") != "unavailable"


def test_the_ladder_keeps_walking_after_a_signal() -> None:
    # `unavailable` by one route does not mean unavailable by every route.
    import asyncio
    conn = _conn()
    ladder = [
        _method("cheap", 0, _raises(access.Unavailable("not on this host"))),
        _method("dear", 1, _yields([_Record()])),
    ]
    winner, records = asyncio.run(access.run_target(conn, "t", "k", ladder))

    assert winner == "dear"
    assert len(records) == 1
    assert _status_of(conn, "cheap") == "unavailable"
    assert _status_of(conn, "dear") == "ok"


def test_a_signalling_rung_never_becomes_the_winner() -> None:
    import asyncio
    conn = _conn()
    ladder = [_method("broken", 0, _raises(access.Broken("dead url")))]
    asyncio.run(access.run_target(conn, "t", "k", ladder))
    assert db.get_access_winner(conn, "t", "k") is None


def test_an_unexpected_exception_still_propagates() -> None:
    # A rung that crashes has not told us why it failed. Swallowing it here would recreate exactly the
    # silence this vocabulary exists to break.
    import asyncio
    conn = _conn()
    ladder = [_method("boom", 0, _raises(RuntimeError("kaboom")))]
    with pytest.raises(RuntimeError, match="kaboom"):
        asyncio.run(access.run_target(conn, "t", "k", ladder))


def test_every_non_ok_outcome_advances_last_fail_at() -> None:
    # The staleness governor reads these timestamps. Before the vocabulary, only 'failed' set them.
    conn = _conn()
    for i, status in enumerate(("unavailable", "blocked", "broken", "failed")):
        db.record_access_attempt(conn, "t", f"k{i}", "m", 0, status)
        conn.commit()
        [row] = db.get_access_methods(conn, "t", f"k{i}")
        assert row[db.ACCESS_METHOD_COLUMNS.index("last_fail_at")] is not None, status


def test_the_database_refuses_a_status_outside_the_vocabulary() -> None:
    # A typo'd status is a silent lie about why a rung stopped working. Two layers refuse it.
    conn = _conn()
    with pytest.raises(ValueError, match="unknown access status"):
        db.record_access_attempt(conn, "t", "k", "m", 0, "paywalled")

    with pytest.raises(Exception, match="access_methods_status_check"):
        conn.execute(
            "INSERT INTO access_methods (target_type, target_key, method, cost_rank, status, "
            "updated_at) VALUES ('t', 'k2', 'm', 0, 'paywalled', 'now')"
        )
        conn.commit()


def test_access_health_sorts_broken_first() -> None:
    conn = _conn()
    db.record_access_attempt(conn, "t", "a", "fine", 0, "ok")
    db.record_access_attempt(conn, "t", "b", "gone", 1, "broken")
    db.record_access_attempt(conn, "t", "c", "walled", 2, "unavailable")
    conn.commit()

    health = db.access_health(conn, "t")
    assert health[0][:2] == ("gone", "broken")          # what a canary must see first
    assert ("walled", "unavailable", 1) in health
    assert ("fine", "ok", 1) in health
