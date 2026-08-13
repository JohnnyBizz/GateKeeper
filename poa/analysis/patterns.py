"""Named candlestick patterns.

The panel shows a pattern by name — "Pin Bar", "Engulfing" — because that is
what a trader recognises at a glance. This module finds and names them.

Two deliberate choices:

* **Everything is measured against ATR**, never against absolute price, so the
  same rules work on EUR/USD at 1.08 and on an index at 38,000.
* **Patterns do not add to the setup score.** A pin bar *is* a long wick and a
  small body, which the Heikin Ashi and structure components already measure;
  scoring it again would double-count the same evidence. Patterns are reported
  as context and as confirmation or warning text — they name what the score is
  already reacting to, rather than inflating it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..indicators.core import atr, last_finite
from ..models import Bias, Candle, Series


@dataclass(frozen=True)
class Pattern:
    """A named formation found at the end of the series.

    Frozen so a shared instance (``NO_PATTERN``) can safely be a default and be
    handed around without any caller mutating it.
    """

    name: str
    bias: Bias
    strength: float  # 0..1
    candles: int  # how many bars the formation spans
    description: str

    @property
    def label(self) -> str:
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bias": self.bias.value,
            "strength": round(self.strength, 3),
            "candles": self.candles,
            "description": self.description,
        }


# The formation reported when nothing else matches.
NO_PATTERN = Pattern(
    name="No pattern",
    bias=Bias.NEUTRAL,
    strength=0.0,
    candles=0,
    description="No classical candlestick formation on the most recent candles.",
)


def _scale(series: Series) -> float:
    """A volatility yardstick for the series, in price units."""
    value = last_finite(atr(series.high, series.low, series.close, 14))
    if np.isfinite(value) and value > 0:
        return float(value)
    spans = series.high - series.low
    mean = float(np.mean(spans)) if spans.size else 0.0
    return mean if mean > 0 else 1e-9


def _body_ratio(candle: Candle) -> float:
    return candle.body_ratio


def detect_patterns(series: Series) -> list[Pattern]:
    """Find every pattern completing on the most recent candle.

    Ordered strongest first. Only formations that *end* on the last candle are
    reported — a pin bar from twenty bars ago is history, not a signal.
    """
    if len(series) < 3:
        return []

    scale = _scale(series)
    candles = series.candles
    found: list[Pattern] = []

    for detector in (
        _marubozu,
        _engulfing,
        _pin_bar,
        _star,
        _three_in_a_row,
        _harami,
        _tweezer,
        _doji,
    ):
        pattern = detector(candles, scale)
        if pattern is not None:
            found.append(pattern)

    found.sort(key=lambda p: -p.strength)
    return found


def primary_pattern(series: Series) -> Pattern:
    """The single most significant formation, or ``NO_PATTERN``."""
    patterns = detect_patterns(series)
    return patterns[0] if patterns else NO_PATTERN


# --------------------------------------------------------------------------
# Individual detectors. Each returns None when the formation is not present.
# --------------------------------------------------------------------------


def _pin_bar(candles: tuple[Candle, ...], scale: float) -> Pattern | None:
    """A long rejection wick with a small body — hammer or shooting star.

    The wick must dominate the candle and the candle must be a meaningful size
    relative to recent volatility, otherwise every quiet bar qualifies.
    """
    last = candles[-1]
    if last.range <= 0 or last.range < scale * 0.6:
        return None

    body = last.body / last.range
    upper = last.upper_wick / last.range
    lower = last.lower_wick / last.range

    if body > 0.36:
        return None

    if lower >= 0.55 and upper <= 0.22:
        strength = min(1.0, lower * 0.9 + (last.range / scale) * 0.12)
        name = "Hammer" if last.bullish else "Pin Bar"
        return Pattern(
            name=name,
            bias=Bias.BULLISH,
            strength=strength,
            candles=1,
            description=(
                f"{name}: a long lower wick ({lower:.0%} of the candle) shows sellers "
                "pushed price down and buyers rejected it back up."
            ),
        )

    if upper >= 0.55 and lower <= 0.22:
        strength = min(1.0, upper * 0.9 + (last.range / scale) * 0.12)
        name = "Shooting Star" if last.bearish else "Pin Bar"
        return Pattern(
            name=name,
            bias=Bias.BEARISH,
            strength=strength,
            candles=1,
            description=(
                f"{name}: a long upper wick ({upper:.0%} of the candle) shows buyers "
                "pushed price up and sellers rejected it back down."
            ),
        )
    return None


def _engulfing(candles: tuple[Candle, ...], scale: float) -> Pattern | None:
    """The last body completely covers the previous body, in the other colour."""
    if len(candles) < 2:
        return None
    previous, last = candles[-2], candles[-1]

    if previous.body <= 0 or last.body < scale * 0.45:
        return None
    if last.body <= previous.body:
        return None

    last_top, last_bottom = max(last.open, last.close), min(last.open, last.close)
    prev_top, prev_bottom = max(previous.open, previous.close), min(
        previous.open, previous.close
    )
    if not (last_top >= prev_top and last_bottom <= prev_bottom):
        return None

    ratio = last.body / max(previous.body, 1e-12)
    strength = min(1.0, 0.5 + min(ratio - 1.0, 1.5) * 0.25 + _body_ratio(last) * 0.2)

    if last.bullish and previous.bearish:
        return Pattern(
            name="Bullish Engulfing",
            bias=Bias.BULLISH,
            strength=strength,
            candles=2,
            description=(
                "Bullish Engulfing: this candle's body completely covers the previous "
                "bearish body, so buyers took back the whole of the last bar's range."
            ),
        )
    if last.bearish and previous.bullish:
        return Pattern(
            name="Bearish Engulfing",
            bias=Bias.BEARISH,
            strength=strength,
            candles=2,
            description=(
                "Bearish Engulfing: this candle's body completely covers the previous "
                "bullish body, so sellers took back the whole of the last bar's range."
            ),
        )
    return None


def _doji(candles: tuple[Candle, ...], scale: float) -> Pattern | None:
    """Open and close almost equal — indecision, with no directional bias."""
    last = candles[-1]
    if last.range <= 0 or last.range < scale * 0.45:
        return None
    if _body_ratio(last) > 0.1:
        return None
    return Pattern(
        name="Doji",
        bias=Bias.NEUTRAL,
        strength=min(1.0, 0.4 + (last.range / scale) * 0.2),
        candles=1,
        description=(
            "Doji: the candle opened and closed at almost the same price. Neither "
            "side finished in control, which argues for waiting rather than entering."
        ),
    )


def _harami(candles: tuple[Candle, ...], scale: float) -> Pattern | None:
    """An inside bar: this candle's whole range sits within the previous one."""
    if len(candles) < 2:
        return None
    previous, last = candles[-2], candles[-1]
    if previous.range < scale * 0.7:
        return None
    if not (last.high <= previous.high and last.low >= previous.low):
        return None
    if last.range >= previous.range * 0.75:
        return None

    compression = 1.0 - (last.range / max(previous.range, 1e-12))
    return Pattern(
        name="Inside Bar",
        bias=Bias.NEUTRAL,
        strength=min(1.0, 0.35 + compression * 0.5),
        candles=2,
        description=(
            "Inside Bar: the entire candle sits within the previous candle's range. "
            "Volatility is contracting, which often precedes a breakout but does not "
            "say in which direction."
        ),
    )


