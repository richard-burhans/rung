"""Shared pydoll Chrome browser configuration."""

import asyncio
import contextlib
import os
from pathlib import Path

from pydoll.browser.options import ChromiumOptions


def _resolve_chrome_path() -> Path | None:
    """Locate a Chromium binary without hardcoding a user/version-specific path.

    Order: an explicit ``DISPENSARY_CHROME_PATH`` override → Playwright's bundled Chromium
    under ``$PLAYWRIGHT_BROWSERS_PATH`` or ``~/.cache/ms-playwright``
    (``chromium-*/chrome-linux64/chrome``, any build) → None. When None, `make_browser_options`
    leaves `binary_location` unset and pydoll falls back to its own `/usr/bin/google-chrome`
    probe. Installed via `uv run playwright install chromium`.
    """
    override = os.environ.get("DISPENSARY_CHROME_PATH")
    if override and Path(override).is_file():
        return Path(override)
    roots: list[Path] = []
    playwright_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if playwright_root:
        roots.append(Path(playwright_root))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        candidates = sorted(root.glob("chromium-*/chrome-linux64/chrome"))
        if candidates:
            return candidates[-1]
    return None


# Resolved once at import; None if no bundled Chromium is found (pydoll then probes a system Chrome).
CHROME_PATH = _resolve_chrome_path()


def make_browser_options(headless: bool = True) -> ChromiumOptions:
    """Return ChromiumOptions configured for this environment.

    Sets an explicit `binary_location` only when a Chromium binary was resolved; otherwise
    pydoll probes for a system Chrome.
    """
    opts = ChromiumOptions()
    if CHROME_PATH is not None:
        opts.binary_location = str(CHROME_PATH)
    opts.headless = headless
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--disable-gpu")
    return opts


def get_script_value(result: dict) -> str | None:
    """Extract the string return value from a pydoll execute_script result dict."""
    try:
        return result["result"]["result"]["value"]
    except (KeyError, TypeError):
        return None


async def render_html(
    tab, url: str, wait: float = 3.5, nav_timeout: int = 25
) -> str:
    """Navigate the tab to url, let JS render, and return the full page HTML.

    go_to defaults to a 300s page-load timeout; many government pages keep a
    connection open (analytics, long-poll) so the load event never fires. We
    cap the wait and, even if it times out, read whatever DOM has rendered.
    Returns "" only when the DOM can't be read at all.
    """
    # load event may never fire; the DOM is usually still usable
    with contextlib.suppress(Exception):
        await tab.go_to(url, timeout=nav_timeout)
    try:
        await asyncio.sleep(wait)
        result = await asyncio.wait_for(
            tab.execute_script("document.documentElement.outerHTML"),
            timeout=nav_timeout,
        )
    except Exception:
        return ""
    return get_script_value(result) or ""
