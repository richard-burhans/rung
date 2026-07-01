"""Cross-worker per-host token-bucket rate limiter over the ``token_buckets`` table.

Per-worker in-process limiters MULTIPLY (N workers × "1 rps" = N rps to one host); when many
workers share a small pool of egress IPs they need ONE authoritative limit per host. This is the
shared fallback from ``docs/distributed_scraping_design.md`` §4: a per-host token-bucket *row* in
Postgres, co-located with the jobs queue and proxy tables ("Postgres for everything"), updated in a
single atomic round-trip so there is no read-then-write race between workers.

A token bucket (not a fixed window) gives a smooth rate with a small burst and avoids the
window-reset spike. Postgres ``now()`` is the one authoritative clock, so no worker's wall clock
skew matters. This module is the pure public primitive (generic infra, like the queue); wiring it
onto a particular scrape path is the caller's job.
"""

from rung import db

# One atomic statement: refill the bucket from elapsed time then deduct `cost` iff enough remains.
# INSERT seeds a full-minus-cost bucket for a first-seen host (grants the first request, assuming
# cost <= burst). ON CONFLICT refills `tokens = LEAST(burst, tokens + rate * elapsed)` and, guarded
# by the WHERE, deducts `cost` — when the refilled level is below `cost` the DO UPDATE matches no row
# so RETURNING is empty (request denied) and the stored tokens/last_refill are left untouched.
_TRY_ACQUIRE = """
INSERT INTO token_buckets (host, tokens, last_refill)
VALUES (%(host)s, %(burst)s - %(cost)s, now())
ON CONFLICT (host) DO UPDATE SET
    tokens = LEAST(%(burst)s,
                   token_buckets.tokens
                     + %(rate)s * EXTRACT(EPOCH FROM (now() - token_buckets.last_refill)))
             - %(cost)s,
    last_refill = now()
WHERE LEAST(%(burst)s,
            token_buckets.tokens
              + %(rate)s * EXTRACT(EPOCH FROM (now() - token_buckets.last_refill))) >= %(cost)s
RETURNING tokens
"""


def try_acquire(
    conn: db.DBConn, host: str, *, rate_per_sec: float, burst: float, cost: float = 1.0
) -> bool:
    """Try to take ``cost`` tokens for ``host``; return whether they were granted. Caller commits.

    Refills the host's bucket to ``LEAST(burst, tokens + rate_per_sec * elapsed)`` (elapsed measured
    by Postgres ``now()``), then grants only if that leaves ``>= cost`` — all in one atomic
    ``INSERT … ON CONFLICT`` so two workers can't both overspend the same bucket. A first-seen host
    starts full at ``burst`` (so the first request is granted when ``cost <= burst``). A denied
    request does NOT consume tokens. Non-blocking: the caller decides whether to wait or shed load.
    """
    row = conn.execute(
        _TRY_ACQUIRE,
        {"host": host, "rate": rate_per_sec, "burst": burst, "cost": cost},
    ).fetchone()
    return row is not None
