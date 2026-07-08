"""The non-cannabis example drives the engine (records + access ladder + queue + a custom table)
end to end, proving `rung` is usable as a general library — no cannabis records or schema involved.
See examples/custom_domain.py.
"""

import asyncio

from examples import custom_domain
from tests.conftest import pg_conn


def test_custom_domain_runs_the_ladder_and_persists_to_a_custom_table() -> None:
    conn = pg_conn()
    results = asyncio.run(custom_domain.run(conn, ["springfield", "shelbyville", "ogdenville"]))

    # Every enqueued city drained through the queue and returned records.
    assert set(results) == {"springfield", "shelbyville", "ogdenville"}
    assert all(count == 1 for _winner, count in results.values())

    # The cost-ranked ladder picked the cheap JSON rung where it worked, and fell back where it didn't.
    assert results["springfield"][0] == "markets_json"
    assert results["shelbyville"][0] == "markets_json"
    assert results["ogdenville"][0] == "markets_html"

    # The records landed in the caller's OWN table, not any reference-app table.
    rows = conn.execute(
        "SELECT city, name FROM farmers_markets ORDER BY city"
    ).fetchall()
    assert rows == [
        ("Ogdenville", "Ogdenville Green"),
        ("Shelbyville", "Riverside Market"),
        ("Springfield", "Downtown Market"),
    ]

    # The engine remembered the cheapest working method per target for a future run.
    winners = dict(
        conn.execute(
            "SELECT target_key, method FROM access_methods "
            "WHERE target_type = %s AND status = 'ok'",
            (custom_domain.TARGET_TYPE,),
        ).fetchall()
    )
    assert winners == {
        "springfield": "markets_json",
        "shelbyville": "markets_json",
        "ogdenville": "markets_html",
    }
