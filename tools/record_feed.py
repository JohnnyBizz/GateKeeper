#!/usr/bin/env python3
"""Capture a sample of the platform's WebSocket traffic, safely.

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
import json
import os
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poa.config import data_root  # noqa: E402
from poa.feed import BrowserError, launch_browser, record_platform  # noqa: E402
from poa.logging_setup import setup_logging  # noqa: E402

FROZEN = bool(getattr(sys, "frozen", False))

# What a double-click records unless told otherwise. Long enough to be a real
# market rather than a protocol sample, short enough that nobody has to plan
# their afternoon around it.
DEFAULT_MINUTES = 30


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


# What each job needs, in candles. Exporting a chart takes 60 — below that
# there is not enough to read. Measuring the engine against one takes 150,
# because a walk-forward needs a warm-up window before it can start stepping.
EXPORT_CANDLES = 60
MEASURE_CANDLES = 150

# The lengths the platform offers that a recording can produce directly.
RECORDED_TIMEFRAMES = (5, 10, 15, 30, 60, 300)


def _coverage(seconds: float) -> str:
    """What a run of this length will and will not be able to answer.

    Worth saying before the wait rather than after it. Half an hour sounds
    generous and is: for the second charts. It is thirty candles of M1, which
    is not a chart — so somebody expecting to have measured the timeframe they
    actually trade would find out at the end, having spent the half hour.
    """
    rows = []
    for timeframe in RECORDED_TIMEFRAMES:
        candles = int(seconds // timeframe)
        if candles >= MEASURE_CANDLES:
            verdict = f"{candles} candles — enough to measure the engine on"
        elif candles >= EXPORT_CANDLES:
            verdict = f"{candles} candles — enough to read, not to measure"
        else:
            needed = MEASURE_CANDLES * timeframe / 3600.0
            verdict = f"{candles} candles — too few; needs about {needed:.1f}h"
        label = f"{timeframe}s" if timeframe < 60 else f"{timeframe // 60}m"
        rows.append(f"    {label:>4}   {verdict}")
    return "\n".join(rows)


def _bundle(
    storage: Path, folder: Path, summary: Path, candles: list[Path]
) -> Path | None:
    """Zip the sendable half of a recording into one file.

    Named for the moment it was taken, so several recordings do not overwrite
    each other — a second session on a different market is worth more than a
    longer one on the same market, and that only works if both survive.
    """
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    path = storage / f"gatekeeper-recording-{stamp}.zip"
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for candle_file in sorted(candles):
                archive.write(candle_file, f"candles/{candle_file.name}")
            if summary.exists():
                archive.write(summary, summary.name)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"Could not write the bundle: {exc}")
        return None
    return path


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
            args.seconds = max(10.0, float(answer or 30) * 60.0)
        except ValueError:
            args.seconds = DEFAULT_MINUTES * 60.0
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
        # Said before the wait, not after it. A chart is only worth as many
        # candles as the run is long, and finding out at the end that the
        # timeframe you actually trade came to thirty bars is the one way this
        # wastes somebody's afternoon.
        print("What this length will produce, per timeframe:\n")
        print(_coverage(args.seconds))
        print()
    else:
        print(f"\nRecording for {args.seconds:.0f} seconds. Leave the chart open.\n")

    written = 0
    started = time.monotonic()
    reported = started
    try:
        with sample_path.open("w", encoding="utf-8") as sink:
            def keep(frame: Any) -> None:
                nonlocal written, reported
                sink.write(json.dumps(frame.to_dict(), default=str) + "\n")
                written += 1
                # Progress in minutes rather than frames. Half an hour is long
                # enough that "1,412,000 frames…" tells you nothing about
                # whether to keep waiting, and how long is left does.
                now = time.monotonic()
                if now - reported >= 30.0:
                    reported = now
                    left = max(0.0, args.seconds - (now - started))
                    print(
                        f"  {(now - started) / 60:.0f}m recorded, "
                        f"{left / 60:.0f}m to go — {written:,} frames",
                        flush=True,
                    )

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

        # One file to send. The candles and the summary answer different
        # questions and both are wanted, but "attach this folder and also that
        # text file" is a step to get wrong at the end of a half-hour wait —
        # and the raw frames sitting beside them are far too large to send and
        # must not be swept up by mistake.
        bundle = _bundle(storage, folder, summary_path, paths)
        if bundle is not None:
            megabytes = bundle.stat().st_size / 1_048_576
            print("\n" + "=" * 70)
            print("SEND THIS ONE FILE:")
            print(f"\n   {bundle}   ({megabytes:.1f} MB)")
            print("\n" + "=" * 70)
            print(
                "\nIt holds the candles — the market itself, which is what the\n"
                "engine gets measured against — and the summary of what the\n"
                f"platform sends. The {size:.0f} MB of raw frames stays behind; it\n"
                "is not needed and can be deleted."
            )
            _reveal(bundle.parent)
        else:
            print(f"\nCould not build the bundle. Send {folder} and {summary_path}.")
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
