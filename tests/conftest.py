"""Shared test DB plumbing: every pg_conn() call returns a connection scoped to a
fresh throwaway schema in the rung_test database, so tests get the same
isolation the old in-memory SQLite connections gave.

Three layers of cleanup, because the suite runs in parallel (`-n 8`) and a session-scoped fixture
therefore runs once per WORKER, not once per run:

1. after each test, `_drop_test_schemas` drops what that test was handed. This is the one that should
   do all the work, and does — but only for connections registered in THIS module's `_open_conns`.
   `tests/test_conftest_is_one_module.py` is what keeps that "this module" singular: seven test files
   once imported `pg_conn` as `tests.conftest.pg_conn`, which is a second, independent copy of this
   file, and every schema handed out through it leaked because the fixture drained the other copy's
   list. ~26 per run, invisible for as long as layer 3 dropped everything unconditionally;
2. at session end, `_drop_this_workers_schemas` reclaims what layer 1 could not — layer 1 drops through
   the connection that made the schema, so it gives up whenever that connection is unusable (a test
   closed it, or left it in a failed transaction). A safety net, and expected to find nothing;
3. at session start, `_sweep_stale_schemas` clears a *previous* crashed run's leftovers — and only
   those. Schemas are named `test_<run>_<worker>_<uuid>` and stamped with a creation `COMMENT` so this
   sweep can tell a sibling worker's live schema from real debris. Dropping every `test_%` schema, as
   it used to, destroys work a concurrent worker is mid-test on."""

import contextlib
import datetime as dt
import os
import uuid
from pathlib import Path

import psycopg
import pytest

from rung import db

#: What to DO about an unreachable test Postgres — DERIVED, because this string ships.
#:
#: It used to name a helper script only the upstream monorepo has, so the first error a contributor
#: met after following CONTRIBUTING.md sent them to a file their checkout does not contain. Asking
#: the filesystem keeps the better instruction where the helper exists and gives every other
#: checkout the route it actually has.
#:
#: ⚠ The path is COMPUTED rather than written out, and that is not style: a literal would sit in
#: published code even where its branch can never run, which no reader — and no scanner — can tell
#: apart from the bug this replaced. This comment names no path for the same reason.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEV_PG = _REPO_ROOT / "scripts" / "dev_pg.sh"
_PG_HINT = (f"run {_DEV_PG.relative_to(_REPO_ROOT)} first" if _DEV_PG.is_file()
            else "start a local Postgres first — CONTRIBUTING.md has a one-line docker command")

_TEST_URL = os.environ.get(
    "DATABASE_URL_TEST",
    "postgresql://rung:rung@localhost:5432/rung_test",
)
_open_conns: list[db.DBConn] = []

# Every schema this RUN creates carries one prefix, so the startup sweep can tell its own live schemas
# from another run's debris. Under xdist the sweep is session-scoped and therefore runs once PER WORKER,
# while `pg_conn` COMMITS its `CREATE SCHEMA` — so nothing holds a lock, and a bare "drop every test_%"
# sweep in worker gw3 would drop the schema gw0 is mid-test on. That is guaranteed corruption, not a
# race window. `PYTEST_XDIST_TESTRUNUID` is identical across all workers of one run and absent when
# running serially, which is exactly the grouping needed.
_RUN = os.environ.get("PYTEST_XDIST_TESTRUNUID", uuid.uuid4().hex)[:8]
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
#: Every schema of this RUN — what the startup sweep must not touch, because a sibling worker may be
#: mid-test on any of them.
_RUN_PREFIX = f"test_{_RUN}_"
#: Every schema of THIS WORKER — what the end-of-session teardown may drop, because by then this worker
#: is done with all of them and no other worker can own one.
_PREFIX = f"{_RUN_PREFIX}{_WORKER}_"
#: Debris younger than this is assumed to belong to a concurrently-running pytest (a second terminal, an
#: editor's runner) and is left alone; older debris is a crashed run's and gets reclaimed.
#:
#: 10 minutes is a bound, not a guess. A schema is stamped when its test creates it and dropped when that
#: test ends, so the only way a LIVE schema ages past this is a single test running that long — and
#: `timeout = 300` in pyproject caps a test at 5 minutes. Double it for margin and the window cannot
#: swallow a schema still in use.
_STALE_AFTER = dt.timedelta(minutes=10)


