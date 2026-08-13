"""CSV replay source.

Feeds recorded candles to the engine one at a time, which is what powers both
the backtester and "replay yesterday's session" style review. The CSV needs a
header with at least ``timestamp,open,high,low,close``; ``volume`` is optional.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..models import Candle, Series
from .base import Capture, ChartSource, ChartSourceError
from .quality import validate_series

_REQUIRED = ("timestamp", "open", "high", "low", "close")


def load_csv(path: str | Path, timeframe_seconds: int | None = None,
             symbol: str = "UNKNOWN") -> Series:
    """Read a candle CSV into a ``Series``.

    The timeframe is inferred from the timestamps when not supplied, since a
    recorded file already tells us how far apart its candles are.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise ChartSourceError(f"CSV file not found: {file_path}")

    candles: list[Candle] = []
    with file_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ChartSourceError(f"{file_path} has no header row")
        missing = [c for c in _REQUIRED if c not in reader.fieldnames]
        if missing:
            raise ChartSourceError(
                f"{file_path} is missing required column(s): {', '.join(missing)}"
            )
        for line_number, row in enumerate(reader, start=2):
            try:
                candles.append(Candle.from_dict(row))
            except (ValueError, KeyError, TypeError) as exc:
                # Skip the bad row rather than aborting the whole file; the
                # quality check downstream will notice if too many are lost.
                raise ChartSourceError(
                    f"{file_path}:{line_number} could not be parsed: {exc}"
                ) from exc

    if not candles:
        raise ChartSourceError(f"{file_path} contains no candles")

    candles.sort(key=lambda c: c.timestamp)

    if timeframe_seconds is None:
        if len(candles) >= 2:
            gaps = [
                int((candles[i + 1].timestamp - candles[i].timestamp).total_seconds())
                for i in range(min(len(candles) - 1, 50))
            ]
            gaps = [g for g in gaps if g > 0]
            timeframe_seconds = min(gaps) if gaps else 60
        else:
            timeframe_seconds = 60

    return Series(candles, timeframe_seconds, symbol)


class CsvChartSource(ChartSource):
    """Replays a CSV file candle by candle."""

    name = "csv"
    vision_based = False

    def __init__(
        self,
        path: str | Path,
        *,
        symbol: str = "EUR/USD",
        timeframe_seconds: int | None = None,
        window: int = 400,
        start_index: int | None = None,
        loop: bool = True,
    ) -> None:
        self.series = load_csv(path, timeframe_seconds, symbol)
        self.symbol = symbol
        self.window = max(60, int(window))
        self.loop = loop
        self.path = Path(path)
        # Start with a full window already visible so the first analysis has
        # enough history to be meaningful.
        self._cursor = (
            int(start_index)
            if start_index is not None
            else min(self.window, len(self.series))
        )

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self.series)

    def capture(self) -> Capture:
        if self.exhausted:
            if not self.loop:
                return Capture(
                    series=None,
                    quality=validate_series(None, source="csv"),
                    asset=self.symbol,
                    timeframe_seconds=self.series.timeframe_seconds,
                    meta={"exhausted": True},
                )
            self._cursor = min(self.window, len(self.series))

        end = self._cursor
        start = max(0, end - self.window)
        visible = self.series[start:end]
        self._cursor += 1

        quality = validate_series(
            visible,
            min_candles=60,
            source="csv",
            recognition_confidence=100.0,
            expected_timeframe=self.series.timeframe_seconds,
        )
        return Capture(
            series=visible,
            quality=quality,
            asset=self.symbol,
            timeframe_seconds=self.series.timeframe_seconds,
            meta={
                "file": str(self.path),
                "position": end,
                "total": len(self.series),
            },
        )

    def describe(self) -> dict:
        return {
            "name": self.name,
            "vision_based": False,
            "file": str(self.path),
            "symbol": self.symbol,
            "timeframe_seconds": self.series.timeframe_seconds,
            "total_candles": len(self.series),
        }


def write_csv(series: Series, path: str | Path) -> Path:
    """Persist a series as CSV, for sample data and recorded sessions."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for candle in series:
            writer.writerow(
                [
                    candle.timestamp.astimezone(timezone.utc).isoformat(),
                    f"{candle.open:.6f}",
                    f"{candle.high:.6f}",
                    f"{candle.low:.6f}",
                    f"{candle.close:.6f}",
                    "" if candle.volume is None else f"{candle.volume:.0f}",
                ]
            )
    return target
