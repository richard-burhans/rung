"""Transient work-queue over the jobs table (claims via FOR UPDATE SKIP LOCKED).

The queue is the per-run companion to the durable access_methods registry: registry
rows remember HOW to access a target; jobs rows arbitrate WHO works it this run.
Stages enqueue their own work and then claim it back ("self-feeding"), so two
concurrent runs of the same command partition the targets between them instead of
double-processing — see docs/stage_contracts.md §5 for the hazards this closes.

Lease/heartbeat/reaper (distributed workers, docs/distributed_scraping_design.md §4-5):
every claim stamps ``last_heartbeat`` and a ``lease_until`` window (``_LEASE_MINUTES``). A running
worker PROCESS keeps all of its in-flight leases fresh with :func:`heartbeat_forever` — a background
task that runs :func:`bump_worker_heartbeat` (one statement covering every claim the worker holds) on
its OWN dedicated connection, so the keep-alive never interleaves with a consumer's in-flight
``run_target`` transaction on the shared connection. (:func:`bump_heartbeat` is the finer per-job
variant.) :func:`reap_expired` re-queues any claim whose lease has passed (via a
``FOR UPDATE SKIP LOCKED`` subquery so concurrent reapers never fight over the same row) — closing the
dead-worker gap a bare SKIP-LOCKED queue leaves open; runners reap at startup and the ``reap-jobs`` CLI
is the standalone reaper. :func:`requeue_stale` remains the coarser claimed_at-age fallback.

Commit discipline: enqueue/complete/requeue_stale/reap_expired/bump_heartbeat/bump_worker_heartbeat
leave committing to the caller (so a job completion commits atomically with the work's data writes);
claim_next commits internally — a claim must be durable before work starts on it. ``heartbeat_forever``
commits on its own dedicated connection.
"""

import asyncio
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import LiteralString

from psycopg.types.json import Jsonb

from rung import db

# Default lease window a claim holds before the reaper may re-queue it (a long job bumps its
# heartbeat to extend). Minutes so the reaper's ``make_interval`` math stays in SQL.
_LEASE_MINUTES = 30

# Each claim stamps last_heartbeat + a lease_until window; the leading %s is the lease minutes so
# the SET clause's placeholders come before the WHERE predicate's.
_CLAIM = """
UPDATE jobs SET status = 'claimed', claimed_by = %s, claimed_at = now(),
                last_heartbeat = now(), lease_until = now() + make_interval(mins => %s),
                attempts = attempts + 1
WHERE id = (SELECT id FROM jobs
            WHERE task_type = %s AND status = 'pending' AND scheduled_at <= now()
            ORDER BY id LIMIT 1
            FOR UPDATE SKIP LOCKED)
RETURNING id, task_type, target_key, payload, attempts
"""

# Same as _CLAIM but scoped to a target_key prefix: a per-state run claims ONLY its state's jobs, so it
# never drains (and then fails as "no store row") another state's jobs that requeue_stale resurrected.
_CLAIM_SCOPED = """
UPDATE jobs SET status = 'claimed', claimed_by = %s, claimed_at = now(),
                last_heartbeat = now(), lease_until = now() + make_interval(mins => %s),
                attempts = attempts + 1
WHERE id = (SELECT id FROM jobs
            WHERE task_type = %s AND status = 'pending' AND scheduled_at <= now()
              AND target_key LIKE %s
            ORDER BY id LIMIT 1
            FOR UPDATE SKIP LOCKED)
RETURNING id, task_type, target_key, payload, attempts
"""

_ENQUEUE = """
INSERT INTO jobs (task_type, target_key, payload) VALUES (%s, %s, %s)
ON CONFLICT (task_type, target_key) WHERE status IN ('pending', 'claimed')
DO NOTHING
"""

# Opt-in spread variant: deterministically place scheduled_at across a window by hashing the
# target_key (Google-SRE distributed-cron), so a future scheduled/cron enqueue doesn't fire the
# whole batch at one instant. Same target always lands at the same offset. The default enqueue keeps
# scheduled_at at its now() column default (immediately claimable) — see enqueue's docstring.
_ENQUEUE_SPREAD = """
INSERT INTO jobs (task_type, target_key, payload, scheduled_at)
VALUES (%s, %s, %s, now() + make_interval(secs => abs(hashtext(%s)) %% %s))
ON CONFLICT (task_type, target_key) WHERE status IN ('pending', 'claimed')
DO NOTHING
"""

