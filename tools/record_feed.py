#!/usr/bin/env python3
"""Capture a sample of the platform's WebSocket traffic, safely.

    python tools/record_feed.py --launch          # start a browser and record
    python tools/record_feed.py --port 9222       # use one already running

Secrets are stripped before anything is written, so the summary this prints is
safe to paste into a message. Nothing is ever sent to the platform.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poa.config import data_root  # noqa: E402
from poa.feed import BrowserError, launch_browser, record_platform  # noqa: E402
from poa.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--launch", action="store_true", help="start the browser too")
    parser.add_argument("--match", default="pocketoption")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    setup_logging("INFO", data_root() / "storage" / "feed.log")

    if args.launch:
        try:
            launch_browser(data_root() / "browser-profile", port=args.port)
        except BrowserError as exc:
            print(f"Could not start a debuggable browser: {exc}")
            return 1
        print(
            "A browser window has opened. Sign in, open your chart, then leave "
            f"it alone for {args.seconds:.0f} seconds while this records.\n"
        )
        input("Press Enter once your chart is on screen... ")

    try:
        capture = record_platform(args.port, seconds=args.seconds, needle=args.match)
    except BrowserError as exc:
        print(f"\n{exc}")
        return 1

    print("\n" + "=" * 70)
    print("SOCKETS")
    print("=" * 70)
    for url in capture.sockets:
        print(f"  {url}")

    print("\n" + "=" * 70)
    print("WHAT CAME THROUGH")
    print("=" * 70)
    print(capture.summary.render())

    out = args.out or (data_root() / "storage" / "feed-sample.jsonl")
    capture.write(out)
    print(f"\nFull redacted capture written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
