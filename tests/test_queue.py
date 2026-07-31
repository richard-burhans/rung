"""Tests for the jobs work queue (rung.queue): live-job dedupe,
claim partitioning across connections, target-specific claims, stale recovery."""

import asyncio
import datetime

import conftest
import psycopg
import pytest
from conftest import pg_conn, pg_conn_sharing

from rung import db, queue


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)
    return conn


def test_enqueue_dedupes_live_jobs_and_keeps_history() -> None:
    conn = _conn()
    assert queue.enqueue(conn, "t", "k", {"x": 1})
    assert not queue.enqueue(conn, "t", "k")  # pending duplicate refused
    job = queue.claim_next(conn, "t", "w1")
    assert job is not None and job.payload == {"x": 1}
    assert not queue.enqueue(conn, "t", "k")  # claimed duplicate refused
    queue.complete(conn, job.id, "done", worker="w1")
    conn.commit()
    assert queue.enqueue(conn, "t", "k")  # done row is history; a new run enqueues
    conn.commit()
    count_row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    assert count_row == (2,)


def test_prune_completed_drops_old_finished_but_keeps_live_and_fresh() -> None:
    conn = _conn()
    # An old DONE job, an old FAILED job, a FRESH done job, and a live PENDING job.
    queue.enqueue(conn, "t", "old-done")
    queue.enqueue(conn, "t", "old-failed")
    queue.enqueue(conn, "t", "fresh-done")
    queue.enqueue(conn, "t", "still-pending")
    for tk, status in [("old-done", "done"), ("old-failed", "failed"), ("fresh-done", "done")]:
        job = queue.claim_target(conn, "t", tk, "w")
        queue.complete(conn, job.id, status, worker="w")
    conn.commit()
    # Backdate the two "old" jobs past the window; leave fresh-done at now().
    conn.execute(
        "UPDATE jobs SET finished_at = now() - make_interval(hours => 200) "
        "WHERE target_key IN ('old-done', 'old-failed')"
    )
    conn.commit()

    deleted = queue.prune_completed(conn, older_than_hours=168)
    conn.commit()
    assert deleted == 2  # only the two old finished jobs
    survivors = {
        tk for (tk,) in conn.execute("SELECT target_key FROM jobs").fetchall()
    }
    assert survivors == {"fresh-done", "still-pending"}  # fresh + live untouched


def test_claims_partition_across_connections() -> None:
    conn1 = _conn()
    queue.enqueue(conn1, "t", "a")
    queue.enqueue(conn1, "t", "b")
    conn1.commit()
    conn2 = pg_conn_sharing(conn1)

    job1 = queue.claim_next(conn1, "t", "w1")
    job2 = queue.claim_next(conn2, "t", "w2")
    assert job1 is not None and job2 is not None
    assert {job1.target_key, job2.target_key} == {"a", "b"}  # never the same job
    assert queue.claim_next(conn1, "t", "w1") is None  # drained


def test_claim_target_is_specific_and_exclusive() -> None:
    conn = _conn()
    queue.enqueue(conn, "dedupe", "PA")
    queue.enqueue(conn, "dedupe", "NY")
    conn.commit()
    job = queue.claim_target(conn, "dedupe", "NY", "w1")
    assert job is not None and job.target_key == "NY"
    assert queue.claim_target(conn, "dedupe", "NY", "w2") is None
    assert queue.live_claim_holder(conn, "dedupe", "NY") == "w1"
    assert queue.live_claim_holder(conn, "dedupe", "PA") is None  # pending, not claimed


def test_requeue_stale_recovers_then_fails_at_attempt_cap() -> None:
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    queue.claim_next(conn, "t", "w1")
    conn.execute("UPDATE jobs SET claimed_at = now() - interval '2 hours'")
    assert queue.requeue_stale(conn, "t") == 1
    conn.commit()
    assert conn.execute("SELECT status, attempts FROM jobs").fetchone() == ("pending", 1)

    # Two more crashed claims exhaust max_attempts (3) → failed, not requeued. Clear the requeue
    # jitter (scheduled_at is pushed ~0–30s out) each round so the retry is claimable now.
    for _ in range(2):
        conn.execute("UPDATE jobs SET scheduled_at = now()")
        queue.claim_next(conn, "t", "w1")
        conn.execute("UPDATE jobs SET claimed_at = now() - interval '2 hours'")
        queue.requeue_stale(conn, "t")
        conn.commit()
    assert conn.execute("SELECT status, error FROM jobs").fetchone() == (
        "failed", "claim timeout",
    )


