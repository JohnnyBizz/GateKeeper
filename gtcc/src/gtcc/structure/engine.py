"""Market structure — specification section 6.

Section 6 ends with a warning worth taking literally: "Avoid pretending
subjective concepts are mathematically certain. Store the exact rules
used to identify them."

So every detection in this module carries the rule and the parameters
that produced it, in a :class:`Detection` record. "This is a break of
structure" is not a fact about the market; it is the output of a
specific definition with specific numbers, and a different swing
lookback gives a different answer. Storing the rule means a result can
be reproduced, argued with, and re-run with other parameters when the
journal is analysed later.

What is objective here: swing pivots under a stated lookback, the
sequence of highs and lows, gaps between candle bodies, prior-period
levels. What is a judgement call, labelled as one: whether a wick
through a level was a "sweep", whether a range has "broken".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from gtcc.domain.market_data import Bar
from gtcc.domain.money import ZERO, D


class StructureKind(StrEnum):
    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"
    HIGHER_HIGH = "HIGHER_HIGH"
    HIGHER_LOW = "HIGHER_LOW"
    LOWER_HIGH = "LOWER_HIGH"
    LOWER_LOW = "LOWER_LOW"
    BREAK_OF_STRUCTURE = "BREAK_OF_STRUCTURE"
    CHANGE_OF_CHARACTER = "CHANGE_OF_CHARACTER"
    RANGE = "RANGE"
    BREAKOUT = "BREAKOUT"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    EQUAL_HIGHS = "EQUAL_HIGHS"
    EQUAL_LOWS = "EQUAL_LOWS"
    LIQUIDITY_SWEEP = "LIQUIDITY_SWEEP"
    FAIR_VALUE_GAP = "FAIR_VALUE_GAP"
    SUPPORT = "SUPPORT"
    RESISTANCE = "RESISTANCE"


class Trend(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    RANGING = "RANGING"
    UNCLEAR = "UNCLEAR"


class Confidence(StrEnum):
    """How much the detection rests on a judgement call.

    OBJECTIVE readings follow from arithmetic alone. INTERPRETED ones
    depend on a threshold somebody chose, and the chosen value travels
    with the detection so the reader can disagree with it.
    """

    OBJECTIVE = "OBJECTIVE"
    INTERPRETED = "INTERPRETED"


@dataclass(frozen=True, slots=True)
class Detection:
    """One structural observation, with the rule that produced it."""

    kind: StructureKind
    #: Index into the bar series the detection was computed from.
    index: int
    timestamp: datetime
    price: Decimal
    #: Plain-language statement of the rule applied.
    rule: str
    #: The exact parameters used. Changing any of these can change the
    #: result, which is why they are stored rather than assumed.
    parameters: dict = field(default_factory=dict)
    confidence: Confidence = Confidence.OBJECTIVE
    #: A second price where the detection spans a region.
    price_to: Decimal | None = None

    def describe(self) -> str:
        params = ", ".join(f"{k}={v}" for k, v in sorted(self.parameters.items()))
        return f"{self.kind} @ {self.price} [{self.rule}; {params}]"


@dataclass(frozen=True, slots=True)
class StructureReport:
    """Everything the structure engine concluded about one series."""

    symbol: str
    timeframe: str
    bar_count: int
    trend: Trend
    trend_rule: str
    detections: tuple[Detection, ...] = ()
    #: Set when the series was too short for the configured lookback.
    insufficient_history: bool = False

    def of(self, *kinds: StructureKind) -> tuple[Detection, ...]:
        return tuple(d for d in self.detections if d.kind in kinds)

    def latest(self, kind: StructureKind) -> Detection | None:
        found = self.of(kind)
        return found[-1] if found else None

    def summary(self) -> str:
        if self.insufficient_history:
            return f"{self.symbol}: not enough history for structure"
        counts: dict[str, int] = {}
        for detection in self.detections:
            counts[str(detection.kind)] = counts.get(str(detection.kind), 0) + 1
        parts = ", ".join(f"{k} x{v}" for k, v in sorted(counts.items()))
        return f"{self.symbol} {self.timeframe}: trend {self.trend} — {parts or 'nothing detected'}"


@dataclass(frozen=True, slots=True)
class StructureSettings:
    """Every threshold the engine uses, in one place.

    These are the numbers that make a reading subjective. They are
    parameters rather than constants so that a journal entry can record
    which were in force, and so a later analysis can ask whether a
    different lookback would have read the same chart differently.
    """

    #: Bars either side that a pivot must exceed to count as a swing.
    swing_lookback: int = 2
    #: Two highs within this fraction of price count as "equal".
    equal_level_tolerance: Decimal = D("0.0005")
    #: A breakout must clear the level by this fraction to count.
    breakout_margin: Decimal = D("0.0005")
    #: A sweep must poke through and close back inside within this many bars.
    sweep_reclaim_bars: int = 3
    #: A fair value gap smaller than this fraction of price is noise.
    min_gap_fraction: Decimal = D("0.0005")
    #: How many swings to consider when reading the trend.
    trend_swings: int = 4

    def as_dict(self) -> dict:
        return {
            "swing_lookback": self.swing_lookback,
            "equal_level_tolerance": str(self.equal_level_tolerance),
            "breakout_margin": str(self.breakout_margin),
            "sweep_reclaim_bars": self.sweep_reclaim_bars,
            "min_gap_fraction": str(self.min_gap_fraction),
            "trend_swings": self.trend_swings,
        }


class StructureEngine:
    """Reads structure from a closed bar series."""

    def __init__(self, settings: StructureSettings | None = None) -> None:
        self.settings = settings or StructureSettings()

    def analyse(self, bars: Sequence[Bar]) -> StructureReport:
        settings = self.settings
        needed = settings.swing_lookback * 2 + 1
        symbol = bars[0].symbol if bars else "?"
        timeframe = str(bars[0].timeframe) if bars else "?"

        if len(bars) < needed:
            return StructureReport(
                symbol=symbol, timeframe=timeframe, bar_count=len(bars),
                trend=Trend.UNCLEAR,
                trend_rule="not enough bars for the configured swing lookback",
                insufficient_history=True,
            )

        swings = self._swings(bars)
        detections: list[Detection] = list(swings)
        detections.extend(self._sequence(swings))
        detections.extend(self._structure_breaks(bars, swings))
        detections.extend(self._equal_levels(swings))
        detections.extend(self._sweeps(bars, swings))
        detections.extend(self._fair_value_gaps(bars))
        detections.extend(self._range_and_breakout(bars, swings))
        detections.extend(self._support_resistance(swings))

        trend, rule = self._trend(swings)
        detections.sort(key=lambda d: (d.index, str(d.kind)))

        return StructureReport(
            symbol=symbol, timeframe=timeframe, bar_count=len(bars),
            trend=trend, trend_rule=rule, detections=tuple(detections),
        )

    # -- pivots ---------------------------------------------------------

    def _swings(self, bars: Sequence[Bar]) -> list[Detection]:
        """Fractal pivots: a bar whose high exceeds its neighbours'.

        Note what this costs: a swing is only confirmed *lookback* bars
        after it happened. The engine never reports one earlier, because
        doing so would be using bars that had not printed yet.
        """
        look = self.settings.swing_lookback
        rule = (
            f"high strictly greater than the {look} highs either side "
            f"(and the mirror for lows); confirmed {look} bars late"
        )
        found: list[Detection] = []
        for index in range(look, len(bars) - look):
            window = bars[index - look : index + look + 1]
            centre = bars[index]
            others = [bar for position, bar in enumerate(window) if position != look]

            if all(centre.high > bar.high for bar in others):
                found.append(
                    Detection(
                        kind=StructureKind.SWING_HIGH, index=index,
                        timestamp=centre.timestamp, price=centre.high,
                        rule=rule, parameters={"swing_lookback": look},
                    )
                )
            if all(centre.low < bar.low for bar in others):
                found.append(
                    Detection(
                        kind=StructureKind.SWING_LOW, index=index,
                        timestamp=centre.timestamp, price=centre.low,
                        rule=rule, parameters={"swing_lookback": look},
                    )
                )
        return found

    def _confirmed_at(self, swings: Sequence[Detection]) -> dict[int, list[Detection]]:
        """Index swings by the bar at which they become KNOWABLE.

        A pivot needs ``swing_lookback`` bars after it before it can be
        confirmed, so a causal walk must not see it at its own index.
        Registering it there let a breakout bar's own swing raise the
        ceiling it was breaking, and let a break of structure reference
        a level that had not been established yet. Both are lookahead,
        and both produced plausible output, which is what made it worth
        a test rather than a comment.
        """
        look = self.settings.swing_lookback
        by_index: dict[int, list[Detection]] = {}
        for swing in swings:
            by_index.setdefault(swing.index + look, []).append(swing)
        return by_index

    def _sequence(self, swings: Sequence[Detection]) -> list[Detection]:
        """Label each swing against the previous one of its own kind."""
        found: list[Detection] = []
        previous: dict[StructureKind, Detection] = {}
        for swing in swings:
            prior = previous.get(swing.kind)
            previous[swing.kind] = swing
            if prior is None:
                continue
            if swing.kind is StructureKind.SWING_HIGH:
                kind = (
                    StructureKind.HIGHER_HIGH
                    if swing.price > prior.price
                    else StructureKind.LOWER_HIGH
                )
            else:
                kind = (
                    StructureKind.HIGHER_LOW
                    if swing.price > prior.price
                    else StructureKind.LOWER_LOW
                )
            found.append(
                Detection(
                    kind=kind, index=swing.index, timestamp=swing.timestamp,
                    price=swing.price,
                    rule=f"compared against the previous {swing.kind} at {prior.price}",
                    parameters={"previous_price": str(prior.price),
                                "previous_index": prior.index},
                )
            )
        return found

    # -- breaks ------------------------------------------------------------

    def _structure_breaks(
        self, bars: Sequence[Bar], swings: Sequence[Detection]
    ) -> list[Detection]:
        """Break of structure, and change of character.

        A BOS continues the prevailing direction: price closes beyond
        the last swing in the way the trend was already going. A CHoCH
        is the first break the *other* way, which is why it is the one
        that matters and also the one most prone to wishful reading.

        Both require a CLOSE beyond the level, not a wick. A wick
        through and back is a sweep, handled separately, and conflating
        the two is the most common way this reading goes wrong.
        """
        found: list[Detection] = []
        direction: Trend = Trend.UNCLEAR
        last_high: Detection | None = None
        last_low: Detection | None = None
        swing_at = self._confirmed_at(swings)

        for index, bar in enumerate(bars):
            for swing in swing_at.get(index, ()):
                if swing.kind is StructureKind.SWING_HIGH:
                    last_high = swing
                else:
                    last_low = swing

            if last_high is not None and bar.close > last_high.price:
                kind = (
                    StructureKind.CHANGE_OF_CHARACTER
                    if direction is Trend.DOWN
                    else StructureKind.BREAK_OF_STRUCTURE
                )
                found.append(
                    Detection(
                        kind=kind, index=index, timestamp=bar.timestamp,
                        price=last_high.price,
                        rule=(
                            "close above the last confirmed swing high while the "
                            f"prevailing read was {direction}"
                        ),
                        parameters={
                            "level": str(last_high.price),
                            "level_index": last_high.index,
                            "close": str(bar.close),
                            "prevailing_trend": str(direction),
                        },
                        confidence=Confidence.INTERPRETED,
                    )
                )
                direction = Trend.UP
                last_high = None

            if last_low is not None and bar.close < last_low.price:
                kind = (
                    StructureKind.CHANGE_OF_CHARACTER
                    if direction is Trend.UP
                    else StructureKind.BREAK_OF_STRUCTURE
                )
                found.append(
                    Detection(
                        kind=kind, index=index, timestamp=bar.timestamp,
                        price=last_low.price,
                        rule=(
                            "close below the last confirmed swing low while the "
                            f"prevailing read was {direction}"
                        ),
                        parameters={
                            "level": str(last_low.price),
                            "level_index": last_low.index,
                            "close": str(bar.close),
                            "prevailing_trend": str(direction),
                        },
                        confidence=Confidence.INTERPRETED,
                    )
                )
                direction = Trend.DOWN
                last_low = None
        return found

    # -- levels ---------------------------------------------------------------

    def _equal_levels(self, swings: Sequence[Detection]) -> list[Detection]:
        """Pairs of swings close enough to read as one level."""
        tolerance = self.settings.equal_level_tolerance
        found: list[Detection] = []
        for kind, result in (
            (StructureKind.SWING_HIGH, StructureKind.EQUAL_HIGHS),
            (StructureKind.SWING_LOW, StructureKind.EQUAL_LOWS),
        ):
            same = [s for s in swings if s.kind is kind]
            for earlier, later in zip(same, same[1:]):
                if earlier.price <= ZERO:
                    continue
                difference = abs(later.price - earlier.price) / earlier.price
                if difference <= tolerance:
                    found.append(
                        Detection(
                            kind=result, index=later.index,
                            timestamp=later.timestamp, price=later.price,
                            price_to=earlier.price,
                            rule=(
                                "two consecutive swings of the same kind within "
                                "the equal-level tolerance"
                            ),
                            parameters={
                                "tolerance": str(tolerance),
                                "observed_difference": str(round(difference, 8)),
                                "other_index": earlier.index,
                            },
                            confidence=Confidence.INTERPRETED,
                        )
                    )
        return found

    def _sweeps(
        self, bars: Sequence[Bar], swings: Sequence[Detection]
    ) -> list[Detection]:
        """A wick through a swing level that closes back inside.

        Deliberately conservative. The wick must exceed the level, the
        same bar must close back on the original side, and price must
        stay back inside for the configured number of bars. Without the
        last condition every ordinary breakout bar reads as a sweep for
        one bar before continuing.
        """
        window = self.settings.sweep_reclaim_bars
        found: list[Detection] = []
        for swing in swings:
            for index in range(swing.index + 1, len(bars)):
                bar = bars[index]
                if swing.kind is StructureKind.SWING_HIGH:
                    poked = bar.high > swing.price and bar.close < swing.price
                else:
                    poked = bar.low < swing.price and bar.close > swing.price
                if not poked:
                    continue
                after = bars[index + 1 : index + 1 + window]
                if len(after) < window:
                    break
                if swing.kind is StructureKind.SWING_HIGH:
                    held = all(later.close < swing.price for later in after)
                else:
                    held = all(later.close > swing.price for later in after)
                if held:
                    found.append(
                        Detection(
                            kind=StructureKind.LIQUIDITY_SWEEP, index=index,
                            timestamp=bar.timestamp, price=swing.price,
                            rule=(
                                "wick beyond a swing level with a close back inside, "
                                f"holding for {window} bars"
                            ),
                            parameters={
                                "level": str(swing.price),
                                "level_kind": str(swing.kind),
                                "reclaim_bars": window,
                                "wick_extreme": str(
                                    bar.high if swing.kind is StructureKind.SWING_HIGH
                                    else bar.low
                                ),
                            },
                            confidence=Confidence.INTERPRETED,
                        )
                    )
                break
        return found

    def _fair_value_gaps(self, bars: Sequence[Bar]) -> list[Detection]:
        """Three-bar imbalance: bar one and bar three do not overlap.

        Fully objective, unlike most of this module. Either the ranges
        overlap or they do not; the only judgement is the minimum size
        below which a gap is treated as noise.
        """
        minimum = self.settings.min_gap_fraction
        found: list[Detection] = []
        for index in range(2, len(bars)):
            first, third = bars[index - 2], bars[index]
            reference = third.close
            if reference <= ZERO:
                continue

            if third.low > first.high:
                size = (third.low - first.high) / reference
                if size >= minimum:
                    found.append(
                        Detection(
                            kind=StructureKind.FAIR_VALUE_GAP, index=index,
                            timestamp=third.timestamp, price=first.high,
                            price_to=third.low,
                            rule="bullish three-bar gap: bar three's low above bar one's high",
                            parameters={
                                "direction": "up",
                                "min_gap_fraction": str(minimum),
                                "observed_fraction": str(round(size, 8)),
                            },
                        )
                    )
            elif third.high < first.low:
                size = (first.low - third.high) / reference
                if size >= minimum:
                    found.append(
                        Detection(
                            kind=StructureKind.FAIR_VALUE_GAP, index=index,
                            timestamp=third.timestamp, price=third.high,
                            price_to=first.low,
                            rule="bearish three-bar gap: bar three's high below bar one's low",
                            parameters={
                                "direction": "down",
                                "min_gap_fraction": str(minimum),
                                "observed_fraction": str(round(size, 8)),
                            },
                        )
                    )
        return found

    def _range_and_breakout(
        self, bars: Sequence[Bar], swings: Sequence[Detection]
    ) -> list[Detection]:
        """The prevailing range, and closes that break it.

        Computed causally: the engine walks forward keeping the range
        implied by the swings confirmed *so far*, and a breakout is a
        close beyond that range as it stood at the time.

        The first version of this took the last three swings of the
        whole series to define the range and then scanned history for
        breaks, which peeks at the future and is self-defeating: the
        breakout bar usually becomes a swing itself and so raises the
        very ceiling it was breaking. Nothing was ever detected.
        """
        margin = self.settings.breakout_margin
        found: list[Detection] = []
        swing_at = self._confirmed_at(swings)

        highs: list[Decimal] = []
        lows: list[Decimal] = []
        broken_at: int | None = None

        for index, bar in enumerate(bars):
            for swing in swing_at.get(index, ()):
                if swing.kind is StructureKind.SWING_HIGH:
                    highs.append(swing.price)
                else:
                    lows.append(swing.price)

            if broken_at is not None or len(highs) < 2 or len(lows) < 2:
                continue

            top = max(highs[-3:])
            bottom = min(lows[-3:])
            if top <= bottom:
                continue

            if bar.close > top * (1 + margin):
                found.append(self._breakout(bars, index, top, "up", margin))
                broken_at = index
            elif bar.close < bottom * (1 - margin):
                found.append(self._breakout(bars, index, bottom, "down", margin))
                broken_at = index

        if len(highs) >= 2 and len(lows) >= 2:
            top = max(highs[-3:])
            bottom = min(lows[-3:])
            if top > bottom:
                found.insert(
                    0,
                    Detection(
                        kind=StructureKind.RANGE, index=len(bars) - 1,
                        timestamp=bars[-1].timestamp, price=bottom, price_to=top,
                        rule=(
                            "highest of the last three swing highs to the lowest "
                            "of the last three swing lows"
                        ),
                        parameters={
                            "top": str(top),
                            "bottom": str(bottom),
                            "swing_highs_used": len(highs[-3:]),
                            "swing_lows_used": len(lows[-3:]),
                        },
                        confidence=Confidence.INTERPRETED,
                    ),
                )
        return found

    def _breakout(
        self, bars: Sequence[Bar], index: int, level: Decimal, direction: str, margin: Decimal
    ) -> Detection:
        """A break of the range, and whether it held.

        "Failed" needs a definition and this one is stated: price
        closed back inside the range within three bars. A different
        number gives a different answer, which is exactly why the
        number is recorded alongside the detection.
        """
        window = bars[index + 1 : index + 4]
        back_inside = any(
            (bar.close < level if direction == "up" else bar.close > level)
            for bar in window
        )
        return Detection(
            kind=StructureKind.FAILED_BREAKOUT if back_inside else StructureKind.BREAKOUT,
            index=index,
            timestamp=bars[index].timestamp,
            price=level,
            rule=(
                f"close beyond the range {direction} by the breakout margin"
                + ("; closed back inside within 3 bars" if back_inside else "")
            ),
            parameters={
                "direction": direction,
                "level": str(level),
                "breakout_margin": str(margin),
                "bars_checked_for_failure": 3,
                "close": str(bars[index].close),
            },
            confidence=Confidence.INTERPRETED,
        )

    def _support_resistance(self, swings: Sequence[Detection]) -> list[Detection]:
        """Levels touched more than once, most-touched first."""
        tolerance = self.settings.equal_level_tolerance
        found: list[Detection] = []
        for kind, result in (
            (StructureKind.SWING_LOW, StructureKind.SUPPORT),
            (StructureKind.SWING_HIGH, StructureKind.RESISTANCE),
        ):
            same = [s for s in swings if s.kind is kind]
            used: set[int] = set()
            for anchor in same:
                if anchor.index in used or anchor.price <= ZERO:
                    continue
                touches = [
                    other for other in same
                    if abs(other.price - anchor.price) / anchor.price <= tolerance
                ]
                if len(touches) < 2:
                    continue
                used.update(touch.index for touch in touches)
                last = touches[-1]
                found.append(
                    Detection(
                        kind=result, index=last.index, timestamp=last.timestamp,
                        price=anchor.price,
                        rule=f"{len(touches)} swings within the equal-level tolerance",
                        parameters={
                            "touches": len(touches),
                            "tolerance": str(tolerance),
                            "touch_indices": [touch.index for touch in touches],
                        },
                        confidence=Confidence.INTERPRETED,
                    )
                )
        return found

    def _trend(self, swings: Sequence[Detection]) -> tuple[Trend, str]:
        """Read the trend from the last few swings of each kind."""
        count = self.settings.trend_swings
        highs = [s for s in swings if s.kind is StructureKind.SWING_HIGH][-count:]
        lows = [s for s in swings if s.kind is StructureKind.SWING_LOW][-count:]
        rule = (
            f"the last {count} swing highs and lows: rising both ways is UP, "
            "falling both ways is DOWN, disagreement is RANGING"
        )
        if len(highs) < 2 or len(lows) < 2:
            return Trend.UNCLEAR, "fewer than two swings of each kind"

        highs_rising = all(b.price > a.price for a, b in zip(highs, highs[1:]))
        highs_falling = all(b.price < a.price for a, b in zip(highs, highs[1:]))
        lows_rising = all(b.price > a.price for a, b in zip(lows, lows[1:]))
        lows_falling = all(b.price < a.price for a, b in zip(lows, lows[1:]))

        if highs_rising and lows_rising:
            return Trend.UP, rule
        if highs_falling and lows_falling:
            return Trend.DOWN, rule
        return Trend.RANGING, rule
