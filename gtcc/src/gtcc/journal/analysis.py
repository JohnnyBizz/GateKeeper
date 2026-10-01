"""Measure real trades with the code that measures backtested ones.

A platform that scores its backtests one way and its paper trading another
cannot answer the only question worth asking of a backtest: did the thing
it predicted actually happen. Any difference between the two measurements
is then indistinguishable from a difference in the strategy.

So journal rows are converted into the same `ClosedTrade` the backtester
produces, and every statistic — metrics, breakdowns, the small-bucket
rules, the sample-size caveats — comes from the same functions. The
conversion is the only new code; nothing here recomputes a statistic.

Only rows with an outcome convert. A setup that was refused, or one still
open, has no result to measure, and including it with a P&L of zero would
drag every average toward nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from gtcc.backtest.attribution import all_breakdowns, Breakdown
from gtcc.backtest.engine import BacktestResult, BarCosts, ClosedTrade, ExitReason
from gtcc.backtest.metrics import Metrics, measure
from gtcc.domain.enums import Side, Timeframe
from gtcc.domain.money import D, ZERO


@dataclass(frozen=True, slots=True)
class JournalSummary:
    """What the account actually did, and what it refused to do."""

    metrics: Metrics
    breakdowns: tuple[Breakdown, ...]
    #: Rows with no outcome yet: still open, or closed without being measured.
    unmeasured: int
    #: Setups the risk engine refused. Counted because they are the other
    #: half of the record: a strategy whose every idea is refused looks
    #: identical to one with no ideas if only taken trades are shown.
    refused: int
    #: Counts per refusal check, so a limit that refuses everything is
    #: visible rather than something to infer from a small trade count.
    refusal_reasons: tuple[tuple[str, int], ...] = ()

    def describe(self) -> list[str]:
        lines = self.metrics.describe_with_warning()
        lines += [""]
        if self.unmeasured:
            lines.append(
                f"{self.unmeasured} row(s) have no result yet — still open, or "
                "closed without a measurement. They are excluded rather than "
                "counted as flat."
            )
        if self.refused:
            lines.append(f"{self.refused} setup(s) were refused by the risk engine:")
            for code, count in self.refusal_reasons:
                lines.append(f"  {count:>4}  {code}")
            lines.append(
                "  A strategy whose every idea is refused looks identical to one "
                "with no ideas, if only taken trades are shown."
            )
        for report in self.breakdowns:
            lines += [""] + report.describe()
        return lines


def _side(direction: str) -> Side:
    return Side.SELL if str(direction).upper() in ("SELL", "SHORT") else Side.BUY


def to_closed_trade(row) -> ClosedTrade | None:
    """Convert a journal row into the backtester's own trade record.

    Returns None for a row with no result. A row that was refused, or is
    still open, is not a trade with a P&L of zero — it is a row with no
    P&L, and the difference matters to every average computed afterwards.
    """
    if row.realised_pnl is None:
        return None
    entry = row.actual_entry if row.actual_entry is not None else row.planned_entry
    exit_price = row.actual_exit
    if entry is None or exit_price is None:
        return None

    stop = row.planned_stop
    quantity = row.actual_size or row.planned_size or ZERO
    opened = row.opened_at or row.considered_at
    closed = row.closed_at or opened

    try:
        reason = ExitReason(str(row.exit_reason))
    except ValueError:
        # An exit reason this module does not know is not forced into one of
        # its own: END_OF_DATA is the backtester's "we stopped looking",
        # which is the closest honest reading of "closed, cause unrecorded".
        reason = ExitReason.END_OF_DATA

    return ClosedTrade(
        symbol=row.symbol,
        strategy=row.strategy,
        side=_side(row.direction),
        quantity=D(quantity),
        entry_index=0,
        entry_at=opened,
        entry_price=D(entry),
        exit_index=0,
        exit_at=closed,
        exit_price=D(exit_price),
        reason=reason,
        planned_stop=D(stop) if stop is not None else D(entry),
        planned_target=(
            D(row.planned_targets[0])
            if row.planned_targets
            else None
        ),
        costs=D(row.fees or 0),
        pnl=D(row.realised_pnl),
        r_multiple=D(row.r_multiple) if row.r_multiple is not None else None,
        regime=row.regime,
        rationale=row.notes or "",
    )


def summarise(rows: Sequence, *, starting_equity: Decimal) -> JournalSummary:
    """Measure a set of journal rows the way a backtest is measured."""
    trades: list[ClosedTrade] = []
    unmeasured = 0
    refused = 0
    reasons: dict[str, int] = {}

    for row in rows:
        if row.outcome != "TAKEN":
            refused += 1
            for failure in (row.risk_verdict or {}).get("failures", []):
                code = str(failure.get("code", "unknown"))
                reasons[code] = reasons.get(code, 0) + 1
            continue
        trade = to_closed_trade(row)
        if trade is None:
            unmeasured += 1
            continue
        trades.append(trade)

    trades.sort(key=lambda trade: trade.entry_at)
    running = D(starting_equity)
    curve = [running]
    for trade in trades:
        running += trade.pnl
        curve.append(running)

    result = BacktestResult(
        symbol="(account)",
        timeframe=Timeframe.M15,
        strategy="(all)",
        bars_seen=0,
        bars_traded=0,
        first_bar_at=trades[0].entry_at if trades else None,
        last_bar_at=trades[-1].exit_at if trades else None,
        starting_equity=D(starting_equity),
        ending_equity=running,
        # Real fills carry their real costs on each trade; there is no
        # assumption to declare, so the declared-cost record is left at its
        # zero default rather than inventing a figure to fill it.
        costs=BarCosts(spread_bps=ZERO, slippage_bps=ZERO, commission_bps=ZERO),
        trades=tuple(trades),
        equity_curve=tuple(curve),
    )

    return JournalSummary(
        metrics=measure(result),
        breakdowns=tuple(all_breakdowns(trades)),
        unmeasured=unmeasured,
        refused=refused,
        refusal_reasons=tuple(
            sorted(reasons.items(), key=lambda pair: (-pair[1], pair[0]))
        ),
    )
