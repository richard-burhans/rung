"""CLI wiring tests (click) — arg parsing, option defaults, exit codes.

These exercise cli.py's command layer only: the inner `_run_*` coroutines (which do
real DB/network work) are monkeypatched to argument recorders, so nothing here touches
Postgres or the network.
"""

import click
from click.testing import CliRunner

from rung import cli


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
    cli.dedupe_stores_cmd, cli.prune_jobs_cmd, cli.reap_jobs_cmd, cli.show_states, cli.analyze,
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
    CliRunner().invoke(cli.scrape_company_stores_cmd, [])
    assert calls == [{"state": "PA", "use_ai": False, "only": None, "remax": False}]


def test_scrape_company_stores_remax_flag(monkeypatch) -> None:
    calls, fake = _recorder()
    monkeypatch.setattr(cli, "_run_company_stores", fake)
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
    assert calls[0] == {"only": {"OR"}, "use_ai": False, "use_render": False}


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
