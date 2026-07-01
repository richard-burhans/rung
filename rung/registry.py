"""Plugin seam between the public pipeline shell and the proprietary stage implementations.

The open-source core ships the framework — the work queue, the Postgres layer, the
access-method *engine* (``access.run_target``), and the generic state-coverage extractors — plus
runnable stubs for the proprietary stages (Stage-2/3 scraping catalogs, the roster-comparison
intel, platform recon). Those proprietary stages live in the private ``dispensary_scraper_intel``
overlay and register their real implementations here via the ``rung.plugins``
entry-point group, discovered once at startup by :func:`load_plugins`.

Resolving an unplugged stage is always safe: :func:`resolve` returns a stub that raises
:class:`StageNotAvailable` (with an install hint) only when the stage is actually invoked, so the
public CLI can list and dispatch every verb whether or not the overlay is installed.

See docs/publish_split_design.md.
"""

import importlib.metadata as importlib_metadata
from collections.abc import Callable
from typing import Any

ENTRY_POINT_GROUP = "rung.plugins"

#: The pip distribution that provides the proprietary stages, named in the stub error so an
#: operator who hits an unplugged stage knows exactly what to install.
INTEL_DISTRIBUTION = "dispensary-scraper-intel"

StageImpl = Callable[..., Any]


class StageNotAvailable(RuntimeError):
    """Raised when an unplugged proprietary stage is invoked (the private overlay is absent)."""


_registry: dict[str, StageImpl] = {}
_plugins_loaded = False


def reset() -> None:
    """Drop all registrations and forget plugin discovery (for test isolation)."""
    global _plugins_loaded
    _registry.clear()
    _plugins_loaded = False


def register(name: str, impl: StageImpl, *, override: bool = True) -> None:
    """Register the implementation for a proprietary stage.

    A plugin's registrar calls this for each stage it provides. ``override`` defaults to True so
    a later plugin (or a re-run of :func:`load_plugins`) supersedes an earlier registration; pass
    ``override=False`` to assert that nothing has claimed ``name`` yet.
    """
    if not override and name in _registry:
        raise ValueError(f"stage {name!r} is already registered")
    _registry[name] = impl


def is_registered(name: str) -> bool:
    """Whether a real implementation has been registered for ``name`` (a stub does not count).

    Reserved seam-introspection API — exercised by the test suite; no production caller yet (the live
    path uses only :func:`load_plugins`/:func:`resolve`)."""
    return name in _registry


def registered_names() -> frozenset[str]:
    """The names with a real registered implementation (excludes resolve-only stubs).

    Reserved seam-introspection API (see :func:`is_registered`)."""
    return frozenset(_registry)


def _stub(name: str) -> StageImpl:
    """A placeholder for an unplugged stage: harmless to hold, informative when called."""

    def _unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise StageNotAvailable(
            f"stage {name!r} requires the proprietary plugin; install it with "
            f"`pip install {INTEL_DISTRIBUTION}` (provides the {ENTRY_POINT_GROUP} entry point)"
        )

    return _unavailable


def resolve(name: str) -> StageImpl:
    """Return the registered implementation for ``name``, or a stub if it is unplugged.

    Resolving never raises — the stub defers :class:`StageNotAvailable` until it is invoked — so
    the CLI can wire every verb regardless of whether the private overlay is installed.
    """
    return _registry.get(name) or _stub(name)


def load_plugins(*, force: bool = False) -> list[str]:
    """Discover and invoke every registrar in the ``rung.plugins`` group.

    Idempotent: a no-op after the first successful call unless ``force`` is set. Each entry point
    resolves to a zero-argument registrar that calls :func:`register` for the stages it provides.
    Returns the entry-point names that were loaded (empty when already loaded or none are present).
    """
    global _plugins_loaded
    if _plugins_loaded and not force:
        return []
    loaded: list[str] = []
    for entry_point in importlib_metadata.entry_points(group=ENTRY_POINT_GROUP):
        registrar = entry_point.load()
        registrar()
        loaded.append(entry_point.name)
    _plugins_loaded = True
    return loaded
