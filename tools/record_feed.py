#!/usr/bin/env python3
"""Capture a sample of the platform's WebSocket traffic, safely.

Double-click the packaged RecordFeed.exe, or from a checkout:

    python tools/record_feed.py                   # start a browser and record
    python tools/record_feed.py --attach          # use one already debugging

Secrets are stripped before anything is written, so the summary this produces
is safe to share. Nothing is ever sent to the platform and no trade is placed:
this subscribes to the browser's own network events and listens.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poa.config import data_root  # noqa: E402
from poa.feed import BrowserError, launch_browser, record_platform  # noqa: E402
from poa.logging_setup import setup_logging  # noqa: E402

FROZEN = bool(getattr(sys, "frozen", False))


def _pause(message: str = "\nPress Enter to close this window... ") -> None:
    """Keep a double-clicked console window open long enough to read it."""
    if FROZEN:
        try:
            input(message)
        except EOFError:  # pragma: no cover - no console attached
            pass


def _reveal(path: Path) -> None:
    """Open the summary, so it can be copied without hunting for the file."""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
    except Exception:  # pragma: no cover - best effort
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument(
        "--attach",
        action="store_true",
        help="use a browser already started with a debugging port",
    )
    parser.add_argument("--match", default="pocketoption")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    storage = data_root() / "storage"
    setup_logging("INFO", storage / "feed.log")

    print("=" * 70)
    print("GateKeeper — feed recorder")
    print("=" * 70)
    print(
        "\nThis listens to the data your browser already receives from the\n"
        "platform, so GateKeeper can read exact prices instead of reading them\n"
        "off the screen. It sends nothing and places no trades.\n"
        "\nPasswords, session tokens and your balance are removed before\n"
        "anything is written to disk.\n"
    )

    if not args.attach:
        try:
            launch_browser(data_root() / "browser-profile", port=args.port)
        except BrowserError as exc:
            print(f"Could not start a browser with debugging enabled:\n  {exc}")
            _pause()
            return 1
        print(
            "A browser window has opened. In THAT window:\n"
            "  1. Sign in. It starts signed out the first time — it uses its own\n"
            "     profile, which is the thing that lets this work at all.\n"
            "  2. Open the chart you normally trade.\n"
        )
        try:
            input("Then come back here and press Enter to start recording... ")
        except EOFError:
            pass

    print(f"\nRecording for {args.seconds:.0f} seconds. Leave the chart open.\n")
    try:
        capture = record_platform(args.port, seconds=args.seconds, needle=args.match)
    except BrowserError as exc:
        print(f"\n{exc}")
        _pause()
        return 1

    lines = ["SOCKETS", "-" * 70]
    lines.extend(f"  {url}" for url in capture.sockets)
    lines += ["", "WHAT CAME THROUGH", "-" * 70, capture.summary.render()]
    report = "\n".join(lines)
    print("\n" + report)

    summary_path = args.out or (storage / "feed-summary.txt")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(report, encoding="utf-8")
    capture.write(storage / "feed-sample.jsonl")

    print(f"\nSaved to {summary_path}")
    print("Copy everything in that file and send it over.")
    _reveal(summary_path)
    _pause()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
