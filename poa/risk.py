"""Stake sizing and session performance.

Binary options pay asymmetrically: at a 92% payout a win returns 0.92 and a
loss costs 1.00. That asymmetry is the whole reason this module exists — it
puts the break-even win rate on screen next to the stake, so a stake is never
chosen without the number it has to beat.

Nothing here places a trade or touches an account. It is arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def breakeven_win_rate(payout: float) -> float:
    """The win rate needed to break even at ``payout``, as a percentage.

    ``payout`` is the profit fraction on a win (0.92 for a 92% payout).
    Derivation: p·payout = (1−p) ⇒ p = 1/(1+payout).
    """
    if payout <= 0:
        return 100.0
    return round(100.0 / (1.0 + payout), 1)


def expected_value(win_rate_pct: float, payout: float) -> float:
    """Expected return per unit staked, at a given win rate and payout."""
    p = max(0.0, min(1.0, win_rate_pct / 100.0))
    return round(p * payout - (1.0 - p), 4)


@dataclass
class RiskAssessment:
    """A stake recommendation and the arithmetic behind it."""

    balance: float
    risk_percent: float
    payout: float
    stake: float
    potential_profit: float
    potential_loss: float
    breakeven_rate: float
    expected_value_at: dict[str, float]
    trades_to_ruin: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "balance": round(self.balance, 2),
            "risk_percent": round(self.risk_percent, 2),
            "payout": round(self.payout, 4),
            "payout_display": f"{self.payout * 100:.0f}%",
            "stake": round(self.stake, 2),
            "potential_profit": round(self.potential_profit, 2),
            "potential_loss": round(self.potential_loss, 2),
            "breakeven_rate": self.breakeven_rate,
            "expected_value_at": {k: round(v, 4) for k, v in self.expected_value_at.items()},
            "trades_to_ruin": self.trades_to_ruin,
            "warnings": list(self.warnings),
        }


def assess_risk(
    balance: float,
    risk_percent: float,
    payout: float,
    *,
    observed_win_rate: float | None = None,
) -> RiskAssessment:
    """Size a stake and report what it needs to achieve to be worth placing."""
    balance = max(0.0, float(balance))
    risk_percent = max(0.0, min(100.0, float(risk_percent)))
    payout = max(0.0, float(payout))

    stake = balance * risk_percent / 100.0
    breakeven = breakeven_win_rate(payout)

    # Expected value at a few reference win rates, so the break-even number is
    # concrete rather than abstract.
    reference: dict[str, float] = {
        "50%": expected_value(50.0, payout) * stake,
        f"{breakeven:.0f}% (break-even)": expected_value(breakeven, payout) * stake,
        "60%": expected_value(60.0, payout) * stake,
        "70%": expected_value(70.0, payout) * stake,
    }
    if observed_win_rate is not None:
        reference[f"{observed_win_rate:.0f}% (your session)"] = (
            expected_value(observed_win_rate, payout) * stake
        )

    # A flat-stake losing streak that would wipe the balance. Not a prediction —
    # a reminder that streaks happen and this is how many it would take.
    trades_to_ruin = int(balance // stake) if stake > 0 else 0

    warnings: list[str] = []
    if risk_percent > 5:
        warnings.append(
            f"Risking {risk_percent:.0f}% of the balance per trade is high. "
            f"A run of {trades_to_ruin} losses would clear the account."
        )
    if payout < 0.7:
        warnings.append(
            f"A {payout * 100:.0f}% payout needs a {breakeven:.1f}% win rate just "
            "to break even."
        )
    if observed_win_rate is not None and observed_win_rate < breakeven:
        warnings.append(
            f"This session's {observed_win_rate:.1f}% win rate is below the "
            f"{breakeven:.1f}% needed to break even at this payout."
        )

    return RiskAssessment(
        balance=balance,
        risk_percent=risk_percent,
        payout=payout,
        stake=stake,
        potential_profit=stake * payout,
        potential_loss=stake,
        breakeven_rate=breakeven,
        expected_value_at=reference,
        trades_to_ruin=trades_to_ruin,
        warnings=warnings,
    )


@dataclass
class SessionStats:
    """Win/loss tally for the current session.

    Counts are fed automatically from settled journal outcomes, and may also be
    adjusted by hand — the platform is the authority on whether a trade won,
    and the user may have taken trades the assistant never signalled.

    ``auto_wins``/``auto_losses`` and the manual adjustments are stored
    separately so a journal refresh cannot wipe a manual correction.
    """

    auto_wins: int = 0
    auto_losses: int = 0
    manual_wins: int = 0
    manual_losses: int = 0

    @property
    def wins(self) -> int:
        return max(0, self.auto_wins + self.manual_wins)

    @property
    def losses(self) -> int:
        return max(0, self.auto_losses + self.manual_losses)

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        """Percentage of decided trades won, or None when nothing is decided."""
        if self.total == 0:
            return None
        return round(self.wins / self.total * 100.0, 1)

    def edge_over_breakeven(self, payout: float) -> float | None:
        """How far the session sits above (or below) break-even, in points."""
        rate = self.win_rate
        if rate is None:
            return None
        return round(rate - breakeven_win_rate(payout), 1)

    def adjust(self, wins: int = 0, losses: int = 0) -> None:
        """Nudge the counters by hand, without going below zero overall."""
        self.manual_wins = max(self.manual_wins + wins, -self.auto_wins)
        self.manual_losses = max(self.manual_losses + losses, -self.auto_losses)

    def set_auto(self, wins: int, losses: int) -> None:
        """Replace the journal-sourced counts, preserving manual adjustments."""
        self.auto_wins = max(0, int(wins))
        self.auto_losses = max(0, int(losses))
        # Keep manual adjustments from driving a total negative after a refresh.
        self.manual_wins = max(self.manual_wins, -self.auto_wins)
        self.manual_losses = max(self.manual_losses, -self.auto_losses)

    def reset(self) -> None:
        self.auto_wins = self.auto_losses = 0
        self.manual_wins = self.manual_losses = 0

    def to_dict(self, payout: float = 0.92) -> dict[str, Any]:
        rate = self.win_rate
        return {
            "wins": self.wins,
            "losses": self.losses,
            "total": self.total,
            "win_rate": rate,
            "win_rate_display": "--" if rate is None else f"{rate:.1f}%",
            "breakeven_rate": breakeven_win_rate(payout),
            "edge": self.edge_over_breakeven(payout),
            "manually_adjusted": bool(self.manual_wins or self.manual_losses),
            # A handful of trades says nothing; the UI greys the rate out below this.
            "meaningful": self.total >= 20,
        }
