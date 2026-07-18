"""CLI wiring tests (click) — arg parsing, option defaults, exit codes.

These exercise cli.py's command layer only: the inner `_run_*` coroutines (which do
real DB/network work) are monkeypatched to argument recorders, so nothing here touches
Postgres or the network.
"""

import click
from click.testing import CliRunner
from conftest import pg_conn

from rung import cli, db, queue
from rung.models import StateProgramRecord


def _recorder():
    """An async stand-in that records the kwargs it was called with."""
    calls: list[dict] = []

    async def _fake(*args, **kwargs) -> None:
        calls.append(kwargs)

    return calls, _fake


_ALL_COMMANDS = [
    cli.recon_cmd, cli.bootstrap_dutchie_cmd, cli.bootstrap_pools_cmd,
    cli.search_states, cli.find_lists, cli.scrape_states,
    cli.scrape_company_stores_cmd, cli.scrape_menus_cmd, cli.compare_stores_cmd,
    cli.dedupe_stores_cmd, cli.prune_jobs_cmd, cli.reap_jobs_cmd, cli.worker_cmd,
    cli.show_states, cli.analyze,
]


def test_every_command_has_working_help() -> None:
    # --help renders each command's options without importing its heavy deps or
    # touching the DB — a smoke test that the click wiring is intact for all of them.
    runner = CliRunner()
    for command in _ALL_COMMANDS:
        result = runner.invoke(command, ["--help"])
        assert result.exit_code == 0, f"{command.name} --help failed: {result.output}"
        assert "Usage:" in result.output