_CLAIM_TARGET = """
UPDATE jobs SET status = 'claimed', claimed_by = %s, claimed_at = now(),
                last_heartbeat = now(), lease_until = now() + make_interval(mins => %s),
                attempts = attempts + 1
WHERE id = (SELECT id FROM jobs
            WHERE task_type = %s AND target_key = %s
              AND status = 'pending' AND scheduled_at <= now()
            ORDER BY id LIMIT 1
            FOR UPDATE SKIP LOCKED)
RETURNING id, task_type, target_key, payload, attempts
"""

# On the →pending branch also spread the next attempt: a wave of jobs requeued at the same instant
# would otherwise all become claimable together and stampede the target. scheduled_at is hashed
# ~0–30s off the target_key (deterministic per target), so simultaneously-requeued jobs de-sync. The
# →failed branch leaves scheduled_at untouched (a failed job is never re-claimed).
_REQUEUE_STALE = """
UPDATE jobs
SET status       = CASE WHEN attempts < max_attempts THEN 'pending' ELSE 'failed' END,
    error        = CASE WHEN attempts < max_attempts THEN error ELSE 'claim timeout' END,
    claimed_by   = NULL,
    claimed_at   = NULL,
    scheduled_at = CASE WHEN attempts < max_attempts
                        THEN now() + make_interval(secs => abs(hashtext(target_key)) %% 30)
                        ELSE scheduled_at END
WHERE task_type = %s AND status = 'claimed'
  AND claimed_at < now() - make_interval(mins => %s)
"""

# Lease-aware reaper: re-queue (or fail at the attempt cap) any claim whose lease has expired —
# a crashed/hung worker that stopped bumping its heartbeat. The COALESCE falls back to claimed_at
# for a row claimed before the lease columns existed. The FOR UPDATE SKIP LOCKED subquery lets many
# reapers run concurrently without two of them re-queuing the same row.
_REAP_EXPIRED = """
UPDATE jobs
SET status       = CASE WHEN attempts < max_attempts THEN 'pending' ELSE 'failed' END,
    error        = CASE WHEN attempts < max_attempts THEN error ELSE 'lease expired' END,
    claimed_by   = NULL,
    claimed_at   = NULL,
    lease_until  = NULL,
    scheduled_at = CASE WHEN attempts < max_attempts
                        THEN now() + make_interval(secs => abs(hashtext(target_key)) %% 30)
                        ELSE scheduled_at END
WHERE id IN (
    SELECT id FROM jobs
    WHERE task_type = %s AND status = 'claimed'
      AND COALESCE(lease_until, claimed_at) < now()
    FOR UPDATE SKIP LOCKED
)
"""


@dataclass(frozen=True)
class Job:
    id: int
    task_type: str
    target_key: str
    payload: dict | None
    attempts: int


def worker_id() -> str:
    """Identify this process in jobs.claimed_by."""
    return f"{socket.gethostname()}:{os.getpid()}"


def enqueue(
    conn: db.DBConn, task_type: str, target_key: str, payload: dict | None = None,
    *, spread_seconds: int | None = None,
) -> bool:
    """Add a pending job unless a live (pending/claimed) one already exists.

    Returns True if a row was inserted. Caller must commit.

    ``spread_seconds`` is an OPT-IN thundering-herd guard for a future scheduled/cron enqueue
    that drains later: when set, the job's ``scheduled_at`` is placed deterministically within
    ``[now, now + spread_seconds)`` by hashing ``target_key`` (Google-SRE distributed-cron), so the
    same target always lands at the same offset and a batch enqueued at one instant spreads across
    the window instead of all becoming claimable at once. When ``None`` (the default, and what every
    existing self-feeding caller uses) ``scheduled_at`` keeps its ``now()`` column default, so the
    job is immediately claimable in the same run — behaviour is unchanged.
    """
    payload_json = Jsonb(payload) if payload is not None else None
    if spread_seconds is None:
        cur = conn.execute(_ENQUEUE, (task_type, target_key, payload_json))
    else:
        cur = conn.execute(
            _ENQUEUE_SPREAD, (task_type, target_key, payload_json, target_key, spread_seconds)
        )
    return cur.rowcount == 1


