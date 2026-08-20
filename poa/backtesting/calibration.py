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

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from .stats import breakeven_rate, wilson_interval

# Settled trades needed before a bucket is allowed to claim anything.
MIN_SAMPLE = 20

# Score bands, in points. Ten is a compromise: narrow enough that a band means
# something, wide enough that a few hundred bars of history can populate one.
BAND_WIDTH = 10

# Gate thresholds worth reporting. Below 50 nothing is ever signalled, and
# above 90 no realistic history has the sample to say anything.
THRESHOLDS = (50, 55, 60, 65, 70, 75, 80, 85, 90)

# A binary that settles below this is worse than guessing. No gate that reads
# a chart this badly is worth walking toward, whatever the payout is paying.
COIN_FLIP = 50.0


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

    @property
    def standard_error(self) -> float | None:
        """How far this rate would wander on a sample this size, in points."""
        if self.settled == 0 or self.win_rate is None:
            return None
        p = self.win_rate / 100.0
        return round(math.sqrt(max(p * (1.0 - p), 0.0) / self.settled) * 100.0, 2)

    def clearly_above(self, floor: float) -> bool:
        """Is this rate past ``floor`` by more than the sample's own noise?

        The partner to :meth:`clearly_below`, and held to the same standard.
        A hundred and sixty trades at 51.25% is not a chart being read well,
        it is a coin landing slightly more one way, and letting a gap that
        size move a gate is the same superstition in the opposite direction.
        """
        if not self.meaningful or self.win_rate is None:
            return False
        error = self.standard_error
        if error is None:
            return False
        return (self.win_rate - error) > floor

    def clearly_below(self, breakeven: float) -> bool:
        """Is this rate short of break-even by more than the sample's own noise?

        Twenty trades at 50% against a 52.1% break-even is not evidence of
        anything — the standard error on twenty trades is around eleven points,
        so that gap is well inside what chance produces. Refusing to trade on
        it would be superstition with a decimal point. The shortfall has to
        exceed one standard error before it counts as a finding.
        """
        if not self.meaningful or self.win_rate is None:
            return False
        error = self.standard_error
        if error is None:
            return False
        return (breakeven - self.win_rate) > error

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
    # How well the expiry fitted, scored separately from the direction. Gated
    # separately too, so it needs its own threshold table.
    duration_score: float = 0.0


