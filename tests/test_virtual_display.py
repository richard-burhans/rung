"""`rung.virtual_display` — the pure helpers run anywhere; starting a server needs Xvfb on the box."""

import os

import pytest


def test_virtual_display_rejects_bad_backend() -> None:
    from rung.virtual_display import virtual_display

    with pytest.raises(ValueError), virtual_display("wayland"):
        pass


def test_pick_free_display_skips_taken_numbers() -> None:
    from rung.virtual_display import pick_free_display

    n = pick_free_display(taken=set(range(99, 105)))
    assert 105 <= n <= 119


def test_xorg_config_uses_the_dummy_driver_and_geometry() -> None:
    from rung.virtual_display import xorg_config

    conf = xorg_config(1280, 800, 24)
    assert 'Driver "dummy"' in conf
    assert "VideoRam 256000" in conf
    assert "DefaultDepth 24" in conf
    assert "Modeline" in conf


def test_cvt_modeline_returns_a_label_and_modeline() -> None:
    from rung.virtual_display import cvt_modeline

    label, line = cvt_modeline(1280, 800)
    assert label and line.startswith("Modeline")
    assert label in line



def test_virtual_display_neutralises_wayland_and_restores_it(monkeypatch) -> None:
    """A Wayland session makes the virtual display a no-op: the browser ignores $DISPLAY entirely
    and runs on the real compositor, GPU and monitor. The context manager must clear the Wayland
    variables for the duration and put them back afterwards, because they are the caller's session.
    """
    from rung.virtual_display import missing_binaries, virtual_display

    if missing_binaries("xvfb"):
        pytest.skip("no Xvfb on this box")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("WAYLAND_SOCKET", "7")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    with virtual_display("xvfb") as env:
        # All three, because popping only WAYLAND_DISPLAY was tried on the host and did NOT work:
        # a client still reaches the compositor via WAYLAND_SOCKET or infers it from XDG_SESSION_TYPE.
        # This is Chromium's own `testing/xvfb.py` recipe, which this module re-implements.
        assert "WAYLAND_DISPLAY" not in os.environ, "a browser here would bypass the virtual display"
        assert "WAYLAND_SOCKET" not in os.environ, "an inherited socket fd reaches the compositor too"
        assert os.environ["XDG_SESSION_TYPE"] == "x11"
        assert os.environ["GDK_BACKEND"] == "x11"   # beyond Chromium's recipe, for a GTK browser
        assert os.environ["DISPLAY"] == env["DISPLAY"] != ":0"
        assert (env["width"], env["height"]) == ("1280", "800")
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-0", "the caller's session must be restored"
    assert os.environ["WAYLAND_SOCKET"] == "7"
    assert os.environ["XDG_SESSION_TYPE"] == "wayland"
    assert "GDK_BACKEND" not in os.environ
