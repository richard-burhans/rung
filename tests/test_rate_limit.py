"""Tests for the public cross-worker token-bucket limiter (rung.rate_limit): burst
then denial, refill from elapsed time, and per-host isolation. Postgres ``now()`` is the clock, so
elapsed time is simulated by back-dating ``last_refill`` on the stored row."""

from conftest import pg_conn

from rung import db, rate_limit


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)
    return conn


def test_burst_then_denied() -> None:
    conn = _conn()
    # rate 0 → no refill; a burst of 3 grants exactly 3 back-to-back, then denies.
    for _ in range(3):
        assert rate_limit.try_acquire(conn, "h", rate_per_sec=0.0, burst=3.0)
    assert not rate_limit.try_acquire(conn, "h", rate_per_sec=0.0, burst=3.0)
    conn.commit()


def test_refill_after_elapsed_time() -> None:
    conn = _conn()
    assert rate_limit.try_acquire(conn, "h", rate_per_sec=1.0, burst=1.0)   # 1 → 0
    assert not rate_limit.try_acquire(conn, "h", rate_per_sec=1.0, burst=1.0)  # empty → denied
    conn.commit()
    # Simulate two seconds of elapsed time: the bucket refills at 1 token/s (capped at burst).
    conn.execute("UPDATE token_buckets SET last_refill = now() - interval '2 seconds' WHERE host = 'h'")
    conn.commit()
    assert rate_limit.try_acquire(conn, "h", rate_per_sec=1.0, burst=1.0)   # refilled → granted
    conn.commit()


def test_cost_greater_than_one_deducts_more() -> None:
    conn = _conn()
    assert rate_limit.try_acquire(conn, "h", rate_per_sec=0.0, burst=5.0, cost=3.0)  # 5 → 2
    assert not rate_limit.try_acquire(conn, "h", rate_per_sec=0.0, burst=5.0, cost=3.0)  # 2 < 3 denied
    # A cheaper request still fits in the remaining 2 tokens.
    assert rate_limit.try_acquire(conn, "h", rate_per_sec=0.0, burst=5.0, cost=2.0)  # 2 → 0
    conn.commit()


def test_denied_request_does_not_consume_tokens() -> None:
    conn = _conn()
    assert rate_limit.try_acquire(conn, "h", rate_per_sec=0.0, burst=1.0)   # 1 → 0
    assert not rate_limit.try_acquire(conn, "h", rate_per_sec=0.0, burst=1.0)
    row = conn.execute("SELECT tokens FROM token_buckets WHERE host = 'h'").fetchone()
    assert row is not None and row[0] == 0.0  # a denial left tokens at 0, not negative
    conn.commit()


def test_hosts_are_isolated() -> None:
    conn = _conn()
    assert rate_limit.try_acquire(conn, "a", rate_per_sec=0.0, burst=1.0)   # drains a
    assert not rate_limit.try_acquire(conn, "a", rate_per_sec=0.0, burst=1.0)
    assert rate_limit.try_acquire(conn, "b", rate_per_sec=0.0, burst=1.0)   # b has its own bucket
    conn.commit()