def test_fresh_claim_not_requeued() -> None:
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    queue.claim_next(conn, "t", "w1")
    assert queue.requeue_stale(conn, "t") == 0  # live claim left alone


def test_complete_only_succeeds_for_the_holding_worker() -> None:
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    job = queue.claim_next(conn, "t", "w1")
    assert job is not None
    # A worker that does not hold the claim cannot complete the job.
    assert queue.complete(conn, job.id, "done", worker="w2") is False
    assert conn.execute("SELECT status FROM jobs WHERE id=%s", (job.id,)).fetchone() == ("claimed",)
    # The holding worker can.
    assert queue.complete(conn, job.id, "done", worker="w1") is True
    assert conn.execute("SELECT status FROM jobs WHERE id=%s", (job.id,)).fetchone() == ("done",)


def test_release_hands_a_claim_back_unworked() -> None:
    """Load shedding: the target returns to pending with the claim cleared and its claim-time
    attempt un-counted, because nothing was ever attempted against it."""
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    job = queue.claim_next(conn, "t", "w1")
    assert job is not None and job.attempts == 1
    assert queue.release(conn, job.id, worker="w1") is True
    conn.commit()
    row = conn.execute(
        "SELECT status, claimed_by, claimed_at, lease_until, attempts FROM jobs WHERE id=%s",
        (job.id,),
    ).fetchone()
    assert row == ("pending", None, None, None, 0)


def test_release_defers_the_target_rather_than_leaving_it_claimable() -> None:
    """The push-out is the termination condition, not politeness: a shed target that stayed
    immediately claimable would let one worker claim → shed → re-claim it without bound, since
    release also gives back the `attempts` increment that normally caps the cycle."""
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    job = queue.claim_next(conn, "t", "w1")
    assert job is not None
    queue.release(conn, job.id, worker="w1", delay_seconds=300)
    conn.commit()
    assert queue.claim_next(conn, "t", "w1") is None  # not claimable again this drain


def test_release_only_succeeds_for_the_holding_worker() -> None:
    """A reaper may already have handed this target to someone else; releasing then would clear
    THEIR claim — the same hazard complete() guards against."""
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    job = queue.claim_next(conn, "t", "w1")
    assert job is not None
    assert queue.release(conn, job.id, worker="w2") is False
    assert conn.execute(
        "SELECT status, claimed_by FROM jobs WHERE id=%s", (job.id,)
    ).fetchone() == ("claimed", "w1")


def test_release_never_drives_attempts_negative() -> None:
    """GREATEST(0, …): a double release (or a release of a row claimed before the counter existed)
    must not leave a negative attempt count that would then absorb real failures."""
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    job = queue.claim_next(conn, "t", "w1")
    assert job is not None
    queue.release(conn, job.id, worker="w1", delay_seconds=0)
    conn.commit()
    again = queue.claim_next(conn, "t", "w1")
    assert again is not None
    queue.release(conn, again.id, worker="w1", delay_seconds=0)
    queue.release(conn, again.id, worker="w1", delay_seconds=0)  # no longer the holder: no-op
    conn.commit()
    assert conn.execute("SELECT attempts FROM jobs WHERE id=%s", (job.id,)).fetchone() == (0,)


