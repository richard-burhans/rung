"""The example overlay plugin proves the public core is extensible by a third party: with the
proprietary overlay absent, a stage is an informative stub; registering a plugin impl makes the
registry resolve to it — no proprietary code required. See examples/example_plugin.py.
"""

import pytest

from rung import registry


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.reset()
    yield
    registry.reset()


def test_unplugged_stage_is_a_stub_until_a_plugin_provides_it() -> None:
    with pytest.raises(registry.StageNotAvailable):
        registry.resolve("compare.run")(None, "PA")


def test_example_plugin_plugs_a_stage_into_the_public_registry() -> None:
    from examples import example_plugin

    example_plugin.register()
    report = registry.resolve("compare.run")(None, "PA")
    assert report["state"] == "PA"
    assert report["site_only"] and report["roster_only"]
    registry.resolve("compare.print")(report)   # the print stage runs cleanly on the demo report
