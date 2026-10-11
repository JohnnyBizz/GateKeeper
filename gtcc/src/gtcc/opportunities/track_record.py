"""Has this setup made money before, on data it was not tuned on?

Every other piece of evidence in a dossier is read off the same chart the
idea came from, so it all tends to agree for the same reason. This is the
one piece that is different in kind: each setup is replayed through the
real backtester and risk engine on recorded history, in-sample and on a
held-out final fifth, and the result is stored.

Three verdicts, and the thresholds are conventions stated as conventions:

  MEASURED_EDGE   >= 30 in-sample and >= 10 held-out trades, positive
                  average R in both
  NO_EDGE         >= 10 held-out trades with an average R at or below zero.
                  Disqualifying: a setup that lost on unseen data is not
                  surfaced as an idea on that instrument.
  INSUFFICIENT    anything else — measured, but too few trades to mean
                  anything. Treated as unknown, never as a pass.

What is measured is the setup's TRIGGER alone, without the evidence filter
the finder applies. The filter can only remove trades, so this is the base
rate the filter is working from — and the filter itself is unmeasured,
which the dossier says.

The held-out segment is counted each time it is measured. Nothing can stop
a second look, but after the first one the data is no longer held out in
any useful sense, and the count travels with the record so that cannot be
hidden.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Sequence

from gtcc.backtest import BacktestSettings, Backtester, BarCosts, split
from gtcc.domain.enums import Decision, Timeframe
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, utcnow
from gtcc.domain.money import D, ZERO
from gtcc.opportunities.analysis import analyse
from gtcc.opportunities.setups import Setup
from gtcc.risk.limits import RiskLimits
from gtcc.strategies.base import (
    Proposal,
    Strategy,
    StrategyContext,
    ValidationStatus,
)

MIN_IN_SAMPLE_TRADES = 30
MIN_OUT_OF_SAMPLE_TRADES = 10


class Verdict(StrEnum):
    MEASURED_EDGE = "MEASURED_EDGE"
    NO_EDGE = "NO_EDGE"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class SetupRecord:
    setup: str
    symbol: str
    timeframe: str
    in_sample_trades: int
    in_sample_expectancy_r: str | None
    out_of_sample_trades: int
    out_of_sample_expectancy_r: str | None
    first_bar: str | None
    last_bar: str | None
    measured_at: str
    costs: str
    looks: int = 1

    @property
    def in_sample_r(self) -> Decimal | None:
        return D(self.in_sample_expectancy_r) if self.in_sample_expectancy_r else None

    @property
    def out_of_sample_r(self) -> Decimal | None:
        return (
            D(self.out_of_sample_expectancy_r)
            if self.out_of_sample_expectancy_r else None
        )

    @property
    def verdict(self) -> Verdict:
        oos = self.out_of_sample_r
        if self.out_of_sample_trades >= MIN_OUT_OF_SAMPLE_TRADES and oos is not None:
            if oos <= ZERO:
                return Verdict.NO_EDGE
            if (
                self.in_sample_trades >= MIN_IN_SAMPLE_TRADES
                and self.in_sample_r is not None
                and self.in_sample_r > ZERO
            ):
                return Verdict.MEASURED_EDGE
        return Verdict.INSUFFICIENT

    def describe(self) -> str:
        def r(value: Decimal | None) -> str:
            return f"{value:+.2f}R" if value is not None else "no trades"

        text = (
            f"{self.in_sample_trades} in-sample trades averaging "
            f"{r(self.in_sample_r)}; {self.out_of_sample_trades} trades on held-out "
            f"data averaging {r(self.out_of_sample_r)} "
            f"({self.first_bar[:10] if self.first_bar else '?'} to "
            f"{self.last_bar[:10] if self.last_bar else '?'}, {self.costs})"
        )
        if self.looks > 1:
            text += (
                f". The held-out data has now been looked at {self.looks} times — "
                "after the first look it is no longer truly held out"
            )
        return text


def _key(setup: str, symbol: str, timeframe: str) -> str:
    return f"{setup}|{symbol}|{timeframe}"


class TrackRecordStore:
    """A JSON file of measurements, one per setup, symbol and timeframe."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # An unreadable file is reported as no record rather than as a
            # crash, but it is never silently treated as a good record.
            return {}

    def lookup(self, setup: str, symbol: str, timeframe: str) -> SetupRecord | None:
        raw = self._load().get(_key(setup, symbol, timeframe))
        if raw is None:
            return None
        try:
            return SetupRecord(**raw)
        except TypeError:
            return None

    def save(self, record: SetupRecord) -> SetupRecord:
        data = self._load()
        key = _key(record.setup, record.symbol, record.timeframe)
        previous = data.get(key)
        looks = (int(previous.get("looks", 1)) + 1) if previous else 1
        stored = SetupRecord(**{**asdict(record), "looks": looks})
        data[key] = asdict(stored)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return stored

    def all(self) -> list[SetupRecord]:
        out = []
        for raw in self._load().values():
            try:
                out.append(SetupRecord(**raw))
            except TypeError:
                continue
        return sorted(out, key=lambda r: (r.symbol, r.timeframe, r.setup))


class SetupStrategy(Strategy):
    """A setup wearing the Strategy interface, so the backtester can run it.

    Always UNTESTED. Measuring a setup is how evidence about it is produced;
    it is not a promotion, and nothing here can make one.
    """

    validation = ValidationStatus.UNTESTED

    def __init__(self, setup: Setup) -> None:
        self.setup = setup
        self.name = f"setup:{setup.name}"

    def evaluate(self, context: StrategyContext) -> Proposal:
        analysis = analyse(context.bars, context.instrument, context.timeframe)
        for candidate in self.setup.detect(analysis):
            return Proposal(
                decision=candidate.direction,
                strategy=self.name,
                rationale="; ".join(item.statement for item in candidate.trigger),
                entry=candidate.entry,
                stop=candidate.stop,
                targets=(candidate.target,),
                conviction=1,
                setup=self.setup.name,
            )
        return Proposal.wait(self.name, "pattern not present")


def _expectancy_r(trades) -> str | None:
    values = [t.r_multiple for t in trades if t.r_multiple is not None]
    if not values:
        return None
    return str(sum(values, ZERO) / D(len(values)))


def measure(
    setup: Setup,
    bars: Sequence[Bar],
    instrument: InstrumentSpec,
    timeframe: Timeframe,
    limits: RiskLimits,
    *,
    costs: BarCosts | None = None,
    warmup: int = 60,
) -> SetupRecord:
    """Replay one setup over a recording, in-sample and on held-out data."""
    costs = costs or BarCosts()
    parts = split(bars, in_sample="0.6", validation="0.2")
    backtester = Backtester(limits=limits)
    strategy = SetupStrategy(setup)

    def run(segment):
        return backtester.run(
            strategy, list(segment), instrument,
            BacktestSettings(
                symbol=instrument.symbol, timeframe=timeframe,
                warmup_bars=warmup, costs=costs,
            ),
        )

    inside = run(parts.in_sample)
    outside = run(parts.out_of_sample)
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    return SetupRecord(
        setup=setup.name,
        symbol=instrument.symbol,
        timeframe=timeframe.value,
        in_sample_trades=len(inside.trades),
        in_sample_expectancy_r=_expectancy_r(inside.trades),
        out_of_sample_trades=len(outside.trades),
        out_of_sample_expectancy_r=_expectancy_r(outside.trades),
        first_bar=ordered[0].timestamp.isoformat() if ordered else None,
        last_bar=ordered[-1].timestamp.isoformat() if ordered else None,
        measured_at=utcnow().isoformat(),
        costs=costs.describe(),
    )
