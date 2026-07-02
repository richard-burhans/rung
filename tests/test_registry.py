"""Contract for the plugin seam (``rung.registry``).

The registry is the boundary between the public pipeline shell (``cli.py``) and the proprietary
stage implementations (Stage-2/3 scraping catalogs, the comparison intel, platform recon) that
ship in the private ``rung_intel`` overlay. The public core must run with those
stages *unplugged* — resolving an unplugged stage yields a stub that raises a clear, install-hinted
error only when invoked — and a private package plugs the real implementations in via the
``rung.plugins`` entry-point group. See docs/publish_split_design.md.
"""

import ast
import importlib.metadata
from pathlib import Path

import pytest
from click.testing import CliRunner

from rung import cli, registry

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CLI_PATH = _REPO_ROOT / "rung" / "cli.py"
_PLUGIN_PATH = _REPO_ROOT / "rung_intel" / "rung_intel" / "intel_plugin.py"


def _stage_names_resolved_by_cli() -> set[str]:
    """Every literal name ``cli.py`` passes to ``_stage(...)`` (its ``registry.resolve`` calls)."""
    tree = ast.parse(_CLI_PATH.read_text(encoding="utf-8"), filename=str(_CLI_PATH))
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_stage"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def _stage_names_registered_by_overlay() -> set[str]:
    """Every literal name ``intel_plugin.register_all`` passes to ``registry.register(...)``."""
    tree = ast.parse(_PLUGIN_PATH.read_text(encoding="utf-8"), filename=str(_PLUGIN_PATH))
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts from an empty registry with plugins not yet discovered."""
    registry.reset()
    yield
    registry.reset()


class _FakeEntryPoint:
    """Stand-in for ``importlib.metadata.EntryPoint`` whose ``load()`` returns a registrar."""

    def __init__(self, name: str, registrar) -> None:
        self.name = name
        self._registrar = registrar

    def load(self):
        return self._registrar


def test_register_and_resolve_roundtrip() -> None:
    sentinel = object()
    registry.register("stage-x", lambda: sentinel)
    assert registry.is_registered("stage-x")
    assert registry.resolve("stage-x")() is sentinel


def test_unregistered_resolves_to_stub_that_raises_only_when_called() -> None:
    assert not registry.is_registered("missing-stage")
    stub = registry.resolve("missing-stage")  # resolving is always safe
    with pytest.raises(registry.StageNotAvailable) as exc:
        stub()
    message = str(exc.value)
    assert "missing-stage" in message
    assert "rung-intel" in message  # carries the install hint


def test_register_override_replaces() -> None:
    registry.register("s", lambda: 1)
    registry.register("s", lambda: 2)
    assert registry.resolve("s")() == 2


def test_register_without_override_rejects_a_clash() -> None:
    registry.register("s", lambda: 1)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("s", lambda: 2, override=False)


def test_load_plugins_discovers_and_invokes_entry_points(monkeypatch) -> None:
    def registrar() -> None:
        registry.register("plugged", lambda: "real")

    ep = _FakeEntryPoint("intel", registrar)
    monkeypatch.setattr(
        registry.importlib_metadata,
        "entry_points",
        lambda group: [ep] if group == registry.ENTRY_POINT_GROUP else [],
    )
    loaded = registry.load_plugins()
    assert loaded == ["intel"]
    assert registry.resolve("plugged")() == "real"


def test_load_plugins_is_idempotent(monkeypatch) -> None:
    calls: list[int] = []

    def registrar() -> None:
        calls.append(1)
        registry.register("p", lambda: 1)

    ep = _FakeEntryPoint("intel", registrar)
    monkeypatch.setattr(registry.importlib_metadata, "entry_points", lambda group: [ep])
    registry.load_plugins()
    registry.load_plugins()  # second call must not re-discover/re-invoke
    assert calls == [1]


def test_a_plugin_overrides_the_stub(monkeypatch) -> None:
    # Out of the box the proprietary stage is unplugged: invoking the stub raises.
    with pytest.raises(registry.StageNotAvailable):
        registry.resolve("scrape-company-stores")()

    def registrar() -> None:
        registry.register("scrape-company-stores", lambda: "scraped")

    ep = _FakeEntryPoint("intel", registrar)
    monkeypatch.setattr(registry.importlib_metadata, "entry_points", lambda group: [ep])
    registry.load_plugins()
    assert registry.resolve("scrape-company-stores")() == "scraped"


def test_registered_names_reflects_real_registrations_only() -> None:
    registry.register("a", lambda: None)
    registry.register("b", lambda: None)
    assert registry.registered_names() == frozenset({"a", "b"})
    # Resolving an unregistered name (returning a stub) does not register it.
    registry.resolve("c")
    assert registry.registered_names() == frozenset({"a", "b"})


# ── The "runs standalone" contract: with the overlay UNINSTALLED (an empty plugins entry-point group),
# the public core still boots and every proprietary stage degrades to an install-hinted stub. ────────

def test_overlay_absent_leaves_every_proprietary_stage_a_stub(monkeypatch) -> None:
    monkeypatch.setattr(registry.importlib_metadata, "entry_points", lambda group: [])
    assert registry.load_plugins() == []  # nothing discovered → nothing registered
    assert registry.registered_names() == frozenset()
    # EVERY proprietary stage the CLI resolves (not a hand-picked sample) must degrade to a stub —
    # the names come straight from cli.py's _stage(...) calls so this can't drift out of coverage.
    stage_names = _stage_names_resolved_by_cli()
    assert stage_names, "no _stage(...) calls found in cli.py — extraction broke"
    for name in sorted(stage_names):
        with pytest.raises(registry.StageNotAvailable):
            registry.resolve(name)()


def test_seam_name_contract_cli_resolves_exactly_what_the_overlay_registers() -> None:
    """Static guard on the seam-name contract: the proprietary stage names ``cli.py`` resolves via
    ``registry.resolve`` must be EXACTLY the set the overlay's ``intel_plugin.register_all`` registers.
    Both hold today only by hand-kept parallelism — a new ``_stage("x")`` with no matching
    ``registry.register("x", …)`` would surface as a ``StageNotAvailable`` in production, and a dropped
    registration would leave a dead entry. This ast-parses both sides (no imports executed) and fails
    with the offending names, mirroring the guards in test_import_layering.py / test_http.py."""
    if not _PLUGIN_PATH.exists():
        pytest.skip("overlay absent — public-repo build")
    resolved = _stage_names_resolved_by_cli()
    registered = _stage_names_registered_by_overlay()
    assert resolved, "no _stage(...) calls found in cli.py — extraction broke"
    cli_only = resolved - registered
    overlay_only = registered - resolved
    assert not cli_only, (
        f"cli.py resolves stage name(s) the overlay never registers (would raise StageNotAvailable "
        f"in production): {sorted(cli_only)}"
    )
    assert not overlay_only, (
        f"the overlay registers stage name(s) cli.py never resolves (dead registration): "
        f"{sorted(overlay_only)}"
    )


def test_rung_plugins_entry_point_is_really_declared_and_loads() -> None:
    """End-to-end packaging guard: the overlay's ``rung.plugins`` entry point must be declared in the
    INSTALLED distribution metadata AND actually register the proprietary stages.

    The ``load_plugins`` tests above monkeypatch ``importlib.metadata.entry_points``, so they cover the
    discovery LOGIC but not the real ``[project.entry-points."rung.plugins"]`` stanza in the overlay's
    pyproject. A deleted/typo'd stanza or a renamed ``register_all`` would leave every one of those
    green while breaking the seam at runtime (every proprietary CLI verb → ``StageNotAvailable``). This
    exercises the UNMONKEYPATCHED path so that packaging regression can't slip through."""
    if not _PLUGIN_PATH.exists():
        pytest.skip("overlay absent — public-repo build")
    declared = {
        ep.name for ep in importlib.metadata.entry_points(group=registry.ENTRY_POINT_GROUP)
    }
    assert "intel" in declared, (
        f"the overlay's {registry.ENTRY_POINT_GROUP!r} entry point is not declared in the installed "
        f"metadata (check rung_intel/pyproject.toml); found {sorted(declared)}"
    )
    loaded = registry.load_plugins(force=True)  # REAL discovery — no monkeypatch
    assert "intel" in loaded
    # the entry point's registrar actually plugged the proprietary stages in (not just discovered)
    assert {"company_stores.run", "menus.run", "compare.run", "recon.run"} <= registry.registered_names()


def test_overlay_absent_cli_proprietary_command_degrades_end_to_end(monkeypatch) -> None:
    # Drive a proprietary verb through the real CLI with the overlay unplugged: it must surface
    # StageNotAvailable (with the install hint), not ImportError or a silent no-op.
    monkeypatch.setattr(registry.importlib_metadata, "entry_points", lambda group: [])
    monkeypatch.setattr(cli.db, "get_connection", lambda: object())  # never used (stage raises first)
    result = CliRunner().invoke(cli.bootstrap_dutchie_cmd, ["--state", "PA"])
    assert result.exit_code != 0
    assert isinstance(result.exception, registry.StageNotAvailable)
    assert "rung-intel" in str(result.exception)
