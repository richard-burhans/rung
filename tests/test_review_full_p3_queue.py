"""Queue fixes from the 2026-10-06 whole-tree review. Each test fails on the code before it."""

import asyncio
import itertools
import time

from conftest import pg_conn

from rung import db, queue


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)
    return conn


def _crash(conn: db.DBConn) -> None:
    """Make the claim look like a crashed worker's: old, and its lease lapsed."""
    conn.execute("UPDATE jobs SET claimed_at = now() - interval '2 hours', "
                 "lease_until = now() - interval '90 minutes'")


def test_a_targeted_claim_takes_a_just_requeued_job() -> None:
    """The requeue pushed `scheduled_at` up to 30 s ahead and a targeted claim waited for it, so a
    dedupe re-run after a crash printed "already running" and skipped its fold."""
    conn = _conn()
    queue.enqueue(conn, "dedupe", "PA")
    conn.commit()
    assert queue.claim_target(conn, "dedupe", "PA", "w1") is not None
    _crash(conn)
    assert queue.requeue_stale(conn, "dedupe") == 1
    conn.execute("UPDATE jobs SET scheduled_at = now() + interval '25 seconds'")  # the spread
    conn.commit()
    queue.enqueue(conn, "dedupe", "PA")   # a no-op: the pending row exists
    conn.commit()
    job = queue.claim_target(conn, "dedupe", "PA", "w2")
    assert job is not None and job.attempts == 2


def test_a_job_failed_at_the_cap_is_finished_and_prunable() -> None:
    """The failed branch of requeue/reap set no `finished_at`, and the prune deletes only finished
    rows, so a lease-expired failure stayed forever."""
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.execute("UPDATE jobs SET max_attempts = 1")
    conn.commit()
    queue.claim_next(conn, "t", "w1")
    _crash(conn)
    queue.reap_expired(conn, "t")
    conn.commit()
    status, finished = conn.execute("SELECT status, finished_at FROM jobs").fetchone()
    assert status == "failed" and finished is not None
    conn.execute("UPDATE jobs SET finished_at = now() - interval '30 days'")
    queue.prune_completed(conn, older_than_hours=24)
    conn.commit()
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_a_heartbeating_claim_is_not_requeued_however_old() -> None:
    """Selecting on claim age alone re-queued a live, heartbeating job at the next process start."""
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    queue.claim_next(conn, "t", "w1")
    conn.execute("UPDATE jobs SET claimed_at = now() - interval '2 hours'")
    queue.bump_worker_heartbeat(conn, "w1")         # the lease is live
    assert queue.requeue_stale(conn, "t") == 0
    conn.commit()
    assert conn.execute("SELECT status, claimed_by FROM jobs").fetchone() == ("claimed", "w1")


def test_targeted_keys_are_claimed_in_the_order_given() -> None:
    """`list.pop()` took from the END, so a stalest-first list was claimed freshest-first."""
    conn = _conn()
    keys = ["PA:a", "PA:b", "PA:c"]
    for key in keys:
        queue.enqueue(conn, "store_menu", key)
    conn.commit()
    given = list(keys)
    claim = queue.make_claimer(conn, "store_menu", "w1", given)
    claimed = []
    while (job := claim()) is not None:
        claimed.append(job.target_key)
    assert claimed == keys
    assert given == keys   # the caller's list is not consumed


class _SlowConn:
    """A connection whose every statement blocks the calling thread."""

    class _Cursor:
        rowcount = 0

    def execute(self, *_args: object) -> "_SlowConn._Cursor":
        time.sleep(0.3)
        return self._Cursor()

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_the_heartbeat_does_not_block_the_event_loop() -> None:
    """The bump, commit and reconnect ran on the loop, so a slow database froze every scrape."""

    async def main() -> float:
        beat = asyncio.create_task(
            queue.heartbeat_forever("w1", interval_s=60, conn_factory=_SlowConn))
        stamps = [time.monotonic()]   # before the task first runs, so its first bump is inside a gap
        for _ in range(40):
            await asyncio.sleep(0.01)
            stamps.append(time.monotonic())
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        return max(later - earlier for earlier, later in itertools.pairwise(stamps))

    # Each bump blocks its thread for 0.3 s; on the loop that is a 0.3 s gap between ticks.
    assert asyncio.run(main()) < 0.2
