"""Turning the score into a measured probability.

The 0-100 score is an opinion. Its weights were chosen by judgement, and a
setup scoring 78 is not thereby 78% likely to win — the number has no units and
nothing has ever checked it against an outcome.

This module checks it. Given settled trades that each carry the score they were
issued at, it reports what setups in that neighbourhood *actually* settled at.
The score stops being a claim and becomes an index into a measurement.

Three views, because they answer three different questions:

* **bands** — "what do setups scoring 70-80 settle at?" Diagnostic: it shows
  whether the score is monotonic at all, which is the first thing to know. A
  score whose 80s lose more often than its 60s is not ranking anything.
* **thresholds** — "if I only took setups at 75 or better, what would I have
  got?" This is the one that sets a gate, and it is far better powered than the
  bands because every trade at or above the threshold counts toward it.
* **by_regime / by_hour / by_direction** — where the edge lives. Trending
  markets and chop are different games; so are the London open and 3am; and a
  tool that only gets calls right on a rising chart has found the trend rather
  than an edge.

Everything here refuses to have an opinion below ``min_sample`` settled trades.
A 100% win rate over three trades is the most confident-looking and least
informative number this codebase can produce, and the guard exists because that
number is exactly what a tool like this is tempted to show.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from .stats import breakeven_rate

# Settled trades needed before a bucket is allowed to claim anything.
MIN_SAMPLE = 20

# Score bands, in points. Ten is a compromise: narrow enough that a band means
# something, wide enough that a few hundred bars of history can populate one.
BAND_WIDTH = 10

# Gate thresholds worth reporting. Below 50 nothing is ever signalled, and
# above 90 no realistic history has the sample to say anything.
THRESHOLDS = (50, 55, 60, 65, 70, 75, 80, 85, 90)


@dataclass
class Bucket:
    """Wins and losses for one slice of the record."""

    label: str
    wins: int = 0
    losses: int = 0
    min_sample: int = MIN_SAMPLE

    @property
    def settled(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        if self.settled == 0:
            return None
        return round(self.wins / self.settled * 100.0, 1)

    @property
    def meaningful(self) -> bool:
        return self.settled >= self.min_sample

    def edge(self, breakeven: float) -> float | None:
        rate = self.win_rate
        if rate is None:
            return None
        return round(rate - breakeven, 1)

    def beats(self, breakeven: float) -> bool | None:
        """True/False only when the sample can carry the claim; None otherwise."""
        if not self.meaningful or self.win_rate is None:
            return None
        return self.win_rate >= breakeven

    def to_dict(self, breakeven: float = 52.1) -> dict[str, Any]:
        return {
            "label": self.label,
            "wins": self.wins,
            "losses": self.losses,
            "settled": self.settled,
            "win_rate": self.win_rate,
            "edge": self.edge(breakeven),
            "meaningful": self.meaningful,
            "beats_breakeven": self.beats(breakeven),
        }


@dataclass
class Record:
    """One settled trade, reduced to what calibration needs from it."""

    score: float
    won: bool
    regime: str = ""
    hour: int | None = None
    direction: str = ""


@dataclass
class Calibration:
    """What the score, the regime and the clock were actually worth."""

    bands: list[Bucket] = field(default_factory=list)
    thresholds: list[tuple[int, Bucket]] = field(default_factory=list)
    by_regime: dict[str, Bucket] = field(default_factory=dict)
    by_hour: dict[int, Bucket] = field(default_factory=dict)
    by_direction: dict[str, Bucket] = field(default_factory=dict)
    payout: float = 0.92
    min_sample: int = MIN_SAMPLE
    total: int = 0

    @property
    def breakeven(self) -> float:
        return breakeven_rate(self.payout)

    # -- looking a live setup up in the record ------------------------------

    def band_for(self, score: float) -> Bucket | None:
        """The band a score falls in, or None when nothing was measured there."""
        low = int(score // BAND_WIDTH) * BAND_WIDTH
        label = _band_label(low)
        for bucket in self.bands:
            if bucket.label == label:
                return bucket
        return None

    def measured_rate(self, score: float) -> Bucket | None:
        """The band, but only when it has the sample to be worth reading."""
        bucket = self.band_for(score)
        if bucket is None or not bucket.meaningful:
            return None
        return bucket

    def verdict(self, score: float, regime: str = "") -> tuple[bool | None, str]:
        """Does the record support taking a setup like this one?

        Returns ``(beats_breakeven, why)``. ``None`` means the record has no
        opinion — which is different from "no", and is said differently.
        """
        breakeven = self.breakeven
        band = self.measured_rate(score)
        if band is None:
            return None, (
                "No measured record for setups scoring like this one yet."
            )

        rate = band.win_rate or 0.0
        detail = (
            f"Setups scoring {band.label} have settled at {rate:.0f}% here "
            f"over {band.settled} trades (break-even {breakeven:.0f}%)"
        )

        # A regime that loses on its own record overrides a score band that
        # looks fine, because the band is averaged across every regime.
        slice_ = self.by_regime.get(regime)
        if slice_ is not None and slice_.beats(breakeven) is False:
            return False, (
                f"{detail}; and in a {regime.replace('_', ' ').lower()} market "
                f"they have settled at {slice_.win_rate:.0f}% over "
                f"{slice_.settled}"
            )
        return band.beats(breakeven), detail

    def recommended_threshold(self) -> tuple[int, Bucket] | None:
        """The score gate the record says was worth the most.

        Not the highest win rate, and not the lowest gate that scrapes past
        break-even. A gate trades signal count for hit rate, and either extreme
        gets that trade wrong: the loosest gate takes weak setups that drag the
        average down, while the strictest can leave a better total on the table
        by refusing forty profitable trades to avoid ten bad ones.

        So it ranks by what the whole sample would have *returned* — trades
        times expected value per trade, in units of stake — and only considers
        gates that clear break-even on a sample big enough to mean anything.
        Ties go to the looser gate, which keeps more setups for the same money.
        """
        breakeven = self.breakeven
        best: tuple[float, int, Bucket] | None = None
        for threshold, bucket in self.thresholds:
            if not bucket.beats(breakeven):
                continue
            rate = (bucket.win_rate or 0.0) / 100.0
            expected = bucket.settled * (rate * self.payout - (1.0 - rate))
            if best is None or expected > best[0]:
                best = (expected, threshold, bucket)
        if best is None:
            return None
        return best[1], best[2]

    def best_hours(self, limit: int = 3) -> list[tuple[int, Bucket]]:
        ranked = [
            (hour, bucket)
            for hour, bucket in sorted(self.by_hour.items())
            if bucket.meaningful and bucket.win_rate is not None
        ]
        ranked.sort(key=lambda item: item[1].win_rate or 0.0, reverse=True)
        return ranked[:limit]

    def to_dict(self) -> dict[str, Any]:
        breakeven = self.breakeven
        recommended = self.recommended_threshold()
        return {
            "total": self.total,
            "payout": self.payout,
            "breakeven": breakeven,
            "min_sample": self.min_sample,
            "bands": [b.to_dict(breakeven) for b in self.bands],
            "thresholds": [
                {"threshold": t, **b.to_dict(breakeven)} for t, b in self.thresholds
            ],
            "by_regime": {k: v.to_dict(breakeven) for k, v in self.by_regime.items()},
            "by_hour": {str(k): v.to_dict(breakeven) for k, v in self.by_hour.items()},
            "by_direction": {
                k: v.to_dict(breakeven) for k, v in self.by_direction.items()
            },
            "recommended_threshold": (
                {"threshold": recommended[0], **recommended[1].to_dict(breakeven)}
                if recommended
                else None
            ),
        }


def _band_label(low: int) -> str:
    return f"{low}-{low + BAND_WIDTH}"


def _add(bucket: Bucket, won: bool) -> None:
    if won:
        bucket.wins += 1
    else:
        bucket.losses += 1


def build_calibration(
    records: Iterable[Record],
    payout: float = 0.92,
    min_sample: int = MIN_SAMPLE,
) -> Calibration:
    """Reduce settled trades to what the score and the clock were worth."""
    rows = list(records)
    calibration = Calibration(payout=payout, min_sample=min_sample, total=len(rows))

    bands: dict[str, Bucket] = {}
    regimes: dict[str, Bucket] = {}
    hours: dict[int, Bucket] = {}
    directions: dict[str, Bucket] = {}

    for row in rows:
        low = int(max(0.0, row.score) // BAND_WIDTH) * BAND_WIDTH
        label = _band_label(low)
        _add(bands.setdefault(label, Bucket(label, min_sample=min_sample)), row.won)

        if row.regime:
            _add(
                regimes.setdefault(row.regime, Bucket(row.regime, min_sample=min_sample)),
                row.won,
            )
        if row.hour is not None:
            _add(
                hours.setdefault(
                    row.hour, Bucket(f"{row.hour:02d}:00 UTC", min_sample=min_sample)
                ),
                row.won,
            )
        if row.direction:
            _add(
                directions.setdefault(
                    row.direction, Bucket(row.direction, min_sample=min_sample)
                ),
                row.won,
            )

    calibration.bands = [bands[key] for key in sorted(bands, key=_band_sort_key)]
    calibration.by_regime = regimes
    calibration.by_hour = hours
    calibration.by_direction = directions

    # Cumulative "at or above" buckets: what a gate set here would have got.
    for threshold in THRESHOLDS:
        bucket = Bucket(f"{threshold}+", min_sample=min_sample)
        for row in rows:
            if row.score >= threshold:
                _add(bucket, row.won)
        calibration.thresholds.append((threshold, bucket))

    return calibration


def _band_sort_key(label: str) -> int:
    try:
        return int(label.split("-", 1)[0])
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return 0


def records_from_trades(trades: Sequence[Any]) -> list[Record]:
    """Reduce backtest ``PaperTrade``s to calibration records.

    Flat expiries are dropped rather than counted: a binary that settles where
    it opened is a refund, and forcing it into a win or a loss moves every rate
    here for no reason.
    """
    records: list[Record] = []
    for trade in trades:
        outcome = getattr(trade, "outcome", None)
        if outcome not in ("win", "loss"):
            continue
        records.append(
            Record(
                # The direction score, matching what the gate has in hand when
                # it consults this record. The duration fit is a separate
                # question with a separate gate.
                score=float(getattr(trade, "direction_confidence", 0.0)),
                won=outcome == "win",
                regime=str(getattr(trade, "regime", "") or ""),
                hour=_hour_of(getattr(trade, "timestamp", None)),
                direction=str(getattr(trade, "direction", "") or ""),
            )
        )
    return records


def _hour_of(timestamp: Any) -> int | None:
    if isinstance(timestamp, datetime):
        return timestamp.hour
    if isinstance(timestamp, str) and timestamp:
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).hour
        except ValueError:
            return None
    return None
