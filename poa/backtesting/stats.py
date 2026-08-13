"""Performance statistics.

Reports what happened, with the sample size attached to every number. Small
samples are labelled as such, because a 70% win rate over ten signals says
close to nothing.

Nothing here predicts future performance. Historical results describe the past
behaviour of the engine on the data it was given, and no more.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..models import format_duration

# Below this many settled signals, a win rate is not worth reading.
MIN_MEANINGFUL_SAMPLE = 30


@dataclass
class Outcome:
    """One settled signal."""

    direction: str
    outcome: str  # "win" | "loss" | "flat" | "unknown"
    confidence: float
    trade_duration: int
    chart_timeframe: int
    setup_quality: str = ""
    regime: str = ""
    asset: str = ""
    timestamp: str = ""

    @property
    def settled(self) -> bool:
        return self.outcome in ("win", "loss", "flat")


@dataclass
class Streaks:
    max_wins: int = 0
    max_losses: int = 0
    current: int = 0
    current_kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_winning_streak": self.max_wins,
            "max_losing_streak": self.max_losses,
            "current_streak": self.current,
            "current_streak_kind": self.current_kind,
        }


def _rate(wins: int, losses: int) -> float | None:
    decided = wins + losses
    if decided == 0:
        return None
    return round(wins / decided * 100.0, 1)


def _expected_value(win_rate: float | None, payout: float = 0.80) -> float | None:
    """EV per unit staked at a given payout.

    Binary options pay less than they take: at an 80% payout a win returns 0.8
    and a loss costs 1.0, so break-even sits at about 55.6% — not 50%. That is
    the number a win rate has to be read against.
    """
    if win_rate is None:
        return None
    p = win_rate / 100.0
    return round(p * payout - (1.0 - p), 4)


def breakeven_rate(payout: float = 0.80) -> float:
    """The win rate needed just to break even at ``payout``."""
    return round(100.0 / (1.0 + payout), 1)


def compute_streaks(outcomes: Sequence[Outcome]) -> Streaks:
    streaks = Streaks()
    run = 0
    kind = ""
    for outcome in outcomes:
        if outcome.outcome not in ("win", "loss"):
            continue
        if outcome.outcome == kind:
            run += 1
        else:
            kind = outcome.outcome
            run = 1
        if kind == "win":
            streaks.max_wins = max(streaks.max_wins, run)
        else:
            streaks.max_losses = max(streaks.max_losses, run)
    streaks.current = run
    streaks.current_kind = kind
    return streaks


def _group_stats(
    outcomes: Iterable[Outcome], key
) -> list[dict[str, Any]]:
    groups: dict[Any, list[Outcome]] = defaultdict(list)
    for outcome in outcomes:
        groups[key(outcome)].append(outcome)

    rows: list[dict[str, Any]] = []
    for name, members in groups.items():
        wins = sum(1 for m in members if m.outcome == "win")
        losses = sum(1 for m in members if m.outcome == "loss")
        flat = sum(1 for m in members if m.outcome == "flat")
        rate = _rate(wins, losses)
        rows.append(
            {
                "group": str(name),
                "signals": len(members),
                "wins": wins,
                "losses": losses,
                "flat": flat,
                "win_rate": rate,
                "expected_value": _expected_value(rate),
                "sufficient_sample": (wins + losses) >= MIN_MEANINGFUL_SAMPLE,
                "average_confidence": (
                    round(sum(m.confidence for m in members) / len(members), 1)
                    if members
                    else None
                ),
            }
        )
    rows.sort(key=lambda r: -r["signals"])
    return rows


def summarise_outcomes(
    raw: Sequence[dict[str, Any]], payout: float = 0.80
) -> dict[str, Any]:
    """Build the full statistics block from journal or backtest rows."""
    outcomes = [
        Outcome(
            direction=str(row.get("direction", "")),
            outcome=str(row.get("outcome") or "pending"),
            confidence=float(row.get("confidence") or 0.0),
            trade_duration=int(row.get("trade_duration") or 0),
            chart_timeframe=int(row.get("chart_timeframe") or 0),
            setup_quality=str(row.get("setup_quality") or ""),
            regime=str(row.get("regime") or ""),
            asset=str(row.get("asset") or ""),
            timestamp=str(row.get("timestamp") or ""),
        )
        for row in raw
    ]

    settled = [o for o in outcomes if o.settled]
    wins = sum(1 for o in settled if o.outcome == "win")
    losses = sum(1 for o in settled if o.outcome == "loss")
    flat = sum(1 for o in settled if o.outcome == "flat")
    win_rate = _rate(wins, losses)
    streaks = compute_streaks(settled)

    decided = wins + losses
    return {
        "total_signals": len(outcomes),
        "settled": len(settled),
        "pending": len(outcomes) - len(settled),
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "win_rate": win_rate,
        "breakeven_rate": breakeven_rate(payout),
        "payout_assumed": payout,
        "expected_value": _expected_value(win_rate, payout),
        "average_confidence": (
            round(sum(o.confidence for o in outcomes) / len(outcomes), 1)
            if outcomes
            else None
        ),
        "average_confidence_wins": (
            round(sum(o.confidence for o in settled if o.outcome == "win") / wins, 1)
            if wins
            else None
        ),
        "average_confidence_losses": (
            round(sum(o.confidence for o in settled if o.outcome == "loss") / losses, 1)
            if losses
            else None
        ),
        "sufficient_sample": decided >= MIN_MEANINGFUL_SAMPLE,
        "sample_warning": (
            None
            if decided >= MIN_MEANINGFUL_SAMPLE
            else (
                f"Only {decided} settled signals. At least {MIN_MEANINGFUL_SAMPLE} "
                "are needed before a win rate means anything."
            )
        ),
        "streaks": streaks.to_dict(),
        "by_direction": _group_stats(settled, lambda o: o.direction),
        "by_duration": _group_stats(
            settled, lambda o: format_duration(o.trade_duration) if o.trade_duration else "unknown"
        ),
        "by_chart_timeframe": _group_stats(
            settled,
            lambda o: format_duration(o.chart_timeframe) if o.chart_timeframe else "unknown",
        ),
        "by_setup_quality": _group_stats(settled, lambda o: o.setup_quality or "unknown"),
        "by_regime": _group_stats(settled, lambda o: o.regime or "unknown"),
        "by_asset": _group_stats(settled, lambda o: o.asset or "unknown"),
        "disclaimer": (
            "Historical results describe how the engine behaved on this data. "
            "They do not predict future performance."
        ),
    }
