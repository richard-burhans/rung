import datetime
import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, LiteralString

import psycopg
import psycopg.sql  # explicit: `import psycopg` alone does not guarantee the submodule

if TYPE_CHECKING:  # real signatures for the reference API that __getattr__ delegates (no runtime import,
    # so the engine stays cannabis-free); keeps LiteralString-typed constants like NATURAL_FLOWER_WHERE
    # inferring correctly at db.* call sites. Retire when callers re-point to rung.reference_db (B5).
    #
    # A NAME MISSING FROM THIS BLOCK IS NOT A COSMETIC MISS. It resolves through `__getattr__` to `Any`,
    # `ty` then swallows every use of it, and the LiteralString SQL-composition guarantee — the whole
    # reason this block exists — is silently OFF for exactly that name. The four guards added after the
    # db/reference_db split were all missing here, i.e. the guards that exist BECAUSE analyses were
    # retracted were the ones the type system had stopped checking.
    from rung.reference_db import (  # noqa: F401
        CA_PROVINCES_SUBQUERY,
        CURRENT_SNAPSHOT_WHERE,
        EFFECTIVE_VARIANT_PRICE,
        GEOCODED_TABLES,
        MEDICAL_ONLY_SUBQUERY,
        NATURAL_FLOWER_WHERE,
        NATURAL_FLOWER_WHERE_NORMALIZED,
        PLAUSIBLE_POTENCY_WHERE,
        TRUSTED_LINEAGE_WHERE,
        TRUSTED_POTENCY_WHERE,
        US_EXCL_TERRITORIES_SUBQUERY,
        US_JURISDICTIONS_SUBQUERY,
        US_TERRITORIES,
        append_store_observation,
        apply_geocode_cache,
        capture_attempts_for_state,
        clear_store_canonical_for_state,
        count_company_stores,
        count_store_products,
        create_reference_tables,
        create_store_pos_observations,
        create_tables,
        current_snapshot_where,
        delete_company_stores_for_company,
        delete_dispensaries_for_state,
        get_all_state_programs,
        get_companies_for_stage2,
        get_company_store_rows,
        get_company_stores_for_dedupe,
        get_geocode_cache,
        get_menu_stores_for_state,
        get_state_program,
        handles_with_snapshots,
        insert_company_store,
        insert_dispensary,
        insert_store_product,
        is_medical_only,
        latest_snapshot_times,
        latest_store_observations,
        medical_only_states,
        natural_flower_where,
        plausible_potency_expr,
        plausible_potency_where,
        put_geocode_cache,
        realign_store_products_company,
        record_capture_attempt,
        record_location_observations,
        record_observations,
        replace_company_stores,
        replace_lifecycle_events,
        replace_store_products,
        set_state_list,
        set_store_canonical,
        set_store_location,
        set_store_storefront,
        trusted_potency_where,
        upsert_recon,
        upsert_state_program,
        us_jurisdictions_where,
    )

type DBConn = psycopg.Connection

_DEFAULT_DATABASE_URL = "postgresql://rung:rung@localhost:5432/rung"

# `status` is the OUTCOME VOCABULARY, and the distinction it draws is the point. A ladder that cannot
# say "I can't get this" and "you may not have this" in different words will always say the latter,
# because it is the more comfortable sentence — and then a broken rung reads as a fact about the world.
# That has happened here three times (see docs/access_methods_design.md §"Outcomes"):
#
#   ok           the method returned plausible records
#   unavailable  the WORLD says no — this target has no data by this route, and never will
#   blocked      we were REFUSED — 403/429/captcha; the data exists, this egress cannot have it
#   broken       WE are wrong — a dead URL, a parse failure, missing config. Fix the rung.
#   failed       a rung returned nothing and did not say why. The honest default; never 'unavailable'.
#   never        no attempt recorded yet
#
# `broken` must be impossible to record as `unavailable`: only an explicit `access.Unavailable` signal
# from the runner produces it. Silence produces `failed`.
_CREATE_ACCESS_METHODS = """
CREATE TABLE IF NOT EXISTS access_methods (
    target_type  TEXT    NOT NULL,
    target_key   TEXT    NOT NULL,
    method       TEXT    NOT NULL,
    cost_rank    INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'never',
    resource_url TEXT,
    params       TEXT,
    record_count INTEGER,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_ok_at   TEXT,
    last_fail_at TEXT,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (target_type, target_key, method),
    CONSTRAINT access_methods_status_check
        CHECK (status IN ('ok', 'unavailable', 'blocked', 'broken', 'failed', 'never'))
)
"""

# The outcome vocabulary, as data. `access.py` owns the semantics; `db` owns the storage and refuses
# anything outside the set — a typo'd status is a silent lie about why a rung stopped working.
ACCESS_STATUSES = frozenset({"ok", "unavailable", "blocked", "broken", "failed", "never"})

