"""The whole record, pooled across every session this journal holds.

A session report judges one sitting, and one sitting is nearly always too
small to judge anything — the reports say so themselves, session after
session. What no report showed until now is the thing that grows: every
settled call this journal has ever recorded, pooled and split along the
axes that could actually condition this market — the expiry traded, the
pair, the score shown, the hour of day. Those splits are the standing open
questions in FINDINGS.md, and this section is where their answers accrete
one session at a time.

The pooling is stated honestly on the page: calls cluster inside sessions
and sessions inside days, so a column's *direction* is the readable part,
not its second decimal. And the same rule the rest of the report lives by
applies to every row: below a meaningful sample no rate is printed at all —
a row says how many calls it holds and that they are too few, because a
percentage over a handful is the easiest lie a report can tell.

Tool calls only. A manual row is the user's trade, not one of the tool's
reads, and mixing the two would grade the tool on decisions it did not
make.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..backtesting.stats import SCORE_BANDS
from ..logging_setup import get_logger
from ..models import format_duration

log = get_logger(__name__)

#: The same bar every other rate in the report answers to.
MEANINGFUL = 20

#: The pair table would otherwise grow one row per instrument ever glanced
#: at; the most-called carry nearly all the information.
MAX_PAIR_ROWS = 8


@dataclass
class LedgerRow:
    label: str
    wins: int = 0
    losses: int = 0

    @property
    def settled(self) -> int:
        return self.wins + self.losses

    @property
    def rate(self) -> float | None:
        if self.settled == 0:
            return None
        return round(self.wins / self.settled * 100.0, 1)


def collect_ledger(
    journal: Any, source: str | None = None
) -> dict[str, list[LedgerRow]] | None:
    """Pool every settled tool call in the journal, split four ways.

    Returns None when the journal holds nothing settled (or cannot be
    read) — the report then simply carries no ledger section, rather than
    an empty frame pretending to be one.
    """
    try:
        rows = journal.all_settled_calls(source=source)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not read the journal for the ledger: %s", exc)
        return None
    if not rows:
        return None

    overall = LedgerRow("all calls")
    by_expiry: dict[int, LedgerRow] = {}
    by_band: dict[tuple[int, int], LedgerRow] = {}
    by_pair: dict[str, LedgerRow] = {}
    by_hour: dict[str, LedgerRow] = {}

    for row in rows:
        won = row["outcome"] == "win"

        def tally(bucket: LedgerRow) -> None:
            bucket.wins += won
            bucket.losses += not won

        tally(overall)

        duration = int(row["trade_duration"] or 0)
        tally(
            by_expiry.setdefault(
                duration, LedgerRow(format_duration(duration))
            )
        )

        shown = float(row["overall_confidence"] or 0.0)
        for low, high in SCORE_BANDS:
            if low <= shown <= high:
                tally(
                    by_band.setdefault(
                        (low, high), LedgerRow(f"{low}-{high}")
                    )
                )
                break

        asset = str(row["asset"] or "")
        if asset:
            tally(by_pair.setdefault(asset, LedgerRow(asset)))

        stamp = str(row["timestamp"] or "")
        # ISO timestamps put the hour at a fixed offset; anything malformed
        # simply stays out of the hour table rather than inventing an hour.
        if len(stamp) >= 13 and stamp[11:13].isdigit():
            hour = stamp[11:13]
            tally(by_hour.setdefault(hour, LedgerRow(f"{hour}:00")))

    pairs = sorted(by_pair.values(), key=lambda r: -r.settled)[:MAX_PAIR_ROWS]

    return {
        "overall": [overall],
        "expiry": [by_expiry[k] for k in sorted(by_expiry)],
        "band": [by_band[k] for k in sorted(by_band)],
        "pair": pairs,
        "hour": [by_hour[k] for k in sorted(by_hour)],
    }


def _row_line(row: LedgerRow) -> str:
    count = f"{row.settled} call{'' if row.settled == 1 else 's'}"
    if row.settled < MEANINGFUL:
        return f"   {row.label:<14}{count:>12}   too few to read a rate from"
    return (
        f"   {row.label:<14}{count:>12}   "
        f"{row.rate:5.1f}%  ({row.wins}W/{row.losses}L)"
    )


def ledger_lines(ledger: dict[str, list[LedgerRow]]) -> list[str]:
    """Render the pooled record in the report's own voice."""
    total = ledger["overall"][0]
    lines = [
        "Every settled call this journal holds, across every session — the",
        "tool's own calls only, settled by their charts' prices. Calls",
        "cluster inside sessions and sessions inside days, so read the",
        "direction of a column, not its decimals. A row under "
        f"{MEANINGFUL} calls",
        "shows no rate: it is not hiding, it is too small to have one.",
        "",
    ]
    lines.append("BY EXPIRY")
    lines.extend(_row_line(row) for row in ledger["expiry"])
    lines.append("")
    lines.append("BY SCORE SHOWN")
    lines.extend(_row_line(row) for row in ledger["band"])
    lines.append("")
    lines.append("BY PAIR (most called)")
    lines.extend(_row_line(row) for row in ledger["pair"])
    lines.append("")
    lines.append("BY HOUR (UTC)")
    lines.extend(_row_line(row) for row in ledger["hour"])
    lines.append("")
    lines.append(_row_line(total).replace("   ", "", 1))
    return lines