def test_reclaim_isolation_holds_across_two_live_connections() -> None:
    # The same resurrection race as below, but the reclaim and the orphaned completion happen on
    # SEPARATE connections — the real shape workers hit. Proves the worker-scoped complete() guard
    # honours a reclaim COMMITTED by another connection, not just same-connection state.
    conn_a = _conn()  # w1's connection
    queue.enqueue(conn_a, "t", "k")
    conn_a.commit()
    conn_b = pg_conn_sharing(conn_a)  # w2's connection, same schema

    job = queue.claim_next(conn_a, "t", "w1")
    assert job is not None
    conn_a.execute("UPDATE jobs SET claimed_at = now() - interval '2 hours'")  # w1 looks stale
    queue.requeue_stale(conn_a, "t")                      # claimed -> pending
    conn_a.execute("UPDATE jobs SET scheduled_at = now()")  # clear the requeue jitter
    conn_a.commit()                                       # w2 must see the freed row

    reclaim = queue.claim_next(conn_b, "t", "w2")         # w2 reclaims on its OWN connection
    assert reclaim is not None and reclaim.id == job.id

    # w1's late completion (conn_a) must be a no-op now that w2 (conn_b) holds the reclaim.
    assert queue.complete(conn_a, job.id, "done", worker="w1") is False
    conn_a.commit()
    assert queue.complete(conn_b, reclaim.id, "done", worker="w2") is True
    conn_b.commit()

    status, holder = conn_a.execute(
        "SELECT status, claimed_by FROM jobs WHERE id = %s", (job.id,)
    ).fetchone()
    assert (status, holder) == ("done", "w2")  # w2 won; w1's orphaned write did not clobber it


def test_requeued_reclaim_orphans_the_original_workers_completion() -> None:
    # The resurrection race: a slow-but-alive w1's claim is requeued (looks stale) and reclaimed by
    # w2; w1's late completion must be a no-op so the orphan rolls back rather than clobbering w2.
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    job = queue.claim_next(conn, "t", "w1")
    assert job is not None
    conn.execute("UPDATE jobs SET claimed_at = now() - interval '2 hours'")  # w1 now looks stale
    queue.requeue_stale(conn, "t")                       # claimed -> pending
    conn.execute("UPDATE jobs SET scheduled_at = now()")  # clear the requeue jitter so it claims now
    conn.commit()
    reclaim = queue.claim_next(conn, "t", "w2")           # w2 reclaims the SAME row
    assert reclaim is not None and reclaim.id == job.id
    assert queue.complete(conn, job.id, "done", worker="w1") is False   # orphaned w1: no-op
    assert queue.complete(conn, reclaim.id, "done", worker="w2") is True  # w2 wins
    assert conn.execute("SELECT claimed_by FROM jobs WHERE id=%s", (job.id,)).fetchone() == ("w2",)


def test_claim_next_target_prefix_scopes_to_state() -> None:
    """A per-state run claims only its own STATE: jobs, never another state's (the orphan-jobs fix)."""
    conn = _conn()
    queue.enqueue(conn, "store_menu", "NY:dutchie:a")
    queue.enqueue(conn, "store_menu", "OK:dutchie:b")
    conn.commit()
    job = queue.claim_next(conn, "store_menu", "w1", target_prefix="NY:")
    assert job is not None and job.target_key == "NY:dutchie:a"
    # No NY jobs left → None even though OK is still pending (it is NOT stolen).
    assert queue.claim_next(conn, "store_menu", "w1", target_prefix="NY:") is None
    # Unscoped drain still reaches the OK job.
    ok = queue.claim_next(conn, "store_menu", "w2")
    assert ok is not None and ok.target_key == "OK:dutchie:b"


def test_claim_next_target_suffix_scopes_to_state() -> None:
    """Stage-2's key is '{company_id}:{state}' (state as SUFFIX), so a per-state run must scope by
    suffix — the prefix trick can't reach it. Same orphan-jobs guarantee as the prefix test."""
    conn = _conn()
    queue.enqueue(conn, "company_stores", "12:NY")
    queue.enqueue(conn, "company_stores", "34:OK")
    conn.commit()
    job = queue.claim_next(conn, "company_stores", "w1", target_suffix=":NY")
    assert job is not None and job.target_key == "12:NY"
    # No NY jobs left → None even though OK is still pending (it is NOT stolen and then failed).
    assert queue.claim_next(conn, "company_stores", "w1", target_suffix=":NY") is None
    # The ':NY' suffix does not spuriously match a different state (e.g. a hypothetical '…:MNY').
    ok = queue.claim_next(conn, "company_stores", "w2")
    assert ok is not None and ok.target_key == "34:OK"


