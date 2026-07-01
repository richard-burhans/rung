"""A minimal example overlay plugin for the rung public core.

The open-source core ships its heavier stages *unplugged* — ``scrape-company-stores``,
``scrape-menus``, ``compare-stores`` and friends resolve to registry stubs that politely raise
``StageNotAvailable`` until something provides them. This module shows how *you* provide one:
register an implementation under the stage name the CLI resolves, and the public CLI uses it. No
proprietary code is involved — these are teaching stubs that return obviously-fake sample data.

Two ways to use it:

1. **Directly** (what tests/test_example_plugin.py does)::

       from examples import example_plugin
       example_plugin.register()
       # now rung.registry.resolve("compare.run") is the demo below

2. **Auto-discovered**, the way the real overlay works: ship this in your own package and point the
   ``rung.plugins`` entry point at a ``register`` callable in your pyproject::

       [project.entry-points."rung.plugins"]
       my-overlay = "my_package.plugin:register"

   ``registry.load_plugins()`` (called once at CLI startup) then finds and runs it automatically.

The stage names + signatures are the contract between the CLI and a plugin; see
``rung/cli.py`` (the ``_stage(...)`` calls) for the full list.
"""

from rung import registry


def register() -> None:
    """Provide example implementations for the ``compare`` stage (one ``run`` + one ``print``)."""
    registry.register("compare.run", demo_compare_run)
    registry.register("compare.print", demo_compare_print)


def demo_compare_run(_conn: object, state: str) -> dict:
    """Stand-in for the roster comparison: a real overlay diffs each operator's own store list
    against the state roster; this just returns a tiny hand-made sample so the wiring is visible."""
    return {
        "state": state,
        "site_only": ["Sunnyside — Demo Ave"],     # on the operator's site, missing from the roster
        "roster_only": ["Closed Demo Co"],          # on the roster, but the operator no longer lists it
    }


def demo_compare_print(report: dict) -> None:
    """Render the sample report the way ``compare-stores`` would print a real one."""
    print(f"[{report['state']}] roster vs. own-site comparison (DEMO DATA):")
    print(f"  {len(report['site_only'])} store(s) the state roster is missing: "
          f"{', '.join(report['site_only'])}")
    print(f"  {len(report['roster_only'])} store(s) the roster still shows but the operator dropped: "
          f"{', '.join(report['roster_only'])}")
