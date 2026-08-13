"""Volatility and market-speed measurement.

The duration engine needs to know how fast the market is actually moving, not
just which way. These readings answer "how much ground does a candle cover, and
is that ground being covered in a straight line or in chop?"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..indicators import IndicatorSnapshot
from ..models import Series


@dataclass
class VolatilityReading:
    atr: float
    atr_percent: float  # ATR as % of price
    atr_percentile: float  # where current ATR sits in its own history
    regime: str  # "low" | "normal" | "elevated" | "extreme"
    expansion: float  # recent ATR / older ATR
    avg_candle_range: float
    avg_body: float
    directional_efficiency: float  # 0..1; net move / total path travelled
    consistency: float  # 0..1; share of candles agreeing with the net direction
    choppy: bool
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "atr": _num(self.atr),
            "atr_percent": _num(self.atr_percent, 4),
            "atr_percentile": _num(self.atr_percentile, 1),
            "regime": self.regime,
            "expansion": _num(self.expansion, 2),
            "avg_candle_range": _num(self.avg_candle_range),
            "avg_body": _num(self.avg_body),
            "directional_efficiency": _num(self.directional_efficiency, 3),
            "consistency": _num(self.consistency, 3),
            "choppy": self.choppy,
            "notes": list(self.notes),
        }


def _num(value: float, digits: int = 6) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), digits)


def analyze_volatility(
    series: Series, indicators: IndicatorSnapshot, lookback: int = 20
) -> VolatilityReading:
    """Measure how volatile, and how *efficient*, recent price action is."""
    n = len(series)
    window = min(lookback, n)
    notes: list[str] = []

    if window < 3:
        return VolatilityReading(
            atr=float("nan"),
            atr_percent=float("nan"),
            atr_percentile=50.0,
            regime="unknown",
            expansion=1.0,
            avg_candle_range=float("nan"),
            avg_body=float("nan"),
            directional_efficiency=0.0,
            consistency=0.0,
            choppy=True,
            notes=["Not enough candles to measure volatility."],
        )

    highs = series.high[-window:]
    lows = series.low[-window:]
    closes = series.close[-window:]
    opens = series.open[-window:]

    avg_range = float(np.mean(highs - lows))
    avg_body = float(np.mean(np.abs(closes - opens)))

    # Directional efficiency: net displacement divided by the total distance
    # walked. A clean trend approaches 1; chop approaches 0.
    path = float(np.sum(np.abs(np.diff(closes))))
    net = abs(float(closes[-1] - closes[0]))
    efficiency = float(net / path) if path > 0 else 0.0

    # Consistency: how many candles closed in the direction of the net move.
    net_sign = np.sign(closes[-1] - closes[0])
    if net_sign == 0:
        consistency = 0.0
    else:
        agreeing = np.sign(closes - opens) == net_sign
        consistency = float(np.mean(agreeing))

    atr_series = indicators.arrays.get("atr")
    expansion = 1.0
    if atr_series is not None:
        finite = atr_series[np.isfinite(atr_series)]
        if finite.size >= 10:
            recent = float(np.mean(finite[-5:]))
            older = float(np.mean(finite[-20:-5])) if finite.size >= 20 else float(np.mean(finite[:-5]))
            if older > 0:
                expansion = recent / older

    percentile = indicators.atr_percentile
    if not np.isfinite(percentile):
        percentile = 50.0

    if percentile >= 90:
        regime = "extreme"
        notes.append("Volatility is at the top of its recent range.")
    elif percentile >= 70:
        regime = "elevated"
        notes.append("Volatility is elevated versus recent candles.")
    elif percentile <= 20:
        regime = "low"
        notes.append("Volatility is compressed — moves may be slow to develop.")
    else:
        regime = "normal"

    if expansion > 1.4:
        notes.append("Ranges are expanding quickly.")
    elif expansion < 0.7:
        notes.append("Ranges are contracting.")

    choppy = bool(efficiency < 0.25 and consistency < 0.6)
    if choppy:
        notes.append("Price is covering ground without making progress — choppy.")

    return VolatilityReading(
        atr=indicators.atr,
        atr_percent=indicators.atr_percent,
        atr_percentile=percentile,
        regime=regime,
        expansion=expansion,
        avg_candle_range=avg_range,
        avg_body=avg_body,
        directional_efficiency=efficiency,
        consistency=consistency,
        choppy=choppy,
        notes=notes,
    )
