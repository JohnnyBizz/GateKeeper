"""One recording, from a live browser to a single file worth sending.

The steps have always existed — listen to the socket, replay the frames into
candles, zip up the half that is small enough to send — but they lived in the
command-line tool, which means they could only be run by downloading a second
executable. That executable is the one Windows refuses ("RecordFeed.exe —
couldn't download, virus detected"), and being refused a download is not
something a user can work around.

So the steps live here, where both callers can reach them: the tool, and
GateKeeper itself, which is already attached to the very browser a recording
has to be taken from and is already on the machine.

Nothing is sent to the platform and no trade is placed. Passwords, session
tokens and balances are stripped before anything reaches disk.
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..logging_setup import get_logger
from .cdp import BrowserError
from .recorder import record_platform

log = get_logger(__name__)

# What each job needs, in candles. Exporting a chart takes 60 — below that
# there is not enough to read. Measuring the engine against one takes 150,
# because a walk-forward needs a warm-up window before it can start stepping.
EXPORT_CANDLES = 60
MEASURE_CANDLES = 150

# The lengths the platform offers that a recording can produce directly.
RECORDED_TIMEFRAMES = (5, 10, 15, 30, 60, 300)

# Past this, a run is a market rather than a protocol sample, and is worth
# turning into candles and a bundle at the end.
LONG_RUN_SECONDS = 300.0

# What a double-click, or the panel's button, records unless told otherwise.
# Long enough to be a real market, short enough that nobody has to plan their
# afternoon around it.
DEFAULT_MINUTES = 30


@dataclass
class Recording:
    """What a recording produced, and where it left it."""

    frames: int = 0
    sample: Path | None = None
    summary: Path | None = None
    candles: list[Path] = field(default_factory=list)
    bundle: Path | None = None
    sockets: list[str] = field(default_factory=list)
    report: str = ""
    sample_bytes: int = 0
    #: Which chart lengths the recording could actually produce, in seconds.
    timeframes: list[int] = field(default_factory=list)
    #: Whether the platform handed over its candle history during the run.
    #: Without it a recording only holds what arrived live, which for a half
    #: hour means sub-minute charts and nothing else.
    history_seen: bool = False
    #: Set when the run could not be taken at all — no debuggable browser, no
    #: platform tab. The caller says so; nothing was written.
    error: str | None = None
    #: Set when the user stopped it early. Whatever was captured still counts.
    interrupted: bool = False

    @property
    def sendable(self) -> Path | None:
        """The one file worth attaching, if there is one."""
        return self.bundle

    def shortfall(self) -> str:
        """What this recording cannot answer, and what would fix it.

        A half-hour capture reliably produces the second charts, because ticks
        arrive continuously. It produces nothing at a minute or longer unless
        the platform hands over its history — thirty minutes is thirty M1
        bars, and sixty are needed before a chart can be read at all.

        The platform sends that history when a chart's timeframe changes. So
        the fix is one click, and worth knowing about before the wait rather
        than after it.
        """
        if not self.candles:
            return ""
        if max(self.timeframes, default=0) >= 60:
            return ""
        return (
            "Sub-minute charts only — the platform never sent its candle "
            "history, so there is nothing here at 1 MIN or longer. Next time, "
            "switch your chart's timeframe once while the recording runs "
            "(1 MIN → 5 MIN → back). That is what makes it hand the history "
            "over."
        )


def coverage(seconds: float) -> str:
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


def bundle(storage: Path, summary: Path | None, candles: list[Path]) -> Path | None:
    """Zip the sendable half of a recording into one file.

    Named for the moment it was taken, so several recordings do not overwrite
    each other — a second session on a different market is worth more than a
    longer one on the same market, and that only works if both survive.
    """
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    path = Path(storage) / f"gatekeeper-recording-{stamp}.zip"
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for candle_file in sorted(candles):
                archive.write(candle_file, f"candles/{candle_file.name}")
            if summary is not None and summary.exists():
                archive.write(summary, summary.name)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not write the bundle: %s", exc)
        return None
    return path


def record_session(
    port: int,
    seconds: float,
    storage: Path,
    *,
    needle: str = "pocketoption",
    summary_path: Path | None = None,
    on_progress: Callable[[float, float, int], None] | None = None,
    progress_every: float = 5.0,
) -> Recording:
    """Record the platform's traffic and leave one sendable file behind.

    ``on_progress`` is called with elapsed seconds, seconds remaining and the
    frame count, no more often than ``progress_every``. It runs on whichever
    thread is doing the recording, so a caller drawing a UI from it has to get
    the value back to its own thread rather than draw from here.
    """
    storage = Path(storage)
    storage.mkdir(parents=True, exist_ok=True)
    sample = storage / "feed-sample.jsonl"
    result = Recording(sample=sample)
    long_run = seconds > LONG_RUN_SECONDS

    started = time.monotonic()
    reported = started
    capture = None

    def announce(force: bool = False) -> None:
        nonlocal reported
        if on_progress is None:
            return
        now = time.monotonic()
        if not force and now - reported < progress_every:
            return
        reported = now
        elapsed = now - started
        on_progress(elapsed, max(0.0, seconds - elapsed), result.frames)

    announce(force=True)
    try:
        with sample.open("w", encoding="utf-8") as sink:
            def keep(frame: Any) -> None:
                sink.write(json.dumps(frame.to_dict(), default=str) + "\n")
                result.frames += 1
                announce()

            capture = record_platform(
                port,
                seconds=seconds,
                needle=needle,
                # Streaming to disk, so the only real bound is how long it runs.
                max_frames=10**9 if long_run else 4000,
                on_frame=keep,
            )
    except BrowserError as exc:
        result.error = str(exc)
        return result
    except KeyboardInterrupt:
        result.interrupted = True
    except OSError as exc:  # pragma: no cover - disk full, permissions
        result.error = f"Could not write the recording: {exc}"
        return result

    if sample.exists():
        result.sample_bytes = sample.stat().st_size

    if capture is not None:
        result.sockets = list(capture.sockets)
        # The event that carries the platform's own candle history. Whether it
        # arrived decides whether this recording holds anything above a
        # minute, so it is worth knowing rather than inferring from what came
        # out at the end.
        result.history_seen = any(
            "loadHistoryPeriodFast" in name for name in capture.summary.by_event
        )
        lines = ["SOCKETS", "-" * 70]
        lines.extend(f"  {url}" for url in capture.sockets)
        lines += ["", "WHAT CAME THROUGH", "-" * 70, capture.summary.render()]
        result.report = "\n".join(lines)
        summary = summary_path or (storage / "feed-summary.txt")
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text(result.report, encoding="utf-8")
        result.summary = summary

    if not long_run:
        return result

    # The frames are large and mostly not market data. The candles are the
    # market data, and the same recording as candles is a fraction of the
    # size — which is the difference between a file that can be sent and one
    # that cannot.
    from .replay import charts_from_recording, export_candles

    folder = storage / "candles"
    try:
        charts = charts_from_recording(sample)
        result.timeframes = sorted({timeframe for _, timeframe, _ in charts})
        result.candles = export_candles(charts, folder)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not build candles: %s", exc)
        result.error = f"Could not build candles: {exc}"

    # Built even with no candles: the summary alone still answers what the
    # platform sent, and a recording that produced nothing sendable should
    # say so by being empty rather than by being absent.
    result.bundle = bundle(storage, result.summary, result.candles)
    return result
