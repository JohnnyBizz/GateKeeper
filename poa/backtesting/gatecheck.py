"""Which gates are earning their keep, and which are just saying no.

Every blocking gate is a claim: *setups failing this are worse than setups
passing it*. Eight such claims stacked conjunctively make a tool that almost
never speaks, and if any one of them is wrong it is silently costing money —
suppressing setups that would have won while looking like prudence.

Nothing in the app had ever checked those claims. This does, on the chart in
front of you: walk the history, and for every bar record the direction the
scorer preferred, which gates refused it, and how it would have settled. Then
for each gate, compare what it blocked against what got through.

A gate blocking setups that settle *above* break-even is not protecting
anybody. That is the finding this exists to surface, and it is one the author
of the gate is in no position to reach by reasoning about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..analysis import build_multi_timeframe
from ..chart_detection.quality import validate_series
from ..logging_setup import get_logger
from ..models import Direction, Series
from ..signals.engine import SignalEngine, SignalRequest
from ..signals.gates import GateSettings
from ..signals.scoring import score_direction
from .stats import breakeven_rate

log = get_logger(__name__)

# A gate needs to have blocked at least this many settled setups before its
# record is worth reading, for the same reason every other sample here does.
MIN_BLOCKED = 15


@dataclass
class GateVerdict:
    """What one gate blocked, and how that would have turned out."""

    name: str
    blocked_wins: int = 0
    blocked_losses: int = 0

    @property
    def blocked(self) -> int:
        return self.blocked_wins + self.blocked_losses

    @property
    def blocked_rate(self) -> float | None:
        if self.blocked == 0:
            return None
        return round(self.blocked_wins / self.blocked * 100.0, 1)

    def costly(self, breakeven: float) -> bool:
        """Is it refusing setups that would have paid?"""
        rate = self.blocked_rate
        return (
            self.blocked >= MIN_BLOCKED
            and rate is not None
            and rate > breakeven
        )

    def to_dict(self, breakeven: float) -> dict[str, Any]:
        return {
            "name": self.name,
            "blocked": self.blocked,
            "blocked_rate": self.blocked_rate,
            "costly": self.costly(breakeven),
        }


@dataclass
class GateReport:
    verdicts: list[GateVerdict] = field(default_factory=list)
    allowed_wins: int = 0
    allowed_losses: int = 0
    payout: float = 0.92

    @property
    def allowed(self) -> int:
        return self.allowed_wins + self.allowed_losses

    @property
    def allowed_rate(self) -> float | None:
        if self.allowed == 0:
            return None
        return round(self.allowed_wins / self.allowed * 100.0, 1)

    @property
    def breakeven(self) -> float:
        return breakeven_rate(self.payout)

    def costly(self) -> list[GateVerdict]:
        """Gates refusing setups that would have paid, worst first."""
        found = [v for v in self.verdicts if v.costly(self.breakeven)]
        found.sort(key=lambda v: (v.blocked_rate or 0.0) * v.blocked, reverse=True)
        return found

    def headline(self) -> str:
        worst = self.costly()
        if not worst:
            return "No gate is measurably costing setups on this chart."
        gate = worst[0]
        return (
            f"{gate.name.replace('_', ' ')} blocked {gate.blocked} setups that "
            f"settled at {gate.blocked_rate:.0f}% — above the "
            f"{self.breakeven:.0f}% break-even."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "allowed_rate": self.allowed_rate,
            "breakeven": self.breakeven,
            "headline": self.headline(),
            "gates": [v.to_dict(self.breakeven) for v in self.verdicts],
        }


def check_gates(
    series: Series,
    *,
    trade_duration: int,
    payout: float = 0.92,
    settings: GateSettings | None = None,
    window: int = 120,
    step: int = 3,
    higher_multiple: int = 5,
    entry_multiple: int = 1,
) -> GateReport:
    """Walk the history and score every gate against what it refused."""
    timeframe = series.timeframe_seconds
    bars_ahead = max(1, round(trade_duration / max(timeframe, 1)))
    engine = SignalEngine()
    gate_settings = settings or GateSettings()
    report = GateReport(payout=payout)
    verdicts: dict[str, GateVerdict] = {}

    last = len(series) - bars_ahead - 2
    for index in range(window, last, max(1, step)):
        visible = series[index - window : index + 1]
        try:
            signal = engine.evaluate(
                SignalRequest(
                    series=visible,
                    asset=series.symbol,
                    chart_timeframe=timeframe,
                    trade_duration=trade_duration,
                    quality=validate_series(visible, source="gatecheck"),
                    higher_multiple=higher_multiple,
                    entry_multiple=entry_multiple,
                    settings=gate_settings,
                )
            )
        except Exception:  # pragma: no cover - defensive
            continue

        # Entry at the first price actually available after the signal's bar,
        # and settlement a duration later — the same rule the replay uses.
        entry = float(series[index + 1].open)
        exit_price = float(series[index + 1 + bars_ahead].open)
        change = exit_price - entry
        if abs(change) < 1e-12:
            continue  # a refund decides nothing about a gate

        if signal.direction in (Direction.CALL, Direction.PUT):
            won = (change > 0) is (signal.direction is Direction.CALL)
            report.allowed_wins += won
            report.allowed_losses += not won
            continue

        # Blocked. Recover the direction the scorer preferred, so the outcome
        # measures the setup the gate refused rather than an arbitrary side.
        try:
            mtf = build_multi_timeframe(visible, higher_multiple, entry_multiple)
        except Exception:  # pragma: no cover - defensive
            continue
        call = score_direction(mtf, Direction.CALL)
        put = score_direction(mtf, Direction.PUT)
        lean = call.direction if call.total >= put.total else put.direction
        # Judged on the side the panel would have SHOWN had the gate let it
        # through. Under the promoted reversal that is the lean's opposite:
        # the allowed calls above settle on the reversed side, and judging
        # the raw lean here graded every gate by the wrong side — a gate
        # protecting the flipped panel read as one costing it money, and
        # auto-tune would have retired it.
        shown = (
            lean.opposite
            if getattr(gate_settings, "invert_calls", False)
            else lean
        )
        won = (change > 0) is (shown is Direction.CALL)

        for failure in signal.gates.failures:
            verdict = verdicts.setdefault(failure.name, GateVerdict(failure.name))
            verdict.blocked_wins += won
            verdict.blocked_losses += not won

    report.verdicts = sorted(verdicts.values(), key=lambda v: -v.blocked)
    return report