def _marubozu(candles: tuple[Candle, ...], scale: float) -> Pattern | None:
    """A big body with almost no wicks — one side in control start to finish."""
    last = candles[-1]
    if last.range <= 0 or last.range < scale * 0.9:
        return None
    if _body_ratio(last) < 0.88:
        return None

    strength = min(1.0, 0.55 + (last.range / scale) * 0.2)
    if last.bullish:
        return Pattern(
            name="Bullish Marubozu",
            bias=Bias.BULLISH,
            strength=strength,
            candles=1,
            description=(
                "Bullish Marubozu: a full-bodied candle with almost no wicks — buyers "
                "held control from open to close."
            ),
        )
    return Pattern(
        name="Bearish Marubozu",
        bias=Bias.BEARISH,
        strength=strength,
        candles=1,
        description=(
            "Bearish Marubozu: a full-bodied candle with almost no wicks — sellers "
            "held control from open to close."
        ),
    )


def _star(candles: tuple[Candle, ...], scale: float) -> Pattern | None:
    """Morning or evening star: push, pause, reverse."""
    if len(candles) < 3:
        return None
    first, middle, last = candles[-3], candles[-2], candles[-1]

    if first.body < scale * 0.5 or last.body < scale * 0.5:
        return None
    if middle.body > first.body * 0.45:
        return None  # the middle candle must be the small "star"

    midpoint = (first.open + first.close) / 2.0

    if first.bearish and last.bullish and last.close > midpoint:
        return Pattern(
            name="Morning Star",
            bias=Bias.BULLISH,
            strength=min(1.0, 0.6 + _body_ratio(last) * 0.3),
            candles=3,
            description=(
                "Morning Star: a strong down candle, a small indecisive candle, then a "
                "strong up candle closing back above the midpoint of the first — the "
                "classical three-bar bottoming sequence."
            ),
        )
    if first.bullish and last.bearish and last.close < midpoint:
        return Pattern(
            name="Evening Star",
            bias=Bias.BEARISH,
            strength=min(1.0, 0.6 + _body_ratio(last) * 0.3),
            candles=3,
            description=(
                "Evening Star: a strong up candle, a small indecisive candle, then a "
                "strong down candle closing back below the midpoint of the first — the "
                "classical three-bar topping sequence."
            ),
        )
    return None


