"""Tests for the cost-ranked access-method runner (rung.access)."""

import asyncio
from types import SimpleNamespace

from conftest import pg_conn

from rung import access, db


def _store(name: str = "Acme", address: str = "1 Main St") -> SimpleNamespace:
    return SimpleNamespace(name=name, address=address, city=None, zip_code=None)


def _conn() -> db.DBConn:
    conn = pg_conn()
    # access_methods is created by create_tables alongside the other tables.
    conn.execute(db._CREATE_ACCESS_METHODS)
    return conn


class _Method:
    """A fake AccessMethod runner that logs calls and returns a canned result."""

    def __init__(self, records: list) -> None:
        self.records = records
        self.calls = 0

    def set(self, records: list) -> None:
        self.records = records

    async def __call__(self, conn, target_key, hint):
        self.calls += 1
        return self.records, "https://example.test/loc", {"k": "v"}


def _run(coro):
    return asyncio.run(coro)


def test_plausibility_gate() -> None:
    assert access.is_plausible(_store())
    assert not access.is_plausible(SimpleNamespace(name="X", address=None, city=None, zip_code=None))
    assert not access.is_plausible(SimpleNamespace(name=None, address="1 Main St", city=None, zip_code=None))
    assert access.is_success([_store(), SimpleNamespace(name=None, address=None, city=None, zip_code=None)])
    assert not access.is_success([], min_records=1)


def test_picks_cheapest_working_and_persists_winner() -> None:
    conn = _conn()
    cheap = _Method([])              # tier 0 — fails (no records)
    dear = _Method([_store()])       # tier 1 — works
    catalog = [
        access.AccessMethod("cheap", 0, cheap),
        access.AccessMethod("dear", 10, dear),
    ]
    method, records = _run(access.run_target(conn, "t", "k", catalog))
    assert method == "dear"
    assert len(records) == 1
    assert cheap.calls == 1 and dear.calls == 1
    winner = db.get_access_winner(conn, "t", "k")
    assert winner[0] == "dear"
    assert winner[db.ACCESS_METHOD_COLUMNS.index("resource_url")] == "https://example.test/loc"


def test_second_run_skips_to_winner() -> None:
    conn = _conn()
    cheap = _Method([])
    dear = _Method([_store()])
    catalog = [access.AccessMethod("cheap", 0, cheap), access.AccessMethod("dear", 10, dear)]
    _run(access.run_target(conn, "t", "k", catalog))
    _run(access.run_target(conn, "t", "k", catalog))
    # Winner (dear) tried first on the second run and succeeded, so cheap is not retried.
    assert dear.calls == 2
    assert cheap.calls == 1


def test_invalidates_failed_winner_and_promotes_next() -> None:
    conn = _conn()
    cheap = _Method([])
    dear = _Method([_store()])
    catalog = [access.AccessMethod("cheap", 0, cheap), access.AccessMethod("dear", 10, dear)]
    _run(access.run_target(conn, "t", "k", catalog))   # dear wins
    dear.set([])                                        # dear breaks
    cheap.set([_store("New")])                          # cheap now works
    method, _records = _run(access.run_target(conn, "t", "k", catalog))
    assert method == "cheap"
    assert db.get_access_winner(conn, "t", "k")[0] == "cheap"


def _stores(n: int) -> list:
    return [_store(f"S{i}", f"{i} Main St") for i in range(n)]


def test_max_yield_picks_richest_not_first_success() -> None:
    conn = _conn()
    cheap = _Method(_stores(1))      # tier 0 — succeeds, but thin
    dear = _Method(_stores(3))       # tier 1 — succeeds, richer
    catalog = [access.AccessMethod("cheap", 0, cheap), access.AccessMethod("dear", 10, dear)]
    method, records = _run(access.run_target(conn, "t", "k", catalog, max_yield=True))
    assert method == "dear" and len(records) == 3
    # Full walk: both rungs run even though the cheap one already succeeded.
    assert cheap.calls == 1 and dear.calls == 1


