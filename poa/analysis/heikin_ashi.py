"""Heikin Ashi conversion and sequence analysis.

A single Heikin Ashi candle says very little. What carries information is the
*sequence*: how many candles have held their colour, whether the bodies are
growing or shrinking, and which side the wicks are appearing on. This module
reads the sequence and reports on it; it never issues a trade opinion on its
own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..models import Bias, Candle, Series


def heikin_ashi(series: Series) -> Series:
    """Convert an OHLC series into Heikin Ashi candles.

    HA close is the bar's average price; HA open is the running average of the
    previous HA bar, which is what produces the characteristic smoothing.
    """
    if len(series) == 0:
        return series

    out: list[Candle] = []
    prev_open: float | None = None
    prev_close: float | None = None

    for candle in series:
        ha_close = (candle.open + candle.high + candle.low + candle.close) / 4.0
        if prev_open is None or prev_close is None:
            ha_open = (candle.open + candle.close) / 2.0
        else:
            ha_open = (prev_open + prev_close) / 2.0
        ha_high = max(candle.high, ha_open, ha_close)
        ha_low = min(candle.low, ha_open, ha_close)
        out.append(
            Candle(
                timestamp=candle.timestamp,
                open=ha_open,
                high=ha_high,
                low=ha_low,
                close=ha_close,
                volume=candle.volume,
                complete=candle.complete,
            )
        )
        prev_open, prev_close = ha_open, ha_close

    return Series(out, series.timeframe_seconds, series.symbol)


@dataclass
class HeikinAshiReading:
    """What the recent Heikin Ashi sequence looks like."""

    bias: Bias
    strength: float  # 0..1 conviction in ``bias``
    streak: int  # consecutive same-colour candles
    body_trend: str  # "expanding" | "contracting" | "flat"
    avg_body_ratio: float
    flat_side: str  # "bullish" (no lower wick), "bearish" (no upper wick), "none"
    doji_like: bool
    momentum_weakening: bool
    color_changed: bool
    pattern: str
    notes: list[str]

    def confirms(self, bias: Bias) -> bool:
        """Does the sequence actively support ``bias``?

        A colour change against a fresh direction still counts, because that is
        exactly what an early reversal looks like — but only when the body is
        meaningful rather than doji-sized.
        """
        if bias is Bias.NEUTRAL:
            return False
        if self.bias is not bias:
            return False
        if self.doji_like and self.streak < 2:
            return False
        return self.strength >= 0.4

    def to_dict(self) -> dict[str, Any]:
        return {
            "bias": self.bias.value,
            "strength": round(self.strength, 3),
            "streak": self.streak,
            "body_trend": self.body_trend,
            "avg_body_ratio": round(self.avg_body_ratio, 3),
            "flat_side": self.flat_side,
            "doji_like": self.doji_like,
            "momentum_weakening": self.momentum_weakening,
            "color_changed": self.color_changed,
            "pattern": self.pattern,
            "notes": list(self.notes),
        }


def _streak(ha: Series) -> tuple[int, Bias]:
    """Length and colour of the current unbroken run of candles."""
    if len(ha) == 0:
        return 0, Bias.NEUTRAL
    last = ha[-1]
    if last.close == last.open:
        return 0, Bias.NEUTRAL
    bullish = last.bullish
    count = 0
    for candle in reversed(ha.candles):
        if candle.bullish == bullish and candle.close != candle.open:
            count += 1
        else:
            break
    return count, Bias.BULLISH if bullish else Bias.BEARISH


def analyze_heikin_ashi(series: Series, lookback: int = 8) -> HeikinAshiReading:
    """Read the recent Heikin Ashi sequence.

    ``lookback`` bounds how far back body/wick statistics are gathered; the
    streak itself is counted over the whole series.
    """
    ha = heikin_ashi(series)
    if len(ha) < 3:
        return HeikinAshiReading(
            bias=Bias.NEUTRAL,
            strength=0.0,
            streak=0,
            body_trend="flat",
            avg_body_ratio=0.0,
            flat_side="none",
            doji_like=True,
            momentum_weakening=False,
            color_changed=False,
            pattern="insufficient data",
            notes=["Not enough Heikin Ashi candles to read a sequence."],
        )

    window = ha.tail(min(lookback, len(ha)))
    candles = window.candles
    last = candles[-1]

    streak, streak_bias = _streak(ha)
    color_changed = streak == 1 and len(ha) >= 2

    bodies = np.array([c.body for c in candles], dtype=np.float64)
    ranges = np.array([c.range for c in candles], dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        body_ratios = np.where(ranges > 0, bodies / ranges, 0.0)
    avg_body_ratio = float(np.mean(body_ratios))

    # Body trend: compare the most recent three bodies against the prior ones,
    # normalised so that a change of scale in price does not distort it.
    recent = bodies[-3:]
    earlier = bodies[:-3] if bodies.size > 3 else bodies
    recent_mean = float(np.mean(recent)) if recent.size else 0.0
    earlier_mean = float(np.mean(earlier)) if earlier.size else 0.0
    if earlier_mean <= 0:
        body_trend = "flat"
    elif recent_mean > earlier_mean * 1.15:
        body_trend = "expanding"
    elif recent_mean < earlier_mean * 0.85:
        body_trend = "contracting"
    else:
        body_trend = "flat"

    # A textbook strong HA trend prints candles with a flat side: no lower wick
    # in an uptrend, no upper wick in a downtrend.
    tail_three = candles[-3:]
    scale = float(np.mean(ranges)) if float(np.mean(ranges)) > 0 else 1.0
    flat_lower = all(c.lower_wick <= scale * 0.08 for c in tail_three)
    flat_upper = all(c.upper_wick <= scale * 0.08 for c in tail_three)
    if flat_lower and not flat_upper:
        flat_side = "bullish"
    elif flat_upper and not flat_lower:
        flat_side = "bearish"
    else:
        flat_side = "none"

    doji_like = last.body_ratio < 0.25

    notes: list[str] = []
    if streak >= 2:
        colour = "bullish" if streak_bias is Bias.BULLISH else "bearish"
        notes.append(f"{streak} consecutive {colour} Heikin Ashi candles.")
    if color_changed:
        colour = "bullish" if streak_bias is Bias.BULLISH else "bearish"
        notes.append(f"Heikin Ashi just flipped {colour}.")
    if body_trend == "expanding":
        notes.append("Bodies are expanding, which signals building conviction.")
    elif body_trend == "contracting":
        notes.append("Bodies are contracting, which signals fading conviction.")
    if flat_side == "bullish":
        notes.append("Minimal lower wicks — buyers are not being pushed back.")
    elif flat_side == "bearish":
        notes.append("Minimal upper wicks — sellers are not being pushed back.")
    if doji_like:
        notes.append("Latest candle is doji-like; direction is indecisive.")

    # Wick rejection: a long wick against the streak direction is an early
    # warning that the move is being faded.
    rejection = "none"
    if last.range > 0:
        if last.upper_wick / last.range > 0.45:
            rejection = "upper"
            notes.append("Long upper wick — sellers rejected the highs.")
        elif last.lower_wick / last.range > 0.45:
            rejection = "lower"
            notes.append("Long lower wick — buyers defended the lows.")

    momentum_weakening = bool(
        streak >= 2
        and (
            body_trend == "contracting"
            or doji_like
            or (streak_bias is Bias.BULLISH and rejection == "upper")
            or (streak_bias is Bias.BEARISH and rejection == "lower")
        )
    )
    if momentum_weakening:
        notes.append("Heikin Ashi momentum is weakening within the current run.")

    # Strength: streak length, body quality, body trend and wick alignment.
    strength = 0.0
    if streak_bias is not Bias.NEUTRAL:
        strength += min(streak, 5) / 5.0 * 0.40
        strength += min(avg_body_ratio / 0.6, 1.0) * 0.25
        if body_trend == "expanding":
            strength += 0.15
        elif body_trend == "flat":
            strength += 0.05
        if (streak_bias is Bias.BULLISH and flat_side == "bullish") or (
            streak_bias is Bias.BEARISH and flat_side == "bearish"
        ):
            strength += 0.20
        if momentum_weakening:
            strength *= 0.6
        if doji_like:
            strength *= 0.7
    strength = float(max(0.0, min(1.0, strength)))

    pattern = _describe_pattern(
        streak, streak_bias, body_trend, momentum_weakening, color_changed, doji_like
    )

    return HeikinAshiReading(
        bias=streak_bias,
        strength=strength,
        streak=streak,
        body_trend=body_trend,
        avg_body_ratio=avg_body_ratio,
        flat_side=flat_side,
        doji_like=doji_like,
        momentum_weakening=momentum_weakening,
        color_changed=color_changed,
        pattern=pattern,
        notes=notes,
    )


def _describe_pattern(
    streak: int,
    bias: Bias,
    body_trend: str,
    weakening: bool,
    color_changed: bool,
    doji_like: bool,
) -> str:
    """A short human label for the sequence."""
    if bias is Bias.NEUTRAL or streak == 0:
        return "indecisive"
    side = "bullish" if bias is Bias.BULLISH else "bearish"
    if doji_like:
        return f"{side} but indecisive"
    if color_changed:
        return f"fresh {side} colour change"
    if weakening:
        return f"{side} run losing momentum"
    if streak >= 4 and body_trend == "expanding":
        return f"strong {side} continuation"
    if streak >= 2:
        return f"{side} continuation"
    return f"early {side}"
