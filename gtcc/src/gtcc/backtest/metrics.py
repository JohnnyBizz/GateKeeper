"""Performance statistics, with their own reliability attached.

A Sharpe ratio from nine trades is not a measurement, it is noise with a
decimal point. The single most common way a backtest misleads is by
reporting a precise-looking statistic computed from a sample far too
small to support it, and the reader has no way to tell from the number
itself.

So every :class:`Metrics` carries ``sample_warning``, and ``trustworthy``
is False below a stated minimum. Nothing here is hidden if it is weak —
it is reported with the reason it is weak.

The second rule: a statistic that cannot be computed is None, not zero. A
profit factor with no losing trades is undefined, not infinite, and a
zero would read as "loses everything".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from gtcc.backtest.engine import BacktestResult, ClosedTrade, ExitReason
from gtcc.domain.money import D, ZERO

#: Below this many closed trades, ratios are reported but marked untrustworthy.
#: Not a statistical threshold anybody derived — a round number chosen to be
#: conservative, and stated so it can be argued with.
MIN_TRADES_FOR_RATIOS = 30


@dataclass(frozen=True, slots=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    scratches: int
    win_rate: Decimal | None
    total_pnl: Decimal
    return_pct: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    expectancy: Decimal | None
    #: Mean R multiple. None when no trade had measurable risk.
    average_r: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal
    max_drawdown_pct: Decimal | None
    longest_losing_streak: int
    stopped_out: int
    took_target: int
    open_at_end: int
    trustworthy: bool
    sample_warning: str = ""

    def describe(self) -> list[str]:
        lines = [
            f"{self.trades} closed trade(s): {self.wins} won, {self.losses} lost"
            + (f", {self.scratches} flat" if self.scratches else ""),
            f"Net P&L {self.total_pnl:,.2f}"
            + (f" ({self.return_pct:+.2f}% of starting equity)"
               if self.return_pct is not None else ""),
        ]
        if self.win_rate is not None:
            lines.append(f"Win rate {self.win_rate:.1f}%")
        if self.average_r is not None:
            lines.append(f"Average result {self.average_r:+.2f}R")
        if self.expectancy is not None:
            lines.append(f"Expectancy {self.expectancy:+,.2f} per trade")
        if self.profit_factor is not None:
            lines.append(f"Profit factor {self.profit_factor:.2f}")
        else:
            lines.append("Profit factor undefined (no losing trade to divide by)")
        lines.append(
            f"Worst drawdown {self.max_drawdown:,.2f}"
            + (f" ({self.max_drawdown_pct:.2f}%)"
               if self.max_drawdown_pct is not None else "")
        )
        lines.append(f"Longest losing streak {self.longest_losing_streak}")
        lines.append(
            f"Exits: {self.stopped_out} stopped, {self.took_target} at target"
            + (f", {self.open_at_end} still open when the data ran out"
               if self.open_at_end else "")
        )
        return lines

    def describe_with_warning(self) -> list[str]:
        """The statistics, preceded by the caveat that qualifies them.

        The warning goes FIRST. A reader who meets a profit factor before
        the sample size has already formed an impression by the time the
        caveat arrives.
        """
        if not self.sample_warning:
            return self.describe()
        return [self.sample_warning, ""] + self.describe()


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, ZERO) / D(len(values))


def measure(result: BacktestResult) -> Metrics:
    trades: tuple[ClosedTrade, ...] = result.trades
    wins = [trade for trade in trades if trade.pnl > ZERO]
    losses = [trade for trade in trades if trade.pnl < ZERO]
    scratches = [trade for trade in trades if trade.pnl == ZERO]

    total = sum((trade.pnl for trade in trades), ZERO)
    gross_win = sum((trade.pnl for trade in wins), ZERO)
    gross_loss = -sum((trade.pnl for trade in losses), ZERO)

    peak = result.starting_equity
    drawdown = ZERO
    for equity in result.equity_curve:
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)

    streak = 0
    longest = 0
    for trade in trades:
        if trade.pnl < ZERO:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

    r_values = [
        trade.r_multiple for trade in trades if trade.r_multiple is not None
    ]

    count = len(trades)
    trustworthy = count >= MIN_TRADES_FOR_RATIOS
    warning = ""
    if count == 0:
        warning = (
            "No trade was closed, so there is nothing to measure. That is not "
            "a result of zero; it is an absence of results."
        )
    elif not trustworthy:
        warning = (
            f"ONLY {count} CLOSED TRADES. Every ratio above is arithmetic on a "
            f"sample too small to mean anything — {MIN_TRADES_FOR_RATIOS} is the "
            "minimum this module will call trustworthy, and even that is a "
            "convention rather than a finding. Do not promote a strategy on this."
        )

    return Metrics(
        trades=count,
        wins=len(wins),
        losses=len(losses),
        scratches=len(scratches),
        win_rate=(D(len(wins)) / D(count) * D(100)) if count else None,
        total_pnl=total,
        return_pct=(
            total / result.starting_equity * D(100)
            if result.starting_equity > ZERO
            else None
        ),
        average_win=_mean([trade.pnl for trade in wins]),
        average_loss=_mean([trade.pnl for trade in losses]),
        expectancy=_mean([trade.pnl for trade in trades]),
        average_r=_mean(r_values),
        # Undefined rather than infinite when nothing lost: dividing by zero
        # is not a perfect strategy, it is a missing denominator.
        profit_factor=(gross_win / gross_loss) if gross_loss > ZERO else None,
        max_drawdown=drawdown,
        max_drawdown_pct=(
            drawdown / result.starting_equity * D(100)
            if result.starting_equity > ZERO
            else None
        ),
        longest_losing_streak=longest,
        stopped_out=len([t for t in trades if t.reason is ExitReason.STOP]),
        took_target=len([t for t in trades if t.reason is ExitReason.TARGET]),
        open_at_end=len([t for t in trades if t.reason is ExitReason.END_OF_DATA]),
        trustworthy=trustworthy,
        sample_warning=warning,
    )
