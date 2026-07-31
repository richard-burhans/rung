"""Async waiting gate over the ``token_buckets`` limiter — the orchestrator for
:func:`rung.rate_limit.try_acquire`.

``try_acquire`` is the pure primitive: one atomic refill-and-deduct that answers "may I go now?"
and leaves the transaction to its caller. That is the right shape for the SQL, and the wrong shape
for a scraper — a menu consumer that is denied does not want an answer, it wants a turn. This
module supplies the missing half: **wait for the token, on a connection of its own**.

The dedicated connection is the whole point, and it is the same reason
:func:`rung.queue.heartbeat_forever` opens one. Stage-2/3 run 6-8 consumers over ONE shared
psycopg connection; committing the token deduction on that connection would commit whatever
partial ``run_target`` transaction a sibling consumer had in flight. So the gate owns a connection,
serialises access to it with an ``asyncio.Lock``, and does the blocking psycopg call in a worker
thread so the event loop keeps running while it waits.

Two deliberate refusals to be clever:

- **It fails OPEN, and latches.** If the gate's own database call raises — a missing table, a dead
  connection, a data source that isn't Postgres — the caller proceeds, the gate counts it, and the
  gate **disables itself for the rest of the process**. A rate limiter that halts a national sweep
  because Postgres blinked has done more damage than the request multiplication it exists to
  prevent, and re-trying a broken gate on every request would add a failed round trip to each one.
  The failure is counted and reported, never silent.
- **It gives up.** ``acquire`` returns False at ``max_wait_s`` rather than waiting forever, so a
  mis-sized bucket shows up as shed work in a report instead of a sweep that appears to hang. What
  the caller does with a False — hand the target back, skip it, proceed anyway — is the caller's
  policy, not the gate's.

**What one gate instance actually coordinates.** A gate is per process, and its keys are expected
to carry an egress identity, so what it coordinates is *the processes sharing one egress* — in
practice several worker processes on one box, plus that box's cron runs. It is not a substitute
for giving each worker its own IP; where egress rotation is in play the caller should not consult
a gate at all, because the limit it would enforce is not the limit that binds.

The per-key *rates*, and which keys exist at all, are policy and live with the caller; this module
knows only how to wait. It names no target and carries no default host table.
"""

import asyncio
import contextlib
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from rung import db, rate_limit

# Floor/ceiling on one wait between re-checks. The floor stops a very high configured rate from
# spinning the gate against the database. The ceiling bounds how long a very slow bucket sleeps
# before looking again, so a worker still notices tokens another worker freed early rather than
# sleeping through most of its deadline. (Overshooting the deadline is not the concern — each
# sleep is separately clamped to the time remaining.)
_MIN_SLEEP = 0.05
_MAX_SLEEP = 5.0


@dataclass
class GateStats:
    """What the gate did this run — printed by the runners so a mis-sized bucket is visible.

    ``shed`` is the count that matters: it is targets the fleet declined to start because the
    shared budget was exhausted. A nonzero ``errors`` means the gate fell open and the limit was
    NOT enforced for that many calls.
    """

    granted: int = 0
    shed: int = 0
    errors: int = 0
    waited_s: float = 0.0
    by_key: dict[str, int] = field(default_factory=dict)  # bucket key -> times shed

    def summary(self) -> str:
        parts = [f"granted {self.granted}", f"shed {self.shed}", f"waited {self.waited_s:.1f}s"]
        if self.errors:
            # Loud on purpose: the gate is disabled and the limit is NOT being enforced.
            parts.append(f"DISABLED after {self.errors} error(s) — limit not enforced")
        if self.by_key:
            worst = sorted(self.by_key.items(), key=lambda kv: -kv[1])[:3]
            parts.append("shed by " + ", ".join(f"{key}×{n}" for key, n in worst))
        return "rate gate: " + ", ".join(parts)