def test_claim_next_rejects_both_prefix_and_suffix() -> None:
    conn = _conn()
    with pytest.raises(ValueError):
        queue.claim_next(conn, "company_stores", "w1", target_prefix="NY:", target_suffix=":NY")


# ── Lease / heartbeat / reaper (distributed-worker hardening, issue #16) ──────────────────────────

def test_claim_stamps_lease_and_heartbeat() -> None:
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    job = queue.claim_next(conn, "t", "w1", lease_minutes=30)
    assert job is not None
    row = conn.execute(
        "SELECT last_heartbeat IS NOT NULL, lease_until IS NOT NULL, "
        "lease_until > now(), lease_until <= now() + make_interval(mins => 31) "
        "FROM jobs WHERE id = %s", (job.id,)
    ).fetchone()
    assert row == (True, True, True, True)  # both stamped; lease is ~30 min out


def test_bump_heartbeat_extends_only_for_the_holding_worker() -> None:
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    job = queue.claim_next(conn, "t", "w1", lease_minutes=1)
    assert job is not None
    # A worker that does not hold the claim cannot extend the lease.
    assert queue.bump_heartbeat(conn, job.id, "w2") is False
    # The holding worker can; the lease is pushed out to the new window.
    assert queue.bump_heartbeat(conn, job.id, "w1", lease_minutes=30) is True
    conn.commit()
    extended = conn.execute(
        "SELECT lease_until > now() + make_interval(mins => 20) FROM jobs WHERE id = %s", (job.id,)
    ).fetchone()
    assert extended == (True,)


def test_reap_expired_requeues_lease_expired_and_ignores_fresh() -> None:
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    queue.claim_next(conn, "t", "w1")
    # A fresh claim (lease in the future) is left alone.
    assert queue.reap_expired(conn, "t") == 0
    # Expire the lease → the reaper re-queues it and clears the claim.
    conn.execute("UPDATE jobs SET lease_until = now() - interval '1 minute'")
    conn.commit()
    assert queue.reap_expired(conn, "t") == 1
    conn.commit()
    assert conn.execute(
        "SELECT status, claimed_by, lease_until FROM jobs"
    ).fetchone() == ("pending", None, None)


def test_reap_expired_fails_at_attempt_cap() -> None:
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    # Exhaust the three attempts, each ending in an expired lease the reaper recovers. Clear the
    # reap jitter (scheduled_at pushed ~0–30s out) each round so the retry is claimable now.
    for _ in range(3):
        conn.execute("UPDATE jobs SET scheduled_at = now()")
        queue.claim_next(conn, "t", "w1")
        conn.execute("UPDATE jobs SET lease_until = now() - interval '1 minute'")
        queue.reap_expired(conn, "t")
        conn.commit()
    assert conn.execute("SELECT status, error FROM jobs").fetchone() == ("failed", "lease expired")


def test_two_reapers_do_not_both_reap_the_same_row() -> None:
    # SKIP LOCKED in the reaper subquery: while one reaper's uncommitted UPDATE holds the row lock,
    # a second reaper skips it rather than double-requeuing.
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    queue.claim_next(conn, "t", "w1")
    conn.execute("UPDATE jobs SET lease_until = now() - interval '1 minute'")
    conn.commit()
    other = pg_conn_sharing(conn)
    n1 = queue.reap_expired(conn, "t")     # conn locks + re-queues the row (uncommitted)
    n2 = queue.reap_expired(other, "t")    # other's SKIP LOCKED skips the locked row
    conn.commit()
    other.commit()
    assert {n1, n2} == {1, 0}


# ── Per-worker heartbeat + reap-jobs command (crash-recovery wiring, issue #16) ───────────────────

