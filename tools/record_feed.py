#!/usr/bin/env python3
"""Capture a sample of the platform's WebSocket traffic, safely.

Double-click the packaged RecordFeed.exe, or from a checkout:

    python tools/record_feed.py                   # one minute, a protocol sample
    python tools/record_feed.py --minutes 120     # two hours of real market
    python tools/record_feed.py --attach          # use one already debugging

A one-minute run answers "what does the platform send". A long one is a
different job: it captures a real market to measure the engine against, and
the longer it runs the more it is worth. Frames stream to disk as they
arrive, so length is bounded by disk rather than memory.

Secrets are stripped before anything is written, so the summary this produces
is safe to share. Nothing is ever sent to the platform and no trade is placed:
this subscribes to the browser's own network events and listens.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

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
        "--minutes", type=float, default=None,
        help="record this long instead — use it for a real-market capture",
    )
    parser.add_argument(
        "--attach",
        action="store_true",
        help="use a browser already started with a debugging port",
    )
    parser.add_argument("--match", default="pocketoption")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if args.minutes is not None:
        args.seconds = args.minutes * 60.0
    elif FROZEN and args.seconds == 60.0:
        # Double-clicked, so there is nowhere to have typed a flag. Ask.
        print("=" * 70)
        print("GateKeeper — feed recorder")
        print("=" * 70)
        print(
            "\nHow long should this record for?\n"
            "\n  1 minute   — a sample of what the platform sends. Enough to fix\n"
            "               how GateKeeper reads the protocol.\n"
            "  Longer     — a real market to measure the engine against. Hours\n"
            "               are better than minutes, and across different\n"
            "               trading sessions is better still.\n"
        )
        try:
            answer = input("Minutes to record [1]: ").strip()
        except EOFError:
            answer = ""
        try:
            args.seconds = max(10.0, float(answer or 1) * 60.0)
        except ValueError:
            args.seconds = 60.0
    long_run = args.seconds > 300

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

    sample_path = storage / "feed-sample.jsonl"
    sample_path.parent.mkdir(parents=True, exist_ok=True)

    if long_run:
        print(
            f"\nRecording for {args.seconds / 60:.0f} minutes. Leave the chart\n"
            "open and leave this window running — the longer it goes, the more\n"
            "of a real market it captures. Nothing is sent and no trade is placed.\n"
        )
    else:
        print(f"\nRecording for {args.seconds:.0f} seconds. Leave the chart open.\n")

    written = 0
    try:
        with sample_path.open("w", encoding="utf-8") as sink:
            def keep(frame: Any) -> None:
                nonlocal written
                sink.write(json.dumps(frame.to_dict(), default=str) + "\n")
                written += 1
                # Something to watch, so a long run does not look like a hang.
                if written % 2000 == 0:
                    print(f"  {written:,} frames…", flush=True)

            capture = record_platform(
                args.port,
                seconds=args.seconds,
                needle=args.match,
                # Streaming to disk, so the only real bound is how long it runs.
                max_frames=10**9 if long_run else 4000,
                on_frame=keep,
            )
    except BrowserError as exc:
        print(f"\n{exc}")
        _pause()
        return 1
    except KeyboardInterrupt:
        print(f"\nStopped early — {written:,} frames kept, which is still usable.")
        capture = None

    summary_path = args.out or (storage / "feed-summary.txt")
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    if capture is not None:
        lines = ["SOCKETS", "-" * 70]
        lines.extend(f"  {url}" for url in capture.sockets)
        lines += ["", "WHAT CAME THROUGH", "-" * 70, capture.summary.render()]
        report = "\n".join(lines)
        print("\n" + report)
        summary_path.write_text(report, encoding="utf-8")

    size = sample_path.stat().st_size / 1_048_576 if sample_path.exists() else 0.0
    print(f"\n{written:,} frames written to {sample_path}  ({size:.1f} MB)")

    if long_run:
        # The frames are large and mostly not market data. The candles are the
        # market data, and the same recording as candles is a fraction of the
        # size — which is the difference between a file that can be sent and
        # one that cannot.
        from poa.feed.replay import charts_from_recording, export_candles

        print("\nBuilding candles from the recording…")
        try:
            charts = charts_from_recording(sample_path)
            folder = storage / "candles"
            paths = export_candles(charts, folder)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"Could not build candles: {exc}")
            paths, folder = [], storage / "candles"

        if paths:
            total = sum(path.stat().st_size for path in paths) / 1_048_576
            print(f"\n{len(paths)} chart(s) written to {folder}  ({total:.1f} MB)")
            for path in sorted(paths):
                print(f"   {path.name}")
        else:
            print(
                "\nNo chart had enough candles to export. A longer recording,"
                "\nwith a chart left open on the platform, is what this needs."
            )

        # Both files are worth having and they answer different questions, so
        # both get named. The summary says what the platform sends, which is
        # what fixes how GateKeeper reads it; the candles are the market
        # itself, which is what the engine gets measured against. A run of any
        # length produces both — there is no reason to mention only one.
        print("\nTWO THINGS TO SEND, and they are for different jobs:")
        if paths:
            print(f"\n  1. {folder}")
            print("     The market — prices and timestamps, nothing else.")
            print("     This is what the engine gets measured against.")
        print(f"\n  {2 if paths else 1}. {summary_path}")
        print("     What the platform sends, and in what shape.")
        print("     This is what fixes how GateKeeper reads it.")
        _reveal(summary_path)
        _pause()
        return 0
    else:
        print(f"A readable summary is in {summary_path}")
        print("Copy everything in that file and send it over.")
        _reveal(summary_path)
    _pause()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