class HostGate:
    """Wait for a cross-worker token before starting work against a host.

    One instance per worker process, opened lazily and closed by the runner that made it. Safe to
    share across concurrent consumers in one event loop: the internal lock serialises the single
    connection, and each database round trip runs in a thread so it never blocks the loop.
    """

    def __init__(
        self,
        conn_factory: Callable[[], db.DBConn] = db.get_connection,
        *,
        max_wait_s: float = 60.0,
    ) -> None:
        self._conn_factory = conn_factory
        self._max_wait_s = max_wait_s
        self._conn: db.DBConn | None = None
        self._lock = asyncio.Lock()
        self._disabled = False
        self.stats = GateStats()

    def _try_once(self, key: str, rate_per_sec: float, burst: float, cost: float) -> bool:
        """One synchronous attempt on the gate's own connection (run in a worker thread)."""
        if self._conn is None:
            conn = self._conn_factory()
            # Autocommit so a waiting gate does not sit idle-in-transaction across its polls,
            # pinning a snapshot and blocking autovacuum on what is by design a hot single row.
            # `try_acquire` is one atomic statement, so autocommit satisfies its caller-commits
            # contract; the explicit commit below stays because it states that contract.
            conn.autocommit = True
            self._conn = conn
        granted = rate_limit.try_acquire(
            self._conn, key, rate_per_sec=rate_per_sec, burst=burst, cost=cost
        )
        self._conn.commit()
        return granted

    def _drop_connection(self) -> None:
        """Discard a connection that raised, so the next attempt reconnects instead of reusing a
        session left in a failed transaction."""
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()  # best-effort: the connection is already known-bad

    async def acquire(
        self, key: str, *, rate_per_sec: float, burst: float, cost: float = 1.0
    ) -> bool:
        """Wait for ``cost`` tokens on ``key``; return whether they were granted before the deadline.

        Retries ``rate_limit.try_acquire`` until it succeeds or ``max_wait_s`` elapses, sleeping
        between attempts for one to two times the interval the bucket needs to accrue what was
        asked for (``cost / rate_per_sec``). ``try_acquire`` reports only a bool, not the token
        level, so that interval is a floor rather than an exact deficit — but a floor is what
        matters, since a denied attempt still takes a row lock on the same tuple and polling under
        it merely serialises the waiters. The jitter is the other half: N workers denied at the
        same instant must not re-collide at the same instant.

        Returns True immediately once the gate has disabled itself (fail open, see the module
        docstring), and rejects a configuration that could never be satisfied.
        """
        if self._disabled:
            return True
        # An unsatisfiable request would not be throttled, it would HANG until the deadline and
        # then shed every single target — a total outage wearing a rate limiter's clothes. The
        # bucket refills to at most `burst`, so `cost > burst` can never clear `try_acquire`'s
        # WHERE; with no refill rate an empty bucket never recovers either.
        if cost > burst or rate_per_sec <= 0:
            raise ValueError(
                f"unsatisfiable rate-gate config for {key!r}: cost={cost} burst={burst} "
                f"rate_per_sec={rate_per_sec} (need cost <= burst and rate_per_sec > 0)"
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._max_wait_s
        started = loop.time()
        while True:
            try:
                async with self._lock:
                    granted = await asyncio.to_thread(
                        self._try_once, key, rate_per_sec, burst, cost
                    )
            except Exception:
                # Any database fault fails OPEN and LATCHES (see the module docstring): the caller
                # proceeds, the gate stops trying, and the report says the limit is not holding.
                # Latching matters — a broken gate retried per request adds a failed round trip to
                # every request in the sweep, which is a slower outage, not a safer one.
                self._drop_connection()
                self._disabled = True
                self.stats.errors += 1
                return True
            if granted:
                self.stats.granted += 1
                self.stats.waited_s += loop.time() - started
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                self.stats.shed += 1
                self.stats.by_key[key] = self.stats.by_key.get(key, 0) + 1
                self.stats.waited_s += loop.time() - started
                return False
            # Sleep at LEAST the time the bucket needs to accrue what we asked for, jittered up to
            # twice that. The floor is not politeness: every denied try_acquire still takes a row
            # lock on the same tuple, so polling faster than the bucket can refill just serialises
            # the waiters on one row and buys nothing. The jitter is what keeps N workers denied at
            # the same instant from re-colliding at the same instant (the full-jitter reasoning
            # behind the retry backoff). Never sleep past what is left of the deadline.
            need = cost / rate_per_sec
            delay = min(max(need * (1.0 + random.random()), _MIN_SLEEP), _MAX_SLEEP, remaining)
            await asyncio.sleep(delay)

    def close(self) -> None:
        """Close the dedicated connection (the runner that built the gate owns this)."""
        self._drop_connection()
