"""Momentum measurement.

Momentum here means two separate things, and the distinction matters for the
duration engine: which way price is pushing, and whether that push is getting
stronger or fading. A strong-but-fading move and a weak-but-building move need
very different expirations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..indicators import IndicatorSnapshot
from ..indicators.core import slope
from ..models import Bias, Series


@dataclass
class MomentumReading:
    bias: Bias
    strength: float  # 0..1 magnitude of the push
    accelerating: bool
    decelerating: bool
    roc_atr: float  # recent rate of change, in ATR per candle
    label: str  # "strong" | "moderate" | "weak" | "flat"
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "bias": self.bias.value,
            "strength": round(self.strength, 3),
            "accelerating": self.accelerating,
            "decelerating": self.decelerating,
            "roc_atr": None if not np.isfinite(self.roc_atr) else round(self.roc_atr, 3),
            "label": self.label,
            "notes": list(self.notes),
        }


def analyze_momentum(
    series: Series, indicators: IndicatorSnapshot, lookback: int = 5
) -> MomentumReading:
    """Score the current directional push and whether it is building."""
    n = len(series)
    notes: list[str] = []
    if n < lookback + 2:
        return MomentumReading(
            bias=Bias.NEUTRAL,
            strength=0.0,
            accelerating=False,
            decelerating=False,
            roc_atr=float("nan"),
            label="flat",
            notes=["Not enough candles to measure momentum."],
        )

    closes = series.close
    atr_now = indicators.atr
    if not np.isfinite(atr_now) or atr_now <= 0:
        atr_now = float(np.mean(series.high[-20:] - series.low[-20:])) or 1.0

    # Rate of change over the lookback, expressed in ATR per candle so it is
    # comparable across assets and timeframes.
    move = float(closes[-1] - closes[-1 - lookback])
    roc_atr = move / atr_now / lookback

    # Compare the most recent half of the window against the earlier half.
    recent_move = abs(float(closes[-1] - closes[-1 - lookback // 2])) / max(lookback // 2, 1)
    earlier_move = abs(
        float(closes[-1 - lookback // 2] - closes[-1 - lookback])
    ) / max(lookback - lookback // 2, 1)

    accelerating = recent_move > earlier_move * 1.2
    decelerating = recent_move < earlier_move * 0.8

    magnitude = min(abs(roc_atr) / 0.5, 1.0)

    if roc_atr > 0.05:
        bias = Bias.BULLISH
    elif roc_atr < -0.05:
        bias = Bias.BEARISH
    else:
        bias = Bias.NEUTRAL

    # MACD histogram direction is an independent read on the same question;
    # when it disagrees, discount the magnitude rather than flipping the bias.
    if bias is not Bias.NEUTRAL and indicators.macd_bias is not Bias.NEUTRAL:
        if indicators.macd_bias is bias:
            magnitude = min(1.0, magnitude + 0.1)
            if indicators.macd_expanding:
                accelerating = True
        else:
            magnitude *= 0.7
            notes.append("MACD histogram disagrees with the raw price momentum.")

    ema_slope = slope(indicators.arrays.get("ema9", closes), lookback=lookback)
    if bias is Bias.BULLISH and ema_slope < 0:
        magnitude *= 0.8
    elif bias is Bias.BEARISH and ema_slope > 0:
        magnitude *= 0.8

    if magnitude >= 0.7:
        label = "strong"
    elif magnitude >= 0.4:
        label = "moderate"
    elif magnitude >= 0.15:
        label = "weak"
    else:
        label = "flat"
        bias = Bias.NEUTRAL if magnitude < 0.1 else bias

    if accelerating and bias is not Bias.NEUTRAL:
        notes.append("Momentum is increasing.")
    elif decelerating and bias is not Bias.NEUTRAL:
        notes.append("Momentum is fading.")

    return MomentumReading(
        bias=bias,
        strength=float(magnitude),
        accelerating=bool(accelerating and not decelerating),
        decelerating=bool(decelerating),
        roc_atr=roc_atr,
        label=label,
        notes=notes,
    )
