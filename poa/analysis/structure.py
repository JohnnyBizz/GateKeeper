"""Market structure: swing points, HH/HL/LH/LL and trend classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from ..indicators.core import slope
from ..models import Bias, Series

SwingKind = Literal["high", "low"]


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    kind: SwingKind

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "price": self.price, "kind": self.kind}


def find_swings(series: Series, left: int = 2, right: int = 2) -> list[Swing]:
    """Fractal swing detection.

    A swing high is a bar whose high is not exceeded by the ``left`` bars
    before it or the ``right`` bars after it (and vice versa for swing lows).
    Confirmation costs ``right`` bars of lag, which is the price of not
    repainting.
    """
    highs = series.high
    lows = series.low
    n = highs.size
    swings: list[Swing] = []
    if n < left + right + 1:
        return swings

    for i in range(left, n - right):
        window_high = highs[i - left : i + right + 1]
        if highs[i] == window_high.max() and (window_high.argmax() == left):
            swings.append(Swing(index=i, price=float(highs[i]), kind="high"))
            continue
        window_low = lows[i - left : i + right + 1]
        if lows[i] == window_low.min() and (window_low.argmin() == left):
            swings.append(Swing(index=i, price=float(lows[i]), kind="low"))

    return swings


@dataclass
class StructureReading:
    """The market-structure picture for one timeframe."""

    bias: Bias
    strength: float  # 0..1
    label: str  # e.g. "higher highs / higher lows"
    swings: list[Swing]
    higher_highs: bool
    higher_lows: bool
    lower_highs: bool
    lower_lows: bool
    break_of_structure: str  # "bullish" | "bearish" | "none"
    consolidating: bool
    notes: list[str]
    # False when there were too few confirmed swings to say anything at all.
    # A trend that runs without pulling back produces exactly this, so callers
    # must distinguish "no opinion" from "opinion: neutral".
    has_swing_history: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "bias": self.bias.value,
            "strength": round(self.strength, 3),
            "label": self.label,
            "higher_highs": self.higher_highs,
            "higher_lows": self.higher_lows,
            "lower_highs": self.lower_highs,
            "lower_lows": self.lower_lows,
            "break_of_structure": self.break_of_structure,
            "consolidating": self.consolidating,
            "has_swing_history": self.has_swing_history,
            "swing_count": len(self.swings),
            "notes": list(self.notes),
        }


def _one_sided_reading(
    series: Series,
    swings: list[Swing],
    highs: list[Swing],
    lows: list[Swing],
) -> StructureReading:
    """Read structure from whichever side of the swing sequence exists."""
    close = float(series.close[-1])
    present = highs if len(highs) >= 2 else lows
    is_high_side = present is highs
    recent = present[-2:]
    rising = recent[-1].price > recent[-2].price

    higher_highs = is_high_side and rising
    lower_highs = is_high_side and not rising
    higher_lows = (not is_high_side) and rising
    lower_lows = (not is_high_side) and not rising

    bias = Bias.BULLISH if rising else Bias.BEARISH
    side_word = "highs" if is_high_side else "lows"
    missing_word = "pullback" if is_high_side else "rally"
    label = f"{'higher' if rising else 'lower'} {side_word}, no confirmed {missing_word}"

    break_of_structure = "none"
    if is_high_side and close > present[-1].price:
        break_of_structure = "bullish"
    elif (not is_high_side) and close < present[-1].price:
        break_of_structure = "bearish"

    # Capped below the two-sided ceiling: this is genuine but weaker evidence.
    strength = 0.55
    if len(present) >= 3:
        ordered = present[-3:]
        consistent = all(
            (ordered[i + 1].price > ordered[i].price) is rising for i in range(2)
        )
        if consistent:
            strength = 0.65
        else:
            strength = 0.35

    return StructureReading(
        bias=bias,
        strength=strength,
        label=label,
        swings=swings,
        higher_highs=higher_highs,
        higher_lows=higher_lows,
        lower_highs=lower_highs,
        lower_lows=lower_lows,
        break_of_structure=break_of_structure,
        consolidating=False,
        notes=[
            f"Only swing {side_word} are confirmed so far — price has not "
            f"produced a {missing_word} deep enough to mark the other side."
        ],
        has_swing_history=True,
    )


def analyze_structure(series: Series, left: int = 2, right: int = 2) -> StructureReading:
    """Classify market structure from the confirmed swing sequence."""
    swings = find_swings(series, left, right)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]

    notes: list[str] = []

    if len(highs) < 2 and len(lows) < 2:
        return StructureReading(
            bias=Bias.NEUTRAL,
            strength=0.0,
            label="insufficient swing history",
            swings=swings,
            higher_highs=False,
            higher_lows=False,
            lower_highs=False,
            lower_lows=False,
            break_of_structure="none",
            consolidating=False,
            notes=["Not enough confirmed swing points to read market structure."],
            has_swing_history=False,
        )

    if len(highs) < 2 or len(lows) < 2:
        # Only one side of the swing sequence exists. A trend that runs without
        # meaningful pullbacks prints swing highs and no swing lows (or the
        # reverse), which is real structural information — just weaker than a
        # confirmed two-sided sequence, so it is scored lower.
        return _one_sided_reading(series, swings, highs, lows)

    last_highs = highs[-3:]
    last_lows = lows[-3:]

    higher_highs = last_highs[-1].price > last_highs[-2].price
    lower_highs = last_highs[-1].price < last_highs[-2].price
    higher_lows = last_lows[-1].price > last_lows[-2].price
    lower_lows = last_lows[-1].price < last_lows[-2].price

    # A break of structure is price trading beyond the last confirmed swing.
    close = float(series.close[-1])
    break_of_structure = "none"
    if close > last_highs[-1].price:
        break_of_structure = "bullish"
    elif close < last_lows[-1].price:
        break_of_structure = "bearish"

    # Consolidation: the swing highs and lows are both roughly flat, and the
    # whole swing range is narrow relative to recent bar ranges.
    high_prices = np.array([s.price for s in last_highs], dtype=np.float64)
    low_prices = np.array([s.price for s in last_lows], dtype=np.float64)
    swing_span = float(high_prices.max() - low_prices.min())
    bar_range = float(np.mean(series.high[-20:] - series.low[-20:])) if len(series) >= 5 else 0.0
    consolidating = bool(
        bar_range > 0 and swing_span < bar_range * 3.0 and not (higher_highs and higher_lows)
        and not (lower_highs and lower_lows)
    )

    bias = Bias.NEUTRAL
    strength = 0.0
    if higher_highs and higher_lows:
        bias = Bias.BULLISH
        label = "higher highs / higher lows"
        strength = 0.8
        notes.append("Structure is printing higher highs and higher lows.")
    elif lower_highs and lower_lows:
        bias = Bias.BEARISH
        label = "lower highs / lower lows"
        strength = 0.8
        notes.append("Structure is printing lower highs and lower lows.")
    elif higher_lows and lower_highs:
        label = "contracting range"
        strength = 0.15
        notes.append("Highs are falling while lows are rising — the range is compressing.")
    elif higher_lows:
        bias = Bias.BULLISH
        label = "higher lows, flat highs"
        strength = 0.45
        notes.append("Lows are rising but highs have not yet been taken out.")
    elif lower_highs:
        bias = Bias.BEARISH
        label = "lower highs, flat lows"
        strength = 0.45
        notes.append("Highs are falling but lows have not yet been broken.")
    elif higher_highs and lower_lows:
        label = "expanding range"
        strength = 0.1
        notes.append("Both highs and lows are extending — an expanding, choppy range.")
    else:
        label = "unclear structure"
        notes.append("Swing sequence does not describe a clean trend.")

    # A break of structure in the same direction reinforces the read; against
    # it, the structure is being challenged.
    if break_of_structure == "bullish":
        if bias is Bias.BULLISH:
            strength = min(1.0, strength + 0.15)
            notes.append("Price has broken above the last swing high.")
        elif bias is Bias.BEARISH:
            strength *= 0.5
            notes.append("Price broke above the last swing high, challenging the downtrend.")
    elif break_of_structure == "bearish":
        if bias is Bias.BEARISH:
            strength = min(1.0, strength + 0.15)
            notes.append("Price has broken below the last swing low.")
        elif bias is Bias.BULLISH:
            strength *= 0.5
            notes.append("Price broke below the last swing low, challenging the uptrend.")

    if consolidating:
        strength *= 0.5
        notes.append("Swings are compressed — the market is consolidating.")

    # Cross-check against the raw close slope so that a structure read which
    # contradicts recent price action is discounted.
    close_slope = slope(series.close, lookback=min(20, len(series)))
    if bias is Bias.BULLISH and close_slope < 0:
        strength *= 0.7
    elif bias is Bias.BEARISH and close_slope > 0:
        strength *= 0.7

    return StructureReading(
        bias=bias,
        strength=float(max(0.0, min(1.0, strength))),
        label=label,
        swings=swings,
        higher_highs=higher_highs,
        higher_lows=higher_lows,
        lower_highs=lower_highs,
        lower_lows=lower_lows,
        break_of_structure=break_of_structure,
        consolidating=consolidating,
        notes=notes,
    )