def _claim(conn: db.DBConn, sql: LiteralString, params: tuple) -> Job | None:
    """Run one claim statement and commit: the claim must be durable before work
    starts, and the SKIP LOCKED row lock must be released for other workers."""
    row = conn.execute(sql, params).fetchone()
    conn.commit()
    if row is None:
        return None
    job_id, job_type, target_key, payload, attempts = row
    return Job(job_id, job_type, target_key, payload, attempts)


def claim_next(
    conn: db.DBConn, task_type: str, worker: str, target_prefix: str | None = None,
    *, lease_minutes: int = _LEASE_MINUTES,
) -> Job | None:
    """Claim the oldest pending job of a type, or None when the queue is drained.

    With ``target_prefix`` (e.g. ``"NY:"``) claim only jobs whose ``target_key`` starts with it — so a
    per-state run drains ITS state's queue (still partitioning across workers via SKIP LOCKED) and never
    steals/fails another state's jobs that ``requeue_stale`` resurrected. The claim stamps a
    ``lease_minutes`` lease window (and the heartbeat) so the reaper can recover a dead worker's job.
    """
    if target_prefix is not None:
        return _claim(conn, _CLAIM_SCOPED, (worker, lease_minutes, task_type, target_prefix + "%"))
    return _claim(conn, _CLAIM, (worker, lease_minutes, task_type))


def claim_target(
    conn: db.DBConn, task_type: str, target_key: str, worker: str,
    *, lease_minutes: int = _LEASE_MINUTES,
) -> Job | None:
    """Claim one specific pending job (e.g. THIS state's dedupe), or None if it
    is absent or already claimed."""
    return _claim(conn, _CLAIM_TARGET, (worker, lease_minutes, task_type, target_key))


def make_claimer(
    conn: db.DBConn, task_type: str, worker: str, targeted_keys: list[str] | None,
    *, target_prefix: str | None = None, lease_minutes: int = _LEASE_MINUTES,
) -> Callable[[], Job | None]:
    """A no-arg claim closure shared by the Stage-2 and Stage-3 consumers: with ``targeted_keys``
    (a ``--only`` run) claim just those targets — so a concurrent full run's jobs are never stolen —
    else drain the shared queue, scoped to ``target_prefix`` when given (e.g. one state, so a full
    ``--state`` run doesn't claim+fail another state's jobs). Only this claim step is common across the
    stages; their ``_consume`` loops (stop-event, per-stage persist/complete handling) stay per-runner."""
    def _claim() -> Job | None:
        if targeted_keys is not None:
            while targeted_keys:
                job = claim_target(conn, task_type, targeted_keys.pop(), worker,
                                   lease_minutes=lease_minutes)
                if job is not None:
                    return job
            return None
        return claim_next(conn, task_type, worker, target_prefix, lease_minutes=lease_minutes)
    return _claim


def bump_heartbeat(
    conn: db.DBConn, job_id: int, worker: str, *, lease_minutes: int = _LEASE_MINUTES
) -> bool:
    """Extend a live claim's lease (a long-running worker's keep-alive). Caller commits.

    Advances ``last_heartbeat`` and pushes ``lease_until`` out ``lease_minutes`` — but only while
    ``worker`` still holds the claim (``claimed_by = worker AND status = 'claimed'``), so a worker
    whose job the reaper already recovered can't resurrect its own expired lease. Returns True when the
    holding worker's lease was extended (rowcount == 1).
    """
    cur = conn.execute(
        "UPDATE jobs SET last_heartbeat = now(), lease_until = now() + make_interval(mins => %s) "
        "WHERE id = %s AND claimed_by = %s AND status = 'claimed'",
        (lease_minutes, job_id, worker),
    )
    return cur.rowcount == 1


def bump_worker_heartbeat(
    conn: db.DBConn, worker: str, *, lease_minutes: int = _LEASE_MINUTES
) -> int:
    """Extend EVERY live claim this worker holds, in one statement. Caller commits.

    A running worker process keeps all of its in-flight jobs' leases fresh at once
    (``claimed_by = worker AND status = 'claimed'``), so the reaper won't reclaim a
    slow-but-alive worker's targets. One UPDATE covering the whole worker means the
    keep-alive is safe to run on a DEDICATED connection — it never interleaves with a
    per-job ``run_target`` transaction on the shared consumer connection. Returns the
    number of claims extended (rowcount).
    """
    cur = conn.execute(
        "UPDATE jobs SET last_heartbeat = now(), lease_until = now() + make_interval(mins => %s) "
        "WHERE claimed_by = %s AND status = 'claimed'",
        (lease_minutes, worker),
    )
    return cur.rowcount


