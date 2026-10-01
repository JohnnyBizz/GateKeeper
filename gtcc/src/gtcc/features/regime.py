"""Market regime classification — specification section 23.

A regime is a statement about conditions, not a prediction. The point
of having one is section 23's second sentence: strategies can be
enabled and disabled by regime, and the regime is stored with every
trade so that performance can later be broken down by it.

Which means the classification has to be reproducible and it has to
say what it was based on. Like the structure engine, every result
carries the thresholds that produced it, because "choppy" is a word
about a number somebody chose.

The classifier reports one primary regime and any number of
qualifiers. Trend and volatility are different axes: a market can be
trending and volatile at once, and collapsing that into a single label
throws away the half the strategy cared about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from gtcc.domain.enums import Regime
from gtcc.domain.market_data import Bar
from gtcc.domain.money import ZERO, D
from gtcc.features.indicators import (
    atr,
    bollinger,
    directional_movement,
    historical_volatility,
)
from gtcc.structure.engine import StructureEngine, StructureReport, Trend


@dataclass(frozen=True, slots=True)
class RegimeSettings:
    """Every threshold the classifier uses.

    Defaults are conventional starting points, not findings. Section 14
    applies here too: these are transparent configurable values to be
    evaluated against history, never asserted to be profitable.
    """

    adx_period: int = 14
    #: Above this, the market is treated as trending.
    adx_trending: Decimal = D(25)
    #: Below this, treated as directionless.
    adx_choppy: Decimal = D(20)
    volatility_period: int = 20
    #: Current ATR against its own average, above which volatility is high.
    high_volatility_ratio: Decimal = D("1.5")
    low_volatility_ratio: Decimal = D("0.6")
    #: Bollinger width below this fraction is a squeeze.
    squeeze_width: Decimal = D("0.02")
    #: Minutes to a high-impact event that make the regime EVENT_RISK.
    event_window_minutes: int = 30

    def as_dict(self) -> dict:
        return {
            "adx_period": self.adx_period,
            "adx_trending": str(self.adx_trending),
            "adx_choppy": str(self.adx_choppy),
            "volatility_period": self.volatility_period,
            "high_volatility_ratio": str(self.high_volatility_ratio),
            "low_volatility_ratio": str(self.low_volatility_ratio),
            "squeeze_width": str(self.squeeze_width),
            "event_window_minutes": self.event_window_minutes,
        }


@dataclass(frozen=True, slots=True)
class RegimeReading:
    """What the market is doing, and what that conclusion rests on."""

    regime: Regime
    #: Conditions that also hold. A trending market can be volatile.
    qualifiers: tuple[Regime, ...] = ()
    rule: str = ""
    #: The measurements behind the call, for the journal.
    evidence: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    #: False when there was not enough history to classify anything.
    confident: bool = True

    @property
    def all_regimes(self) -> tuple[Regime, ...]:
        return (self.regime,) + self.qualifiers

    def permits(self, allowed: Sequence[Regime]) -> bool:
        """Would a strategy restricted to *allowed* be enabled here?

        Empty means no restriction. An unconfident reading permits
        nothing: a strategy gated on regime should not run when the
        regime is unknown.
        """
        if not allowed:
            return True
        if not self.confident:
            return False
        return any(regime in allowed for regime in self.all_regimes)

    def describe(self) -> str:
        extra = f" ({', '.join(str(q) for q in self.qualifiers)})" if self.qualifiers else ""
        return f"{self.regime}{extra}"


class RegimeClassifier:
    """Classifies one symbol's conditions from its bars and structure."""

    def __init__(self, settings: RegimeSettings | None = None) -> None:
        self.settings = settings or RegimeSettings()

    def classify(
        self,
        bars: Sequence[Bar],
        *,
        structure: StructureReport | None = None,
        minutes_to_high_impact_event: float | None = None,
    ) -> RegimeReading:
        settings = self.settings
        needed = max(settings.adx_period * 2, settings.volatility_period + 1)

        if len(bars) < needed:
            return RegimeReading(
                regime=Regime.UNKNOWN,
                rule=f"needs {needed} bars to classify, got {len(bars)}",
                settings=settings.as_dict(),
                confident=False,
            )

        # An imminent high-impact release dominates everything else:
        # whatever the chart was doing is about to be interrupted.
        if (
            minutes_to_high_impact_event is not None
            and 0 <= minutes_to_high_impact_event <= settings.event_window_minutes
        ):
            return RegimeReading(
                regime=Regime.EVENT_RISK,
                rule=(
                    "a high-impact release falls inside the event window, which "
                    "overrides the technical reading"
                ),
                evidence={"minutes_to_event": minutes_to_high_impact_event},
                settings=settings.as_dict(),
            )

        structure = structure or StructureEngine().analyse(bars)
        movement = directional_movement(bars, settings.adx_period)
        strength = self._last(movement.adx)
        plus_di = self._last(movement.plus_di)
        minus_di = self._last(movement.minus_di)
        volatility_ratio = self._volatility_ratio(bars)
        width = self._last(bollinger(bars, period=settings.volatility_period).width())
        annualised = self._last(
            historical_volatility(bars, period=settings.volatility_period)
        )

        evidence = {
            "adx": str(strength) if strength is not None else None,
            "atr_ratio": str(volatility_ratio) if volatility_ratio is not None else None,
            "bollinger_width": str(width) if width is not None else None,
            "historical_volatility": str(annualised) if annualised is not None else None,
            "plus_di": str(plus_di) if plus_di is not None else None,
            "minus_di": str(minus_di) if minus_di is not None else None,
            "structure_trend": str(structure.trend),
        }

        qualifiers = self._volatility_qualifiers(volatility_ratio, width)
        regime, rule = self._primary(
            strength, plus_di, minus_di, structure, qualifiers
        )

        return RegimeReading(
            regime=regime,
            qualifiers=tuple(q for q in qualifiers if q is not regime),
            rule=rule,
            evidence=evidence,
            settings=settings.as_dict(),
            confident=strength is not None,
        )

    # -- internals ---------------------------------------------------------

    def _last(self, series: Sequence[Decimal | None]) -> Decimal | None:
        for value in reversed(series):
            if value is not None:
                return value
        return None

    def _volatility_ratio(self, bars: Sequence[Bar]) -> Decimal | None:
        """Current ATR against the average ATR of the lookback.

        A ratio rather than an absolute: what counts as volatile for an
        index future is calm for a small-cap, and an absolute threshold
        would need a different value per symbol.
        """
        series = atr(bars, self.settings.volatility_period)
        defined = [value for value in series if value is not None]
        if len(defined) < 2:
            return None
        average = sum(defined, ZERO) / len(defined)
        if average <= ZERO:
            return None
        return defined[-1] / average

    def _volatility_qualifiers(
        self, ratio: Decimal | None, width: Decimal | None
    ) -> list[Regime]:
        settings = self.settings
        found: list[Regime] = []
        if ratio is not None:
            if ratio >= settings.high_volatility_ratio:
                found.append(Regime.HIGH_VOLATILITY)
            elif ratio <= settings.low_volatility_ratio:
                found.append(Regime.LOW_VOLATILITY)
        if (
            width is not None
            and width <= settings.squeeze_width
            and Regime.LOW_VOLATILITY not in found
        ):
            found.append(Regime.LOW_VOLATILITY)
        return found

    def _primary(
        self,
        strength: Decimal | None,
        plus_di: Decimal | None,
        minus_di: Decimal | None,
        structure: StructureReport,
        qualifiers: Sequence[Regime],
    ) -> tuple[Regime, str]:
        settings = self.settings
        rule = (
            f"ADX above {settings.adx_trending} is trending, with the direction "
            "taken from the swing sequence or, where there are no swings, from "
            f"the DI lines; ADX below {settings.adx_choppy} is choppy; a recent "
            "breakout outranks a ranging read"
        )

        if strength is None:
            return Regime.UNKNOWN, "ADX could not be computed"

        from gtcc.structure.engine import StructureKind

        breakout = structure.latest(StructureKind.BREAKOUT)
        recent_breakout = (
            breakout is not None and breakout.index >= structure.bar_count - 5
        )

        if strength >= settings.adx_trending:
            if structure.trend is Trend.UP:
                return Regime.TRENDING_UP, rule
            if structure.trend is Trend.DOWN:
                return Regime.TRENDING_DOWN, rule
            # Strong directional movement that the swing sequence has
            # not caught up with. A clean one-way move has no pivots at
            # all, so structure reports UNCLEAR and the direction has
            # to come from the DI lines instead. Calling this RANGING,
            # as an earlier version did, was exactly backwards.
            if plus_di is not None and minus_di is not None and plus_di != minus_di:
                return (
                    Regime.TRENDING_UP if plus_di > minus_di else Regime.TRENDING_DOWN
                ), rule
            if recent_breakout:
                return Regime.BREAKOUT, rule
            return (
                Regime.HIGH_VOLATILITY
                if Regime.HIGH_VOLATILITY in qualifiers
                else Regime.RANGING
            ), rule

        if recent_breakout:
            return Regime.BREAKOUT, rule

        if strength <= settings.adx_choppy:
            # Directionless. Whether that is an orderly range or a chop
            # is the difference between tradeable and not.
            if structure.trend is Trend.RANGING:
                return Regime.RANGING, rule
            return Regime.CHOPPY, rule

        return Regime.RANGING, rule
