"""Measuring the assistant against the chart in front of you.

Everything else in this app is an opinion about what price will do. This is the
one part that checks. It replays the *current* chart's own history through the
same signal engine, bar by bar, settling every signal it would have produced at
the duration you have selected — and reports what that would have been worth.

Two properties make the number worth reading:

* **No look-ahead.** The backtester hands the engine a strict prefix of the
  series and nothing after it, so each decision is made from what was knowable
  at that bar. Any result that quietly peeked at the future would be a lie in
  the most flattering possible direction.
* **The same settings you are trading.** Gates, payout and expiry come from the
  live configuration, so the measurement describes the tool as configured
  rather than some other version of it.

What it is *not* is a forecast. It is a small, recent, in-sample record on one
instrument, and it is labelled as such below twenty trades, where a win rate is
noise. The honest use is comparative: this pair against that one, this expiry
against another.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..backtesting.calibration import (
    MIN_SAMPLE,
    build_calibration,
    records_from_trades,
)
from ..backtesting.attribution import attribute
from ..backtesting.gatecheck import check_gates
from ..backtesting.paper import Backtester
from ..backtesting.stats import breakeven_rate
from ..logging_setup import get_logger
from ..models import Series, format_duration
from ..signals.gates import GateSettings

log = get_logger(__name__)

# Below this many settled trades a win rate is noise. Same threshold the
# session tally uses, for the same reason.
MEANINGFUL_TRADES = 20

# Bars needed before replaying is worth the electricity. The engine needs a
# warm-up window before it will say anything at all, so a shorter series
# produces a confident-looking "0 signals" that means nothing.
MIN_BARS = 150

# The warm-up the engine sees at each step, and the least it can be. Every bar
# spent on warm-up is a bar that cannot be evaluated, so a fixed 120 out of 150
# available spends four fifths of a short history on nothing and leaves fifteen
# decisions to measure — which is no sample at all. The window shrinks toward
# the floor when history is short and grows back when there is plenty.
WINDOW = 120
MIN_WINDOW = 70

# The gates the *survey* runs at, well below anything worth trading. Their
# job is to produce a population of setups across the whole score range so
# the threshold table has something to rank; the live gates then decide
# which of them the user would actually have taken.
SURVEY_CONFIDENCE = 40.0
SURVEY_DURATION = 25.0

# The most decisions one replay will test. Depth is worth having — the
# sample scales with it — but a replay that takes a minute is one the user
# is waiting on, so past this point the walk is sampled more coarsely
# rather than truncated. Coverage of the whole history beats a fine-
# grained look at the most recent tenth of it.
MAX_EVALUATIONS = 1100


def _step_for(bars: int, window: int) -> int:
    """Sample the walk so deep history stays affordable."""
    testable = max(0, bars - window)
    return max(2, -(-testable // MAX_EVALUATIONS))


def _window_for(bars: int) -> int:
    """Warm-up that leaves a usable number of decisions behind it."""
    return max(MIN_WINDOW, min(WINDOW, bars // 2))


@dataclass
class ProofResult:
    """What replaying this chart through the engine produced."""

    asset: str
    timeframe_seconds: int
    trade_duration: int
    bars: int
    evaluated: int
    signals: int
    wins: int
    losses: int
    payout: float
    error: str | None = None
    # Setups the looser survey turned up, of which ``signals`` are the ones the
    # live gates would have taken. The gap between the two is what the
    # threshold table is ranking.
    surveyed: int = 0
    # Which gates are earning their keep on this chart, and which are
    # refusing setups that would have paid.
    gate_report: Any | None = None
    # What the losing calls had in common that the winning ones did not.
    attribution: Any | None = None
    by_direction: dict[str, Any] = field(default_factory=dict)
    # What the score was actually worth, by band, by threshold, by regime and
    # by hour. None when the replay produced nothing to calibrate against.
    calibration: Any | None = None

    @property
    def settled(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        if self.settled == 0:
            return None
        return round(self.wins / self.settled * 100.0, 1)

    @property
    def breakeven(self) -> float:
        return breakeven_rate(self.payout)

    @property
    def edge(self) -> float | None:
        """Points above (or below) the rate this payout needs to break even."""
        rate = self.win_rate
        if rate is None:
            return None
        return round(rate - self.breakeven, 1)

    @property
    def meaningful(self) -> bool:
        return self.settled >= MEANINGFUL_TRADES

    def summary(self) -> str:
        """One line, honest about its own sample size."""
        if self.error:
            return self.error
        if self.settled == 0:
            line = (
                f"Replayed {self.bars} bars of {self.asset}: no setup passed "
                "the gates."
            )
            # "Nothing to measure" is a dead end unless it says why. On a short
            # history the honest reason is usually the history: most of it is
            # spent warming the indicators up, and what is left is too few
            # decisions to be a sample of anything.
            if self.evaluated < 40:
                return (
                    f"{line} Only {self.evaluated} decisions could be tested on "
                    "this much history — scroll the chart back to load more."
                )
            if self.surveyed:
                # The survey found setups; the live gates rejected all of them.
                # That is a gate problem, not a market problem, and the tuner
                # has the evidence to act on it.
                return (
                    f"{line} {self.surveyed} were found at looser settings — "
                    "the gates are tightening or loosening toward them."
                )
            return f"{line} Either a quiet stretch, or nothing here to trade."
        rate = self.win_rate or 0.0
        line = (
            f"Replayed {self.bars} bars: {self.settled} setups, "
            f"{self.wins}W/{self.losses}L — {rate:.0f}% at "
            f"{format_duration(self.trade_duration)}"
        )
        if not self.meaningful:
            return line + f" (only {self.settled}, too few to read)"
        edge = self.edge or 0.0
        return line + f", {edge:+.0f} pts vs {self.breakeven:.0f}% break-even"

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "timeframe_seconds": self.timeframe_seconds,
            "trade_duration": self.trade_duration,
            "bars": self.bars,
            "evaluated": self.evaluated,
            "signals": self.signals,
            "wins": self.wins,
            "losses": self.losses,
            "settled": self.settled,
            "win_rate": self.win_rate,
            "breakeven": self.breakeven,
            "edge": self.edge,
            "meaningful": self.meaningful,
            "summary": self.summary(),
            "error": self.error,
            "by_direction": self.by_direction,
        }


def measure(
    series: Series,
    *,
    trade_duration: int,
    payout: float,
    settings: GateSettings | None = None,
    higher_multiple: int = 5,
    entry_multiple: int = 1,
    step: int = 2,
    min_gap_bars: int = 3,
    min_sample: int = MIN_SAMPLE,
    real_records: Any | None = None,
) -> ProofResult:
    """Replay ``series`` through the engine and score what came out.

    ``step`` samples the walk rather than evaluating every bar: adjacent bars
    produce near-identical readings, and ``min_gap_bars`` would suppress most
    of them anyway. It is the difference between one second of work and five.
    """
    asset = series.symbol
    timeframe = series.timeframe_seconds
    bars = len(series)

    if bars < MIN_BARS:
        return ProofResult(
            asset=asset,
            timeframe_seconds=timeframe,
            trade_duration=trade_duration,
            bars=bars,
            evaluated=0,
            signals=0,
            wins=0,
            losses=0,
            payout=payout,
            error=(
                f"{bars} bars of history — {MIN_BARS} are needed before a "
                "replay says anything."
            ),
        )

    # The replay runs *looser* than the live gates, deliberately. Running it at
    # the live gates measures only what already passes, which can never answer
    # the question the threshold table exists for — would a different gate have
    # been better? It also closes a loop: strict gates yield few setups, few
    # setups cannot carry a recommendation, so the gates never move off
    # whatever they happened to be set to.
    #
    # Only the two selectivity dials are relaxed. Regime, structure,
    # multi-timeframe and volatility are the strategy itself rather than a
    # measure of how picky to be, and loosening those would measure a different
    # tool. The user's own gates are still what the headline reports.
    live = settings or GateSettings()
    loose = replace(
        live,
        min_confidence=SURVEY_CONFIDENCE,
        min_duration_compatibility=SURVEY_DURATION,
        require_measured_edge=False,
    )
    backtester = Backtester(
        settings=loose,
        window=_window_for(bars),
        higher_multiple=higher_multiple,
        entry_multiple=entry_multiple,
        payout=payout,
    )
    try:
        result = backtester.run(
            series,
            trade_duration=trade_duration,
            asset=asset,
            step=max(int(step), _step_for(bars, _window_for(bars))),
            min_gap_bars=min_gap_bars,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("replay failed")
        return ProofResult(
            asset=asset,
            timeframe_seconds=timeframe,
            trade_duration=trade_duration,
            bars=bars,
            evaluated=0,
            signals=0,
            wins=0,
            losses=0,
            payout=payout,
            error=f"Could not replay this chart: {exc}",
        )

    # The headline is what *your* gates would have taken, not what the survey
    # turned up: a number describing a looser tool than the one running would
    # be quietly answering a question nobody asked.
    taken = [
        trade
        for trade in result.trades
        if trade.direction_confidence >= live.min_confidence
        and trade.duration_confidence >= live.min_duration_compatibility
    ]
    # "flat" is neither — a binary that expires exactly where it opened is a
    # refund, and counting it either way moves the rate for no reason.
    wins = sum(1 for trade in taken if trade.outcome == "win")
    losses = sum(1 for trade in taken if trade.outcome == "loss")

    # Split by direction, because a tool that only gets calls right on a rising
    # chart has found the trend, not an edge.
    by_direction: dict[str, Any] = {}
    for name in ("CALL", "PUT"):
        picked = [t for t in taken if t.direction == name]
        won = sum(1 for t in picked if t.outcome == "win")
        lost = sum(1 for t in picked if t.outcome == "loss")
        by_direction[name] = {
            "wins": won,
            "losses": lost,
            "win_rate": round(won / (won + lost) * 100.0, 1) if won + lost else None,
        }

    # Real settled trades and replayed ones are different experiments: one was
    # taken, the other only considered. Pooling them would dilute the record
    # that matters with the one that does not, so the real record is used
    # *instead* as soon as there is enough of it to stand on its own.
    # Which of the gates deserve their veto, measured the same way.
    try:
        gates = check_gates(
            series,
            trade_duration=trade_duration,
            payout=payout,
            settings=live,
            window=_window_for(bars),
            step=_step_for(bars, _window_for(bars)) + 1,
            higher_multiple=higher_multiple,
            entry_multiple=entry_multiple,
        )
    except Exception:  # pragma: no cover - defensive
        log.exception("gate audit failed")
        gates = None

    real = list(real_records or [])
    if len(real) >= min_sample:
        calibration = build_calibration(real, payout=payout, min_sample=min_sample)
        calibration.from_real_trades = True
    else:
        calibration = build_calibration(
            records_from_trades(result.trades), payout=payout, min_sample=min_sample
        )
        calibration.from_real_trades = False
    calibration.real_available = len(real)

    return ProofResult(
        asset=asset,
        timeframe_seconds=timeframe,
        trade_duration=trade_duration,
        bars=bars,
        evaluated=result.evaluated_bars,
        signals=len(taken),
        surveyed=len(result.trades),
        wins=wins,
        losses=losses,
        payout=payout,
        by_direction=by_direction,
        calibration=calibration,
        gate_report=gates,
        attribution=attribute(result.trades),
    )
