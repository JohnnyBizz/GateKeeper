"""Shared fixtures and deterministic series builders.

Tests use hand-built series with known shapes rather than random data wherever
the assertion is about behaviour, so a failure points at a real regression
instead of an unlucky seed.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poa.models import Candle, DataQuality, Series

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def build_series(
    closes,
    *,
    timeframe: int = 60,
    symbol: str = "TEST",
    wick: float = 0.0002,
    volume: float | None = 100.0,
) -> Series:
    """Build a series from a list of closes, with each open at the prior close."""
    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_price = previous
        high = max(open_price, close) + wick
        low = min(open_price, close) - wick
        candles.append(
            Candle(
                timestamp=START + timedelta(seconds=timeframe * index),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
        previous = close
    return Series(candles, timeframe, symbol)


def trending_series(
    n: int = 300,
    *,
    start: float = 1.08,
    step: float = 0.00012,
    noise: float = 0.00002,
    timeframe: int = 60,
) -> Series:
    """A clean, steadily rising (or falling, with a negative step) market."""
    closes = []
    price = start
    for i in range(n):
        # A small deterministic wobble keeps it from being a perfect straight
        # line, which would make several indicators degenerate.
        price += step + noise * math.sin(i / 3.0)
        closes.append(price)
    return build_series(closes, timeframe=timeframe)


def pullback_trend(
    n: int = 400,
    *,
    direction: int = 1,
    start: float = 1.08,
    impulse: float = 0.0022,
    retrace: float = 0.45,
    leg: int = 7,
    timeframe: int = 60,
) -> Series:
    """A realistic trend: impulse legs separated by partial retracements.

    ``trending_series`` never ticks against itself, which is degenerate — it
    produces no fractal swings at all and pins RSI at 100. Real trends breathe,
    and the analysis layer is built around that, so this is the fixture to use
    whenever the assertion is about trend *recognition*.
    """
    closes: list[float] = [start]
    price = start
    retrace_bars = max(2, leg // 2)

    def push(target: float, bars: int) -> float:
        nonlocal price
        for i in range(bars):
            closes.append(price + (target - price) * (i + 1) / bars)
        price = target
        return price

    # Full impulse/retrace cycles, then a final impulse so the series ends with
    # the trend resuming rather than mid-pullback. Where the series happens to
    # stop changes what the last Heikin Ashi candles look like, and a fixture
    # that always ends inside a retracement would be testing the wrong moment.
    while len(closes) < n - leg:
        push(price + direction * impulse, leg)
        push(price - direction * impulse * retrace, retrace_bars)
    push(price + direction * impulse, leg)

    return build_series(closes, timeframe=timeframe, wick=0.00008)


def ranging_series(
    n: int = 300, *, centre: float = 1.08, amplitude: float = 0.0008, timeframe: int = 60
) -> Series:
    """A market oscillating inside a band, with no net direction."""
    closes = [
        centre + amplitude * math.sin(i / 5.0) + amplitude * 0.35 * math.sin(i / 1.7)
        for i in range(n)
    ]
    return build_series(closes, timeframe=timeframe)


def choppy_series(n: int = 300, *, centre: float = 1.08, timeframe: int = 60) -> Series:
    """Violent alternation: maximum path, minimum progress."""
    closes = []
    for i in range(n):
        offset = 0.0009 if i % 2 == 0 else -0.0009
        closes.append(centre + offset + 0.0002 * math.sin(i / 2.3))
    return build_series(closes, timeframe=timeframe, wick=0.0006)


def good_quality(series: Series, source: str = "test") -> DataQuality:
    return DataQuality(
        ok=True,
        confidence=98.0,
        candle_count=len(series),
        issues=[],
        source=source,
    )


@pytest.fixture
def uptrend() -> Series:
    return trending_series(300, step=0.00012)


@pytest.fixture
def downtrend() -> Series:
    return trending_series(300, step=-0.00012)


@pytest.fixture
def rangebound() -> Series:
    return ranging_series(300)


@pytest.fixture
def chop() -> Series:
    return choppy_series(300)


@pytest.fixture
def tmp_journal(tmp_path):
    from poa.storage import Journal

    journal = Journal(tmp_path / "journal.db")
    yield journal
    journal.close()
