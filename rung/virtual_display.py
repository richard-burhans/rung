"""Virtual X display for head-full browsers on a display-less box — Xvfb or Xorg + the dummy driver.

A browser that must run with a window (not ``--headless``) on a server, a container or CI needs an X
server to draw into. This re-implements the reusable pieces of Chromium's own test harness,
``testing/xvfb.py``: pick a free ``DISPLAY``, launch the X server, wait for it to accept connections
(``xdpyinfo``, not a blind sleep), start a window manager and a dbus session (a GLib hang otherwise),
and tear it all down on exit::

    from rung.virtual_display import missing_binaries, virtual_display

    if not missing_binaries("xvfb"):
        with virtual_display("xvfb", width=1920, height=1080) as env:
            ...  # launch the browser here; it draws on env["DISPLAY"]

Two backends: ``xvfb`` (Xvfb, the usual choice) and ``xorg`` (Xorg with the ``dummy`` video driver,
for software that needs a real Xorg server). The system binaries are NOT Python dependencies:
``Xvfb`` or ``Xorg`` + ``xserver-xorg-video-dummy``, plus ``xdpyinfo``; ``cvt``, ``dbus-launch`` and
``openbox`` are used when present. :func:`missing_binaries` says what is absent before you start.

BSD-referenced (Chromium is BSD-licensed); this is a re-implementation, not a copy.
"""

import contextlib
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

Backend = str  # "xvfb" | "xorg"

#: The environment this context manager owns for the duration — set or cleared on entry, restored on
#: exit. `WAYLAND_DISPLAY`/`GDK_BACKEND` are here because a Wayland session otherwise bypasses the
#: virtual display entirely (see :func:`virtual_display`).
_MANAGED_ENV = ("DISPLAY", "XVFB_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XDG_SESSION_TYPE", "GDK_BACKEND")

# Chromium's xvfb.py scans displays 99–119 (xvfb-run convention) and skips any with a lock file.
_DISPLAY_RANGE = range(99, 120)
_READY_TIMEOUT_S = 10.0
_READY_POLL_S = 0.2
_TERM_WAIT_S = 5.0


def _binaries_for(backend: Backend) -> list[str]:
    return (["Xvfb"] if backend == "xvfb" else ["Xorg"]) + ["xdpyinfo"]


def missing_binaries(backend: Backend) -> list[str]:
    """The required binaries for ``backend`` that are not on PATH (empty = ready to run)."""
    return [b for b in _binaries_for(backend) if shutil.which(b) is None]


def pick_free_display(taken: set[int] | None = None) -> int:
    """The first display number in 99–119 with no ``/tmp/.X<N>-lock`` (and not in ``taken``)."""
    taken = taken or set()
    for num in _DISPLAY_RANGE:
        if num in taken:
            continue
        if not Path(f"/tmp/.X{num}-lock").exists():
            return num
    raise RuntimeError("no free X display in 99–119")


