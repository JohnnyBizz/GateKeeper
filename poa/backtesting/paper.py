"""Paper trading and backtesting.

Walks historical candles forward one bar at a time, showing the engine only
what it would have seen at that moment, and settles each signal at the price
that actually printed when its expiration elapsed.

Two rules keep the results honest:

* **no lookahead** — the engine is handed a strict prefix of the data, and the
  settlement price is read only after the expiration has passed;
* **settlement matches the instrument** — a binary option is decided by where
  price sits at expiry versus entry, not by whether it moved favourably at any
  point in between.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..chart_detection.quality import validate_series
from ..config import TRADE_DURATIONS
from ..logging_setup import get_logger
from ..models import DataQuality, Direction, Series, format_duration
from ..signals.engine import Signal, SignalEngine, SignalRequest
from ..signals.gates import GateSettings
from .stats import directional_baselines, summarise_outcomes, wilson_interval

log = get_logger(__name__)


@dataclass
class PaperTrade:
    """One recorded signal and, once settled, its outcome."""

    index: int
    timestamp: str
    asset: str
    direction: str
    chart_timeframe: int
    trade_duration: int
    recommended_duration: int
    entry_price: float
    confidence: float
    direction_confidence: float
    duration_confidence: float
    setup_quality: str
    regime: str
    reason: str
    exit_index: int
    # What each part of the analysis scored, so a loss can be traced to
    # the component that argued for it rather than left as "it failed".
    components: dict[str, float] = field(default_factory=dict)
    pattern: str = ""
    exit_price: float | None = None
    outcome: str | None = None
    price_change: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "asset": self.asset,
            "direction": self.direction,
            "chart_timeframe": self.chart_timeframe,
            "trade_duration": self.trade_duration,
            "trade_duration_label": format_duration(self.trade_duration),
            "recommended_duration": self.recommended_duration,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "confidence": round(self.confidence, 1),
            "direction_confidence": round(self.direction_confidence, 1),
            "duration_confidence": round(self.duration_confidence, 1),
            "setup_quality": self.setup_quality,
            "regime": self.regime,
            "pattern": self.pattern,
            "components": self.components,
            "outcome": self.outcome,
            "price_change": self.price_change,
            "reason": self.reason,
        }


@dataclass
class BacktestResult:
    trades: list[PaperTrade]
    evaluated_bars: int
    wait_count: int
    no_trade_count: int
    asset: str
    chart_timeframe: int
    trade_duration: int
    statistics: dict[str, Any] = field(default_factory=dict)

    @property
    def signal_rate(self) -> float:
        """Share of evaluated bars that produced a directional signal."""
        if self.evaluated_bars == 0:
            return 0.0
        return round(len(self.trades) / self.evaluated_bars * 100.0, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "chart_timeframe": self.chart_timeframe,
            "chart_timeframe_label": format_duration(self.chart_timeframe),
            "trade_duration": self.trade_duration,
            "trade_duration_label": format_duration(self.trade_duration),
            "evaluated_bars": self.evaluated_bars,
            "signals": len(self.trades),
            "signal_rate": self.signal_rate,
            "waits": self.wait_count,
            "no_trades": self.no_trade_count,
            "statistics": self.statistics,
            "trades": [t.to_dict() for t in self.trades],
        }


class Backtester:
    """Replays a series through the signal engine."""

    def __init__(
        self,
        engine: SignalEngine | None = None,
        settings: GateSettings | None = None,
        *,
        window: int = 300,
        higher_multiple: int = 5,
        entry_multiple: int = 1,
        available_durations: Sequence[int] = TRADE_DURATIONS,
        payout: float = 0.80,
        calibration: Any | None = None,
    ) -> None:
        self.engine = engine or SignalEngine()
        self.settings = settings or GateSettings()
        self.window = max(60, int(window))
        self.higher_multiple = higher_multiple
        self.entry_multiple = entry_multiple
        self.available_durations = tuple(available_durations)
        self.payout = payout
        # A record for the gates that consult one. None by default, and it has
        # to stay None wherever the replay's own trades are what *builds* the
        # record — measuring a tool against a record derived from that very
        # measurement answers a question about itself.
        self.calibration = calibration

    def run(
        self,
        series: Series,
        *,
        trade_duration: int = 180,
        asset: str | None = None,
        step: int = 1,
        use_recommended_duration: bool = False,
        progress: Callable[[int, int], None] | None = None,
        min_gap_bars: int = 0,
        realistic_entry: bool = True,
    ) -> BacktestResult:
        """Walk ``series`` forward and collect the signals the engine produced.

        ``use_recommended_duration`` settles each trade at the engine's own
        preferred expiration instead of the fixed one, which is how you find out
        whether the duration analysis is earning its place.

        ``min_gap_bars`` enforces a cooldown between recorded signals, mirroring
        the live cooldown so backtest and live behaviour stay comparable.

        ``realistic_entry`` decides *when the trade is taken*, and it is not a
        detail. A signal is produced by a bar that has closed, so nobody can
        buy at that bar's closing price — it is already in the past by the time
        the signal exists. Settling against it measures a trade that could not
        be placed, and flatters the result by exactly the amount price moved
        while the user was reading the panel.

        With it on, entry is the next bar's open — the first price actually
        available after the signal — and expiry counts from there. That is
        still the best case (an instant click), but it is a price that was
        really on offer. Off, for comparing the two and seeing how much of a
        measured edge is an artefact of entering in the past.
        """
        asset = asset or series.symbol
        timeframe = series.timeframe_seconds
        total = len(series)
        trades: list[PaperTrade] = []
        wait_count = 0
        no_trade_count = 0
        evaluated = 0
        last_signal_index = -(10**9)

        start = max(self.window, 60)
        if total <= start + 1:
            log.warning(
                "series has %d candles; at least %d are needed to backtest",
                total,
                start + 2,
            )
            return BacktestResult(
                trades=[],
                evaluated_bars=0,
                wait_count=0,
                no_trade_count=0,
                asset=asset,
                chart_timeframe=timeframe,
                trade_duration=trade_duration,
                statistics=summarise_outcomes([], self.payout),
            )

        for index in range(start, total, max(1, int(step))):
            # Strict prefix: the engine sees candle ``index`` and nothing after.
            visible = series[max(0, index - self.window) : index + 1]
            evaluated += 1
            if progress is not None and evaluated % 50 == 0:
                progress(index - start, total - start)

            quality = validate_series(
                visible,
                min_candles=min(60, self.window),
                source="backtest",
                expected_timeframe=timeframe,
            )
            request = SignalRequest(
                series=visible,
                asset=asset,
                chart_timeframe=timeframe,
                trade_duration=trade_duration,
                quality=quality,
                available_durations=self.available_durations,
                higher_multiple=self.higher_multiple,
                entry_multiple=self.entry_multiple,
                settings=self.settings,
                calibration=self.calibration,
            )
            try:
                signal = self.engine.evaluate(request)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("evaluation failed at bar %d: %s", index, exc)
                continue

            if signal.direction is Direction.NO_TRADE:
                no_trade_count += 1
                continue
            if signal.direction is Direction.WAIT:
                wait_count += 1
                continue
            if index - last_signal_index < min_gap_bars:
                continue

            duration_seconds = (
                signal.duration.recommended_seconds
                if (use_recommended_duration and signal.duration)
                else trade_duration
            )
            bars_ahead = max(1, round(duration_seconds / timeframe))
            # The signal exists only once its bar has closed, so the earliest
            # price anyone could actually trade is the next bar's open. The
            # expiry then runs from there rather than from a price that was
            # already history when the panel lit up.
            entry_index = index + 1 if realistic_entry else index
            exit_index = entry_index + bars_ahead
            if exit_index >= total:
                # The data ends before this trade would settle; recording it
                # unsettled would bias the sample, so it is dropped.
                continue

            if realistic_entry:
                entry_price = float(series[entry_index].open)
                exit_price = float(series[exit_index].open)
            else:
                entry_price = float(visible.close[-1])
                exit_price = float(series[exit_index].close)
            change = exit_price - entry_price
            if abs(change) < 1e-12:
                outcome = "flat"
            elif signal.direction is Direction.CALL:
                outcome = "win" if change > 0 else "loss"
            else:
                outcome = "win" if change < 0 else "loss"

            last_signal_index = index
            trades.append(
                PaperTrade(
                    index=index,
                    timestamp=series[index].timestamp.isoformat(),
                    asset=asset,
                    direction=signal.direction.value,
                    chart_timeframe=timeframe,
                    trade_duration=duration_seconds,
                    recommended_duration=(
                        signal.duration.recommended_seconds if signal.duration else 0
                    ),
                    entry_price=entry_price,
                    exit_index=exit_index,
                    exit_price=exit_price,
                    confidence=signal.overall_confidence,
                    direction_confidence=signal.direction_confidence,
                    duration_confidence=signal.duration_confidence,
                    setup_quality=signal.setup_quality.value,
                    components={
                        c.name: float(c.score)
                        for c in (signal.score.components if signal.score else [])
                    },
                    pattern=(
                        signal.mtf.current.pattern.name if signal.mtf else ""
                    ),
                    regime=(
                        signal.mtf.current.regime.regime.value if signal.mtf else ""
                    ),
                    reason=signal.reason,
                    outcome=outcome,
                    price_change=change,
                )
            )

        statistics = summarise_outcomes(
            [
                {
                    "direction": t.direction,
                    "outcome": t.outcome,
                    "confidence": t.confidence,
                    "trade_duration": t.trade_duration,
                    "chart_timeframe": t.chart_timeframe,
                    "setup_quality": t.setup_quality,
                    "regime": t.regime,
                    "asset": t.asset,
                    "timestamp": t.timestamp,
                }
                for t in trades
            ],
            self.payout,
        )
        # What the same entries would have paid with no opinion at all, and an
        # interval around the engine's own rate. A win rate is only worth
        # reading beside these: a run that calls BUY almost every time posts
        # whatever the market did, and reads as analysis.
        statistics["baselines"] = [
            baseline.to_dict()
            for baseline in directional_baselines(t.price_change for t in trades)
        ]
        decided = int(statistics["wins"]) + int(statistics["losses"])
        interval = wilson_interval(int(statistics["wins"]), decided)
        statistics["interval"] = (
            None if interval is None else [round(interval[0], 1), round(interval[1], 1)]
        )

        return BacktestResult(
            trades=trades,
            evaluated_bars=evaluated,
            wait_count=wait_count,
            no_trade_count=no_trade_count,
            asset=asset,
            chart_timeframe=timeframe,
            trade_duration=trade_duration,
            statistics=statistics,
        )
