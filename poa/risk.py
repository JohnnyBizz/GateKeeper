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


# Below this many settled trades a win rate says essentially nothing, so the
# app neither highlights it nor warns about it.
MIN_MEANINGFUL_SAMPLE = 20


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
    # Set when a configured limit has been reached. The panel says so loudly
    # and stops recommending a stake, because the point of a limit is that it
    # is reached on the day you are least inclined to respect it.
    paused: bool = False
    paused_reason: str = ""

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
            "paused": self.paused,
            "paused_reason": self.paused_reason,
        }


def assess_risk(
    balance: float,
    risk_percent: float,
    payout: float,
    *,
    observed_win_rate: float | None = None,
    stake_override: float | None = None,
    observed_sample: int = 0,
    session: "SessionStats | None" = None,
    max_losses_in_a_row: int = 0,
    max_daily_loss_percent: float = 0.0,
) -> RiskAssessment:
    """Size a stake and report what it needs to achieve to be worth placing.

    ``stake_override`` lets the user type the exact amount they intend to put
    on, instead of deriving it from a percentage. The percentage is then
    computed *backwards* from the stake so the risk warnings still fire — a
    typed stake of a quarter of the balance is still a quarter of the balance.

    ``max_losses_in_a_row`` and ``max_daily_loss_percent`` are the brakes. Both
    are off at zero, and neither ever raises a stake or suggests recovering a
    loss — a limit that can be argued with is not a limit, and the argument
    always arrives on the day it should not be listened to.
    """
    balance = max(0.0, float(balance))
    payout = max(0.0, float(payout))

    if stake_override is not None and stake_override > 0:
        stake = float(stake_override)
        risk_percent = (stake / balance * 100.0) if balance > 0 else 100.0
    else:
        risk_percent = max(0.0, min(100.0, float(risk_percent)))
        stake = balance * risk_percent / 100.0
    breakeven = breakeven_win_rate(payout)

    # The brakes, before anything else is said about sizing.
    paused, paused_reason = False, ""
    if session is not None:
        streak = session.losing_streak
        if max_losses_in_a_row > 0 and streak >= max_losses_in_a_row:
            paused = True
            paused_reason = (
                f"{streak} losses in a row. The limit set here was "
                f"{max_losses_in_a_row}."
            )
        elif max_daily_loss_percent > 0 and balance > 0:
            # What the session has actually cost, at the stake being used.
            lost = (session.losses * stake) - (session.wins * stake * payout)
            if lost > 0 and (lost / balance * 100.0) >= max_daily_loss_percent:
                paused = True
                paused_reason = (
                    f"Down {lost / balance * 100.0:.1f}% of the balance today. "
                    f"The limit set here was {max_daily_loss_percent:.0f}%."
                )

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
    if balance > 0 and stake > balance:
        warnings.append(
            f"The stake ({stake:.2f}) is larger than the whole balance "
            f"({balance:.2f})."
        )
    elif risk_percent > 5:
        warnings.append(
            f"Risking {risk_percent:.0f}% of the balance per trade is high. "
            f"A run of {trades_to_ruin} losses would clear the account."
        )
    if payout < 0.7:
        warnings.append(
            f"A {payout * 100:.0f}% payout needs a {breakeven:.1f}% win rate just "
            "to break even."
        )
    # Only worth saying once there are enough settled trades for the rate to
    # mean anything. Two trades at 50% is not a losing streak, it is a coin
    # landing twice, and warning about it trains the user to ignore warnings.
    if (
        observed_win_rate is not None
        and observed_win_rate < breakeven
        and observed_sample >= MIN_MEANINGFUL_SAMPLE
    ):
        warnings.append(
            f"This session's {observed_win_rate:.1f}% win rate over "
            f"{observed_sample} trades is below the {breakeven:.1f}% needed to "
            "break even at this payout."
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
        paused=paused,
        paused_reason=paused_reason,
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
    # Outcomes in the order they happened, newest last. The tallies alone
    # cannot say whether four losses were spread across an afternoon or arrived
    # one after another, and those are not the same afternoon.
    sequence: list[bool] = field(default_factory=list)

    def record(self, won: bool) -> None:
        """Note an outcome, in order."""
        self.sequence.append(bool(won))

    @property
    def losing_streak(self) -> int:
        """Losses since the last win.

        The number that matters during a bad run. A session at eight wins and
        four losses reads healthily right up until the four were the last four.
        """
        streak = 0
        for won in reversed(self.sequence):
            if won:
                break
            streak += 1
        return streak

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
