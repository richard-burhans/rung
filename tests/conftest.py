"""Shared test DB plumbing: every pg_conn() call returns a connection scoped to a
fresh throwaway schema in the rung_test database, so tests get the same
isolation the old in-memory SQLite connections gave. Schemas are dropped after
each test; a session-start sweep clears leftovers from crashed runs."""

import contextlib
import os
import uuid

import psycopg
import pytest

from rung import db

_TEST_URL = os.environ.get(
    "DATABASE_URL_TEST",
    "postgresql://rung:rung@localhost:5432/rung_test",
)
_open_conns: list[db.DBConn] = []


def pg_conn() -> db.DBConn:
    """A connection whose search_path is a fresh test_<uuid> schema."""
    try:
        conn = psycopg.connect(_TEST_URL)
    except psycopg.OperationalError:
        pytest.fail(
            "test Postgres unreachable — run scripts/dev_pg.sh first", pytrace=False
        )
    schema = f"test_{uuid.uuid4().hex}"
    conn.execute(f"CREATE SCHEMA {schema}")
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
            conn.rollback()
        finally:
            conn.close()


@pytest.fixture(scope="session", autouse=True)
def _sweep_stale_schemas():
    """Clear test_* schemas left behind by a previous crashed run.

    A killed run can leave a backend still holding a lock on its throwaway schema; a
    bare ``DROP SCHEMA`` would then block the WHOLE session indefinitely. Cap the wait
    with ``lock_timeout`` and skip any schema still locked (harmless debris — a later
    clean run reclaims it) so one stuck lock can never wedge the suite.
    """
    try:
        conn = psycopg.connect(_TEST_URL)
    except psycopg.OperationalError:
        pytest.exit("test Postgres unreachable — run scripts/dev_pg.sh first", 1)
    conn.execute("SET lock_timeout = '10s'")
    conn.commit()
    stale = conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'test_%'"
    ).fetchall()
    for (schema,) in stale:
        try:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()
        except psycopg.Error:
            conn.rollback()  # still locked by a stuck backend — skip; a clean run reclaims it
    conn.close()
    yield
