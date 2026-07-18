"""The geocoded location must survive a roster re-scrape.

`scrape-states` replaces a state's rows with DELETE + INSERT, and the source republishes only what
the source publishes. For a roster carrying a street but no ZIP (NV, IL, UT, MD…), the
latitude/longitude/zip_code/city that `backfill_geocode.py` derived are therefore destroyed by the
next SUCCESSFUL scrape — and `compare`'s keys both carry the ZIP, so the state silently stops
matching. That happened on 2026-07-12, reverting the NV and MD fixes.

The existing guard in `scrape_all_states` ("only replace when we extracted something") defends the
failure that was *anticipated* — an empty scrape wiping good rows — and is blind to this one. These
tests pin the behaviour that closes it.
"""

import pytest

from rung import db, models
from tests.conftest import pg_conn


def _roster_row(name: str, address: str, state: str) -> models.DispensaryRecord:
    """A roster row as a no-ZIP source publishes it: a street, a state, and nothing else."""
    return models.DispensaryRecord(source="test", name=name, address=address, state=state)


def _insert(conn: db.DBConn, *records: models.DispensaryRecord) -> None:
    for record in records:
        db.insert_dispensary(conn, record)
    conn.commit()


def _row(conn: db.DBConn, name: str) -> tuple:
    return conn.execute(
        "SELECT latitude, longitude, zip_code, city FROM dispensaries WHERE name = %s", (name,),
    ).fetchone()


def test_apply_geocode_cache_restores_what_a_rescrape_destroyed() -> None:
    conn = pg_conn()
    db.create_reference_tables(conn)
    _insert(conn, _roster_row("Silver Sage", "1 Casino Way", "NV"))

    # The backfill geocoded it once and wrote the result through to the cache.
    db.put_geocode_cache(conn, "1 Casino Way, NV", 36.1, -115.2, "89101", "Las Vegas")
    assert db.apply_geocode_cache(conn, "dispensaries", "NV") == 1
    conn.commit()
    assert _row(conn, "Silver Sage") == (36.1, -115.2, "89101", "Las Vegas")

    # Now the state is re-scraped: every row is deleted and re-inserted from the source, which
    # publishes no ZIP. Before the fix, this is where the deliverable silently broke.
    db.delete_dispensaries_for_state(conn, "NV")
    _insert(conn, _roster_row("Silver Sage", "1 Casino Way", "NV"))
    assert _row(conn, "Silver Sage") == (None, None, None, None)

    # The restore costs no geocoder call and puts it back.
    assert db.apply_geocode_cache(conn, "dispensaries", "NV") == 1
    conn.commit()
    assert _row(conn, "Silver Sage") == (36.1, -115.2, "89101", "Las Vegas")


def test_the_source_stays_authoritative_over_the_cache() -> None:
    """A cached geocoder GUESS must never overwrite a value the source actually published."""
    conn = pg_conn()
    db.create_reference_tables(conn)
    published = models.DispensaryRecord(
        source="test", name="Roster Truth", address="2 Main St", state="NV",
        city="Reno", zip_code="89501", latitude=39.5, longitude=-119.8,
    )
    _insert(conn, published)

    db.put_geocode_cache(conn, "2 Main St, Reno, NV 89501", 1.0, 2.0, "00000", "Wrongtown")
    db.apply_geocode_cache(conn, "dispensaries", "NV")
    conn.commit()

    # Every column is still the source's. The cache only ever fills what the source left empty.
    assert _row(conn, "Roster Truth") == (39.5, -119.8, "89501", "Reno")


def test_a_cache_miss_leaves_the_row_alone() -> None:
    conn = pg_conn()
    db.create_reference_tables(conn)
    _insert(conn, _roster_row("Never Geocoded", "9 Nowhere Rd", "NV"))

    assert db.apply_geocode_cache(conn, "dispensaries", "NV") == 0
    conn.commit()
    assert _row(conn, "Never Geocoded") == (None, None, None, None)


def test_apply_geocode_cache_refuses_an_arbitrary_table() -> None:
    """The table name is interpolated into SQL, so it must come from a fixed allow-list."""
    conn = pg_conn()
    db.create_reference_tables(conn)
    with pytest.raises(ValueError, match="not a geocoded table"):
        db.apply_geocode_cache(conn, "store_products")  # ty: ignore[invalid-argument-type]


def test_put_geocode_cache_overwrites_rather_than_duplicating() -> None:
    """A later geocode of one address is a better reading of it, not a second fact about it."""
    conn = pg_conn()
    db.create_reference_tables(conn)
    db.put_geocode_cache(conn, "3 Elm St, NV", 1.0, 2.0, "89101", "Old")
    db.put_geocode_cache(conn, "3 Elm St, NV", 3.0, 4.0, "89102", "New")
    conn.commit()

    assert db.get_geocode_cache(conn, ["3 Elm St, NV"]) == {
        "3 Elm St, NV": (3.0, 4.0, "89102", "New"),
    }