@pytest.fixture(autouse=True, scope="session")
def _no_inherited_git_repo_env() -> None:
    """Strip git's repo-location env vars, so fixture repos can never be THIS repository.

    ⚠ PORTED FROM THE LIBRARY, WHERE THE GUARD WAS PAID FOR (2026-08-16, its fb78ea3). When a
    `pre-push` hook fires from a git WORKTREE, git runs it with an ABSOLUTE `GIT_DIR`
    (`.git/worktrees/<name>`) and `subprocess` inherits it — so every fixture here that builds a
    throwaway repo (`git init` in a tmp_path: the synthetic libraries in `test_handoff`, the
    squash-merge repos, the marker worktree in `test_open_pr`) would silently operate on the REAL
    repository. In the library one such gate run committed fixtures onto `main`, flipped
    `core.bare`, and overwrote the shared identity. Session-scoped and unconditional: a test that
    needs a repo builds one; none legitimately wants the caller's.
    """
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_COMMON_DIR"):
        os.environ.pop(var, None)


def pg_conn() -> db.DBConn:
    """A connection whose search_path is a fresh test_<run>_<uuid> schema."""
    try:
        conn = psycopg.connect(_TEST_URL)
    except psycopg.OperationalError:
        pytest.fail(f"test Postgres unreachable — {_PG_HINT}", pytrace=False)
    schema = f"{_PREFIX}{uuid.uuid4().hex}"
    conn.execute(f"CREATE SCHEMA {schema}")
    # pg_namespace carries no creation time, and the sweep needs one to tell live debris from dead.
    # A comment is the only per-schema slot Postgres offers, and it survives the connection that set it.
    conn.execute(f"COMMENT ON SCHEMA {schema} IS '{dt.datetime.now(dt.UTC).isoformat()}'")
    conn.execute(f"SET search_path TO {schema}")
    conn.commit()
    _open_conns.append(conn)
    return conn


def pg_conn_sharing(conn: db.DBConn) -> db.DBConn:
    """A second connection into conn's schema — for multi-worker claim tests."""
    row = conn.execute("SELECT current_schema()").fetchone()
    assert row is not None
    other = psycopg.connect(_TEST_URL)
    other.execute(f"SET search_path TO {row[0]}")
    other.commit()
    _open_conns.append(other)
    return other


@pytest.fixture(autouse=True)
def _drop_test_schemas():
    """Drop every schema handed out during the test, even on failure.

    Roll back ALL connections BEFORE dropping any schema. A two-connection test
    (pg_conn_sharing) otherwise deadlocks teardown: dropping the schema via one
    connection needs an ACCESS EXCLUSIVE lock, which a read/write lock the other
    connection still holds (an uncommitted SELECT/INSERT) would block forever. A
    lock_timeout is a backstop so a stray lock can never hang teardown outright.
    """
    yield
    for conn in _open_conns:  # release every lingering lock first, so no DROP blocks on a sibling
        with contextlib.suppress(psycopg.Error):
            conn.rollback()
    while _open_conns:
        conn = _open_conns.pop()
        try:
            row = conn.execute("SELECT current_schema()").fetchone()
            if row and row[0] and row[0].startswith("test_"):
                conn.execute("SET lock_timeout = '15s'")
                # IF EXISTS: a sharing connection points at a schema an earlier-popped
                # connection may have dropped already.
                conn.execute(f"DROP SCHEMA IF EXISTS {row[0]} CASCADE")
                conn.commit()
        except psycopg.Error:
            # The recovery must not itself raise. If the test closed the connection, `rollback()` raises
            # InterfaceError — from inside the handler, so it escapes and turns a PASSING test into a
            # teardown ERROR, reported against a test that did nothing wrong. The schema it could not
            # drop is reclaimed by `_drop_this_workers_schemas` at end of session.
            with contextlib.suppress(psycopg.Error):
                conn.rollback()
        finally:
            with contextlib.suppress(psycopg.Error):
                conn.close()


# The cross-worker rate gate is ON by default in production, and it opens its OWN connection from
# DATABASE_URL rather than the schema-scoped one a test hands the runner. Left enabled, a test that
# calls run_store_menus/run_company_stores would reach past its throwaway schema and spend tokens
# in whatever database DATABASE_URL points at — the dev container, or worse. A unit test is not a
# fleet; the gate has its own coverage in test_rate_gate.py, and tests/test_host_limits.py asserts
# that production leaves it on.
os.environ.setdefault("RUNG_HOST_RATE_LIMIT", "0")


