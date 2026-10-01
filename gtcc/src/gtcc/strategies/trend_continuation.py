"""Trend continuation — the first strategy module.

The idea, stated so it can be tested rather than admired: in a market
that is trending up by both measures the platform has, buy the first
pullback that holds and resumes, with the stop under the structure that
held.

What it requires, all of it:

* The regime classifier says TRENDING_UP or TRENDING_DOWN.
* The structure engine independently agrees, by swing sequence.
* Price has pulled back toward a moving average and is turning from it.
* The pullback did not break the last swing against the trend. A break
  means the structure that justified the trade is gone.
* Gross reward to the first target clears a configured minimum.

**This strategy has not been tested and its status says so.** It is
UNTESTED, which means the framework will refuse to let it propose
anything in paper or live. That is the correct state for a strategy
whose edge nobody has measured, and it stays that way until a backtest
and an out-of-sample run say otherwise. Nothing about the reasoning
below should be read as a claim that it works.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from gtcc.domain.enums import Decision, Regime, Timeframe
from gtcc.domain.money import ZERO, D
from gtcc.features.indicators import atr, ema
from gtcc.strategies.base import Proposal, Strategy, StrategyContext, ValidationStatus
from gtcc.structure.engine import StructureKind, Trend


@dataclass(frozen=True, slots=True)
class TrendContinuationSettings:
    fast_ma: int = 20
    slow_ma: int = 50
    atr_period: int = 14
    #: How close to the moving average counts as a pullback, in ATRs.
    pullback_atr: Decimal = D("1.0")
    #: Stop placed this many ATRs beyond the protective swing.
    stop_atr_buffer: Decimal = D("0.5")
    #: Target distance as a multiple of the risk taken.
    target_r_multiple: Decimal = D("2.0")
    #: Minimum gross reward:risk before proposing at all.
    min_gross_reward_risk: Decimal = D("1.8")

    def as_dict(self) -> dict:
        return {
            "fast_ma": self.fast_ma,
            "slow_ma": self.slow_ma,
            "atr_period": self.atr_period,
            "pullback_atr": str(self.pullback_atr),
            "stop_atr_buffer": str(self.stop_atr_buffer),
            "target_r_multiple": str(self.target_r_multiple),
            "min_gross_reward_risk": str(self.min_gross_reward_risk),
        }


class TrendContinuation(Strategy):
    name = "trend_continuation"
    timeframes = (Timeframe.M15, Timeframe.H1, Timeframe.H4)
    regimes = (Regime.TRENDING_UP, Regime.TRENDING_DOWN)
    #: Never measured. The framework will not let it trade.
    validation = ValidationStatus.UNTESTED
    description = (
        "Buys the first pullback that holds in an established uptrend, and "
        "the mirror in a downtrend. Untested."
    )

    def __init__(self, settings: TrendContinuationSettings | None = None) -> None:
        self.settings = settings or TrendContinuationSettings()

    def evaluate(self, context: StrategyContext) -> Proposal:
        settings = self.settings
        bars = list(context.bars)

        if len(bars) < settings.slow_ma + 5:
            return Proposal.wait(
                self.name,
                f"needs {settings.slow_ma + 5} bars, has {len(bars)}",
                bars=len(bars),
            )

        fast = ema(bars, settings.fast_ma)[-1]
        slow = ema(bars, settings.slow_ma)[-1]
        volatility = atr(bars, settings.atr_period)[-1]
        if fast is None or slow is None or volatility is None or volatility <= ZERO:
            return Proposal.wait(self.name, "indicators are still warming up")

        rising = context.regime.regime is Regime.TRENDING_UP
        structural = context.structure.trend

        # Two independent reads must agree. The regime classifier uses
        # ADX and the DI lines; the structure engine uses the swing
        # sequence. Requiring both is the cheapest way to avoid acting
        # on one indicator's opinion.
        if rising and structural is not Trend.UP:
            return Proposal.wait(
                self.name,
                f"the regime reads TRENDING_UP but structure reads {structural}",
                regime=str(context.regime.regime), structure=str(structural),
            )
        if not rising and structural is not Trend.DOWN:
            return Proposal.wait(
                self.name,
                f"the regime reads TRENDING_DOWN but structure reads {structural}",
                regime=str(context.regime.regime), structure=str(structural),
            )

        if rising and fast <= slow:
            return Proposal.wait(self.name, "the fast average is not above the slow one")
        if not rising and fast >= slow:
            return Proposal.wait(self.name, "the fast average is not below the slow one")

        last = bars[-1]
        distance = abs(last.close - fast) / volatility
        if distance > settings.pullback_atr:
            return Proposal.wait(
                self.name,
                f"price is {distance:.2f} ATR from the fast average, further than "
                f"the {settings.pullback_atr} ATR pullback this setup waits for",
                atr_distance=str(round(distance, 3)),
            )

        protective = self._protective_swing(context, rising)
        if protective is None:
            return Proposal.wait(
                self.name, "no confirmed swing to place a stop behind"
            )

        # The pullback must not already have broken the structure the
        # trade is leaning on. If it has, the reason for the trade is
        # gone and the stop would be placed under nothing.
        if rising and last.close <= protective:
            return Proposal.wait(
                self.name,
                "price has already closed below the swing low this setup leans on",
                swing=str(protective),
            )
        if not rising and last.close >= protective:
            return Proposal.wait(
                self.name,
                "price has already closed above the swing high this setup leans on",
                swing=str(protective),
            )

        entry = last.close
        buffer = volatility * settings.stop_atr_buffer
        stop = protective - buffer if rising else protective + buffer
        risk = abs(entry - stop)
        if risk <= ZERO:
            return Proposal.wait(self.name, "the stop computes to the entry price")

        target = (
            entry + risk * settings.target_r_multiple
            if rising
            else entry - risk * settings.target_r_multiple
        )
        entry, stop, targets = self.normalised(
            context, entry=entry, stop=stop, targets=(target,)
        )

        gross = self.reward_to_risk(entry, stop, targets[0])
        if gross is None or gross < settings.min_gross_reward_risk:
            return Proposal.wait(
                self.name,
                f"gross reward:risk of {gross} is below the {settings.min_gross_reward_risk} "
                "this setup requires before it is worth proposing",
                gross_reward_risk=str(gross),
            )

        return Proposal(
            decision=Decision.LONG if rising else Decision.SHORT,
            strategy=self.name,
            setup="TREND_CONTINUATION_PULLBACK",
            rationale=(
                f"{'up' if rising else 'down'}trend confirmed by both the regime "
                f"classifier and the swing sequence; price pulled back to within "
                f"{distance:.2f} ATR of the {settings.fast_ma} EMA without breaking "
                "the last protective swing"
            ),
            entry=entry,
            stop=stop,
            targets=targets,
            conviction=2 if context.regime.qualifiers else 3,
            evidence={
                "regime": str(context.regime.regime),
                "structure_trend": str(structural),
                "fast_ema": str(fast),
                "slow_ema": str(slow),
                "atr": str(volatility),
                "atr_distance_to_ema": str(round(distance, 3)),
                "protective_swing": str(protective),
                "gross_reward_risk": str(round(gross, 3)),
                "settings": settings.as_dict(),
                "structure_settings": context.structure.detections[0].parameters
                if context.structure.detections
                else {},
            },
        )

    def _protective_swing(
        self, context: StrategyContext, rising: bool
    ) -> Decimal | None:
        """The most recent swing the trade's premise rests on."""
        kind = StructureKind.SWING_LOW if rising else StructureKind.SWING_HIGH
        swing = context.structure.latest(kind)
        return swing.price if swing is not None else None
