"""Rebuild real candles from a recorded feed session.

Everything the tool knows about its own accuracy is measured against candles.
Where those candles come from therefore decides what the measurement is worth,
and until now the only market available offline was a generated one — a random
walk with some drift laid over it.

That is a harsher limit than it sounds. A random walk is unpredictable by
construction: past the drift there is nothing in it to find, so a score that
ranks setups perfectly and a score that ranks them by coin toss produce the
same number on it. Measuring "does the score separate winners from losers"
against generated data cannot answer the question either way.

Real recorded traffic can. ``RecordFeed`` already writes the platform's own
frames to disk with the secrets stripped; this reads them back through exactly
the parsing the live source uses, so the candles that come out are the candles
the tool would have seen, and every measurement that runs on them is a
measurement about the real market.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from ..logging_setup import get_logger
from ..models import Series
from .frames import AttachmentNamer
from .source import FeedChartSource

log = get_logger(__name__)


def read_frames(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield the frames of a recording, skipping anything unreadable.

    A recording is a log, not a database. One malformed line is a line, not a
    reason to lose the session around it.
    """
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                log.debug("line %d of the recording is not readable", number)


def charts_from_recording(
    path: str | Path,
    *,
    max_candles: int = 5000,
    min_candles: int = 60,
) -> list[tuple[str, int, Series]]:
    """Every chart a recording contains, as candles.

    The frames are replayed through the live source's own handler rather than
    through a second parser written for this purpose. Two parsers would be two
    answers to one question, and the one used offline would be the one nobody
    notices has drifted.
    """
    source = FeedChartSource(port=0, min_candles=min_candles, max_candles=max_candles)
    # The platform's larger messages arrive as a header naming the event and
    # then a payload with no name on it. Reading each frame on its own dropped
    # every one of those — including loadHistoryPeriodFast, which is the
    # platform's own candle history and the only source of anything at a
    # minute or longer. Recordings came back holding sub-minute charts and
    # nothing else, and the reason looked like the platform not sending it.
    # It had sent it: a hundred and fifty M1 candles, replayed as an anonymous
    # payload that matched no handler.
    namer = AttachmentNamer()
    seen = 0
    for frame in read_frames(path):
        direction = str(frame.get("direction") or "in")
        event = namer.name_for(frame.get("event"), frame.get("announces"), direction)
        payload = frame.get("payload")
        if payload is None:
            continue
        try:
            source._handle(event, payload, direction)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("frame %d could not be replayed: %s", seen, exc)
        seen += 1

    charts = [
        (asset, timeframe, series)
        for asset, timeframe, series in source.watched()
        if len(series) >= min_candles
    ]
    log.info(
        "replayed %d frames into %d chart%s",
        seen,
        len(charts),
        "" if len(charts) == 1 else "s",
    )
    return charts


def series_from_recording(
    path: str | Path,
    asset: str | None = None,
    timeframe: int | None = None,
    **kwargs: Any,
) -> Series | None:
    """One chart from a recording — the longest, or the one asked for."""
    charts = charts_from_recording(path, **kwargs)
    if asset is not None:
        charts = [c for c in charts if c[0] == asset]
    if timeframe is not None:
        charts = [c for c in charts if c[1] == timeframe]
    if not charts:
        return None
    return max(charts, key=lambda c: len(c[2]))[2]


def export_candles(
    charts: list[tuple[str, int, Series]], directory: str | Path
) -> list[Path]:
    """Write each chart as a candle CSV, and return what was written.

    This exists because of size. A couple of hours of raw frames is tens of
    megabytes — every heartbeat, every acknowledgement, every message the
    platform sends for reasons of its own — and none of that is what the
    engine gets measured against. The candles are, and the same two hours of
    them across every timeframe is a few hundred kilobytes.

    Written in the format ``--csv`` already reads, so an exported chart is
    replayable by the tools that exist rather than by a new one.
    """
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for asset, timeframe, series in charts:
        safe = "".join(c if c.isalnum() else "-" for c in asset).strip("-")
        path = folder / f"{safe}-{timeframe}s.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write("timestamp,open,high,low,close\n")
            for candle in series:
                handle.write(
                    f"{candle.timestamp.isoformat()},{candle.open},"
                    f"{candle.high},{candle.low},{candle.close}\n"
                )
        written.append(path)
        log.info("wrote %d candles to %s", len(series), path)
    return written