@pytest.fixture(scope="session", autouse=True)
def _sweep_stale_schemas():
    """Clear test_* schemas left behind by a previous crashed run — and ONLY those.

    Two hazards, both of which have to be handled here:

    * A killed run can leave a backend still holding a lock on its throwaway schema; a bare
      ``DROP SCHEMA`` would then block the WHOLE session indefinitely. Cap the wait with
      ``lock_timeout`` and skip any schema still locked (harmless debris — a later clean run
      reclaims it) so one stuck lock can never wedge the suite.
    * This fixture is session-scoped, so under xdist it runs once per WORKER. Dropping every
      ``test_%`` schema would therefore destroy schemas that sibling workers — or a second pytest
      in another terminal — are actively using, and nothing would block it because `pg_conn`
      commits the CREATE. So skip our own run's prefix, and skip anything stamped recently enough
      to plausibly belong to someone still running.
    """
    try:
        conn = psycopg.connect(_TEST_URL)
    except psycopg.OperationalError:
        # `pytest.fail`, not `pytest.exit`. Raising SystemExit from a session fixture inside an xdist
        # worker trips `dsession.worker_workerfinished`'s `assert not crashitem` and the whole run dies
        # with an INTERNALERROR traceback and "no tests ran" — measured, not assumed. A failing autouse
        # fixture is noisier (one error per test) but it names the actual problem.
        pytest.fail(f"test Postgres unreachable — {_PG_HINT}", pytrace=False)
    conn.execute("SET lock_timeout = '10s'")
    conn.commit()
    stale = conn.execute(
        "SELECT nspname, obj_description(oid, 'pg_namespace') "
        "FROM pg_namespace WHERE nspname LIKE 'test\\_%'"
    ).fetchall()
    cutoff = dt.datetime.now(dt.UTC) - _STALE_AFTER
    for schema, stamp in stale:
        if schema.startswith(_RUN_PREFIX):
            continue                     # this run's — a sibling worker may be mid-test on it
        created = _parse_stamp(stamp) if stamp else None
        if created is not None and created > cutoff:
            continue                     # young: another run is plausibly still using it
        try:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()
        except psycopg.Error:
            conn.rollback()  # still locked by a stuck backend — skip; a clean run reclaims it
    conn.close()
    yield
    _drop_this_workers_schemas()


def _drop_this_workers_schemas() -> None:
    """Reclaim anything this worker created and the per-test teardown missed.

    `_drop_test_schemas` drops via the connection that made the schema, so it silently gives up whenever
    that connection is unusable — closed by the test, or left in a failed transaction. This runs on a
    FRESH connection at end of session, scoped to this WORKER's prefix: by then the worker has finished
    every test it owns, and no other worker can own one of its schemas.

    A safety net, and on a healthy run it finds nothing. The ~26-per-run leak this was written for had a
    different cause — a duplicated `conftest` module, fixed at the source and pinned by
    `tests/test_conftest_is_one_module.py`. Keep this anyway: a test that closes its own connection is a
    legitimate thing to write, and the alternative to a net is debris nobody notices until the sweep's
    age window happens to be wrong. Best-effort throughout; tidying up must never fail a green run.
    """
    with contextlib.suppress(psycopg.Error):
        conn = psycopg.connect(_TEST_URL)
        # 1s, not 10s. This is best-effort tidying at the very end of a run: if something still holds a
        # lock, waiting on it buys nothing and 26 stuck schemas × 10s is a minute of dead wall-clock
        # bolted onto every green run. Debris survives to the next run's sweep, which is what it is for.
        conn.execute("SET lock_timeout = '1s'")
        conn.commit()
        # Filter in Python, NOT with LIKE. `_PREFIX` is full of underscores and `_` is a single-character
        # LIKE wildcard, so `test_<run>_gw1_%` also matches gw10's and gw11's schemas — worker gw1
        # finishing early would then drop a live sibling's schema. Harmless at -n 8 (gw0..gw7) and a
        # silent corruption the moment anyone raises the worker count past ten.
        rows = conn.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'test\\_%'").fetchall()
        for (schema,) in [r for r in rows if r[0].startswith(_PREFIX)]:
            try:
                conn.execute(f"DROP SCHEMA {schema} CASCADE")
                conn.commit()
            except psycopg.Error:
                conn.rollback()
        conn.close()


def _parse_stamp(stamp: str) -> dt.datetime | None:
    """The creation time a schema comment carries, or None if it carries something else.

    Unparseable means "not written by `pg_conn`", and the safe reading of an unknown schema is that
    it is debris — so the caller treats None as sweepable rather than protected.
    """
    try:
        return dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