def test_recon_normalizes_state_to_upper(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_recon", fake)
    result = CliRunner().invoke(cli.recon_cmd, ["--state", "pa"])
    assert result.exit_code == 0
    assert calls == [{"state": "PA", "discover": False}]


def test_recon_blank_state_becomes_none(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_recon", fake)
    CliRunner().invoke(cli.recon_cmd, [])
    assert calls == [{"state": None, "discover": False}]


def test_find_lists_parses_only_csv(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_find_lists", fake)
    CliRunner().invoke(cli.find_lists, ["--only", "ny, co ", "--force"])
    assert calls[0]["only"] == {"NY", "CO"}
    assert calls[0]["force"] is True


def test_find_lists_blank_only_is_none(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_find_lists", fake)
    CliRunner().invoke(cli.find_lists, [])
    assert calls[0]["only"] is None and calls[0]["force"] is False


def test_scrape_company_stores_default_state_pa(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_company_stores", fake)
    monkeypatch.setattr(cli, "_dedupe_state", lambda abbr: None)
    CliRunner().invoke(cli.scrape_company_stores_cmd, [])
    assert calls == [
        {"state": "PA", "use_ai": False, "only": None, "remax": False, "record_history": False}
    ]


def test_scrape_company_stores_auto_dedupes_a_full_scrape_but_not_a_scoped_probe(monkeypatch) -> None:
    # A full scrape must fold its duplicates immediately (dedupe FOLLOWS the scrape) so fragmentation +
    # dangling folds never accumulate; a scoped --only probe stays narrow and does NOT fold the state.
    monkeypatch.setattr(cli, "_run_company_stores", _recorder()[1])
    folded: list[str] = []
    monkeypatch.setattr(cli, "_dedupe_state", folded.append)
    CliRunner().invoke(cli.scrape_company_stores_cmd, ["--state", "on"])
    assert folded == ["ON"]                        # full scrape → auto-fold, state upper-cased
    folded.clear()
    CliRunner().invoke(cli.scrape_company_stores_cmd, ["--only", "One Plant"])
    assert folded == []                            # scoped probe → no fold


def test_scrape_company_stores_remax_flag(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_company_stores", fake)
    monkeypatch.setattr(cli, "_dedupe_state", lambda abbr: None)
    CliRunner().invoke(cli.scrape_company_stores_cmd, ["--remax"])
    assert calls[0]["remax"] is True


def test_scrape_company_stores_parses_only(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_company_stores", fake)
    CliRunner().invoke(cli.scrape_company_stores_cmd, ["--only", "Curaleaf, Trulieve "])
    assert calls[0]["only"] == {"curaleaf", "trulieve"}  # split, trimmed, lowercased


def test_scrape_menus_passes_through_flags(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_store_menus", fake)
    CliRunner().invoke(
        cli.scrape_menus_cmd,
        ["--state", "il", "--max-age-hours", "24", "--skip-aggregators", "--stop-on-cooldown",
         "--only", "curaleaf", "--record-history"],
    )
    assert calls == [{
        "state": "IL", "max_age_hours": 24.0,
        "skip_aggregators": True, "only_aggregators": False, "stop_on_cooldown": True,
        "only": {"curaleaf"}, "record_history": True,
    }]


def test_scrape_menus_blank_only_is_none(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_store_menus", fake)
    CliRunner().invoke(cli.scrape_menus_cmd, [])
    assert calls[0]["only"] is None


def test_scrape_states_flags_default_off(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_scrape_states", fake)
    CliRunner().invoke(cli.scrape_states, ["--only", "or"])
    assert calls[0] == {
        "only": {"OR"}, "use_ai": False, "use_render": False, "record_history": False,
    }


def test_bootstrap_dutchie_requires_state() -> None:
    result = CliRunner().invoke(cli.bootstrap_dutchie_cmd, [])
    assert result.exit_code == 2  # click: missing required option
    assert "Missing option" in result.output or "Error" in result.output


def test_bootstrap_pools_requires_state() -> None:
    assert CliRunner().invoke(cli.bootstrap_pools_cmd, []).exit_code == 2


def test_analyze_requires_url() -> None:
    assert CliRunner().invoke(cli.analyze, []).exit_code == 2


def test_max_age_hours_must_be_numeric(monkeypatch) -> None:
    _calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_store_menus", fake)
    result = CliRunner().invoke(cli.scrape_menus_cmd, ["--max-age-hours", "soon"])
    assert result.exit_code == 2  # click rejects non-float


def test_commands_are_click_commands() -> None:
    # Guards the [project.scripts] entry points: each name resolves to a click Command.
    for command in _ALL_COMMANDS:
        assert isinstance(command, click.Command)


# --- DB-backed command bodies (the standalone commands that own their own connection) ---
#
# These run the real command body against a throwaway test schema, so they cover the
# queue/DB plumbing the monkeypatched wiring tests above deliberately skip.

class _NoCloseConn:
    """Wraps a test connection so a command's ``conn.close()`` is a no-op — conftest still
    owns the real connection and drops its schema in teardown (a command-closed connection
    would break teardown's ``current_schema()`` probe and leak the schema)."""

    def __init__(self, conn: db.DBConn) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _bind_test_conn(monkeypatch) -> db.DBConn:
    """Point ``db.get_connection`` at a fresh test schema; return the real connection so the
    test can seed rows on the same session the command will read."""
    conn = pg_conn()
    db.create_tables(conn)
    monkeypatch.setattr(db, "get_connection", lambda: _NoCloseConn(conn))
    return conn


def test_prune_jobs_deletes_aged_finished(monkeypatch) -> None:
    conn = _bind_test_conn(monkeypatch)
    queue.enqueue(conn, "store_menu", "PA:1")
    job = queue.claim_next(conn, "store_menu", "w1")
    assert job is not None
    queue.complete(conn, job.id, "done", worker="w1")
    conn.execute("UPDATE jobs SET finished_at = now() - make_interval(days => 10)")
    conn.commit()
    result = CliRunner().invoke(cli.prune_jobs_cmd, ["--older-than-hours", "168"])
    assert result.exit_code == 0, result.output
    assert "Pruned 1 finished jobs" in result.output


def test_reap_jobs_reports_count_on_empty_queue(monkeypatch) -> None:
    _bind_test_conn(monkeypatch)
    result = CliRunner().invoke(cli.reap_jobs_cmd, [])
    assert result.exit_code == 0, result.output
    assert "Reaped 0 lease-expired jobs." in result.output


def test_show_states_empty_warns(monkeypatch) -> None:
    _bind_test_conn(monkeypatch)
    result = CliRunner().invoke(cli.show_states, [])
    assert result.exit_code == 0
    assert "No state data" in result.output


def test_show_states_prints_report(monkeypatch) -> None:
    conn = _bind_test_conn(monkeypatch)
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="PA", name="Pennsylvania", programs="medical",
        program_term="medical", agency="DOH"))
    conn.commit()
    result = CliRunner().invoke(cli.show_states, [])
    assert result.exit_code == 0, result.output
    assert "PA" in result.output or "Pennsylvania" in result.output


# --- worker command ---

def test_worker_requires_state() -> None:
    assert CliRunner().invoke(cli.worker_cmd, []).exit_code == 2


def test_worker_drains_each_state_both_stages(monkeypatch) -> None:
    _bind_test_conn(monkeypatch)
    seen: list[tuple[str, str]] = []

    async def _fake_company(_conn, abbr, **_kw):
        seen.append(("company", abbr))
        return []

    async def _fake_menus(_conn, abbr, **_kw):
        seen.append(("menus", abbr))
        return []

    stages = {
        "company_stores.run": _fake_company, "company_stores.print": lambda r, s: None,
        "menus.run": _fake_menus, "menus.print": lambda r, s: None,
    }
    monkeypatch.setattr(cli, "_stage", lambda name: stages[name])
    result = CliRunner().invoke(cli.worker_cmd, ["--state", "pa, nj", "--task", "both"])
    assert result.exit_code == 0, result.output
    # each state drained once, company-stores before menus, states in order, uppercased.
    assert seen == [("company", "PA"), ("menus", "PA"), ("company", "NJ"), ("menus", "NJ")]


def test_worker_defaults_to_menus_only(monkeypatch) -> None:
    _bind_test_conn(monkeypatch)
    seen: list[str] = []

    async def _fake_menus(_conn, abbr, **_kw):
        seen.append(abbr)
        return []

    # Only the menus stage is registered; if the default task resolved company-stores too,
    # `_stage("company_stores.run")` would KeyError and fail the run.
    stages = {"menus.run": _fake_menus, "menus.print": lambda r, s: None}
    monkeypatch.setattr(cli, "_stage", lambda name: stages[name])
    result = CliRunner().invoke(cli.worker_cmd, ["--state", "OK"])
    assert result.exit_code == 0, result.output
    assert seen == ["OK"]