def test_bump_worker_heartbeat_extends_all_of_a_workers_claims_only() -> None:
    conn = _conn()
    for k in ("a", "b", "c"):
        queue.enqueue(conn, "t", k)
    conn.commit()
    # w1 holds two claims (a, b — oldest first); w2 holds one (c).
    queue.claim_next(conn, "t", "w1", lease_minutes=1)
    queue.claim_next(conn, "t", "w1", lease_minutes=1)
    assert queue.claim_next(conn, "t", "w2", lease_minutes=1) is not None
    # One statement bumps EVERY live claim w1 holds; returns the count.
    assert queue.bump_worker_heartbeat(conn, "w1", lease_minutes=30) == 2
    conn.commit()
    moved = conn.execute(
        "SELECT count(*) FROM jobs WHERE claimed_by = 'w1' "
        "AND lease_until > now() + make_interval(mins => 20)"
    ).fetchone()
    assert moved == (2,)  # both of w1's leases pushed to the new window
    untouched = conn.execute(
        "SELECT lease_until <= now() + make_interval(mins => 5) FROM jobs WHERE claimed_by = 'w2'"
    ).fetchone()
    assert untouched == (True,)  # w2's lease was NOT touched


def test_heartbeat_forever_bumps_the_lease_then_stops_cleanly_on_cancel() -> None:
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    job = queue.claim_next(conn, "t", "w1", lease_minutes=1)
    assert job is not None

    def _dedicated_conn() -> db.DBConn:
        # A connection into THIS test's schema that heartbeat_forever OWNS and closes on cancel;
        # deliberately NOT registered with the conftest pool (it manages its own lifecycle).
        row = conn.execute("SELECT current_schema()").fetchone()
        assert row is not None
        other = psycopg.connect(conftest._TEST_URL)
        other.execute(f"SET search_path TO {row[0]}")
        other.commit()
        return other

    async def run() -> None:
        hb = asyncio.create_task(queue.heartbeat_forever(
            "w1", interval_s=0.05, lease_minutes=30, conn_factory=_dedicated_conn,
        ))
        await asyncio.sleep(0.2)  # let at least one bump + commit fire
        hb.cancel()
        assert await hb is None  # CancelledError is swallowed; the coroutine returns

    asyncio.run(run())
    # A separate connection committed a bump that pushed the 1-min lease out to ~30 min.
    extended = conn.execute(
        "SELECT lease_until > now() + make_interval(mins => 20) FROM jobs WHERE id = %s",
        (job.id,),
    ).fetchone()
    assert extended == (True,)


def test_reap_jobs_cmd_requeues_lease_expired_across_task_types(monkeypatch) -> None:
    from click.testing import CliRunner

    from rung import cli

    conn = _conn()
    queue.enqueue(conn, "store_menu", "expired")
    queue.enqueue(conn, "company_stores", "fresh")
    conn.commit()
    queue.claim_next(conn, "store_menu", "w1")
    queue.claim_next(conn, "company_stores", "w2")
    # Expire ONLY the store_menu claim's lease.
    conn.execute(
        "UPDATE jobs SET lease_until = now() - interval '1 minute' WHERE task_type = 'store_menu'"
    )
    conn.commit()
    # Route the command at this test's schema and keep its connection open for teardown.
    monkeypatch.setattr(cli.db, "get_connection", lambda: conn)
    monkeypatch.setattr(conn, "close", lambda: None)

    result = CliRunner().invoke(cli.reap_jobs_cmd, [])
    assert result.exit_code == 0
    assert "Reaped 1 lease-expired jobs." in result.output
    rows = dict(conn.execute("SELECT task_type, status FROM jobs").fetchall())
    assert rows == {"store_menu": "pending", "company_stores": "claimed"}


# ── Queue residuals: opt-in run_at spread + retry jitter (distributed-cron, §4-5) ─────────────────

def test_enqueue_default_is_immediately_claimable() -> None:
    # Back-compat: the self-feeding callers (menus/company_stores/cli) enqueue then drain in the
    # SAME run, so the default (no spread) must leave scheduled_at at now() → claimable at once.
    conn = _conn()
    assert queue.enqueue(conn, "t", "k")
    conn.commit()
    job = queue.claim_next(conn, "t", "w1")
    assert job is not None and job.target_key == "k"
    assert conn.execute(
        "SELECT scheduled_at <= now() FROM jobs WHERE id = %s", (job.id,)
    ).fetchone() == (True,)