# Transient per-run work items (claims close the documented concurrency hazards —
# see docs/stage_contracts.md §5). Companion to access_methods, which is the
# DURABLE per-target memory; jobs rows are run bookkeeping. timestamptz here
# (unlike the TEXT timestamps elsewhere) because the requeue-timeout math runs in SQL.
_CREATE_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    id           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    task_type    TEXT NOT NULL,
    target_key   TEXT NOT NULL,
    payload      JSONB,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    claimed_by   TEXT,
    claimed_at   TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    error        TEXT
)
"""

# Dedupes LIVE jobs per (task_type, target_key) while letting done/failed rows
# accumulate as run history. Also indexes the claim predicate.
_CREATE_JOBS_LIVE_UNIQUE = """
CREATE UNIQUE INDEX IF NOT EXISTS jobs_live_unique
    ON jobs (task_type, target_key) WHERE status IN ('pending', 'claimed')
"""

# Partial index over just the CLAIMABLE rows, matching the claim scan's shape
# (WHERE task_type=… AND status='pending' AND scheduled_at<=now() ORDER BY id LIMIT 1). As
# done/failed history accumulates between prunes the live-unique index above still spans every
# status; this one covers only pending rows so the FOR UPDATE SKIP LOCKED claim stays an
# index-only scan of the tiny working set. See docs/distributed_scraping_design.md §4-5.
_CREATE_JOBS_PENDING_CLAIM = """
CREATE INDEX IF NOT EXISTS jobs_pending_claim ON jobs (task_type, id) WHERE status = 'pending'
"""

# jobs columns added after the table's first release — applied in-place by _migrate_jobs so an
# existing database picks them up. lease/heartbeat back the distributed-worker lease + SKIP-LOCKED
# reaper (queue.reap_expired / bump_heartbeat); see docs/distributed_scraping_design.md §4-5. A
# claim stamps both; a long-running worker bumps last_heartbeat/lease_until; the reaper re-queues a
# claim whose lease_until has passed (a crashed/hung worker), so a dead worker no longer wedges a job.
_JOBS_ADDED_COLUMNS = {
    "lease_until": "TIMESTAMPTZ",
    "last_heartbeat": "TIMESTAMPTZ",
}

# Durable cross-worker proxy health, keyed per (proxy_url × host) — a proxy can be fine on Dutchie
# but banned on Weedmaps, so health is tracked per host. Public infra (the DDL); the selection/health
# POLICY (claim the healthiest, disable after N consecutive fails + cooldown) is private
# (rung_intel.proxy_store). See docs/distributed_scraping_design.md §2.1/§3.
_CREATE_PROXIES = """
CREATE TABLE IF NOT EXISTS proxies (
    proxy_url         TEXT    NOT NULL,
    host              TEXT    NOT NULL,
    success_count     INTEGER NOT NULL DEFAULT 0,
    error_count       INTEGER NOT NULL DEFAULT 0,
    consecutive_fails INTEGER NOT NULL DEFAULT 0,
    disabled_until    TIMESTAMPTZ,
    last_used_at      TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (proxy_url, host)
)
"""
# The claim scan picks the healthiest non-disabled proxy for a host (order by consecutive_fails,
# last_used_at). Partial over the not-disabled fast path (the common case) so the scan is index-only.
_CREATE_PROXIES_CLAIM_INDEX = """
CREATE INDEX IF NOT EXISTS proxies_host_health ON proxies (host, consecutive_fails, last_used_at)
    WHERE disabled_until IS NULL
