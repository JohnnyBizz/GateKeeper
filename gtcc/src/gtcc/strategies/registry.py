"""The strategy registry.

Holds the strategies this deployment knows about and runs the eligible
ones. It gathers proposals; it does not choose between them and it
certainly does not place anything. Both of those happen downstream,
and the risk engine has the last word regardless.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gtcc.domain.enums import TradingMode
from gtcc.strategies.base import Proposal, Strategy, StrategyContext


@dataclass(frozen=True, slots=True)
class StrategyOutcome:
    """One strategy's answer, and whether it was allowed to give one."""

    strategy: str
    eligible: bool
    reason: str
    proposal: Proposal | None = None


@dataclass
class StrategyRegistry:
    strategies: dict[str, Strategy] = field(default_factory=dict)

    def register(self, strategy: Strategy) -> None:
        if strategy.name in self.strategies:
            raise ValueError(f"a strategy named {strategy.name!r} is already registered")
        self.strategies[strategy.name] = strategy

    def get(self, name: str) -> Strategy:
        try:
            return self.strategies[name]
        except KeyError:
            raise KeyError(
                f"no strategy named {name!r}; registered: {sorted(self.strategies)}"
            ) from None

    def evaluate_all(
        self, context: StrategyContext, mode: TradingMode
    ) -> list[StrategyOutcome]:
        """Ask every strategy, and record why the silent ones were silent.

        Ineligible strategies are reported rather than skipped. "Nothing
        proposed anything" and "everything was disabled" look identical
        from the outside, and only one of them is a market condition.
        """
        outcomes: list[StrategyOutcome] = []
        for name in sorted(self.strategies):
            strategy = self.strategies[name]
            eligible, reason = strategy.may_run(context, mode)
            proposal = strategy.propose(context, mode) if eligible else None
            outcomes.append(
                StrategyOutcome(
                    strategy=name,
                    eligible=eligible,
                    reason=reason or "eligible",
                    proposal=proposal,
                )
            )
        return outcomes

    def actionable(
        self, context: StrategyContext, mode: TradingMode
    ) -> list[Proposal]:
        """Only the proposals that suggest doing something."""
        return [
            outcome.proposal
            for outcome in self.evaluate_all(context, mode)
            if outcome.proposal is not None and outcome.proposal.is_actionable
        ]
