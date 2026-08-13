"""Full analysis of a single timeframe.

``analyze_timeframe`` is the unit of work everything else composes: the signal
engine runs it on the entry, current and higher timeframes and then reconciles
the three readings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..indicators import IndicatorSnapshot, compute_indicators
from ..models import Bias, Series, format_duration
from .heikin_ashi import HeikinAshiReading, analyze_heikin_ashi
from .levels import LevelsReading, detect_levels
from .momentum import MomentumReading, analyze_momentum
from .patterns import NO_PATTERN, Pattern, primary_pattern
from .regime import RegimeReading, classify_regime
from .structure import StructureReading, analyze_structure
from .volatility import VolatilityReading, analyze_volatility


@dataclass
class TimeframeAnalysis:
    """Everything the engine knows about one timeframe."""

    series: Series
    timeframe_seconds: int
    indicators: IndicatorSnapshot
    heikin_ashi: HeikinAshiReading
    structure: StructureReading
    levels: LevelsReading
    volatility: VolatilityReading
    momentum: MomentumReading
    regime: RegimeReading
    pattern: Pattern = NO_PATTERN

    @property
    def label(self) -> str:
        return format_duration(self.timeframe_seconds)

    @property
    def price(self) -> float:
        return self.indicators.price

    @property
    def trend_bias(self) -> Bias:
        """The timeframe's directional lean, weighing its several readings."""
        votes: dict[Bias, float] = {Bias.BULLISH: 0.0, Bias.BEARISH: 0.0}
        for bias, weight in (
            (self.structure.bias, 0.35 * max(self.structure.strength, 0.2)),
            (self.regime.bias, 0.25 * self.regime.confidence),
            (self.indicators.ema_alignment, 0.20 * max(self.indicators.ema_alignment_strength, 0.2)),
            (self.heikin_ashi.bias, 0.20 * max(self.heikin_ashi.strength, 0.2)),
        ):
            if bias in votes:
                votes[bias] += weight
        bull, bear = votes[Bias.BULLISH], votes[Bias.BEARISH]
        if abs(bull - bear) < 0.08:
            return Bias.NEUTRAL
        return Bias.BULLISH if bull > bear else Bias.BEARISH

    @property
    def trend_strength(self) -> float:
        """0-100 conviction in ``trend_bias``."""
        bias = self.trend_bias
        if bias is Bias.NEUTRAL:
            # Report how *undecided* it is rather than pretending to 0.
            return round(
                30.0 * (1.0 - min(self.volatility.directional_efficiency * 2, 1.0)) + 20.0,
                1,
            )
        agree = 0.0
        agree += 30.0 * self.structure.strength if self.structure.bias is bias else 0.0
        agree += 20.0 * self.regime.confidence if self.regime.bias is bias else 0.0
        agree += 15.0 * self.indicators.ema_alignment_strength if self.indicators.ema_alignment is bias else 0.0
        agree += 15.0 * self.heikin_ashi.strength if self.heikin_ashi.bias is bias else 0.0
        agree += 10.0 * self.indicators.trend_strength
        agree += 10.0 * min(self.volatility.directional_efficiency * 2.0, 1.0)
        return round(float(max(0.0, min(100.0, agree))), 1)

    def to_dict(self, include_levels: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "timeframe_seconds": self.timeframe_seconds,
            "label": self.label,
            "price": None if not np.isfinite(self.price) else round(self.price, 8),
            "trend": self.trend_bias.value,
            "strength": self.trend_strength,
            "candles": len(self.series),
            "indicators": self.indicators.to_dict(),
            "heikin_ashi": self.heikin_ashi.to_dict(),
            "structure": self.structure.to_dict(),
            "volatility": self.volatility.to_dict(),
            "momentum": self.momentum.to_dict(),
            "regime": self.regime.to_dict(),
            "pattern": self.pattern.to_dict(),
        }
        if include_levels:
            data["levels"] = self.levels.to_dict()
        return data


def analyze_timeframe(series: Series) -> TimeframeAnalysis:
    """Run the full analysis stack over one timeframe's candles."""
    indicators = compute_indicators(series)
    ha = analyze_heikin_ashi(series)
    structure = analyze_structure(series)
    levels = detect_levels(series)
    volatility = analyze_volatility(series, indicators)
    momentum = analyze_momentum(series, indicators)
    regime = classify_regime(series, indicators, structure, ha, volatility, levels)
    # Named purely for reporting — the wick and body evidence a pattern encodes
    # is already measured by the Heikin Ashi and structure components, so it is
    # not scored again here.
    pattern = primary_pattern(series)
    return TimeframeAnalysis(
        series=series,
        timeframe_seconds=series.timeframe_seconds,
        indicators=indicators,
        heikin_ashi=ha,
        structure=structure,
        levels=levels,
        volatility=volatility,
        momentum=momentum,
        regime=regime,
        pattern=pattern,
    )
