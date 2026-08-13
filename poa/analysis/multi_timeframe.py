"""Multi-timeframe reconciliation.

Three views are built from the same base candles:

* **higher** — sets the prevailing trend, and holds a veto over counter-trend
  entries unless a genuine reversal is confirmed;
* **current** — the chart the user is actually looking at, which decides
  market structure;
* **entry** — the fastest view available, which decides timing.

The reconciliation is deliberately conservative: agreement is rewarded, and
disagreement with the higher timeframe is treated as a reason to stand aside.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import Bias, Series
from .resample import resample
from .timeframe import TimeframeAnalysis, analyze_timeframe


@dataclass
class MultiTimeframeAnalysis:
    higher: TimeframeAnalysis
    current: TimeframeAnalysis
    entry: TimeframeAnalysis

    @property
    def views(self) -> tuple[TimeframeAnalysis, TimeframeAnalysis, TimeframeAnalysis]:
        return (self.higher, self.current, self.entry)

    @property
    def agreement(self) -> float:
        """0..1 — how much the three views agree on a single direction."""
        biases = [view.trend_bias for view in self.views]
        weights = [0.4, 0.35, 0.25]
        bull = sum(w for b, w in zip(biases, weights) if b is Bias.BULLISH)
        bear = sum(w for b, w in zip(biases, weights) if b is Bias.BEARISH)
        return round(max(bull, bear), 3)

    @property
    def consensus(self) -> Bias:
        """The direction the stack leans, or NEUTRAL when it is split."""
        biases = [view.trend_bias for view in self.views]
        weights = [0.4, 0.35, 0.25]
        bull = sum(w for b, w in zip(biases, weights) if b is Bias.BULLISH)
        bear = sum(w for b, w in zip(biases, weights) if b is Bias.BEARISH)
        if abs(bull - bear) < 0.15:
            return Bias.NEUTRAL
        return Bias.BULLISH if bull > bear else Bias.BEARISH

    def conflicts_with_higher(self, direction: Bias) -> bool:
        """Would trading ``direction`` fight the higher timeframe?"""
        higher_bias = self.higher.trend_bias
        if higher_bias is Bias.NEUTRAL or direction is Bias.NEUTRAL:
            return False
        return higher_bias is not direction

    def reversal_confirmed(self, direction: Bias) -> bool:
        """Is there enough evidence to trade against the higher timeframe?

        A counter-trend entry needs the higher timeframe itself to be showing
        exhaustion, not merely the lower timeframe looking attractive.
        """
        if direction is Bias.NEUTRAL:
            return False
        higher = self.higher
        current = self.current
        entry = self.entry

        higher_exhausted = (
            higher.regime.reversal_risk >= 0.5
            or higher.heikin_ashi.momentum_weakening
            or higher.heikin_ashi.bias is direction
        )
        current_flipped = (
            current.structure.bias is direction
            or current.structure.break_of_structure
            == ("bullish" if direction is Bias.BULLISH else "bearish")
        )
        entry_confirms = (
            entry.heikin_ashi.confirms(direction) and entry.momentum.bias is direction
        )
        return bool(higher_exhausted and current_flipped and entry_confirms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "higher": self.higher.to_dict(include_levels=False),
            "current": self.current.to_dict(include_levels=True),
            "entry": self.entry.to_dict(include_levels=False),
            "agreement": self.agreement,
            "consensus": self.consensus.value,
        }


def build_multi_timeframe(
    series: Series,
    higher_multiple: int = 5,
    entry_multiple: int = 1,
) -> MultiTimeframeAnalysis:
    """Analyse the chart timeframe plus a higher and an entry view.

    ``entry_multiple`` of 1 means the base series *is* the entry view, which is
    the normal case: the chart the user watches is already the fastest data we
    have. Resampling never invents faster candles.
    """
    base_tf = series.timeframe_seconds
    current = analyze_timeframe(series)

    higher_multiple = max(1, int(higher_multiple))
    higher_series = (
        resample(series, base_tf * higher_multiple) if higher_multiple > 1 else series
    )
    # Aggregation costs candles; if too few survive, fall back to the base view
    # rather than analysing a handful of bars.
    if len(higher_series) < 30 and higher_multiple > 1:
        for fallback in (3, 2):
            if fallback >= higher_multiple:
                continue
            candidate = resample(series, base_tf * fallback)
            if len(candidate) >= 30:
                higher_series = candidate
                break
        else:
            higher_series = series
    higher = current if higher_series is series else analyze_timeframe(higher_series)

    entry_multiple = max(1, int(entry_multiple))
    if entry_multiple > 1:
        entry_series = resample(series, base_tf * entry_multiple)
        entry = analyze_timeframe(entry_series) if len(entry_series) >= 30 else current
    else:
        entry = current

    return MultiTimeframeAnalysis(higher=higher, current=current, entry=entry)
