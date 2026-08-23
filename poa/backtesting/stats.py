"""Performance statistics.

Reports what happened, with the sample size attached to every number. Small
samples are labelled as such, because a 70% win rate over ten signals says
close to nothing.

Nothing here predicts future performance. Historical results describe the past
behaviour of the engine on the data it was given, and no more.
"""

from __future__ import annotations

import math
import random
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


def wilson_interval(wins: int, total: int) -> tuple[float, float] | None:
    """The 95% interval around a win rate, as percentages.

    A rate without one invites the mistake this exists to stop. Thirty-six
    trades at 66.7% reads like a working edge and is consistent with anything
    from 50.3% to 79.8% — which straddles break-even, so it is equally
    consistent with a losing tool. Wilson rather than the textbook normal
    interval, because the samples here are small and the rates near the edges.
    """
    if total <= 0:
        return None
    z = 1.96
    p = wins / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    spread = (
        z
        * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
        / (1 + z * z / total)
    )
    return (max(0.0, centre - spread) * 100, min(1.0, centre + spread) * 100)


def auc(pairs: Sequence[tuple[float, bool]]) -> tuple[float, float] | None:
    """Ranking power and its standard error, by Hanley–McNeil.

    Given one winning call and one losing call, how often did the score argue
    harder for the winner? Fifty is a score that says nothing. Below fifty is
    worse than nothing, because the weight is pulling the wrong way.

    An AUC without an interval is a number pretending to be a fact, and at
    these sample sizes the interval is usually wide enough to contain fifty.

    The standard error assumes independent calls. It is not, here — a run on
    one pair in one direction is one read sampled several times — so use
    ``cluster_bootstrap`` over ``episodes`` for the interval that can be
    believed, and this one for the point estimate.
    """
    wins = [value for value, won in pairs if won]
    losses = [value for value, won in pairs if not won]
    if not wins or not losses:
        return None
    better = sum(
        1.0 if a > b else 0.5 if a == b else 0.0 for a in wins for b in losses
    )
    a = better / (len(wins) * len(losses))
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    variance = (
        a * (1 - a)
        + (len(wins) - 1) * (q1 - a * a)
        + (len(losses) - 1) * (q2 - a * a)
    ) / (len(wins) * len(losses))
    return a * 100, math.sqrt(variance) * 100


def episodes(outcomes: Sequence[Outcome]) -> list[list[Outcome]]:
    """Calls grouped into the reads they actually were.

    A tool that says PUT on one pair and keeps saying it while the pair drifts
    down has not made six calls. It has made one, and sampled it six times.
    They settle together, so a rate measured over the rows counts one
    right-or-wrong answer several times and dilutes the independent ones
    beside it — and every interval printed over them is narrower than the
    evidence deserves.

    A run is broken by the pair changing, the direction changing, or a call on
    another pair coming between: the tool watches several charts at once, so
    consecutive rows are not consecutive on one chart.
    """
    grouped: list[list[Outcome]] = []
    for call in outcomes:
        key = (call.asset, call.direction)
        if grouped and (grouped[-1][0].asset, grouped[-1][0].direction) == key:
            grouped[-1].append(call)
        else:
            grouped.append([call])
    return grouped


#: Fixed, so the same session prints the same interval every time it is read.
#: A report whose numbers move when nothing else did cannot be trusted with
#: the ones that are supposed to move.
BOOTSTRAP_SEED = 20260821
BOOTSTRAP_ROUNDS = 20_000


def cluster_bootstrap(
    groups: Sequence[Sequence[Outcome]],
    statistic: Any,
    rounds: int = BOOTSTRAP_ROUNDS,
) -> tuple[float, float, list[float]] | None:
    """A 95% interval that resamples whole episodes rather than single calls.

    Resampling rows treats six samples of one opinion as six opinions and
    reports an interval far too narrow for what was actually observed.
    Resampling episodes keeps each read whole, which is the unit the tool
    really produced.

    Returns the interval and the sorted draws, so a caller can ask its own
    question of them — how often the statistic cleared a threshold, say.
    """
    usable = [list(g) for g in groups if g]
    if len(usable) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    draws: list[float] = []
    for _ in range(rounds):
        picked = [usable[rng.randrange(len(usable))] for _ in usable]
        value = statistic([call for group in picked for call in group])
        if value is not None:
            draws.append(value)
    if not draws:
        return None
    draws.sort()
    low = draws[int(0.025 * len(draws))]
    high = draws[min(len(draws) - 1, int(0.975 * len(draws)))]
    return low, high, draws


#: The bands the score is read in. Fixed in advance and never chosen to fit a
#: result: a split picked after seeing the outcomes finds a split in noise.
#: Narrower near the top because that is where a gate puts the calls: a
#: session run at 85 lands entirely in the last three, and two fat bands
#: would hide a decline running through them.
SCORE_BANDS: tuple[tuple[int, int], ...] = (
    (0, 69), (70, 79), (80, 84), (85, 89), (90, 94), (95, 100),
)


