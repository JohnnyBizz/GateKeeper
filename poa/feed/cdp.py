"""Attaching to a browser through the Chrome DevTools Protocol.

This reads the data the platform's own page has already received, in the user's
own browser, on the user's own account — the same thing the Network tab in
DevTools shows. It sends nothing to the platform, places no trade, and touches
no account.

Why bother: the chart is drawn on a canvas from numbers that arrive over a
WebSocket. Reading pixels means reconstructing those numbers from a picture of
them, and every stage of that is inference that can fail on its own. The
numbers are right there.

A browser only speaks this protocol when it is started with a debugging port
open, and Chrome refuses to open one on your everyday profile. So the app
starts a second browser window against its own profile directory, which the
user signs into once.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_PORT = 9222


class BrowserError(RuntimeError):
    """Raised when no debuggable browser can be reached or started."""


@dataclass
class Target:
    """One debuggable page."""

    id: str
    title: str
    url: str
    websocket_url: str

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Target | None":
        websocket_url = raw.get("webSocketDebuggerUrl")
        if not websocket_url or raw.get("type") != "page":
            return None
        return cls(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            url=str(raw.get("url", "")),
            websocket_url=str(websocket_url),
        )


def list_targets(port: int = DEFAULT_PORT, timeout: float = 2.0) -> list[Target]:
    """Every debuggable page in the browser listening on ``port``."""
    url = f"http://127.0.0.1:{port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BrowserError(
            f"No browser is listening for debugging on port {port} ({exc})."
        ) from exc

    targets = [Target.from_json(item) for item in raw]
    return [target for target in targets if target is not None]


def pick_target(targets: list[Target], needle: str = "pocketoption") -> Target | None:
    """The page that looks like the trading platform."""
    needle = needle.lower()
    for target in targets:
        if needle in target.url.lower() or needle in target.title.lower():
            return target
    return None


# --------------------------------------------------------------------------
# Starting a browser that will talk to us


def _candidate_browsers() -> Iterator[Path]:
    """Where Chrome and Edge tend to live, per platform."""
    env_override = os.environ.get("GATEKEEPER_BROWSER")
    if env_override:
        yield Path(env_override)

    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(name)
        if found:
            yield Path(found)

    if os.name == "nt":
        roots = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        relative = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
        ]
        for root in roots:
            for tail in relative:
                if root:
                    yield Path(root) / tail
    else:
        yield Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        yield Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")


def find_browser() -> Path | None:
    for candidate in _candidate_browsers():
        if candidate.is_file():
            return candidate
    return None


def launch_browser(
    profile_dir: Path,
    url: str = "https://pocketoption.com/en/cabinet/demo-high-low/",
    port: int = DEFAULT_PORT,
    wait_seconds: float = 25.0,
) -> subprocess.Popen | None:
    """Start a browser with debugging open, and wait for it to answer.

    A dedicated profile directory is not optional: Chrome ignores
    ``--remote-debugging-port`` when it is reusing an already-running instance
    of your normal profile, which looks exactly like the flag being broken. The
    cost is that this window is signed out until the user signs in once.
    """
    executable = find_browser()
    if executable is None:
        raise BrowserError(
            "No Chrome, Chromium or Edge installation was found. Set "
            "GATEKEEPER_BROWSER to the browser's executable if it is installed "
            "somewhere unusual."
        )

    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    log.info("starting %s with debugging on port %d", executable.name, port)
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            list_targets(port)
            return process
        except BrowserError:
            if process.poll() is not None:
                raise BrowserError(
                    "The browser exited immediately. This usually means another "
                    "copy is already running on the same profile — close it, or "
                    "let GateKeeper use its own profile directory."
                )
            time.sleep(0.4)

    raise BrowserError(
        f"The browser started but never opened its debugging port ({port}) "
        f"within {wait_seconds:.0f}s."
    )