def test_max_yield_quality_key_prefers_handles_over_count() -> None:
    # A quality_key (menu handles first) must beat raw count: one handle-bearing store outranks
    # three bare addresses, so a large aggregator sweep can't displace a smaller first-party list
    # (which the keep-the-best replace would then wrongly block).
    conn = _conn()
    bare = _Method([_store(f"S{i}", f"{i} Main St") for i in range(3)])   # tier 0, 3 bare rows
    handled = _Method([SimpleNamespace(
        name="H", address="9 Oak St", city=None, zip_code=None, external_id="42")])  # tier 10, 1 handle

    def quality_key(records: list) -> tuple[int, int]:
        return sum(1 for r in records if getattr(r, "external_id", None)), len(records)

    catalog = [access.AccessMethod("bare", 0, bare), access.AccessMethod("handled", 10, handled)]
    method, records = _run(access.run_target(
        conn, "t", "k", catalog, max_yield=True, quality_key=quality_key))
    assert method == "handled" and len(records) == 1  # handle-bearing beats the larger bare set


def test_max_yield_tie_keeps_cheaper_rung() -> None:
    conn = _conn()
    cheap = _Method(_stores(2))
    dear = _Method(_stores(2))
    catalog = [access.AccessMethod("cheap", 0, cheap), access.AccessMethod("dear", 10, dear)]
    method, records = _run(access.run_target(conn, "t", "k", catalog, max_yield=True))
    assert method == "cheap" and len(records) == 2


def test_max_yield_default_off_keeps_first_success() -> None:
    # max_yield defaults False → unchanged first-cheap-success behavior.
    conn = _conn()
    cheap = _Method(_stores(1))
    dear = _Method(_stores(3))
    catalog = [access.AccessMethod("cheap", 0, cheap), access.AccessMethod("dear", 10, dear)]
    method, records = _run(access.run_target(conn, "t", "k", catalog))
    assert method == "cheap" and len(records) == 1
    assert dear.calls == 0          # cheap succeeded first, dear never tried