"""

# Per-platform egress-tier registry. One row per menu/store platform recording which proxy tier
# (direct/datacenter/residential) it should scrape through, the escalation-gate block rate that
# decided it, and where the decision came from. Public infra (the DDL, like `proxies`); the
# selection POLICY (config default + this override) is private (rung_intel.proxy_tiers).
# See docs/distributed_scraping_design.md §0 (escalation gate) / §1 (tiers).
_CREATE_PROXY_TIERS = """
CREATE TABLE IF NOT EXISTS proxy_tiers (
    platform        TEXT PRIMARY KEY,
    tier            TEXT NOT NULL,
    block_rate_pct  DOUBLE PRECISION,
    source          TEXT,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Externally-sourced facts an analysis depends on but cannot derive from its own data — "brand X is
# owned by company Y", "state Z began retail sales in 2018-01". Such a fact is a *premise*, and a
# premise with no recorded source is unfalsifiable: a reader cannot check it without redoing the
# author's research.
#
# Each row is one subject–predicate–object triple carrying the evidence for itself: where it came
# from, the exact supporting text, and when it was retrieved. `retrieved_at` matters because sources
# change — a corporate brand list is true of a filing, not of the world forever.
#
# `confidence` is the attestor's own grading, not a probability: `verified` (a primary source states
# it), `reported` (a reputable secondary source), `inferred` (derived, and therefore attackable).
# Generic infra, like `jobs`: the engine stores provenance; a domain decides what facts need it.
@dataclass
class Attestation:
    """One externally-sourced fact, carrying the evidence for itself.

    A *premise* an analysis relies on but cannot derive from its own data — "brand X is owned by
    company Y". Recorded as a subject-predicate-object triple plus the source that supports it, so a
    reader can check the premise without repeating the author's research.

    Engine infrastructure, like ``jobs``: the vocabulary of subject types and predicates belongs to
    the domain, and the engine has no opinion about it. (Defined here rather than in ``models`` so
    importing the engine loads no domain module — see ``tests/test_import_layering.py``.)
    """

    subject_type: str                   # 'brand' | 'company' | 'state' | … (the domain's vocabulary)
    subject: str
    predicate: str                      # 'owned_by' | 'operates' | 'not_owned_by' | …
    object: str
    source_type: str                    # 'sec_filing' | 'press_release' | 'company_site' | 'trade_press'
    source_ref: str                     # a citable handle, e.g. "Acme FY2024 Form 10-K"
    retrieved_at: str                   # ISO-8601 date — sources change; a fact is true *of a source*
    source_url: str | None = None
    quote: str | None = None            # the exact supporting text, so the claim is checkable in place
    confidence: str = "reported"        # 'verified' (primary source) | 'reported' | 'inferred'
    notes: str | None = None


_CREATE_ATTESTATIONS = """
CREATE TABLE IF NOT EXISTS attestations (
    subject_type  TEXT NOT NULL,
    subject       TEXT NOT NULL,
    predicate     TEXT NOT NULL,
    object        TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    source_ref    TEXT NOT NULL,
    source_url    TEXT,
    quote         TEXT,
    confidence    TEXT NOT NULL DEFAULT 'reported',
    retrieved_at  DATE NOT NULL,
    notes         TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subject_type, subject, predicate, object, source_ref),
    CONSTRAINT attestations_confidence_check
        CHECK (confidence IN ('verified', 'reported', 'inferred'))
)
"""

_CREATE_ATTESTATIONS_LOOKUP = """
CREATE INDEX IF NOT EXISTS attestations_subject_idx
    ON attestations (subject_type, lower(subject), predicate)
"""

# Cross-worker per-host rate limiter (token bucket). One row per host: `tokens` is the current
# allowance, refilled at `rate_per_sec` up to `burst` on each access using `now() - last_refill` as
# the elapsed clock. Public infra (like `jobs`); the refill/deduct policy lives in `rate_limit.py`
# and the aggregator-path integration is private (overlay). See docs/distributed_scraping_design.md §3-4.
_CREATE_TOKEN_BUCKETS = """
CREATE TABLE IF NOT EXISTS token_buckets (
    host        TEXT PRIMARY KEY,
    tokens      DOUBLE PRECISION NOT NULL,
    last_refill TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Fleet/infra heartbeat — one row per scraping machine (the menu host, the aggregator VPS), upserted
# at the end of each of its sweeps. It is how the scrape-health dashboard shows the DISTRIBUTED side a
# store-level view can't: is a fleet member alive, on current code, and are its proxy exits healthy? A
# stalled VPS cron or a box left on stale code shows here as a heartbeat that stopped advancing. Public
# infra (like `token_buckets`); the emitter is `scripts/emit_heartbeat.py`. Keyed by (host, ROLE), so
# each JOB on a machine has its own current row (history is not the point — liveness is).
#
# ⚠ It was keyed by host alone until 2026-08-30, and that erased a whole job. The DigitalOcean box
# runs TWO crons — daily sweedpos and a MONTHLY aggregator pass — and the daily one's upsert
# overwrote the monthly one's row, `role` column included. So the aggregator job was unobservable
# from this table by construction: its liveness existed only in /root/menu-vps.log on the box. A
# fleet table keyed by machine cannot describe a fleet where a machine has more than one job.
_CREATE_INFRA_HEARTBEAT = """
CREATE TABLE IF NOT EXISTS infra_heartbeat (
    host             TEXT NOT NULL,
    role             TEXT NOT NULL,
    git_sha          TEXT,
    proxy_tier       TEXT,
    pool_total       INTEGER,
    pool_healthy     INTEGER,
    pool_quarantined INTEGER,
    last_run_at      TIMESTAMPTZ,
    last_status      TEXT,
    rows_written     INTEGER,
    note             TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (host, role)
)
"""


def record_heartbeat(
    conn: DBConn,
    host: str,
    role: str,
    *,
    git_sha: str | None = None,
    proxy_tier: str | None = None,
    pool_total: int | None = None,
    pool_healthy: int | None = None,
    pool_quarantined: int | None = None,
    last_run_at: str | None = None,
    last_status: str | None = None,
    rows_written: int | None = None,
    note: str | None = None,
) -> None:
    """Upsert one fleet member's heartbeat (one row per ``(host, role)``). Caller commits.

    Called at the end of a machine's sweep (`scripts/emit_heartbeat.py`); ``last_run_at`` defaults to
    ``now()`` when None so a bare heartbeat still stamps liveness."""
    conn.execute(
        "INSERT INTO infra_heartbeat "
        "(host, role, git_sha, proxy_tier, pool_total, pool_healthy, pool_quarantined, "
        " last_run_at, last_status, rows_written, note, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()), %s, %s, %s, now()) "
        "ON CONFLICT (host, role) DO UPDATE SET "
        "  git_sha = excluded.git_sha, proxy_tier = excluded.proxy_tier, "
        "  pool_total = excluded.pool_total, pool_healthy = excluded.pool_healthy, "
        "  pool_quarantined = excluded.pool_quarantined, last_run_at = excluded.last_run_at, "
        "  last_status = excluded.last_status, rows_written = excluded.rows_written, "
        "  note = excluded.note, updated_at = now()",
        (host, role, git_sha, proxy_tier, pool_total, pool_healthy, pool_quarantined,
         last_run_at, last_status, rows_written, note),
    )


HEARTBEAT_COLUMNS = (
    "host", "role", "git_sha", "proxy_tier", "pool_total", "pool_healthy", "pool_quarantined",
    "last_run_at", "last_status", "rows_written", "note", "updated_at",
)


def get_heartbeats(conn: DBConn) -> list[tuple]:
    """Every fleet member's current heartbeat, freshest first (columns = :data:`HEARTBEAT_COLUMNS`)."""
    return conn.execute(
        f"SELECT {', '.join(HEARTBEAT_COLUMNS)} FROM infra_heartbeat ORDER BY updated_at DESC"
    ).fetchall()

def without_parallel_workers(conn: DBConn) -> None:
    """Run this session's queries with no parallel workers. For WHOLE-CORPUS analytical scans.

    Postgres allocates a dynamic shared memory segment per parallel worker, and on Linux those live
    in ``/dev/shm`` — which Docker caps at **64 MB** by default and cannot be resized on a running
    container. A big scan over `product_observations` (42 GB) asks for more than that and dies with
    ``could not resize shared memory segment … No space left on device``, which reads like a full
    disk and is not one: measured 2026-09-15, the same database wrote a 100k-row temp table
    immediately afterwards.

    ⚠ **THIS COSTS ESSENTIALLY NOTHING ON THE QUERIES IT IS FOR, WHICH IS THE ONLY REASON TO DO IT
    RATHER THAN RAISE THE LIMIT.** Measured on a 7-day aggregate over `product_observations`, with
    the plans checked rather than assumed — the default plan really is `Gather Merge` over a
    `Parallel Seq Scan`, and the forced one really is serial:

        parallel (default 2)   18.0 s
        serial (0)             17.6 s

    A sequential scan of a table that size is bound by I/O, not by CPU, so the workers were buying
    ~2%. Do NOT reach for this on the pipeline's own queries, which are indexed, short, and where
    parallelism does pay.

    The real fix is ``--shm-size=1g`` when the Postgres container is next recreated; this is the
    part that can be done from inside a session, and it is per-session deliberately so nothing else
    in the fleet changes behaviour.
    """
    conn.execute("SET max_parallel_workers_per_gather = 0")


def get_connection(*, prefer_readonly: bool = False) -> DBConn:
    """Open and return a connection to the dispensaries data source.

    Live Postgres by default (``DATABASE_URL``, or the local dev container). When
    ``RUNG_DATA_SOURCE=static`` is set, returns a DuckDB-backed, psycopg-shaped connection over a frozen
    clean-dataset export at ``RUNG_STATIC_PATH`` instead — so the analysis scripts run unchanged off a
    portable file, with no database and no credentials (see ``rung.static_source``). This is the seam a
    Galaxy tool flips to reproduce an analysis from the published dataset.

    ``prefer_readonly=True`` consults ``DATABASE_URL_RO`` first (falling back to ``DATABASE_URL`` /
    the dev default): a read-only analysis script should hold a credential that CANNOT write, when
    one is configured. This flag is the one home for that preference — before it, each such script
    hand-rolled the ``DATABASE_URL_RO or DATABASE_URL`` resolution and thereby lost the static-source
    seam and the dev-container fallback every other caller gets.
    """
    from rung import (
        static_source,  # local import: keep the engine store free of the static dep
    )
    if static_source.is_static():
        return static_source.connect()  # ty: ignore[invalid-return-type]  (duck-typed psycopg surface)
    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    if prefer_readonly:
        url = os.environ.get("DATABASE_URL_RO") or url
    try:
        return psycopg.connect(url)
    except psycopg.OperationalError as exc:
        raise psycopg.OperationalError(
            f"{exc} — is the dev Postgres running?"
        ) from exc


class VerificationError(RuntimeError):
    """A migration's post-commit read on a FRESH connection disagreed with what it expected — the change
    did not land as reported. See :func:`apply_and_verify`."""


_UNSET: Any = object()


def apply_and_verify(
    conn: DBConn,
    apply: Callable[[DBConn], object],
    verify: Callable[[DBConn], object],
    *,
    expected: object = _UNSET,
    connect: Callable[[], DBConn] = get_connection,
) -> object:
    """Run a DML change, COMMIT it, and confirm it on a BRAND-NEW connection.

    ``apply(conn)`` performs the change; ``verify(fresh)`` reads the post-state on a fresh connection
    from ``connect()`` and returns a value; when ``expected`` is given it must equal that value, else
    :class:`VerificationError`. Returns the verified value. The fresh connection is closed here.

    **Verifying on a new connection is the load-bearing part, not a nicety.** ``get_connection()`` hands
    back an ``autocommit=False`` connection, so once any statement runs psycopg holds an implicit
    transaction and a ``with conn.transaction()`` block nests inside it as a mere SAVEPOINT — leaving that
    block releases the savepoint but commits NOTHING, and closing the connection rolls it all back. A
    verify query in the SAME session cannot catch this: it reads its own UNCOMMITTED work. That is
    incident 5 — ``scripts/refold_companies.py`` reported "APPLIED: 9 companies merged", a same-session
    count confirmed it, and a fresh connection still saw all 15. So the explicit ``conn.commit()`` below
    is the fix, and reading the result on a new connection is the proof that it landed. Any migration that
    reports success should confirm it through this, not through the applying session's own rowcounts.
    """
    apply(conn)
    conn.commit()
    fresh = connect()
    try:
        got = verify(fresh)
    finally:
        fresh.close()
    if expected is not _UNSET and got != expected:
        raise VerificationError(
            f"post-commit verify read {got!r} on a fresh connection, expected {expected!r}: the change "
            "did not land — a `with conn.transaction()` savepoint that never committed? (incident 5)"
        )
    return got


def one(conn: DBConn, query: LiteralString, params: tuple = ()) -> tuple:
    """Run a query expected to return exactly one row and return it (never None).

    For aggregate / ``RETURNING`` / single-row SELECTs where ``fetchone()`` is logically non-None.
    Centralizes the ``Row | None`` narrowing so callers (esp. analysis scripts) can subscript the
    result directly without a per-call ``assert``. Raises if the query yields no row.
    """
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise LookupError("query returned no row")
    return row


def create_engine_tables(conn: DBConn) -> None:
    """Create the **generic engine** tables only — the domain-neutral infrastructure any pipeline
    needs: the ``jobs`` work queue, the ``access_methods`` registry, the ``token_buckets`` rate
    limiter, the ``proxies``/``proxy_tiers`` egress pool, and the ``infra_heartbeat`` fleet liveness
    table. Creates **no** domain tables.

    This is the table-creation call for a *build-your-own-domain* plugin (see
    ``docs/build-your-own-domain.md``): call it, then create your own record tables with your own DDL.
    Idempotent (``IF NOT EXISTS``); commits.
    """
    conn.execute(_CREATE_ACCESS_METHODS)
    conn.execute(_CREATE_JOBS)
    ensure_index(conn, _CREATE_JOBS_LIVE_UNIQUE)
    ensure_index(conn, _CREATE_JOBS_PENDING_CLAIM)
    _migrate_jobs(conn)
    _migrate_access_methods(conn)
    conn.execute(_CREATE_TOKEN_BUCKETS)
    conn.execute(_CREATE_INFRA_HEARTBEAT)
    _migrate_infra_heartbeat(conn)
    conn.execute(_CREATE_PROXIES)
    ensure_index(conn, _CREATE_PROXIES_CLAIM_INDEX)
    conn.execute(_CREATE_PROXY_TIERS)
    conn.execute(_CREATE_ATTESTATIONS)
    ensure_index(conn, _CREATE_ATTESTATIONS_LOOKUP)
    conn.commit()


# --- start-up DDL that asks for no lock when there is nothing to do ---------------------------------
#
# ⚠ `IF NOT EXISTS` IS NOT "NO LOCK". Postgres takes the statement's lock FIRST and looks second:
# `ALTER TABLE … ADD COLUMN IF NOT EXISTS` waits for ACCESS EXCLUSIVE on a table whose column is
# already there, `CREATE INDEX IF NOT EXISTS` for SHARE on a table whose index exists, and
# `CREATE OR REPLACE VIEW` for ACCESS EXCLUSIVE on a view it will not change. Every command runs
# this suite at start-up, in ONE transaction, so each process start queued for the strongest lock
# on every table behind whatever a live worker held, while holding the ones it had already been
# granted — a lock-order inversion against any writer mid-transaction. Measured 2026-10-04 on a
# two-box fleet sharing one database: 36 commands deadlocked at start-up, seven states skipped
# outright by one monthly run, and two workers' stores rolled back mid-run.
#
# So each helper below READS THE CATALOG and issues DDL only when the object is actually missing —
# the rule `_migrate_infra_heartbeat` already followed. A catalog read takes no lock on the table.
# `tests/test_db.py` holds a writer on every table and a reader on every view and requires the
# whole suite to pass through; a new statement that locks unconditionally fails it.

_INDEX_NAME_RE = re.compile(r"\bINDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.IGNORECASE)
_VIEW_NAME_RE = re.compile(r"\bCREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)", re.IGNORECASE)
_QUOTED_RE = re.compile(r"'([^']*)'")


def _relation_exists(conn: DBConn, name: str) -> bool:
    """Whether ``name`` resolves on the caller's ``search_path`` (so a test schema sees its own)."""
    row = conn.execute("SELECT to_regclass(%s::text) IS NOT NULL", (name,)).fetchone()
    return row is not None and bool(row[0])


def table_columns(conn: DBConn, table: str) -> set[str]:
    """The live column names of ``table``; empty when the table does not exist."""
    rows = conn.execute(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = to_regclass(%s::text) AND attnum > 0 AND NOT attisdropped",
        (table,),
    ).fetchall()
    return {row[0] for row in rows}


def add_missing_columns(conn: DBConn, table: LiteralString, columns: dict[str, str]) -> None:
    """``ALTER TABLE … ADD COLUMN`` for each of ``columns`` the table lacks — and nothing otherwise."""
    have = table_columns(conn, table)
    for column, col_type in columns.items():
        if column in have:
            continue
        # DDL with a hardcoded table and a column/type from an all-literal module constant — not a
        # LiteralString to the stub, but no caller input ever reaches it.
        conn.execute(  # ty: ignore[no-matching-overload]
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
        )


def ensure_index(conn: DBConn, ddl: LiteralString) -> None:
    """Run a ``CREATE [UNIQUE] INDEX IF NOT EXISTS <name> …`` only when ``<name>`` is absent."""
    named = _INDEX_NAME_RE.search(ddl)
    if named is None:
        raise ValueError(f"not a CREATE INDEX IF NOT EXISTS statement: {ddl.strip()[:60]!r}")
    if not _relation_exists(conn, named.group(1)):
        conn.execute(ddl)


def ensure_view(conn: DBConn, ddl: LiteralString) -> None:
    """Run a ``CREATE OR REPLACE VIEW <name> …`` only when the view is absent or its DDL changed.

    A view's stored definition is Postgres's re-rendering of the query, not the text that made it,
    so the two cannot be compared. The view's COMMENT therefore carries a digest of the DDL that
    last created it: it lives on the object and travels with a dump, and a view dropped and
    recreated by hand loses it and is rebuilt. (One replaced IN PLACE by hand keeps the stamp and
    is not noticed — the digest witnesses this module's DDL, not the catalog's.)
    """
    named = _VIEW_NAME_RE.search(ddl)
    if named is None:
        raise ValueError(f"not a CREATE OR REPLACE VIEW statement: {ddl.strip()[:60]!r}")
    name = named.group(1)
    stamp = "ddl sha256:" + hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    current = conn.execute(
        "SELECT obj_description(to_regclass(%s::text), 'pg_class')", (name,)
    ).fetchone()
    if current is not None and current[0] == stamp:
        return
    conn.execute(ddl)
    conn.execute(
        psycopg.sql.SQL("COMMENT ON VIEW {} IS {}").format(
            psycopg.sql.Identifier(name), psycopg.sql.Literal(stamp)
        )
    )


def constraint_definition(conn: DBConn, table: str, name: str) -> str | None:
    """Postgres's rendering of constraint ``name`` on ``table``, or None when it is not there."""
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = to_regclass(%s::text) AND conname = %s",
        (table, name),
    ).fetchone()
    return row[0] if row is not None else None


def _migrate_access_methods(conn: DBConn) -> None:
    """Add the status CHECK to a database created before the outcome vocabulary existed.

    Safe on live data: production held only 'ok' and 'failed', both in the vocabulary. Postgres
    validates existing rows when the constraint is added, so a database carrying some other status
    fails loudly here rather than silently accepting it forever.
    """
    current = constraint_definition(conn, "access_methods", "access_methods_status_check")
    if current is not None and set(_QUOTED_RE.findall(current)) == ACCESS_STATUSES:
        return  # already the current vocabulary: no ALTER, so no ACCESS EXCLUSIVE and no table scan
    conn.execute(
        "ALTER TABLE access_methods DROP CONSTRAINT IF EXISTS access_methods_status_check"
    )
    conn.execute(
        "ALTER TABLE access_methods ADD CONSTRAINT access_methods_status_check "
        "CHECK (status IN ('ok', 'unavailable', 'blocked', 'broken', 'failed', 'never'))"
    )


def _migrate_infra_heartbeat(conn: DBConn) -> None:
    """Repoint a pre-2026-08-30 database's primary key from ``(host)`` to ``(host, role)``.

    The old key allowed a machine exactly one row, so a box running two jobs had the second one's
    heartbeat silently overwrite the first — including the ``role`` column, which made the loss
    invisible rather than merely lossy. See the comment on :data:`_CREATE_INFRA_HEARTBEAT`.

    Guarded on the CURRENT key width, not on a version marker, so it is a catalog read on every call
    but an ``ALTER TABLE`` (which takes ACCESS EXCLUSIVE) exactly once. ``to_regclass`` resolves
    through ``search_path``, so this migrates the schema the caller is actually using — the test
    suite's throwaway schemas included — rather than whichever ``infra_heartbeat`` it finds first.

    No row can be lost: ``(host)`` was UNIQUE, so ``(host, role)`` is unique over the same rows.
    """
    row = conn.execute(
        "SELECT i.indnatts FROM pg_index i "
        "WHERE i.indrelid = to_regclass('infra_heartbeat') AND i.indisprimary"
    ).fetchone()
    if row is None or row[0] == 2:   # no table/PK yet (fresh CREATE already has it), or already composite
        return
    name = conn.execute(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = to_regclass('infra_heartbeat') AND contype = 'p'"
    ).fetchone()
    if name is None:
        return
    # The constraint name comes from the catalog, not from caller input; quote it as an identifier
    # anyway so a non-default name (a restored dump can carry one) cannot break the statement.
    conn.execute(
        psycopg.sql.SQL("ALTER TABLE infra_heartbeat DROP CONSTRAINT {}").format(
            psycopg.sql.Identifier(name[0])
        )
    )
    conn.execute("ALTER TABLE infra_heartbeat ADD PRIMARY KEY (host, role)")


def _migrate_jobs(conn: DBConn) -> None:
    """Add any jobs columns missing from an older database (the lease/heartbeat columns)."""
    add_missing_columns(conn, "jobs", _JOBS_ADDED_COLUMNS)


_UPSERT_ACCESS_METHOD = """
INSERT INTO access_methods
  (target_type, target_key, method, cost_rank, status, resource_url, params,
   record_count, error, attempts, last_ok_at, last_fail_at, updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s)
ON CONFLICT (target_type, target_key, method) DO UPDATE SET
  cost_rank    = excluded.cost_rank,
  status       = excluded.status,
  -- On success keep/refresh the winning locator; on FAILURE clear it, so the next
  -- attempt re-discovers instead of re-serving a now-broken URL (the stale-hint lock).
  resource_url = CASE WHEN excluded.status = 'ok'
                      THEN COALESCE(excluded.resource_url, access_methods.resource_url)
                      ELSE excluded.resource_url END,
  params       = CASE WHEN excluded.status = 'ok'
                      THEN COALESCE(excluded.params, access_methods.params)
                      ELSE excluded.params END,
  record_count = excluded.record_count,
  error        = excluded.error,
  attempts     = access_methods.attempts + 1,
  last_ok_at   = CASE WHEN excluded.status = 'ok'
                      THEN excluded.updated_at ELSE access_methods.last_ok_at END,
  -- Every non-ok, non-never outcome is a failure timestamp — 'blocked'/'broken'/'unavailable'
  -- must advance last_fail_at too (mirrors record_access_attempt's fail_at set), or an ok row that
  -- later goes Cloudflare-dark never gets last_fail_at > last_ok_at and the silent-failure signal
  -- never fires.
  last_fail_at = CASE WHEN excluded.status NOT IN ('ok', 'never')
                      THEN excluded.updated_at ELSE access_methods.last_fail_at END,
  updated_at   = excluded.updated_at
"""

# Columns returned by the access_methods getters, in order.
ACCESS_METHOD_COLUMNS = (
    "method", "cost_rank", "status", "resource_url", "params",
    "record_count", "error", "attempts", "last_ok_at", "last_fail_at",
)


def record_access_attempt(
    conn: DBConn,
    target_type: str,
    target_key: str,
    method: str,
    cost_rank: int,
    status: str,
    resource_url: str | None = None,
    params: str | None = None,
    record_count: int | None = None,
    error: str | None = None,
) -> None:
    """Upsert the outcome of one method attempt against one target. Caller commits.

    ``status`` must be one of ``ACCESS_STATUSES``. On 'ok' the last_ok_at timestamp advances; on any
    other outcome, last_fail_at. attempts increments on every call. On 'ok' a null resource_url /
    params preserves the stored value; on any OTHER outcome the stored values are REPLACED with what
    was passed — null included — so the next attempt re-discovers instead of re-serving a locator
    that just failed (the stale-hint lock; see ``_UPSERT_ACCESS_METHOD``).
    """
    if status not in ACCESS_STATUSES:
        raise ValueError(f"unknown access status {status!r}; expected one of {sorted(ACCESS_STATUSES)}")
    now = datetime.datetime.now(datetime.UTC).isoformat()
    ok_at = now if status == "ok" else None
    # Every outcome that is not a success advances last_fail_at — 'broken' and 'blocked' are failures
    # too, and the staleness governor reads these timestamps. (Before the vocabulary existed this
    # tested `== "failed"`, which would have left the new statuses without a failure time.)
    fail_at = None if status in ("ok", "never") else now
    conn.execute(
        _UPSERT_ACCESS_METHOD,
        (
            target_type, target_key, method, cost_rank, status, resource_url,
            params, record_count, error, ok_at, fail_at, now,
        ),
    )


def get_access_methods(conn: DBConn, target_type: str, target_key: str) -> list[tuple]:
    """Return all recorded method rows for a target (see ACCESS_METHOD_COLUMNS)."""
    return conn.execute(
        "SELECT method, cost_rank, status, resource_url, params, record_count, "
        "error, attempts, last_ok_at, last_fail_at FROM access_methods "
        "WHERE target_type = %s AND target_key = %s ORDER BY cost_rank",
        (target_type, target_key),
    ).fetchall()


def access_health(conn: DBConn, target_type: str | None = None) -> list[tuple[str, str, int]]:
    """Attempt counts per (method, status) — the query a canary runs, and the reason the vocabulary exists.

    A method whose rows are all ``ok`` or ``unavailable`` is healthy: it works, or the world has
    nothing for it. A method accumulating ``broken`` is asking to be repaired, and ``blocked`` says the
    egress is wrong, not the code. Before the vocabulary, all three read ``failed`` and no query could
    tell them apart — so a rung that died because an endpoint moved stayed green for weeks.

    Ordered worst-first: broken, then blocked, then the rest.
    """
    sql = (
        "SELECT method, status, count(*) FROM access_methods "
        + ("WHERE target_type = %s " if target_type else "")
        + "GROUP BY method, status "
        "ORDER BY CASE status WHEN 'broken' THEN 0 WHEN 'blocked' THEN 1 WHEN 'failed' THEN 2 "
        "ELSE 3 END, count(*) DESC, method"
    )
    params = (target_type,) if target_type else ()
    return [(row[0], row[1], row[2]) for row in conn.execute(sql, params).fetchall()]


def get_access_winner(
    conn: DBConn, target_type: str, target_key: str
) -> tuple | None:
    """Return the cheapest 'ok' method row for a target, or None (see columns)."""
    return conn.execute(
        "SELECT method, cost_rank, status, resource_url, params, record_count, "
        "error, attempts, last_ok_at, last_fail_at FROM access_methods "
        "WHERE target_type = %s AND target_key = %s AND status = 'ok' "
        "ORDER BY cost_rank LIMIT 1",
        (target_type, target_key),
    ).fetchone()




_UPSERT_ATTESTATION = """
INSERT INTO attestations (subject_type, subject, predicate, object, source_type, source_ref,
                          source_url, quote, confidence, retrieved_at, notes)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (subject_type, subject, predicate, object, source_ref) DO UPDATE SET
    source_type = EXCLUDED.source_type,
    source_url  = EXCLUDED.source_url,
    quote       = EXCLUDED.quote,
    confidence  = EXCLUDED.confidence,
    retrieved_at = EXCLUDED.retrieved_at,
    notes       = EXCLUDED.notes,
    updated_at  = now()
"""


def upsert_attestation(conn: DBConn, record: Attestation) -> None:
    """Record one externally-sourced fact and the evidence for it. Caller must commit.

    Keyed on the triple *plus* ``source_ref``: two sources may attest the same fact, and a reader is
    better served by both than by whichever was written last.
    """
    conn.execute(
        _UPSERT_ATTESTATION,
        (
            record.subject_type,
            record.subject,
            record.predicate,
            record.object,
            record.source_type,
            record.source_ref,
            record.source_url,
            record.quote,
            record.confidence,
            record.retrieved_at,
            record.notes,
        ),
    )


def attestations_for(
    conn: DBConn, subject_type: str, subject: str, predicate: str | None = None
) -> list[Attestation]:
    """Every recorded fact about a subject, most recently retrieved first. Case-insensitive subject.

    Returns the *evidence*, not a verdict: a caller that finds two rows disagreeing has learned
    something true about the world, and should not silently take one.
    """
    sql = (
        "SELECT subject_type, subject, predicate, object, source_type, source_ref, retrieved_at, "
        "source_url, quote, confidence, notes FROM attestations "
        "WHERE subject_type = %s AND lower(subject) = lower(%s)"
    )
    params: list[object] = [subject_type, subject]
    if predicate is not None:
        sql += " AND predicate = %s"
        params.append(predicate)
    sql += " ORDER BY retrieved_at DESC, source_ref"
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        Attestation(
            subject_type=r[0], subject=r[1], predicate=r[2], object=r[3], source_type=r[4],
            source_ref=r[5], retrieved_at=str(r[6]), source_url=r[7], quote=r[8],
            confidence=r[9], notes=r[10],
        )
        for r in rows
    ]


def __getattr__(name: str) -> Any:
    """Back-compat delegation: the cannabis reference schema + CRUD moved to ``rung.reference_db``
    (genericization B1/B3). ``from rung import db; db.insert_dispensary(...)`` still resolves — routed
    here lazily so importing the *engine* ``db`` (as ``queue``/``access`` do) loads no cannabis
    ``models``/``text``. Returns ``Any`` (PEP 562 idiom); re-point call sites to ``rung.reference_db``
    for full typing (B5). Prefer importing ``rung.reference_db`` directly in new code."""
    import rung.reference_db as _ref
    try:
        return getattr(_ref, name)
    except AttributeError:
        raise AttributeError(f"module 'rung.db' has no attribute {name!r}") from None
