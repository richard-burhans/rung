"""Tests for the waiting gate over the token bucket (rung.rate_gate).

The bucket primitive itself is covered by test_rate_limit.py; what is tested here is everything
the gate adds on top — that it waits and then succeeds, that it gives up at the deadline instead
of hanging, that a broken database fails OPEN and latches rather than stalling a sweep, and that
a configuration which could never be satisfied is rejected rather than silently shedding
everything. Imports only ``rung`` (no overlay), so this coverage ships with the public package.
"""

import asyncio

import pytest
from conftest import pg_conn

from rung import db, rate_gate


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)
    return conn


def _gate(conn: db.DBConn, **kwargs) -> rate_gate.HostGate:
    """A gate over the test's own schema-scoped connection (the factory hands back the same one,
    which is what a single-connection gate does anyway)."""
    return rate_gate.HostGate(lambda: conn, **kwargs)


def test_grants_from_a_full_bucket() -> None:
    conn = _conn()
    gate = _gate(conn)
    assert asyncio.run(gate.acquire("k", rate_per_sec=10.0, burst=5.0))
    assert gate.stats.granted == 1
    assert gate.stats.shed == 0


def test_waits_then_grants_once_the_bucket_refills() -> None:
    """The point of the gate: a denial is a wait, not a failure."""
    conn = _conn()
    gate = _gate(conn, max_wait_s=10.0)

    async def drain_then_acquire() -> bool:
        # Empty the bucket, then ask again — the second call must sleep and come back granted.
        assert await gate.acquire("k", rate_per_sec=20.0, burst=1.0)
        return await gate.acquire("k", rate_per_sec=20.0, burst=1.0)

    assert asyncio.run(drain_then_acquire())
    assert gate.stats.granted == 2
    assert gate.stats.waited_s > 0  # the second call really did wait


def test_sheds_at_the_deadline_instead_of_hanging() -> None:
    conn = _conn()
    gate = _gate(conn, max_wait_s=0.0)
    # A rate this slow cannot refill within the deadline, so the second call must give up.
    assert asyncio.run(gate.acquire("k", rate_per_sec=0.001, burst=1.0))
    assert not asyncio.run(gate.acquire("k", rate_per_sec=0.001, burst=1.0))
    assert gate.stats.shed == 1
    assert gate.stats.by_key == {"k": 1}


def test_buckets_are_isolated_by_key() -> None:
    conn = _conn()
    gate = _gate(conn, max_wait_s=0.0)
    assert asyncio.run(gate.acquire("a", rate_per_sec=0.001, burst=1.0))
    assert not asyncio.run(gate.acquire("a", rate_per_sec=0.001, burst=1.0))
    assert asyncio.run(gate.acquire("b", rate_per_sec=0.001, burst=1.0))  # its own budget


def test_database_failure_fails_open_and_latches() -> None:
    """A broken gate must not stall the sweep, and must not re-pay the failure per request."""
    calls = []

    def broken_factory() -> db.DBConn:
        calls.append(1)
        raise RuntimeError("no database")

    gate = rate_gate.HostGate(broken_factory)
    assert asyncio.run(gate.acquire("k", rate_per_sec=1.0, burst=1.0))   # proceeds anyway
    assert asyncio.run(gate.acquire("k", rate_per_sec=1.0, burst=1.0))
    assert asyncio.run(gate.acquire("k", rate_per_sec=1.0, burst=1.0))
    assert gate.stats.errors == 1        # counted once...
    assert len(calls) == 1               # ...and the broken factory is never retried
    assert "DISABLED" in gate.stats.summary()


@pytest.mark.parametrize(
    ("cost", "burst", "rate"),
    [
        (2.0, 1.0, 1.0),   # cost above burst: the bucket can never hold enough
        (1.0, 1.0, 0.0),   # no refill: an emptied bucket never recovers
    ],
)
def test_unsatisfiable_config_is_rejected_not_waited_out(
    cost: float, burst: float, rate: float
) -> None:
    """These would not throttle, they would hang to the deadline and then shed EVERY target — a
    total outage wearing a rate limiter's clothes. Fail loudly at the call instead."""
    conn = _conn()
    gate = _gate(conn)
    with pytest.raises(ValueError, match="unsatisfiable"):
        asyncio.run(gate.acquire("k", rate_per_sec=rate, burst=burst, cost=cost))


def test_concurrent_waiters_share_one_budget() -> None:
    """The whole reason the gate exists: N concurrent callers must not each get their own rate."""
    conn = _conn()
    gate = _gate(conn, max_wait_s=0.0)

    async def race() -> list[bool]:
        return await asyncio.gather(
            *[gate.acquire("k", rate_per_sec=0.001, burst=3.0) for _ in range(6)]
        )

    granted = asyncio.run(race())
    assert sum(granted) == 3      # exactly the burst, not one per caller
    assert gate.stats.shed == 3