def test_admitted_staleness_rewalk_demotes_to_cheaper() -> None:
    conn = _conn()
    cheap = _Method([])
    dear = _Method([_store()])
    catalog = [access.AccessMethod("cheap", 0, cheap), access.AccessMethod("dear", 10, dear)]
    _run(access.run_target(conn, "t", "k", catalog))   # dear wins
    # Age the winner well past max_days and make the cheap rung now succeed.
    conn.execute("UPDATE access_methods SET last_ok_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    cheap.set([_store("Cheaper")])
    gov = access.ReExploreGovernor(rand=lambda: 0.0)  # always admit when prob > 0
    method, _ = _run(access.run_target(conn, "t", "k", catalog, governor=gov))
    # Admitted re-walk → cheapest-first → cheap is reached and demotes dear.
    assert method == "cheap"
    assert cheap.calls == 2


def test_no_governor_trusts_winner_indefinitely() -> None:
    conn = _conn()
    cheap = _Method([])
    dear = _Method([_store()])
    catalog = [access.AccessMethod("cheap", 0, cheap), access.AccessMethod("dear", 10, dear)]
    _run(access.run_target(conn, "t", "k", catalog))   # dear wins
    conn.execute("UPDATE access_methods SET last_ok_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    _run(access.run_target(conn, "t", "k", catalog))   # no governor → no re-walk
    assert cheap.calls == 1  # cheap never retried despite the winner being ancient


def test_governor_temporal_ramp() -> None:
    gov = access.ReExploreGovernor(min_days=25, max_days=35, max_age_prob=0.6)
    assert gov._age_prob(10) == 0.0          # below min: never
    assert gov._age_prob(40) == 1.0          # above max: always wanted
    assert gov._age_prob(30) == 0.3          # midpoint: half of max_age_prob


def test_governor_per_host_throttle() -> None:
    # rand=0 always admits when probability > 0; host_hard caps a single host.
    gov = access.ReExploreGovernor(
        min_days=0, max_days=1, host_soft=2, host_hard=4, rand=lambda: 0.0
    )
    admitted = [gov.admit(age_days=10, host="dutchie.com") for _ in range(8)]
    assert sum(admitted) == 4  # host_hard caps re-walks against one site per run
    # A different host is unaffected by dutchie.com's saturation.
    assert gov.admit(age_days=10, host="iheartjane.com") is True


def test_failed_attempt_clears_resource_url() -> None:
    # The stale-hint fix: a winning locator is kept on success but cleared on
    # failure, so the next attempt re-discovers instead of re-serving a broken URL.
    conn = _conn()
    db.record_access_attempt(conn, "t", "k", "m", 0, "ok",
                             resource_url="https://x/loc", params='{"a": 1}', record_count=3)
    conn.commit()
    row = db.get_access_methods(conn, "t", "k")[0]
    assert row[db.ACCESS_METHOD_COLUMNS.index("resource_url")] == "https://x/loc"

    db.record_access_attempt(conn, "t", "k", "m", 0, "failed", error="boom")
    conn.commit()
    row = db.get_access_methods(conn, "t", "k")[0]
    assert row[db.ACCESS_METHOD_COLUMNS.index("resource_url")] is None
    assert row[db.ACCESS_METHOD_COLUMNS.index("params")] is None


def test_new_untried_cheaper_rung_triggers_one_rewalk() -> None:
    conn = _conn()
    dear = _Method([_store()])
    catalog = [access.AccessMethod("dear", 9, dear)]
    _run(access.run_target(conn, "t", "k", catalog))   # dear wins (only rung)
    assert dear.calls == 1

    # A NEW cheaper rung lands in the catalog: it must get tried even though the
    # stored winner still works...
    fresh = _Method([_store("Better")])
    catalog = [access.AccessMethod("fresh", 1, fresh), access.AccessMethod("dear", 9, dear)]
    method, _ = _run(access.run_target(conn, "t", "k", catalog))
    assert method == "fresh" and fresh.calls == 1 and dear.calls == 1

    # ...and once it has an attempt row, winner-first resumes (fresh IS the winner
    # now) — dear is not touched again.
    fresh2 = _Method([_store("Better")])
    catalog = [access.AccessMethod("fresh", 1, fresh2), access.AccessMethod("dear", 9, dear)]
    method, _ = _run(access.run_target(conn, "t", "k", catalog))
    assert method == "fresh" and fresh2.calls == 1 and dear.calls == 1


def test_a_preferred_rung_that_once_failed_runs_ahead_of_the_stored_winner() -> None:
    """A pin added after its rung had failed: without `preferred` the stored winner keeps winning
    (the rung has a row, so it is not "untried") — the 2026-10-23 THE SOURCE / PRC / Matchbox case.
    A preferred rung costlier than the winner runs first too; one that yields nothing costs one
    attempt and the winner still serves."""
    conn = _conn()
    pinned = _Method([])
    generic = _Method([_store()])
    catalog = [access.AccessMethod("pinned", 2, pinned), access.AccessMethod("generic", 3, generic)]
    assert _run(access.run_target(conn, "t", "k", catalog))[0] == "generic"   # pinned failed once

    pinned.set([_store("Pinned")])                      # the pin is now right
    assert _run(access.run_target(conn, "t", "k", catalog))[0] == "generic"   # winner-first: ignored
    method, _ = _run(access.run_target(conn, "t", "k", catalog, preferred={"pinned"}))
    assert method == "pinned"

    costly = _Method([_store("Costly")])
    catalog = [access.AccessMethod("generic", 3, generic), access.AccessMethod("costly", 30, costly)]
    assert _run(access.run_target(conn, "t2", "k", catalog))[0] == "generic"
    assert _run(access.run_target(conn, "t2", "k", catalog, preferred={"costly"}))[0] == "costly"

    dud = _Method([])
    catalog = [access.AccessMethod("generic", 3, generic), access.AccessMethod("dud", 1, dud)]
    calls = generic.calls
    method, _ = _run(access.run_target(conn, "t2", "k", catalog, preferred={"dud"}))
    assert method == "generic" and generic.calls == calls + 1


def test_a_revision_bump_gives_a_fixed_rung_one_fresh_walk() -> None:
    """A cheaper rung failed, a costlier one won, then the cheaper rung's CODE was fixed: without a
    revision the stored winner shadows it forever (2026-10-23, menu_apps and breadstack_locations)."""
    conn = _conn()
    fixed = _Method([])
    generic = _Method([_store()])
    catalog = [access.AccessMethod("fixed", 0, fixed), access.AccessMethod("generic", 3, generic)]
    assert _run(access.run_target(conn, "t", "k", catalog))[0] == "generic"   # fixed failed at rev 0

    fixed.set([_store("Fixed")])                                              # the code is fixed...
    assert _run(access.run_target(conn, "t", "k", catalog))[0] == "generic"   # ...and still shadowed
    calls = fixed.calls

    bumped = [access.AccessMethod("fixed", 0, fixed, revision=1), access.AccessMethod("generic", 3, generic)]
    assert _run(access.run_target(conn, "t", "k", bumped))[0] == "fixed"      # one fresh walk
    assert fixed.calls == calls + 1
    row = {r[0]: r for r in db.get_access_methods(conn, "t", "k")}["fixed"]
    assert row[db.ACCESS_METHOD_COLUMNS.index("revision")] == 1


def test_a_cheaper_rungs_old_failure_is_retried_and_a_recent_one_is_not() -> None:
    """The Dispensary NV (2026-10-23): one `dutchie_directory` failure in June shadowed the rung behind
    a Weedmaps winner for four months, though the stores had been on Dutchie all along."""
    conn = _conn()
    cheap = _Method([])
    winner = _Method([_store()])
    catalog = [access.AccessMethod("cheap", 1, cheap), access.AccessMethod("winner", 9, winner)]
    assert _run(access.run_target(conn, "t", "k", catalog))[0] == "winner"   # cheap failed just now
    cheap.set([_store("Fixed upstream")])
    assert _run(access.run_target(conn, "t", "k", catalog))[0] == "winner"   # a recent failure holds
    calls = cheap.calls

    conn.execute("UPDATE access_methods SET last_fail_at = %s WHERE method = 'cheap'",
                 ("2026-06-17T00:00:00+00:00",))
    assert _run(access.run_target(conn, "t", "k", catalog, failure_ttl_days=None))[0] == "winner"
    assert cheap.calls == calls                                              # disabled: still shadowed
    assert _run(access.run_target(conn, "t", "k", catalog))[0] == "cheap"    # 60 days on: asked again
    assert cheap.calls == calls + 1


def test_a_revision_bump_walks_a_still_failing_rung_once_and_never_an_ok_one() -> None:
    conn = _conn()
    dud = _Method([])
    generic = _Method([_store()])
    catalog = [access.AccessMethod("dud", 0, dud), access.AccessMethod("generic", 3, generic)]
    _run(access.run_target(conn, "t", "k", catalog))
    bumped = [access.AccessMethod("dud", 0, dud, revision=1), access.AccessMethod("generic", 3, generic)]
    _run(access.run_target(conn, "t", "k", bumped))         # retried at rev 1, still nothing
    _run(access.run_target(conn, "t", "k", bumped))         # rev-1 failure is current: not again
    assert dud.calls == 2

    # A bump to the WINNING rung re-decides nothing: an ok row is never stale.
    ok_cheap = _Method([_store()])
    costly = _Method([_store("Costly")])
    catalog = [access.AccessMethod("cheap", 0, ok_cheap), access.AccessMethod("costly", 5, costly)]
    _run(access.run_target(conn, "t2", "k", catalog))
    bumped = [access.AccessMethod("cheap", 0, ok_cheap, revision=7), access.AccessMethod("costly", 5, costly)]
    assert _run(access.run_target(conn, "t2", "k", bumped))[0] == "cheap" and costly.calls == 0


def test_new_cheaper_rung_that_fails_falls_back_to_winner() -> None:
    conn = _conn()
    dear = _Method([_store()])
    catalog = [access.AccessMethod("dear", 9, dear)]
    _run(access.run_target(conn, "t", "k", catalog))
    dud = _Method([])
    catalog = [access.AccessMethod("dud", 1, dud), access.AccessMethod("dear", 9, dear)]
    method, _ = _run(access.run_target(conn, "t", "k", catalog))
    assert method == "dear" and dud.calls == 1  # tried once, then the winner served
    dud2 = _Method([])
    catalog = [access.AccessMethod("dud", 1, dud2), access.AccessMethod("dear", 9, dear)]
    _run(access.run_target(conn, "t", "k", catalog))
    assert dud2.calls == 0  # failed row exists — no repeat exploration


def test_winner_absent_from_current_catalog_falls_back_to_cheapest() -> None:
    # A winner stored from a prior catalog (e.g. ai_llm won on a --ai run) is no longer in
    # this run's catalog; run_target must ignore the absent winner and walk cheapest-first.
    conn = _conn()
    db.record_access_attempt(conn, "t", "k", "ai_llm", 99, "ok", "https://ai/loc", None, 3)
    conn.commit()
    cheap = _Method([_store()])
    method, records = _run(access.run_target(conn, "t", "k", [access.AccessMethod("cheap", 0, cheap)]))
    assert method == "cheap"        # present cheapest wins; the absent winner is not tried
    assert cheap.calls == 1 and len(records) == 1


def test_a_crashing_rung_is_recorded_broken_and_the_ladder_continues() -> None:
    """A rung that raises an UNLABELLED exception must be recorded `broken`, not vanish.

    THE BUG THIS PINS. `_attempt` caught only `MethodOutcome` and let everything else propagate — on the
    stated reasoning that a silent crash must not be swallowed. But `run_target`'s only two callers both
    wrap it in `except Exception: return None, []`, so the crash WAS swallowed, one frame up:

      * no `access_methods` row was written — not `broken`, not `failed`, NOTHING. The target read as
        *has no method*, which is a failure of OURS reported as a fact about the world (the top-of-file
        comment names this the project's signature bug);
      * and the rest of the ladder never ran, so one shape-changed payload in a CHEAP rung silently
        disabled every EXPENSIVE rung that would have worked.

    The extractors walk raw dicts/lists, so `TypeError`/`AttributeError` from an upstream shape change is
    the EXPECTED trigger, not a hypothetical.
    """
    conn = _conn()

    async def crashes(conn, target_key, hint):
        raise TypeError("'NoneType' object is not subscriptable")   # the real shape-change signature

    catalog = [
        access.AccessMethod(name="cheap_and_crashing", cost_rank=1, run=crashes),
        access.AccessMethod(name="expensive_and_working", cost_rank=9, run=_Method([_store()])),
    ]
    winner, records = _run(access.run_target(conn, "store_menu", "XX:crash", catalog))

    # 1. The ladder CONTINUED past the crash — the expensive rung that works still got its turn.
    assert winner == "expensive_and_working"
    assert len(records) == 1

    # 2. The crash is ON THE RECORD, as `broken` — the rung is asking to be repaired — and it carries the
    #    exception type, so `access_health` can show what actually happened instead of showing nothing.
    rows = {row[0]: row for row in db.get_access_methods(conn, "store_menu", "XX:crash")}
    assert "cheap_and_crashing" in rows, "a crashed rung wrote NO ROW — the target reads as 'no method'"
    status = rows["cheap_and_crashing"][db.ACCESS_METHOD_COLUMNS.index("status")]
    error = rows["cheap_and_crashing"][db.ACCESS_METHOD_COLUMNS.index("error")]
    assert status == "broken", f"a crashed rung must be `broken`, got {status!r}"
    assert "TypeError" in error


# ── A rung declares the vantage it needs (2026-10-13, scraping design review item 5) ────────────


def test_fits_vantage_accepts_unknowns_and_a_country_less_class_accepts_any_country() -> None:
    AM = access.AccessMethod
    assert access.fits_vantage(AM("x", 0, _Method([]), vantage=None, egress="direct"))
    assert access.fits_vantage(AM("x", 0, _Method([]), vantage=frozenset({"residential"}), egress=None))
    assert access.fits_vantage(AM("x", 0, _Method([]), vantage=frozenset({"residential"}), egress="residential:CA"))
    assert access.fits_vantage(AM("x", 0, _Method([]), vantage=frozenset({"residential", "host"}), egress="host"))
    assert not access.fits_vantage(AM("x", 0, _Method([]), vantage=frozenset({"residential", "host"}), egress="direct"))
    assert not access.fits_vantage(AM("x", 0, _Method([]), vantage=frozenset({"residential:CA"}), egress="residential:US"))
    assert not access.fits_vantage(AM("x", 0, _Method([]), vantage=frozenset({"residential:CA"}), egress="residential"))


def test_a_rung_outside_the_vantage_writes_no_row_is_counted_and_does_not_reopen_the_ladder() -> None:
    """A skipped rung has no attempt row, which `untried_cheaper` would otherwise read as "never
    tried" and re-walk the full ladder for every costlier winner on every run."""
    from collections import Counter

    conn = _conn()
    cheap = _Method([_store()])     # rank 1, but this box's egress is outside its vantage
    costly = _Method([_store()])    # rank 9, the persisted winner
    catalog = [
        access.AccessMethod("cheap_directory", 1, cheap, vantage=frozenset({"residential"}), egress="direct"),
        access.AccessMethod("costly_site", 9, costly),
    ]
    skips: Counter[str] = Counter()
    assert _run(access.run_target(conn, "t", "k", catalog, vantage_skips=skips)) == ("costly_site", [cheap.records[0]])
    assert cheap.calls == 0 and costly.calls == 1
    assert skips == {"cheap_directory": 1}
    rows = {r[0]: r for r in db.get_access_methods(conn, "t", "k")}
    assert set(rows) == {"costly_site"}                       # no row for the skipped rung
    # Second run: the winner is trusted; the skipped rung must not count as an untried cheaper rung.
    _run(access.run_target(conn, "t", "k", catalog, vantage_skips=skips))
    assert cheap.calls == 0 and costly.calls == 2 and skips == {"cheap_directory": 2}
    # Without a counter the drop still happens, silently for the caller.
    _run(access.run_target(conn, "t", "k", catalog))
    assert cheap.calls == 0
