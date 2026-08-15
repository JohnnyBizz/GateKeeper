"""Which parts of the analysis were right, and which were talking nonsense.

The score is ten weighted components. When a call loses, "the setup failed" is
useless — the question is *which of the ten was wrong*, and that is answerable:
compare what each component scored on the trades that won against what it
scored on the trades that lost.

Three outcomes, and only one of them is good:

* **Discriminating.** It scores higher on winners than losers. It is doing the
  job its weight assumes it does.
* **Silent.** It scores the same on both. It is contributing weight without
  contributing information — an expensive way to add a constant.
* **Inverted.** It scores *higher on losers*. It is not merely useless; it is
  arguing for the losing side, and its weight is actively spent on being
  wrong. This is the finding worth having, and it is invisible from any
  amount of looking at the indicator itself.

The comparison is deliberately crude — a difference in means, with a sample
floor — because the sample sizes here are small and anything more elaborate
would be reading tea leaves with more decimal places.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..logging_setup import get_logger

log = get_logger(__name__)

# Settled trades of each kind needed before a component's record is read. Below
# this the difference between two means is noise wearing a sign.
MIN_EACH = 8

# How far apart the two means must be, in score points (components run 0..1),
# before the gap counts as a finding rather than as sampling.
MEANINGFUL_GAP = 0.05


@dataclass
class ComponentVerdict:
    """What one component scored on winners against losers."""

    name: str
    winner_scores: list[float] = field(default_factory=list)
    loser_scores: list[float] = field(default_factory=list)

    @staticmethod
    def _mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    @property
    def on_winners(self) -> float | None:
        return self._mean(self.winner_scores)

    @property
    def on_losers(self) -> float | None:
        return self._mean(self.loser_scores)

    @property
    def readable(self) -> bool:
        return len(self.winner_scores) >= MIN_EACH and len(self.loser_scores) >= MIN_EACH

    @property
    def gap(self) -> float | None:
        """How much higher it scores on winners. Negative means inverted."""
        won, lost = self.on_winners, self.on_losers
        if won is None or lost is None:
            return None
        return round(won - lost, 3)

    @property
    def verdict(self) -> str:
        if not self.readable:
            return "unread"
        gap = self.gap or 0.0
        if gap > MEANINGFUL_GAP:
            return "discriminating"
        if gap < -MEANINGFUL_GAP:
            return "inverted"
        return "silent"

    def describe(self) -> str:
        name = self.name.replace("_", " ")
        if not self.readable:
            return f"{name}: too few trades to read"
        won, lost = self.on_winners or 0.0, self.on_losers or 0.0
        if self.verdict == "inverted":
            return (
                f"{name} scored {lost:.2f} on losers against {won:.2f} on "
                "winners — it argued for the wrong side"
            )
        if self.verdict == "silent":
            return (
                f"{name} scored {won:.2f} either way — it is carrying weight "
                "without carrying information"
            )
        return f"{name} scored {won:.2f} on winners against {lost:.2f} on losers"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "on_winners": self.on_winners,
            "on_losers": self.on_losers,
            "gap": self.gap,
            "verdict": self.verdict,
            "winners": len(self.winner_scores),
            "losers": len(self.loser_scores),
            "describe": self.describe(),
        }


@dataclass
class Attribution:
    """The post-mortem: what the losing calls had in common."""

    components: list[ComponentVerdict] = field(default_factory=list)
    losers: int = 0
    winners: int = 0
    loss_regimes: dict[str, int] = field(default_factory=dict)
    loss_patterns: dict[str, int] = field(default_factory=dict)

    def inverted(self) -> list[ComponentVerdict]:
        """Components arguing for the losing side, worst first."""
        found = [c for c in self.components if c.verdict == "inverted"]
        found.sort(key=lambda c: c.gap or 0.0)
        return found

    def silent(self) -> list[ComponentVerdict]:
        return [c for c in self.components if c.verdict == "silent"]

    def worst_condition(self) -> tuple[str, int] | None:
        """The market condition the losses cluster in, if they cluster at all."""
        if not self.loss_regimes:
            return None
        name, count = max(self.loss_regimes.items(), key=lambda kv: kv[1])
        # A cluster means most of the losses, not merely the largest slice of a
        # flat spread.
        if self.losers and count / self.losers >= 0.5 and count >= 5:
            return name, count
        return None

    def headline(self) -> str:
        if self.winners + self.losers == 0:
            return "No settled calls to learn from yet."
        inverted = self.inverted()
        if inverted:
            return inverted[0].describe().capitalize() + "."
        condition = self.worst_condition()
        if condition:
            name, count = condition
            return (
                f"{count} of {self.losers} losses came in a "
                f"{name.replace('_', ' ').lower()} market."
            )
        silent = self.silent()
        if silent:
            names = ", ".join(c.name.replace("_", " ") for c in silent[:2])
            return f"{names} scored the same on winners and losers."
        return "Nothing in the losses separates them from the wins."

    def to_dict(self) -> dict[str, Any]:
        return {
            "winners": self.winners,
            "losers": self.losers,
            "headline": self.headline(),
            "components": [c.to_dict() for c in self.components],
            "loss_regimes": self.loss_regimes,
            "loss_patterns": self.loss_patterns,
        }


def attribute(trades: Iterable[Any]) -> Attribution:
    """Work out what the losing calls had in common that the winners did not."""
    report = Attribution()
    verdicts: dict[str, ComponentVerdict] = {}

    for trade in trades:
        outcome = getattr(trade, "outcome", None)
        if outcome not in ("win", "loss"):
            continue
        won = outcome == "win"
        report.winners += won
        report.losers += not won

        if not won:
            regime = str(getattr(trade, "regime", "") or "")
            if regime:
                report.loss_regimes[regime] = report.loss_regimes.get(regime, 0) + 1
            pattern = str(getattr(trade, "pattern", "") or "")
            if pattern and pattern.lower() not in ("", "none", "no pattern"):
                report.loss_patterns[pattern] = report.loss_patterns.get(pattern, 0) + 1

        for name, score in (getattr(trade, "components", None) or {}).items():
            try:
                value = float(score)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            verdict = verdicts.setdefault(name, ComponentVerdict(name))
            (verdict.winner_scores if won else verdict.loser_scores).append(value)

    report.components = sorted(
        verdicts.values(), key=lambda c: (c.gap if c.gap is not None else 0.0)
    )
    return report
