"""The strategy framework — specification sections 22 and 23.

A strategy's job is to propose: here is a setup, here is where it is
wrong, here is what I expect. It does not size the position, does not
decide whether the account can afford it, and cannot reach a broker.
It returns a :class:`Proposal`, which the runtime turns into an order
request and hands to the risk engine like any other.

Two gates sit in front of every strategy, and both default to closed.

**Validation status.** Section 25 forbids running anything that has not
passed out-of-sample testing. A strategy declares its own status and
:meth:`Strategy.may_run` refuses to let an untested one propose
anything in paper or live. The default is UNTESTED, so a new strategy
is inert until somebody has done the work and said so.

**Regime.** A strategy declares the regimes it was built for.
Section 23 asks that strategies be enabled and disabled by regime, and
an unknown regime enables nothing: a mean-reversion strategy that
cannot tell whether the market is trending should not guess.

Nothing here assumes any strategy is profitable. Section 22 is explicit
and the framework is arranged so that the burden sits on evidence
rather than on the author's confidence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, Sequence

from gtcc.domain.enums import Decision, Market, Regime, Side, Timeframe, TradingMode
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, Quote
from gtcc.domain.money import ZERO, D
from gtcc.domain.pricing import normalise_stop, normalise_target
from gtcc.features.regime import RegimeReading
from gtcc.structure.engine import StructureReport


class ValidationStatus(StrEnum):
    """How much evidence stands behind a strategy.

    The order is the promotion path, and each step is a thing somebody
    did, not an opinion somebody holds.
    """

    #: Written, never measured. Cannot run outside a backtest.
    UNTESTED = "UNTESTED"
    #: Profitable in-sample. This is the weakest possible evidence and
    #: is deliberately not enough to run anywhere real.
    IN_SAMPLE = "IN_SAMPLE"
    #: Held up on data it was not fitted to.
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    #: Survived a forward paper run.
    PAPER_VALIDATED = "PAPER_VALIDATED"
    #: Explicitly retired. Keeps running in backtests for comparison.
    RETIRED = "RETIRED"

    @property
    def may_trade_paper(self) -> bool:
        return self in (
            ValidationStatus.OUT_OF_SAMPLE,
            ValidationStatus.PAPER_VALIDATED,
        )

    @property
    def may_trade_live(self) -> bool:
        return self is ValidationStatus.PAPER_VALIDATED


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy is allowed to see.

    No account, no position sizes, no equity. A strategy that knew the
    balance would be tempted to size, and sizing is the risk engine's
    job. It sees the market and nothing else.
    """

    symbol: str
    market: Market
    timeframe: Timeframe
    instrument: InstrumentSpec
    bars: Sequence[Bar]
    quote: Quote | None
    structure: StructureReport
    regime: RegimeReading
    #: Higher-timeframe reports, keyed by timeframe. Section 4's cascade.
    higher_timeframes: dict[Timeframe, StructureReport] = field(default_factory=dict)
    now: datetime | None = None

    @property
    def last_bar(self) -> Bar | None:
        return self.bars[-1] if self.bars else None


@dataclass(frozen=True, slots=True)
class Proposal:
    """A strategy's suggestion. Advisory, like everything upstream of risk."""

    decision: Decision
    strategy: str
    #: Why, in terms a person reading the journal can check.
    rationale: str
    #: What the strategy saw. Stored with the trade.
    evidence: dict = field(default_factory=dict)
    entry: Decimal | None = None
    stop: Decimal | None = None
    targets: tuple[Decimal, ...] = ()
    #: The strategy's own ordering of its ideas, not a probability.
    #: Deliberately not called "confidence": nothing here is calibrated.
    conviction: int = 0
    setup: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.decision in (Decision.LONG, Decision.SHORT)

    @property
    def side(self) -> Side | None:
        if self.decision is Decision.LONG:
            return Side.BUY
        if self.decision is Decision.SHORT:
            return Side.SELL
        return None

    def coherent(self) -> tuple[bool, str]:
        """Does the proposal make internal sense?

        Checked before it reaches the risk engine so that a strategy
        bug is reported as a strategy bug, rather than arriving as a
        confusing risk rejection.
        """
        if not self.is_actionable:
            return True, ""
        if self.entry is None or self.stop is None:
            return False, "an actionable proposal needs an entry and a stop"
        if not self.targets:
            return False, "an actionable proposal needs at least one target"
        if self.decision is Decision.LONG:
            if self.stop >= self.entry:
                return False, f"a long's stop {self.stop} is not below its entry {self.entry}"
            if any(target <= self.entry for target in self.targets):
                return False, "a long's targets must sit above its entry"
        else:
            if self.stop <= self.entry:
                return False, f"a short's stop {self.stop} is not above its entry {self.entry}"
            if any(target >= self.entry for target in self.targets):
                return False, "a short's targets must sit below its entry"
        return True, ""

    @classmethod
    def wait(cls, strategy: str, rationale: str, **evidence) -> "Proposal":
        """The default answer. Section 13: WAIT is a first-class result."""
        return cls(
            decision=Decision.WAIT, strategy=strategy, rationale=rationale,
            evidence=evidence,
        )


