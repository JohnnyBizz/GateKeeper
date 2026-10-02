"""Where the result actually came from — specification section 21.

A breakdown table is the most persuasive object in trading software and
the easiest to mislead with. Forty trades split across five regimes and
three sessions gives fifteen cells averaging under three trades each, and
every cell gets a win rate to two decimal places. The table looks like
analysis. It is mostly arithmetic on noise, and the reader cannot tell
which cells are which.

So every bucket here carries its own trade count, and a bucket below the
minimum reports no ratios at all — not small ratios, none. ``rated`` is
False and ``caveat`` says why. A reader skimming for the best regime
cannot accidentally pick one that had four trades in it.

The second rule: a bucket that does not exist is absent, not zero. A
strategy that never traded in a high-volatility regime has no
high-volatility row. Printing one with 0% would read as "tried it and it
failed".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Sequence

from gtcc.backtest.engine import ClosedTrade, ExitReason
from gtcc.domain.money import D, ZERO

#: Below this many trades in a bucket, no ratio is reported for it. Lower
#: than the 30 required of a whole backtest, because a breakdown is read as
#: a comparison between buckets rather than as a result on its own — but
#: still a convention, stated so it can be argued with.
MIN_TRADES_PER_BUCKET = 10

#: UTC hour ranges for the three main forex sessions. Approximate by nature:
#: sessions overlap and shift with daylight saving. Named so a reader knows
#: these are conventional boundaries rather than exchange hours.
SESSIONS: tuple[tuple[str, int, int], ...] = (
    ("ASIA", 0, 8),
    ("LONDON", 8, 13),
    ("LONDON_NY_OVERLAP", 13, 16),
    ("NEW_YORK", 16, 21),
    ("LATE", 21, 24),
)


def session_of(moment: datetime) -> str:
    hour = moment.astimezone(tz=moment.tzinfo).hour if moment.tzinfo else moment.hour
    for name, start, end in SESSIONS:
        if start <= hour < end:
            return name
    return "LATE"


@dataclass(frozen=True, slots=True)
class Bucket:
    """One slice of the trades, with its reliability attached."""

    key: str
    trades: int
    total_pnl: Decimal
    wins: int
    losses: int
    #: None when the bucket is too small to rate. Not 0 — an absence.
    win_rate: Decimal | None
    expectancy: Decimal | None
    average_r: Decimal | None
    stopped_out: int
    took_target: int
    rated: bool
    caveat: str = ""

    def describe(self) -> str:
        head = f"{self.key:<22}{self.trades:>5} trades  {self.total_pnl:>12,.2f}"
        if not self.rated:
            return f"{head}   {self.caveat}"
        return (
            f"{head}   {self.win_rate:>5.1f}% won"
            f"   {self.average_r:>+6.2f}R avg"
            f"   {self.stopped_out} stopped / {self.took_target} target"
        )


@dataclass(frozen=True, slots=True)
class Breakdown:
    dimension: str
    buckets: tuple[Bucket, ...]
    #: Trades that had no value for this dimension, so could not be placed.
    unattributed: int = 0

    def describe(self) -> list[str]:
        lines = [f"By {self.dimension}:"]
        if not self.buckets:
            lines.append("  no trade carried a value for this dimension")
            return lines
        for bucket in self.buckets:
            lines.append("  " + bucket.describe())
        if self.unattributed:
            lines.append(
                f"  {self.unattributed} trade(s) had no {self.dimension} recorded "
                "and are left out rather than bundled into a bucket"
            )
        rated = [bucket for bucket in self.buckets if bucket.rated]
        if not rated:
            lines.append(
                f"  NO BUCKET reaches {MIN_TRADES_PER_BUCKET} trades. This table "
                "shows where the trades fell, and nothing about which conditions "
                "work better."
            )
        return lines


def _bucket(key: str, trades: Sequence[ClosedTrade]) -> Bucket:
    count = len(trades)
    wins = [trade for trade in trades if trade.pnl > ZERO]
    losses = [trade for trade in trades if trade.pnl < ZERO]
    total = sum((trade.pnl for trade in trades), ZERO)
    rated = count >= MIN_TRADES_PER_BUCKET
    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]

    return Bucket(
        key=key,
        trades=count,
        total_pnl=total,
        wins=len(wins),
        losses=len(losses),
        win_rate=(D(len(wins)) / D(count) * D(100)) if rated and count else None,
        expectancy=(total / D(count)) if rated and count else None,
        average_r=(
            sum(r_values, ZERO) / D(len(r_values)) if rated and r_values else None
        ),
        stopped_out=len([t for t in trades if t.reason is ExitReason.STOP]),
        took_target=len([t for t in trades if t.reason is ExitReason.TARGET]),
        rated=rated,
        caveat=(
            ""
            if rated
            else f"too few to rate (under {MIN_TRADES_PER_BUCKET})"
        ),
    )


def breakdown(
    trades: Sequence[ClosedTrade],
    *,
    dimension: str,
    key_of: Callable[[ClosedTrade], str | None],
) -> Breakdown:
    """Group trades by one dimension, keeping the small buckets visible.

    A trade whose key is None is counted as unattributed rather than put
    in a bucket of its own: an "unknown regime" row invites comparison
    against the real ones.
    """
    groups: dict[str, list[ClosedTrade]] = {}
    unattributed = 0
    for trade in trades:
        key = key_of(trade)
        if key is None:
            unattributed += 1
            continue
        groups.setdefault(key, []).append(trade)

    buckets = [_bucket(key, group) for key, group in groups.items()]
    # Largest first: the buckets worth reading are the ones with trades in
    # them, and sorting by P&L would put a two-trade fluke at the top.
    buckets.sort(key=lambda bucket: (-bucket.trades, bucket.key))
    return Breakdown(
        dimension=dimension, buckets=tuple(buckets), unattributed=unattributed
    )


def by_regime(trades: Sequence[ClosedTrade]) -> Breakdown:
    return breakdown(trades, dimension="regime", key_of=lambda trade: trade.regime)


def by_session(trades: Sequence[ClosedTrade]) -> Breakdown:
    return breakdown(
        trades, dimension="session", key_of=lambda trade: session_of(trade.entry_at)
    )


def by_strategy(trades: Sequence[ClosedTrade]) -> Breakdown:
    return breakdown(
        trades, dimension="strategy", key_of=lambda trade: trade.strategy
    )


def by_side(trades: Sequence[ClosedTrade]) -> Breakdown:
    return breakdown(
        trades, dimension="direction", key_of=lambda trade: str(trade.side)
    )


def by_weekday(trades: Sequence[ClosedTrade]) -> Breakdown:
    names = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
    return breakdown(
        trades,
        dimension="weekday",
        key_of=lambda trade: names[trade.entry_at.weekday()],
    )


def all_breakdowns(trades: Sequence[ClosedTrade]) -> list[Breakdown]:
    return [
        by_strategy(trades),
        by_regime(trades),
        by_session(trades),
        by_side(trades),
        by_weekday(trades),
    ]
