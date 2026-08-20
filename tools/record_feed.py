#!/usr/bin/env python3
"""Capture a sample of the platform's WebSocket traffic, safely.

GateKeeper records on its own now — the RECORD button on the panel does
exactly what this does, using the browser it is already attached to — so this
is the separate, scriptable way in rather than the only way in.

Double-click the packaged RecordFeed.exe, or from a checkout:

    python tools/record_feed.py --minutes 30      # a real market to measure against
    python tools/record_feed.py --minutes 120     # two hours, better still
    python tools/record_feed.py --seconds 60      # a protocol sample
    python tools/record_feed.py --attach          # use one already debugging

A one-minute run answers "what does the platform send". Anything longer is a
different job: it captures a real market to measure the engine against, and
the longer it runs the more it is worth. Frames stream to disk as they arrive,
so length is bounded by disk rather than memory.

A long run ends with one zip holding the candles and the summary — the two
things worth sending — while the raw frames, which are far larger and needed
by nobody, stay behind.

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
from poa.feed import BrowserError, launch_browser  # noqa: E402
from poa.feed.session_recording import (  # noqa: E402
    DEFAULT_MINUTES,
    LONG_RUN_SECONDS,
    coverage,
    record_session,
)
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
        # Double-clicked, so there is nowhere to have typed a flag. Ask, and
        # default to the length that is actually useful: a one-minute sample
        # answers what the platform sends, which is already known. Thirty
        # minutes is a real market, which is what the engine can be measured
        # against — so pressing Enter should give that rather than the sample.
        print("=" * 70)
        print("GateKeeper — feed recorder")
        print("=" * 70)
        print(
            "\nHow long should this record for?\n"
            "\n  30 minutes — a real market to measure the engine against.\n"
            "               This is the one to send. Press Enter for it.\n"
            "  1 minute   — a sample of what the platform sends. Only useful\n"
            "               for fixing how the protocol is read.\n"
            "  Longer     — better still. Hours beat minutes, and different\n"
            "               trading sessions beat one long stretch.\n"
        )
        try:
            answer = input("Minutes to record [30]: ").strip()
        except EOFError:
            answer = ""
        try:
            args.seconds = max(10.0, float(answer or DEFAULT_MINUTES) * 60.0)
        except ValueError:
            args.seconds = DEFAULT_MINUTES * 60.0
    long_run = args.seconds > LONG_RUN_SECONDS

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

    if long_run:
        print(
            f"\nRecording for {args.seconds / 60:.0f} minutes. Leave the chart\n"
            "open and leave this window running — the longer it goes, the more\n"
            "of a real market it captures. Nothing is sent and no trade is placed.\n"
        )
        # Said before the wait, not after it. A chart is only worth as many
        # candles as the run is long, and finding out at the end that the
        # timeframe you actually trade came to thirty bars is the one way this
        # wastes somebody's afternoon.
        print("What this length will produce, per timeframe:\n")
        print(coverage(args.seconds))
        print(
            "\nSwitch your chart's timeframe once while this runs "
            "(1 MIN → 5 MIN → back).\nThe platform sends its candle history "
            "when a timeframe changes, and that\nhistory is the only way a "
            "recording gets charts above a minute.\n"
        )
    else:
        print(f"\nRecording for {args.seconds:.0f} seconds. Leave the chart open.\n")

    # Progress in minutes rather than frames. Half an hour is long enough that
    # "1,412,000 frames…" tells you nothing about whether to keep waiting, and
    # how long is left does.
    def progress(elapsed: float, left: float, frames: int) -> None:
        if elapsed < 1.0:
            return
        print(
            f"  {elapsed / 60:.0f}m recorded, {left / 60:.0f}m to go "
            f"— {frames:,} frames",
            flush=True,
        )

    result = record_session(
        args.port,
        args.seconds,
        storage,
        needle=args.match,
        summary_path=args.out,
        on_progress=progress,
        progress_every=30.0,
    )

    if result.error and result.frames == 0:
        print(f"\n{result.error}")
        _pause()
        return 1
    if result.interrupted:
        print(
            f"\nStopped early — {result.frames:,} frames kept, which is still usable."
        )

    if result.report:
        print("\n" + result.report)

    size = result.sample_bytes / 1_048_576
    print(f"\n{result.frames:,} frames written to {result.sample}  ({size:.1f} MB)")

    if not long_run:
        summary = result.summary or (storage / "feed-summary.txt")
        print(f"A readable summary is in {summary}")
        print("Copy everything in that file and send it over.")
        _reveal(summary)
        _pause()
        return 0

    folder = storage / "candles"
    if result.candles:
        total = sum(path.stat().st_size for path in result.candles) / 1_048_576
        print(f"\n{len(result.candles)} chart(s) written to {folder}  ({total:.1f} MB)")
        for path in sorted(result.candles):
            print(f"   {path.name}")
        shortfall = result.shortfall()
        if shortfall:
            print(f"\n  ! {shortfall}")
    else:
        print(f"\n  ! {result.shortfall()}")

    # One file to send. The candles and the summary answer different questions
    # and both are wanted, but "attach this folder and also that text file" is
    # a step to get wrong at the end of a half-hour wait — and the raw frames
    # sitting beside them are far too large to send and must not be swept up
    # by mistake.
    if not result.candles:
        # Never "SEND THIS ONE FILE" over a bundle with no market in it. That
        # is how half an hour gets spent, sent on, and only then found empty.
        print("\n" + "=" * 70)
        print("NOTHING WORTH SENDING — this recording produced no charts.")
        print("=" * 70)
        _pause()
        return 1
    if result.bundle is not None:
        megabytes = result.bundle.stat().st_size / 1_048_576
        print("\n" + "=" * 70)
        print("SEND THIS ONE FILE:")
        print(f"\n   {result.bundle}   ({megabytes:.1f} MB)")
        print("\n" + "=" * 70)
        print(
            "\nIt holds the candles — the market itself, which is what the\n"
            "engine gets measured against — and the summary of what the\n"
            f"platform sends. The {size:.0f} MB of raw frames stays behind; it\n"
            "is not needed and can be deleted."
        )
        _reveal(result.bundle.parent)
    else:
        print(f"\nCould not build the bundle. Send {folder} and {result.summary}.")
        if result.summary is not None:
            _reveal(result.summary)
    _pause()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
