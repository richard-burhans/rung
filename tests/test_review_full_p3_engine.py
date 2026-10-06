"""CLI, access-engine, dedupe and geocode-cache fixes from the 2026-10-06 whole-tree review. Each
test fails on the code before it."""

import pytest
from click.testing import CliRunner
from conftest import pg_conn

from rung import access, cli, db, queue, reference_db
from rung.models import CompanyStoreRecord
from rung.sources import dedupe


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)
    return conn


@pytest.mark.parametrize("command", [cli.scrape_menus_cmd, cli.worker_cmd])
def test_both_aggregator_filters_are_refused(command) -> None:
    """Together they filter out every store: 0 scraped and no error. Refused before any work."""
    result = CliRunner().invoke(command, ["--state", "PA", "--skip-aggregators", "--only-aggregators"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_a_dedupe_that_raises_releases_its_claim(monkeypatch) -> None:
    """The claim had no failure path: the job stayed `claimed`, and every later fold of the state
    printed "already running" until the lease ran out."""
    conn = _conn()

    def boom(_conn: db.DBConn, _state: str) -> None:
        raise RuntimeError("fold failed")

    monkeypatch.setattr(dedupe, "run_dedupe", boom)
    with pytest.raises(RuntimeError):
        cli._run_dedupe_claimed(conn, "PA")
    assert conn.execute("SELECT status FROM jobs WHERE task_type = 'dedupe'").fetchone() == ("failed",)
    assert queue.live_claim_holder(conn, "dedupe", "PA") is None


def test_url_less_winners_do_not_share_one_governor_bucket() -> None:
    """Every winner without a resource URL counted against the same '' host, so after `host_hard`
    admissions no other URL-less stale winner was re-explored, whatever site it used."""
    governor = access.ReExploreGovernor(rand=lambda: 0.0)
    for _ in range(governor.host_hard):
        assert governor.admit(365.0, access._governor_key(None, "method_a"))
    assert not governor.admit(365.0, access._governor_key(None, "method_a"))
    assert governor.admit(365.0, access._governor_key(None, "method_b"))
    assert access._governor_key("https://www.example.com/x", "method_a") == "example.com"


def test_the_brand_must_match_a_whole_word() -> None:
    """'The Mint' scored on 'the' inside 'Northern' and 'Bethesda' and won the canonical."""
    names = {1: "The Mint", 2: "Harvest"}
    stores = {1: ["Northern Ave", "Bethesda"], 2: ["Harvest Gaithersburg", "Rockville store"]}
    assert dedupe.pick_canonical({1, 2}, names, stores) == 2
    stores = {1: ["The Mint Tempe", "Mint Phoenix"], 2: ["Harvest Gaithersburg"]}
    assert dedupe.pick_canonical({1, 2}, names, stores) == 1


def test_a_stage_two_replace_keeps_cached_coordinates() -> None:
    """The replace deletes and re-inserts a company's rows; nothing restored the geocode cache for
    `company_stores`, so a re-scrape erased every backfilled coordinate."""
    conn = _conn()
    record = CompanyStoreRecord(company_id=1, canonical_name="A", state="PA", source="x",
                                name="Store", address="100 Main St")
    query = reference_db.text.geocode_query(record.address, record.city, record.state, record.zip_code)
    assert query is not None
    reference_db.put_geocode_cache(conn, query, 40.1, -75.2, "19001", "Abington")
    reference_db.replace_company_stores(conn, 1, "PA", [record])
    row = conn.execute("SELECT latitude, longitude, zip_code, city FROM company_stores").fetchone()
    assert row == (40.1, -75.2, "19001", "Abington")
    # A value the source published is never replaced by the cache (same key: coordinates are not
    # part of the query), while the fields it left empty are still filled.
    published = CompanyStoreRecord(company_id=1, canonical_name="A", state="PA", source="x",
                                   name="Store", address="100 Main St", latitude=39.9, longitude=-75.1)
    filled = reference_db._with_cached_geocodes(conn, [published])[0]
    assert (filled.latitude, filled.longitude) == (39.9, -75.1)
    assert (filled.zip_code, filled.city) == ("19001", "Abington")