@dataclass
class Calibration:
    """What the score, the regime and the clock were actually worth."""

    bands: list[Bucket] = field(default_factory=list)
    thresholds: list[tuple[int, Bucket]] = field(default_factory=list)
    duration_thresholds: list[tuple[int, Bucket]] = field(default_factory=list)
    by_regime: dict[str, Bucket] = field(default_factory=dict)
    by_hour: dict[int, Bucket] = field(default_factory=dict)
    by_direction: dict[str, Bucket] = field(default_factory=dict)
    payout: float = 0.92
    min_sample: int = MIN_SAMPLE
    total: int = 0
    # True when this was built from trades that were actually placed rather
    # than from a replay. Real trades carry the click delay, the broker's own
    # settlement and the user's own hesitation; replayed ones carry none of it.
    from_real_trades: bool = False
    # How many real settled trades exist for this chart, whether or not there
    # were enough of them to build on.
    real_available: int = 0

    @property
    def provenance(self) -> str:
        if self.from_real_trades:
            return f"your {self.total} settled trades"
        return f"{self.total} replayed setups"

    @property
    def breakeven(self) -> float:
        """The rate this payout needs to be worth taking. For display only."""
        return breakeven_rate(self.payout)

    @property
    def decision_threshold(self) -> float:
        """The rate a setup has to clear to count as *right*, not as *profitable*.

        Deliberately fifty, and deliberately not the payout's break-even.

        Whether a call is worth taking at 50% or 92% is a question about money,
        and it belongs to whoever is placing the trade — they said so, more
        than once. Judging the analysis by it meant the same reading of the
        same chart was endorsed on one pair and vetoed on another purely
        because the broker was paying less that morning, which is not a fact
        about the chart. Worse, on a bad payout the veto tightened to 66.7%,
        so the tool went quiet exactly when it had least to do with the market.

        So the record answers one question only: do setups like this one come
        out right more often than a coin. The payout is still printed in the
        risk block, where the money question lives.
        """
        return 50.0

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
        breakeven = self.decision_threshold
        band = self.measured_rate(score)
        if band is None:
            return None, (
                "No measured record for setups scoring like this one yet."
            )

        rate = band.win_rate or 0.0
        detail = (
            f"Setups scoring {band.label} have settled at {rate:.0f}% here "
            f"over {band.settled} trades (a coin flip is {breakeven:.0f}%)"
        )

        # A regime that loses on its own record overrides a score band that
        # looks fine, because the band is averaged across every regime.
        slice_ = self.by_regime.get(regime)
        if slice_ is not None and slice_.clearly_below(breakeven):
            return False, (
                f"{detail}; and in a {regime.replace('_', ' ').lower()} market "
                f"they have settled at {slice_.win_rate:.0f}% over "
                f"{slice_.settled}"
            )
        # Short of break-even is not the same as *measurably* short of it.
        # Twenty trades at 50% against a 52.1% break-even is inside the noise,
        # and refusing to trade on that gap is superstition with a decimal
        # point. Only a shortfall bigger than the sample's own error counts.
        if band.clearly_below(breakeven):
            return False, detail
        return True, detail

    def overall_rate(self) -> float | None:
        """How often setups on this chart have won, across every regime."""
        wins = sum(bucket.wins for bucket in self.by_regime.values())
        losses = sum(bucket.losses for bucket in self.by_regime.values())
        total = wins + losses
        return round(wins / total * 100.0, 1) if total else None

    def weak_regimes(self) -> dict[str, Bucket]:
        """Market conditions this chart measurably wins less often in.

        Not "loses money in" — how much a win pays is somebody else's
        arithmetic, and it changes with the payout on offer without anything
        about the market changing at all. This is the simpler and more useful
        question: *are these the conditions where the reading is least often
        right?*

        Judged against this chart's own overall rate rather than a fixed
        number, because a chart that reads at 70% everywhere and one that
        reads at 45% everywhere need different bars, and neither of them is a
        constant anybody could pick in advance. A regime has to fall short of
        that average by more than the sample's own error before it counts —
        eight trades running badly is a bad afternoon, not a finding.

        Unlike a verdict on the score band, this cannot silence the tool: it
        rules out some conditions and leaves the rest open, so trading
        continues and the record keeps growing. That is what makes it safe to
        act on replayed evidence rather than waiting for real trades.
        """
        average = self.overall_rate()
        if average is None:
            return {}
        weak: dict[str, Bucket] = {}
        for name, bucket in self.by_regime.items():
            if not bucket.meaningful or bucket.win_rate is None:
                continue
            error = bucket.standard_error or 0.0
            if average - bucket.win_rate > error:
                weak[name] = bucket
        # Never every regime at once. If each of them reads below the average
        # the average is wrong, not the market, and refusing them all would be
        # a tool that has argued itself into never trading.
        if len(weak) >= len(self.by_regime):
            return {}
        return weak

    def recommended_duration_threshold(self) -> tuple[int, Bucket] | None:
        """The expiry-fit gate the record says was worth the most."""
        return self._best_of(self.duration_thresholds)

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
        return self._best_of(self.thresholds)

    def _best_of(
        self, table: list[tuple[int, Bucket]]
    ) -> tuple[int, Bucket] | None:
        floor = self.decision_threshold
        populated = [b for b in self.bands if b.settled]
        discriminating = len(populated) > 1
        baseline = table[0][1] if table else None
        baseline_rate = (baseline.win_rate or 0.0) if baseline is not None else 0.0
        best: tuple[float, int, Bucket] | None = None
        for threshold, bucket in table:
            if not bucket.meaningful or bucket.win_rate is None:
                continue
            # Ranked by the *lower* end of the band's interval rather than by
            # its rate, and never by what it would have paid.
            #
            # Expected value let the payout pick the gate — the same chart
            # chose differently on a 50% morning than a 92% one, having
            # learned nothing about the market. Plain accuracy swings the
            # other way and hands it to whichever thin band got lucky, since
            # twenty trades at 100% outranks eighty at 65%. The lower bound
            # asks what the band can be relied on for, so a small sample has
            # to be very good to beat a large one, and a coin flip clears
            # nothing at all.
            # Eligibility and ranking are two questions, and conflating them
            # broke both. Requiring the *interval* to clear a coin is the
            # right bar for publishing a finding and the wrong one for
            # choosing a gate: twenty-four real trades at 67% have a lower
            # bound of 43%, so nothing a session could realistically produce
            # would ever move it, and the record could not learn from the
            # trades it was built to learn from.
            #
            # So: to be eligible a band has to be right more often than a
            # coin. To win, it has to be the one that can be *relied* on for
            # it — which is the lower bound, and which is what stops twenty
            # lucky trades at 100% outranking eighty solid ones at 65%.
            if not bucket.clearly_above(floor):
                continue
            # A gate has to be better than taking everything, or it is not a
            # gate — it is the same trades with a number written next to them.
            # When every band settles at the same rate the score is separating
            # nothing, and moving the threshold along it only changes how many
            # of those identical trades get taken.
            #
            # Unless there is only one band with anything in it, in which case
            # there is no separating to be done and the only question is
            # whether those setups come out right. That is the case that
            # matters most in practice: a gate refusing setups that scored 72
            # is corrected by twenty-four of them settling at 67%, and holding
            # that to a discrimination test it cannot take would keep the gate
            # shut on the evidence against it.
            if discriminating and bucket is not baseline:
                if not bucket.clearly_above(baseline_rate):
                    continue
            elif discriminating:
                continue
            interval = wilson_interval(bucket.wins, bucket.settled)
            if interval is None:
                continue
            if best is None or interval[0] > best[0]:
                best = (interval[0], threshold, bucket)
        if best is not None:
            return best[1], best[2]
        return self._best_discriminator(table)

    def _best_discriminator(
        self, table: list[tuple[int, Bucket]]
    ) -> tuple[int, Bucket] | None:
        """The gate that reads this chart best, whatever the payout pays.

        When nothing clears break-even the expected-value ranking has nothing
        to return, and returning nothing froze the gate whenever a chart was
        marginal — which is most of them. But "no gate clears this payout" is a
        fact about the payout, not about which score threshold separates the
        winners from the losers on this chart, and those are two questions.

        This answers the second one. Whether the answer is worth trading at the
        payout on offer is the first one, it belongs to whoever is taking the
        trade, and the panel prints the break-even rate beside every number so
        that it can be answered.

        Two things it will not do, because both would be loosening until
        something fires — which is the worst thing this tool could do.

        It will not point at a gate that is no better than taking everything.
        A chart where every band settles at the same rate has nothing to find:
        the score is not separating anything, and moving the gate along it only
        changes how many of the same trades get taken. The improvement over the
        loosest gate has to clear that gate's own noise before it counts.

        And it will not point at a gate that loses to a coin. Below 50% the
        reading is worse than no reading, and walking the gate toward it would
        take more of exactly the trades that are not working. On a chart like
        that the answer is a different chart, and the honest output is silence.
        """
        usable = [
            (threshold, bucket)
            for threshold, bucket in table
            if bucket.meaningful and bucket.win_rate is not None
        ]
        if not usable:
            return None

        # The loosest gate is "take everything this survey found". Anything
        # stricter has to beat it to be worth having.
        base = usable[0][1]
        best_threshold, best_bucket = max(usable, key=lambda row: row[1].win_rate or 0.0)
        rate = best_bucket.win_rate or 0.0

        if rate < COIN_FLIP:
            return None
        margin = base.standard_error or 0.0
        if rate - (base.win_rate or 0.0) <= margin:
            return None
        return best_threshold, best_bucket

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

    # The same question for the expiry gate, which is scored and gated apart
    # from the direction and therefore needs its own answer.
    for threshold in THRESHOLDS:
        bucket = Bucket(f"{threshold}+", min_sample=min_sample)
        for row in rows:
            if row.duration_score >= threshold:
                _add(bucket, row.won)
        calibration.duration_thresholds.append((threshold, bucket))

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
                duration_score=float(getattr(trade, "duration_confidence", 0.0)),
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
