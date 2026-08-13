"""A synthetic market source.

This exists so the whole application can be run, demonstrated and tested
without a screen, a browser or a broker. It is not a market model and makes no
claim to realism beyond producing candles with the statistical features the
analysis layer is meant to react to: trends, ranges, breakouts, pullbacks and
volatility clustering.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..models import Candle, DataQuality, Series
from .base import Capture, ChartSource
from .quality import validate_series

# Regimes the generator walks between, with their drift and volatility scale.
_PHASES = (
    ("trend_up", 1.0, 1.0),
    ("trend_down", -1.0, 1.0),
    ("range", 0.0, 0.7),
    ("volatile", 0.0, 2.2),
    ("pullback", -0.4, 0.9),
)


@dataclass
class SyntheticConfig:
    symbol: str = "EUR/USD"
    timeframe_seconds: int = 60
    start_price: float = 1.08000
    base_volatility: float = 0.00022
    history: int = 400
    max_candles: int = 600
    seed: int | None = None


class SyntheticChartSource(ChartSource):
    """Generates a continuous candle stream with shifting market phases."""

    name = "synthetic"
    vision_based = False

    def __init__(self, config: SyntheticConfig | None = None) -> None:
        self.config = config or SyntheticConfig()
        self._rng = random.Random(self.config.seed)
        self._candles: list[Candle] = []
        self._price = self.config.start_price
        self._phase_index = 0
        self._phase_remaining = 0
        self._clock = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(
            seconds=self.config.timeframe_seconds * self.config.history
        )
        self._warm_up()

    # ------------------------------------------------------------------

    def _warm_up(self) -> None:
        for _ in range(self.config.history):
            self._advance()

    def _next_phase(self) -> tuple[str, float, float]:
        if self._phase_remaining <= 0:
            self._phase_index = self._rng.randrange(len(_PHASES))
            self._phase_remaining = self._rng.randint(25, 70)
        self._phase_remaining -= 1
        return _PHASES[self._phase_index]

    def _advance(self) -> Candle:
        name, drift_sign, volatility_scale = self._next_phase()
        volatility = self.config.base_volatility * volatility_scale

        # Volatility clusters: nudge the effective volatility around slowly.
        volatility *= 1.0 + 0.3 * math.sin(len(self._candles) / 17.0)

        drift = drift_sign * volatility * 0.55
        open_price = self._price
        close_price = open_price + drift + self._rng.gauss(0.0, volatility)

        # Wicks are drawn separately so bodies and wicks vary independently,
        # which is what the Heikin Ashi reader keys off.
        wick_scale = volatility * (0.4 + self._rng.random() * 0.9)
        high = max(open_price, close_price) + abs(self._rng.gauss(0.0, wick_scale))
        low = min(open_price, close_price) - abs(self._rng.gauss(0.0, wick_scale))

        candle = Candle(
            timestamp=self._clock,
            open=round(open_price, 6),
            high=round(high, 6),
            low=round(low, 6),
            close=round(close_price, 6),
            volume=float(self._rng.randint(40, 600)),
            complete=True,
        )
        self._candles.append(candle)
        if len(self._candles) > self.config.max_candles:
            del self._candles[: len(self._candles) - self.config.max_candles]

        self._price = close_price
        self._clock += timedelta(seconds=self.config.timeframe_seconds)
        return candle

    # ------------------------------------------------------------------

    def capture(self) -> Capture:
        self._advance()
        series = Series(
            self._candles, self.config.timeframe_seconds, self.config.symbol
        )
        quality = validate_series(
            series,
            min_candles=60,
            source="synthetic",
            recognition_confidence=100.0,
            expected_timeframe=self.config.timeframe_seconds,
        )
        # Be explicit in the UI that this is not live market data.
        quality.issues.append(
            "Synthetic demo data — not a live market feed."
        )
        return Capture(
            series=series,
            quality=quality,
            asset=self.config.symbol,
            timeframe_seconds=self.config.timeframe_seconds,
            meta={"phase": _PHASES[self._phase_index][0], "demo": True},
        )

    def describe(self) -> dict:
        return {
            "name": self.name,
            "vision_based": False,
            "symbol": self.config.symbol,
            "timeframe_seconds": self.config.timeframe_seconds,
            "demo": True,
        }


def generate_series(
    count: int = 400,
    *,
    symbol: str = "EUR/USD",
    timeframe_seconds: int = 60,
    seed: int | None = 42,
    start_price: float = 1.08,
    base_volatility: float = 0.00022,
) -> Series:
    """Build a standalone series — used by the tests and the sample data tool."""
    source = SyntheticChartSource(
        SyntheticConfig(
            symbol=symbol,
            timeframe_seconds=timeframe_seconds,
            history=count,
            max_candles=max(count, 1),
            seed=seed,
            start_price=start_price,
            base_volatility=base_volatility,
        )
    )
    return Series(source._candles, timeframe_seconds, symbol)