def band_of(score: float) -> tuple[int, int] | None:
    """Which band a score belongs to — every score, gaps included.

    The bands are written as integer edges, but the scores are not integers:
    the shown confidence rounds to one decimal, so 89.6 is a value the
    journal actually holds. Read literally, ``85 <= 89.6 <= 89`` and
    ``90 <= 89.6 <= 94`` are both false, and the call silently vanished
    from the very split the overheat evidence rests on. A band owns
    everything from its low edge up to — not including — the next band's
    low edge, and the top band owns its high edge too.
    """
    for low, high in SCORE_BANDS:
        if low <= score < high + 1 or (high == SCORE_BANDS[-1][1] and score == high):
            return (low, high)
    return None


def score_bands(outcomes: Sequence[Outcome]) -> list[dict[str, Any]]:
    """Win rate by score band.

    The question a single AUC cannot answer: not "does the score rank" but
    "does it rank the *right way all the way up*". A monotone decline across
    fixed bands is much harder to read as noise than one split that happened
    to look good, and it is what a score wired backwards looks like.
    """
    rows = []
    for low, high in SCORE_BANDS:
        band = [
            o for o in outcomes
            if o.outcome in ("win", "loss") and band_of(o.confidence) == (low, high)
        ]
        if not band:
            continue
        wins = sum(1 for o in band if o.outcome == "win")
        rows.append({
            "low": low, "high": high, "settled": len(band), "wins": wins,
            "win_rate": round(wins / len(band) * 100.0, 1),
        })
    return rows


def by_asset(outcomes: Sequence[Outcome]) -> list[dict[str, Any]]:
    """Per pair: how it settled, and how much of one mind the tool was.

    Direction concentration is the number that explains an unbeatable-looking
    baseline. A pair called sixteen times in one direction and never the other
    *is* always-BUY over that window, so the baseline comparison beside it
    cannot find an edge — both sides of it are the same strategy.
    """
    rows: dict[str, list[Outcome]] = defaultdict(list)
    for call in outcomes:
        rows[call.asset or "—"].append(call)
    out = []
    for asset, calls in rows.items():
        decided = [c for c in calls if c.outcome in ("win", "loss")]
        if not decided:
            continue
        wins = sum(1 for c in decided if c.outcome == "win")
        ups = sum(1 for c in calls if c.direction == "CALL")
        out.append({
            "asset": asset,
            "calls": len(calls),
            "settled": len(decided),
            "wins": wins,
            "win_rate": round(wins / len(decided) * 100.0, 1),
            "calls_up": ups,
            "calls_down": len(calls) - ups,
            "one_way": round(max(ups, len(calls) - ups) / len(calls) * 100.0, 1),
        })
    return sorted(out, key=lambda r: -r["calls"])


@dataclass
class Baseline:
    """What a rule with no opinion would have scored on the same entries."""

    name: str
    wins: int
    settled: int

    @property
    def win_rate(self) -> float | None:
        return None if not self.settled else self.wins / self.settled * 100

    def to_dict(self) -> dict[str, Any]:
        rate = self.win_rate
        interval = wilson_interval(self.wins, self.settled)
        return {
            "name": self.name,
            "wins": self.wins,
            "settled": self.settled,
            "win_rate": None if rate is None else round(rate, 1),
            "interval": None if interval is None
            else [round(interval[0], 1), round(interval[1], 1)],
        }


def directional_baselines(changes: Iterable[float | None]) -> list[Baseline]:
    """Always-buy and always-sell, judged on the engine's own entries.

    The number that matters is not the engine's win rate but the gap between
    it and this. A tool calling BUY thirty-four times out of thirty-six has
    barely made a decision, and over a half hour when price happened to rise
    it posts a healthy-looking rate for a reason that has nothing to do with
    its analysis. Measured on a real recording, always-buy beat the engine on
    every chart-and-expiry combination tried, on identical entries.

    Printed beside every result from now on, because that was found by
    happening to check rather than by the report saying so.
    """
    ups = downs = 0
    for change in changes:
        if change is None or change == 0:
            continue
        if change > 0:
            ups += 1
        else:
            downs += 1
    settled = ups + downs
    return [
        Baseline("always BUY", ups, settled),
        Baseline("always SELL", downs, settled),
    ]


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
    # Voided rows could not be settled honestly (wrong source, wrong scale, or
    # settled far too late). They are reported, never counted.
    voided = sum(1 for o in outcomes if o.outcome in ("void", "unknown"))
    wins = sum(1 for o in settled if o.outcome == "win")
    losses = sum(1 for o in settled if o.outcome == "loss")
    flat = sum(1 for o in settled if o.outcome == "flat")
    win_rate = _rate(wins, losses)
    streaks = compute_streaks(settled)

    decided = wins + losses
    return {
        "total_signals": len(outcomes),
        "settled": len(settled),
        "pending": len(outcomes) - len(settled) - voided,
        "voided": voided,
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
