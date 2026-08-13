"""Market regime classification.

The regime is decided *before* any directional scoring, and it can veto a trade
outright. An UNCLEAR or HIGH_VOLATILITY regime means WAIT regardless of how
attractive the individual components look.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..indicators import IndicatorSnapshot
from ..models import Bias, Regime, Series
from .heikin_ashi import HeikinAshiReading
from .levels import LevelsReading
from .structure import StructureReading
from .volatility import VolatilityReading


@dataclass
class RegimeReading:
    regime: Regime
    confidence: float  # 0..1 in the classification itself
    breakout: bool
    reversal_risk: float  # 0..1
    notes: list[str]

    @property
    def bias(self) -> Bias:
        return self.regime.bias

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime.value,
            "label": self.regime.label,
            "bias": self.bias.value,
            "confidence": round(self.confidence, 3),
            "tradeable": self.regime.tradeable,
            "breakout": self.breakout,
            "reversal_risk": round(self.reversal_risk, 3),
            "notes": list(self.notes),
        }


def classify_regime(
    series: Series,
    indicators: IndicatorSnapshot,
    structure: StructureReading,
    ha: HeikinAshiReading,
    volatility: VolatilityReading,
    levels: LevelsReading,
) -> RegimeReading:
    """Decide which regime the market is in right now."""
    notes: list[str] = []
    adx_value = indicators.adx if np.isfinite(indicators.adx) else 0.0
    trending = adx_value >= 22
    strongly_trending = adx_value >= 30

    # --- reversal pressure ------------------------------------------------
    reversal_risk = 0.0
    if ha.momentum_weakening:
        reversal_risk += 0.3
    if ha.color_changed and structure.bias is not Bias.NEUTRAL and ha.bias is not structure.bias:
        reversal_risk += 0.25
    if structure.bias is Bias.BULLISH and structure.break_of_structure == "bearish":
        reversal_risk += 0.3
    if structure.bias is Bias.BEARISH and structure.break_of_structure == "bullish":
        reversal_risk += 0.3
    if np.isfinite(indicators.rsi):
        if structure.bias is Bias.BULLISH and indicators.rsi > 75:
            reversal_risk += 0.15
            notes.append("RSI is stretched into overbought territory.")
        elif structure.bias is Bias.BEARISH and indicators.rsi < 25:
            reversal_risk += 0.15
            notes.append("RSI is stretched into oversold territory.")

    # Price pressing into a strong opposing level adds reversal pressure.
    if structure.bias is Bias.BULLISH and levels.nearest_resistance is not None:
        distance = levels.distance_in_atr(levels.nearest_resistance)
        if distance is not None and distance < 0.5 and levels.nearest_resistance.strength >= 70:
            reversal_risk += 0.2
            notes.append("Price is pressed against major resistance.")
    if structure.bias is Bias.BEARISH and levels.nearest_support is not None:
        distance = levels.distance_in_atr(levels.nearest_support)
        if distance is not None and distance < 0.5 and levels.nearest_support.strength >= 70:
            reversal_risk += 0.2
            notes.append("Price is pressed against major support.")

    reversal_risk = float(min(1.0, reversal_risk))

    # --- breakout detection -----------------------------------------------
    breakout = False
    if (
        structure.break_of_structure != "none"
        and volatility.expansion > 1.25
        and volatility.directional_efficiency > 0.35
    ):
        breakout = True
        notes.append(
            f"Structure broke to the {structure.break_of_structure} side on expanding range."
        )

    # --- classification ----------------------------------------------------
    # Order matters: data-quality style vetoes first, then trend, then range.
    if volatility.regime == "extreme" and volatility.directional_efficiency < 0.4:
        notes.append("Volatility is extreme without a clean direction.")
        return RegimeReading(
            regime=Regime.HIGH_VOLATILITY,
            confidence=0.75,
            breakout=breakout,
            reversal_risk=max(reversal_risk, 0.5),
            notes=notes,
        )

    if volatility.choppy and not trending:
        notes.append("Directional efficiency is poor and ADX is flat.")
        return RegimeReading(
            regime=Regime.UNCLEAR,
            confidence=0.7,
            breakout=False,
            reversal_risk=reversal_risk,
            notes=notes,
        )

    if reversal_risk >= 0.6:
        notes.append("Multiple reversal cues are lining up against the prevailing trend.")
        return RegimeReading(
            regime=Regime.POTENTIAL_REVERSAL,
            confidence=0.65,
            breakout=breakout,
            reversal_risk=reversal_risk,
            notes=notes,
        )

    if breakout:
        return RegimeReading(
            regime=Regime.BREAKOUT,
            confidence=0.7,
            breakout=True,
            reversal_risk=reversal_risk,
            notes=notes,
        )

    # Structure is the primary read, but it needs confirmed swings on both
    # sides to say anything. A trend that runs without pulling back prints
    # swing highs and no swing lows, which leaves structure with no opinion —
    # exactly the case where the trend is strongest. Fall back to the EMA
    # stack when that happens, gated on the move being genuinely directional
    # so the fallback cannot rescue chop.
    directional_bias = structure.bias
    structural_strength = structure.strength
    if directional_bias is Bias.NEUTRAL and trending:
        if (
            indicators.ema_alignment is not Bias.NEUTRAL
            and volatility.directional_efficiency > 0.5
        ):
            directional_bias = indicators.ema_alignment
            # Held below the "strong structure" bar: this is a weaker form of
            # evidence than a confirmed swing sequence, and it should not on
            # its own promote a regime to STRONG.
            structural_strength = min(0.55, indicators.ema_alignment_strength)
            notes.append(
                "Trend has not pulled back enough to print swing lows; "
                "direction taken from the EMA stack and price efficiency."
            )

    aligned_bull = (
        directional_bias is Bias.BULLISH
        and indicators.ema_alignment is not Bias.BEARISH
        and volatility.directional_efficiency > 0.25
    )
    aligned_bear = (
        directional_bias is Bias.BEARISH
        and indicators.ema_alignment is not Bias.BULLISH
        and volatility.directional_efficiency > 0.25
    )

    if aligned_bull:
        if strongly_trending and structural_strength >= 0.6 and ha.bias is Bias.BULLISH:
            notes.append("Trend, structure and Heikin Ashi all point up with strong ADX.")
            return RegimeReading(Regime.STRONG_UPTREND, 0.85, breakout, reversal_risk, notes)
        notes.append("Uptrend in place but without full confirmation.")
        return RegimeReading(Regime.WEAK_UPTREND, 0.6, breakout, reversal_risk, notes)

    if aligned_bear:
        if strongly_trending and structural_strength >= 0.6 and ha.bias is Bias.BEARISH:
            notes.append("Trend, structure and Heikin Ashi all point down with strong ADX.")
            return RegimeReading(Regime.STRONG_DOWNTREND, 0.85, breakout, reversal_risk, notes)
        notes.append("Downtrend in place but without full confirmation.")
        return RegimeReading(Regime.WEAK_DOWNTREND, 0.6, breakout, reversal_risk, notes)

    if structure.consolidating or levels.in_range or (not trending and volatility.regime != "extreme"):
        if volatility.regime == "low":
            notes.append("Range-bound with compressed volatility.")
            return RegimeReading(Regime.LOW_VOLATILITY, 0.6, False, reversal_risk, notes)
        notes.append("Price is rotating inside a range rather than trending.")
        return RegimeReading(Regime.RANGE, 0.65, False, reversal_risk, notes)

    notes.append("Signals do not agree on a regime.")
    return RegimeReading(Regime.UNCLEAR, 0.5, breakout, reversal_risk, notes)