async def heartbeat_forever(
    worker: str, *, interval_s: int = 600, lease_minutes: int = _LEASE_MINUTES,
    conn_factory: Callable[[], db.DBConn] = db.get_connection,
) -> None:
    """Keep a worker process's in-flight leases fresh until cancelled.

    Opens ONE dedicated connection (via ``conn_factory`` — injectable so a test can point
    it at a throwaway schema) and loops: bump every claim this ``worker`` holds, commit,
    sleep ``interval_s``. The dedicated connection is the whole point — a bump on the
    shared consumer connection would interleave with ``run_target``'s in-flight writes and
    corrupt its transaction. ``interval_s`` defaults to a third of the 30-minute lease so a
    single missed tick still leaves the lease live. Runners launch this as a task and cancel
    it in a ``finally``; on ``CancelledError`` it closes the connection and returns.
    """
    conn = conn_factory()
    try:
        while True:
            bump_worker_heartbeat(conn, worker, lease_minutes=lease_minutes)
            conn.commit()
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        return
    finally:
        conn.close()


def complete(
    conn: db.DBConn, job_id: int, status: str, *, worker: str, error: str | None = None
) -> bool:
    """Mark a job 'done'/'failed' — but ONLY while it is still claimed by ``worker``.

    A stale-claim requeue (``requeue_stale``) can hand a slow-but-alive worker's target to
    another worker; scoping the completion to the holding worker (``claimed_by = worker AND
    status = 'claimed'``) makes the original, now-orphaned worker's completion a no-op, so it can
    roll back its redundant write rather than clobber the reclaimer's result. Caller must commit
    (atomically with the work's own data writes). Returns True if this worker still held the claim.
    """
    cur = conn.execute(
        "UPDATE jobs SET status = %s, error = %s, finished_at = now() "
        "WHERE id = %s AND claimed_by = %s AND status = 'claimed'",
        (status, error, job_id, worker),
    )
    return cur.rowcount == 1


def requeue_stale(
    conn: db.DBConn, task_type: str, *, older_than_minutes: int = 60
) -> int:
    """Recover claims from crashed workers: claimed → pending (or → failed once
    attempts reach max_attempts). Call at consuming-command startup; caller commits.
    Returns the number of rows touched.
    """
    cur = conn.execute(_REQUEUE_STALE, (task_type, older_than_minutes))
    return cur.rowcount


def reap_expired(conn: db.DBConn, task_type: str) -> int:
    """Re-queue claims whose lease has expired — the lease-aware dead-worker recovery. Caller commits.

    A claim whose ``lease_until`` (falling back to ``claimed_at`` for pre-lease rows) is in the past
    has stopped heartbeating, so its worker is presumed dead: reset it to ``pending`` (or ``failed`` at
    ``max_attempts``) and clear the claim. Run periodically by any worker/reaper; a ``FOR UPDATE SKIP
    LOCKED`` subquery keeps concurrent reapers from re-queuing the same row twice. Returns rows touched.
    """
    cur = conn.execute(_REAP_EXPIRED, (task_type,))
    return cur.rowcount


def prune_completed(conn: db.DBConn, *, older_than_hours: int = 168) -> int:
    """Delete FINISHED (done/failed) jobs whose ``finished_at`` is older than the window.

    Stage 3 enqueues one ``store_menu`` job per store every day, so without pruning the table
    accumulates dead 'done' tuples that slow the ``FOR UPDATE SKIP LOCKED`` claim scans. Live
    (pending/claimed) jobs are never touched. Caller commits. Returns the rows deleted.
    """
    cur = conn.execute(
        "DELETE FROM jobs WHERE status IN ('done', 'failed') "
        "AND finished_at IS NOT NULL AND finished_at < now() - make_interval(hours => %s)",
        (older_than_hours,),
    )
    return cur.rowcount


def live_claim_holder(conn: db.DBConn, task_type: str, target_key: str) -> str | None:
    """Who currently holds a live claim on this target, or None."""
    row = conn.execute(
        "SELECT claimed_by FROM jobs "
        "WHERE task_type = %s AND target_key = %s AND status = 'claimed'",
        (task_type, target_key),
    ).fetchone()
    return row[0] if row else None