def _three_in_a_row(candles: tuple[Candle, ...], scale: float) -> Pattern | None:
    """Three white soldiers / three black crows — sustained one-sided pressure."""
    if len(candles) < 3:
        return None
    trio = candles[-3:]

    if all(c.bullish and c.body >= scale * 0.4 and _body_ratio(c) >= 0.55 for c in trio):
        if trio[0].close < trio[1].close < trio[2].close:
            return Pattern(
                name="Three White Soldiers",
                bias=Bias.BULLISH,
                strength=min(1.0, 0.6 + float(np.mean([_body_ratio(c) for c in trio])) * 0.35),
                candles=3,
                description=(
                    "Three White Soldiers: three consecutive strong bullish candles, "
                    "each closing higher than the last."
                ),
            )
    if all(c.bearish and c.body >= scale * 0.4 and _body_ratio(c) >= 0.55 for c in trio):
        if trio[0].close > trio[1].close > trio[2].close:
            return Pattern(
                name="Three Black Crows",
                bias=Bias.BEARISH,
                strength=min(1.0, 0.6 + float(np.mean([_body_ratio(c) for c in trio])) * 0.35),
                candles=3,
                description=(
                    "Three Black Crows: three consecutive strong bearish candles, "
                    "each closing lower than the last."
                ),
            )
    return None


def _tweezer(candles: tuple[Candle, ...], scale: float) -> Pattern | None:
    """Two candles rejecting from almost exactly the same high or low."""
    if len(candles) < 2:
        return None
    previous, last = candles[-2], candles[-1]
    tolerance = scale * 0.08

    if (
        abs(previous.low - last.low) <= tolerance
        and previous.bearish
        and last.bullish
        and last.range >= scale * 0.5
    ):
        return Pattern(
            name="Tweezer Bottom",
            bias=Bias.BULLISH,
            strength=0.55,
            candles=2,
            description=(
                "Tweezer Bottom: two candles bottomed at almost the same price and the "
                "second closed up — a level being defended."
            ),
        )
    if (
        abs(previous.high - last.high) <= tolerance
        and previous.bullish
        and last.bearish
        and last.range >= scale * 0.5
    ):
        return Pattern(
            name="Tweezer Top",
            bias=Bias.BEARISH,
            strength=0.55,
            candles=2,
            description=(
                "Tweezer Top: two candles topped at almost the same price and the "
                "second closed down — a level being defended."
            ),
        )
    return None