def cvt_modeline(width: int, height: int) -> tuple[str, str]:
    """A `(label, "Modeline …")` pair from ``cvt`` for a resolution (falls back to a static 60 Hz line)."""
    with contextlib.suppress(Exception):
        out = subprocess.run(["cvt", str(width), str(height)], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.strip().startswith("Modeline"):
                label = line.split('"', 2)[1]
                return label, line.strip()
    label = f"{width}x{height}"
    # a plausible generic timing if cvt is unavailable; the dummy driver is not picky about exact pixel clocks
    return label, f'Modeline "{label}" 83.50 {width} {width+72} {width+200} {width+400} {height} {height+3} {height+9} {height+31} -hsync +vsync'


def xorg_config(width: int, height: int, depth: int) -> str:
    """An ``xorg.conf`` using the ``dummy`` driver at the given geometry (Chromium's recipe)."""
    label, modeline = cvt_modeline(width, height)
    return f"""Section "Device"
  Identifier "dummy"
  Driver "dummy"
  VideoRam 256000
EndSection
Section "Monitor"
  Identifier "mon"
  HorizSync 5.0-1000.0
  VertRefresh 5.0-200.0
  {modeline}
EndSection
Section "Screen"
  Identifier "screen"
  Device "dummy"
  Monitor "mon"
  DefaultDepth {depth}
  SubSection "Display"
    Depth {depth}
    Modes "{label}"
  EndSubSection
EndSection
"""


def _wait_ready(display: str, timeout: float) -> bool:
    """Poll ``xdpyinfo`` until the X server on ``display`` answers, or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = subprocess.run(["xdpyinfo", "-display", display], capture_output=True)
        if probe.returncode == 0:
            return True
        time.sleep(_READY_POLL_S)
    return False


def _start_dbus() -> tuple[str | None, int | None]:
    """Start a session ``dbus-daemon`` and return ``(address, pid)`` (the GLib-hang workaround)."""
    if shutil.which("dbus-launch") is None:
        return None, None
    with contextlib.suppress(Exception):
        out = subprocess.run(["dbus-launch", "--sh-syntax"], capture_output=True, text=True, timeout=5).stdout
        addr = re.search(r"DBUS_SESSION_BUS_ADDRESS='([^']*)'", out)
        pid = re.search(r"DBUS_SESSION_BUS_PID=(\d+)", out)
        return (addr.group(1) if addr else None), (int(pid.group(1)) if pid else None)
    return None, None


def _terminate(proc: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_TERM_WAIT_S)
            return
        proc.kill()


def _kill_pid(pid: int) -> None:
    import signal

    with contextlib.suppress(Exception):
        os.kill(pid, signal.SIGTERM)


@contextmanager
def virtual_display(backend: Backend, *, width: int = 1280, height: int = 800, depth: int = 24,
                    timeout: float = _READY_TIMEOUT_S) -> Iterator[dict[str, str]]:
    """Start ``backend`` (``xvfb`` or ``xorg``), export its env, and tear it down on exit.

    Sets :data:`_MANAGED_ENV` in ``os.environ`` for the duration so a browser launched inside the block
    runs head-full on the virtual display, then restores every one of them. That includes CLEARING
    ``WAYLAND_DISPLAY`` — without which a browser in a Wayland session ignores ``DISPLAY`` entirely and
    the virtual display is never used (see the comment at the assignment). Yields the display env dict,
    including the geometry, so the caller can verify the browser actually landed there.
    """
    if backend not in ("xvfb", "xorg"):
        raise ValueError(f"backend must be 'xvfb' or 'xorg', got {backend!r}")
    missing = missing_binaries(backend)
    if missing:
        raise RuntimeError(f"virtual display backend {backend!r} needs missing binaries: {', '.join(missing)}")

    display = f":{pick_free_display()}"
    procs: list[subprocess.Popen] = []
    conf_path: str | None = None
    dbus_pid: int | None = None
    saved = {k: os.environ.get(k) for k in _MANAGED_ENV}
    try:
        if backend == "xvfb":
            x = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", f"{width}x{height}x{depth}", "-ac", "-nolisten", "tcp",
                 "-dpi", "96", "+extension", "RANDR", "-maxclients", "512"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            with NamedTemporaryFile("w", suffix=".conf", delete=False, encoding="utf-8") as handle:
                handle.write(xorg_config(width, height, depth))
                conf_path = handle.name
            x = subprocess.Popen(
                ["Xorg", display, "-noreset", "-config", conf_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        procs.append(x)

        if not _wait_ready(display, timeout):
            raise RuntimeError(f"{backend} X server on {display} did not become ready within {timeout}s")

        os.environ["DISPLAY"] = display
        os.environ["XVFB_DISPLAY"] = display
        # ⚠ WAYLAND MUST BE NEUTRALISED OR THIS WHOLE CONTEXT MANAGER IS A NO-OP. A browser in a
        # Wayland session talks to the compositor and never reads `$DISPLAY`, so the X server started
        # above sits idle while the browser runs on the real one — real GPU, real monitor geometry —
        # and nothing fails: the run simply happened somewhere else. Observed on a desktop session:
        # a "virtual display" run reported the physical monitor's 5120x2160 and the machine's GPU.
        #
        # THESE THREE LINES ARE CHROMIUM'S, verbatim in effect, from the same `testing/xvfb.py` this
        # module re-implements:
        #
        #     # Do not let the host Wayland session make clients select Wayland instead
        #     # of the isolated X server.
        #     env.pop('WAYLAND_DISPLAY', None)
        #     env.pop('WAYLAND_SOCKET', None)
        #     env['XDG_SESSION_TYPE'] = 'x11'
        #
        # ⚠ POPPING ONLY `WAYLAND_DISPLAY` DOES NOT WORK: a client can still reach the compositor
        # through `WAYLAND_SOCKET` or infer it from `XDG_SESSION_TYPE`. All three, as Chromium does.
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ.pop("WAYLAND_SOCKET", None)
        os.environ["XDG_SESSION_TYPE"] = "x11"
        # Beyond Chromium's recipe: Chromium's harness drives Chrome, and a GTK browser (Firefox)
        # reads `GDK_BACKEND` instead — the GTK-level equivalent.
        os.environ["GDK_BACKEND"] = "x11"
        dbus_addr, dbus_pid = _start_dbus()
        if dbus_addr:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = dbus_addr

        with contextlib.suppress(Exception):  # a WM makes the env look real; best-effort
            procs.append(subprocess.Popen(["openbox", "--sm-disable"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        # The geometry is yielded so the caller can VERIFY the browser landed here (compare it with
        # the screen size the page reports). A browser that ignores `DISPLAY` (a Wayland session, a
        # driver with its own windowing path) still runs — just on the wrong screen.
        yield {"DISPLAY": display, "backend": backend, "DBUS_SESSION_BUS_ADDRESS": dbus_addr or "",
               "width": str(width), "height": str(height)}
    finally:
        for proc in reversed(procs):
            _terminate(proc)
        if dbus_pid is not None:
            _kill_pid(dbus_pid)
        if conf_path is not None:
            Path(conf_path).unlink(missing_ok=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