def test_enqueue_spread_seconds_is_future_and_deterministic() -> None:
    conn = _conn()
    # A batch enqueued at one instant is spread across the window, not all claimable at once.
    for i in range(30):
        queue.enqueue(conn, "menu", f"store-{i}", spread_seconds=3600)
    conn.commit()
    offsets = [
        off for (off,) in conn.execute(
            "SELECT extract(epoch FROM scheduled_at - created_at) FROM jobs"
        ).fetchall()
    ]
    assert all(0 <= off < 3600 for off in offsets)  # every job placed inside the 1h window
    assert max(offsets) > 0                          # genuinely spread into the future
    # Determinism: the SAME target hashes to the SAME offset (different task_type, same key).
    assert queue.enqueue(conn, "other", "store-7", spread_seconds=3600)
    conn.commit()
    pair = [
        off for (off,) in conn.execute(
            "SELECT extract(epoch FROM scheduled_at - created_at) FROM jobs "
            "WHERE target_key = 'store-7' ORDER BY task_type"
        ).fetchall()
    ]
    assert len(pair) == 2 and pair[0] == pair[1]


def test_requeue_stale_spreads_pending_retries() -> None:
    conn = _conn()
    for i in range(20):
        queue.enqueue(conn, "t", f"k{i}")
    conn.commit()
    for _ in range(20):
        queue.claim_next(conn, "t", "w1")
    conn.execute("UPDATE jobs SET claimed_at = now() - interval '2 hours'")
    assert queue.requeue_stale(conn, "t") == 20
    conn.commit()
    # Each retry is re-queued with a small (~0–30s) future scheduled_at, and the wave is spread
    # (not all one instant) so simultaneously-requeued jobs don't stampede the target.
    offsets = [
        off for (off,) in conn.execute(
            "SELECT extract(epoch FROM scheduled_at - now()) FROM jobs WHERE status = 'pending'"
        ).fetchall()
    ]
    assert len(offsets) == 20
    assert all(-1 <= off < 31 for off in offsets)
    assert max(offsets) > 0                                  # jitter pushed retries forward
    assert len({round(off) for off in offsets}) > 1          # spread across the window


def test_reap_expired_jitters_the_requeued_pending_attempt() -> None:
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    queue.claim_next(conn, "t", "w1")
    conn.execute("UPDATE jobs SET lease_until = now() - interval '1 minute'")
    assert queue.reap_expired(conn, "t") == 1
    conn.commit()
    assert conn.execute(
        "SELECT status, scheduled_at BETWEEN now() - interval '2 seconds' "
        "AND now() + interval '30 seconds' FROM jobs"
    ).fetchone() == ("pending", True)


def test_reap_expired_failed_branch_does_not_jitter_scheduled_at() -> None:
    conn = _conn()
    queue.enqueue(conn, "t", "k")
    conn.commit()
    # Drive attempts to just under the cap (each round recovered by the reaper).
    for _ in range(2):
        conn.execute("UPDATE jobs SET scheduled_at = now()")
        queue.claim_next(conn, "t", "w1")
        conn.execute("UPDATE jobs SET lease_until = now() - interval '1 minute'")
        queue.reap_expired(conn, "t")
        conn.commit()
    # Final claim → attempts hit the cap; pin a sentinel scheduled_at, then reap → FAILED. The
    # jitter must NOT touch scheduled_at on the →failed branch (a failed job is never re-claimed).
    conn.execute("UPDATE jobs SET scheduled_at = now()")
    queue.claim_next(conn, "t", "w1")
    conn.execute(
        "UPDATE jobs SET lease_until = now() - interval '1 minute', "
        "scheduled_at = timestamptz '2020-01-01 00:00:00+00'"
    )
    assert queue.reap_expired(conn, "t") == 1
    conn.commit()
    status, scheduled_at = conn.execute("SELECT status, scheduled_at FROM jobs").fetchone()
    assert status == "failed"
    assert scheduled_at == datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)  # untouched