class Strategy(ABC):
    """Base class. Subclasses implement :meth:`evaluate` only."""

    #: Stable identifier, stored on every trade.
    name: str = "unnamed"
    #: Timeframes this strategy was built for.
    timeframes: tuple[Timeframe, ...] = ()
    #: Regimes it may run in. Empty means any, which should be rare.
    regimes: tuple[Regime, ...] = ()
    #: Evidence standing behind it. Default is the honest one.
    validation: ValidationStatus = ValidationStatus.UNTESTED
    #: Declared event strategies opt out of the macro blackout.
    event_strategy: bool = False
    description: str = ""

    #: Attribute names that are easy to write instead of the real ones, and
    #: the name meant. A typo here is quiet and one-directional: the gate
    #: keeps its safe default, so the strategy never runs and nothing says
    #: why. Caught at class-definition time instead.
    _MISNAMED: ClassVar[dict[str, str]] = {
        "validation_status": "validation",
        "status": "validation",
        "allowed_regimes": "regimes",
        "allowed_timeframes": "timeframes",
        "regime": "regimes",
        "timeframe": "timeframes",
        "is_event_strategy": "event_strategy",
    }

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        for wrong, right in Strategy._MISNAMED.items():
            if wrong in vars(cls):
                raise TypeError(
                    f"{cls.__name__} sets {wrong!r}, which this framework does "
                    f"not read; the attribute is {right!r}. Left alone, "
                    f"{cls.__name__} would keep the safe default for {right!r} "
                    "and silently never trade."
                )

    def may_run(self, context: StrategyContext, mode: TradingMode) -> tuple[bool, str]:
        """Should this strategy be consulted at all?

        Returns the reason when the answer is no, so a silent strategy
        can be explained rather than guessed at.
        """
        if mode is TradingMode.LIVE and not self.validation.may_trade_live:
            return False, (
                f"{self.name} is {self.validation} and only a PAPER_VALIDATED "
                "strategy may run live"
            )
        if mode is TradingMode.PAPER and not self.validation.may_trade_paper:
            return False, (
                f"{self.name} is {self.validation}; a strategy must pass "
                "out-of-sample testing before it trades, even on paper"
            )
        if self.timeframes and context.timeframe not in self.timeframes:
            return False, (
                f"{self.name} runs on {[str(t) for t in self.timeframes]}, "
                f"not {context.timeframe}"
            )
        if not context.regime.permits(self.regimes):
            allowed = [str(r) for r in self.regimes]
            return False, (
                f"{self.name} runs in {allowed} and the regime is "
                f"{context.regime.describe()}"
            )
        return True, ""

    def propose(self, context: StrategyContext, mode: TradingMode) -> Proposal:
        """Run the strategy, with its gates applied.

        Subclasses override :meth:`evaluate`, not this. A strategy that
        could skip its own gates would make them decorative.
        """
        allowed, reason = self.may_run(context, mode)
        if not allowed:
            return Proposal.wait(self.name, reason, gate="not_eligible")

        if context.regime.regime is Regime.EVENT_RISK and not self.event_strategy:
            return Proposal.wait(
                self.name,
                "a high-impact release is imminent and this strategy was not "
                "built or tested for event conditions",
                gate="event_risk",
            )

        proposal = self.evaluate(context)
        coherent, problem = proposal.coherent()
        if not coherent:
            # A strategy bug must not reach the risk engine disguised as
            # a trade idea.
            return Proposal.wait(
                self.name,
                f"{self.name} produced an incoherent proposal and it was discarded: "
                f"{problem}",
                gate="incoherent",
                original_decision=str(proposal.decision),
            )
        return proposal

    @abstractmethod
    def evaluate(self, context: StrategyContext) -> Proposal:
        """Read the market and propose. Called only when eligible."""

    # -- helpers for subclasses ------------------------------------------

    def normalised(
        self,
        context: StrategyContext,
        *,
        entry: Decimal,
        stop: Decimal,
        targets: Sequence[Decimal],
    ) -> tuple[Decimal, Decimal, tuple[Decimal, ...]]:
        """Put levels on the instrument's tick grid, safely.

        Stops and targets round toward the entry, so a strategy cannot
        accidentally widen its own risk or flatter its own reward by
        choosing an off-grid price.
        """
        tick = context.instrument.tick_size
        return (
            entry,
            normalise_stop(stop, tick, entry=entry),
            tuple(normalise_target(target, tick, entry=entry) for target in targets),
        )

    def reward_to_risk(
        self, entry: Decimal, stop: Decimal, target: Decimal
    ) -> Decimal | None:
        """Gross ratio, for the strategy's own filtering.

        Gross, and the name says so. The risk engine computes the net
        figure after fees, spread and slippage, and that is the one
        that decides.
        """
        risk = abs(entry - stop)
        if risk <= ZERO:
            return None
        return abs(target - entry) / risk
