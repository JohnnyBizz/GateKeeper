"""Walk bars forward, one at a time, and never look at the next one.

Everything in a backtester is downstream of one question: at the moment a
decision was made, what could actually be known? Get that wrong and the
results are not optimistic, they are fiction — and the failure is silent,
because a look-ahead bug produces a beautiful equity curve rather than an
error.

Three rules enforce it here, and each has a test.

**A decision at a bar's close fills no earlier than the next bar.** The
strategy sees bars 0..i, all closed. If it wants in, the fill happens at
bar i+1's open. Filling at bar i's close would use a price that was only
knowable at the instant the decision was made, which in live trading you
would never get.

**When a bar touches both the stop and the target, the stop wins.** Bar
data does not say which came first within the bar. Assuming the target
would inflate every result that depends on it, and the inflation grows
with the strategy's reward:risk — the more ambitious the target, the
bigger the lie. Assuming the stop is the only choice that cannot flatter.

**Costs are assumptions, declared and recorded.** A bar close is not a
quote, so the spread is unknowable from this data; the replay adapter
refuses to invent one, and so does this. The caller states the spread,
slippage and commission it is assuming, and `BacktestResult` carries them
so no statistic can be read without them.

The risk engine in the loop is the real one. A backtest that sized
positions its own way would measure a system that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from gtcc.data.quality import DataQualityReport
from gtcc.domain.enums import (
    DataQuality,
    Market,
    OrderType,
    Side,
    Timeframe,
    TradingMode,
)
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, Quote
from gtcc.domain.money import D, ZERO
from gtcc.domain.orders import Account, OrderRequest
from gtcc.features.regime import RegimeClassifier, RegimeReading
from gtcc.risk.engine import RiskContext, RiskEngine, RiskVerdict
from gtcc.risk.limits import RiskLimits
from gtcc.risk.safety import initial_state
from gtcc.risk.state import fresh_state
from gtcc.strategies.base import Proposal, Strategy, StrategyContext
from gtcc.structure.engine import StructureEngine


class ExitReason(StrEnum):
    STOP = "STOP"
    TARGET = "TARGET"
    #: The series ended while the position was open. Counted separately
    #: because it is not a result the strategy produced.
    END_OF_DATA = "END_OF_DATA"


@dataclass(frozen=True, slots=True)
class BarCosts:
    """What this backtest assumes execution costs.

    None of these can be derived from OHLCV data. They are the caller's
    assumptions, they are recorded in the result, and they are applied as
    costs in every direction — never as a benefit.
    """

    #: Half-spread paid on entry and on exit.
    spread_bps: Decimal = D("1.0")
    #: Slippage against the fill price, in addition to the spread.
    slippage_bps: Decimal = D("1.0")
    #: Commission per side.
    commission_bps: Decimal = D("1.0")

    def per_side_bps(self) -> Decimal:
        return self.spread_bps / D(2) + self.slippage_bps + self.commission_bps

    def describe(self) -> str:
        return (
            f"assumed costs: {self.spread_bps} bps spread, "
            f"{self.slippage_bps} bps slippage, "
            f"{self.commission_bps} bps commission per side"
        )


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    symbol: str
    strategy: str
    side: Side
    quantity: Decimal
    entry_index: int
    entry_at: datetime
    entry_price: Decimal
    exit_index: int
    exit_at: datetime
    exit_price: Decimal
    reason: ExitReason
    planned_stop: Decimal
    planned_target: Decimal | None
    costs: Decimal
    #: Profit and loss after costs, in account currency.
    pnl: Decimal
    #: P&L as a multiple of the risk taken at entry. None when the risk
    #: was zero, which should not happen but is not worth inventing a
    #: number for.
    r_multiple: Decimal | None
    regime: str | None = None
    rationale: str = ""

    @property
    def won(self) -> bool:
        return self.pnl > ZERO


@dataclass(frozen=True, slots=True)
class SkippedSetup:
    """A setup the strategy wanted and the risk engine refused.

    Kept for the same reason the live journal keeps refusals: a backtest
    that silently discards them cannot answer whether the limits cost
    money or saved it.
    """

    index: int
    at: datetime
    side: Side
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BacktestSettings:
    symbol: str
    timeframe: Timeframe
    #: Bars reserved for indicator warm-up. No trade is taken inside it.
    warmup_bars: int = 120
    starting_equity: Decimal = D("100000")
    costs: BarCosts = field(default_factory=BarCosts)
    #: Backtests hold one position at a time. Stated rather than implied:
    #: concurrent positions interact through the exposure limits, and
    #: simulating that correctly needs portfolio accounting this does not
    #: have yet.
    one_position_at_a_time: bool = True


@dataclass(frozen=True, slots=True)
class BacktestResult:
    symbol: str
    timeframe: Timeframe
    strategy: str
    bars_seen: int
    bars_traded: int
    first_bar_at: datetime | None
    last_bar_at: datetime | None
    starting_equity: Decimal
    ending_equity: Decimal
    costs: BarCosts
    trades: tuple[ClosedTrade, ...] = ()
    skipped: tuple[SkippedSetup, ...] = ()
    #: Equity after each closed trade, for drawdown. Starts at the opening
    #: balance so a first losing trade shows as a drawdown.
    equity_curve: tuple[Decimal, ...] = ()
    #: Set when the series ended with a position still open.
    open_at_end: bool = False

    def describe(self) -> str:
        return (
            f"{self.strategy} on {self.symbol} {self.timeframe}: "
            f"{len(self.trades)} closed trade(s) over {self.bars_traded} tradeable "
            f"bars ({self.bars_seen} seen, the rest warm-up). "
            f"{len(self.skipped)} setup(s) refused by risk. "
            f"{self.costs.describe()}."
        )


class Backtester:
    """Replays one symbol, one timeframe, one strategy.

    Holds no broker and no adapter: it is handed a bar series. The point
    is that there is nothing for it to read ahead *from* — the series is
    sliced before the strategy ever sees it.
    """

    def __init__(
        self,
        *,
        limits: RiskLimits,
        engine: RiskEngine | None = None,
        structure: StructureEngine | None = None,
        classifier: RegimeClassifier | None = None,
    ) -> None:
        self.limits = limits
        self.engine = engine or RiskEngine()
        self.structure = structure or StructureEngine()
        self.classifier = classifier or RegimeClassifier()

    # -- the loop ------------------------------------------------------------

    def run(
        self,
        strategy: Strategy,
        bars: Sequence[Bar],
        instrument: InstrumentSpec,
        settings: BacktestSettings,
    ) -> BacktestResult:
        closed = [bar for bar in bars if bar.closed]
        equity = D(settings.starting_equity)
        trades: list[ClosedTrade] = []
        skipped: list[SkippedSetup] = []
        curve: list[Decimal] = [equity]

        position: _OpenPosition | None = None

        for index in range(len(closed)):
            bar = closed[index]

            # Manage an open position FIRST, against this bar. The
            # position was opened at an earlier bar's open, so this bar's
            # high and low are legitimately new information.
            if position is not None:
                exit_at = self._exit(position, bar)
                if exit_at is not None:
                    price, reason = exit_at
                    trade, equity = self._close(
                        position, index, bar, price, reason, equity, settings
                    )
                    trades.append(trade)
                    curve.append(equity)
                    position = None

            if index < settings.warmup_bars:
                continue
            if position is not None and settings.one_position_at_a_time:
                continue
            if index + 1 >= len(closed):
                # Nothing left to fill against. Deciding here and filling
                # at this same bar's close is the look-ahead this whole
                # module exists to avoid.
                continue

            # Only bars up to and including this one exist as far as the
            # strategy is concerned. The slice is the enforcement.
            visible = closed[: index + 1]
            proposal, regime = self._ask(
                strategy, visible, instrument, settings, bar
            )
            if proposal is None or not proposal.is_actionable:
                continue

            verdict = self._evaluate(
                proposal, instrument, settings, equity, bar, visible
            )
            if not verdict.allowed or verdict.approved_quantity <= ZERO:
                skipped.append(
                    SkippedSetup(
                        index=index,
                        at=bar.timestamp,
                        side=proposal.side or Side.BUY,
                        reasons=verdict.reasons or ("no size was approved",),
                    )
                )
                continue

            # Fill at the NEXT bar's open, with costs against us.
            fill_bar = closed[index + 1]
            position = self._open(
                proposal, verdict, fill_bar, index + 1, settings, instrument,
                regime=regime,
            )

        open_at_end = position is not None
        if position is not None:
            last = closed[-1]
            trade, equity = self._close(
                position, len(closed) - 1, last, last.close,
                ExitReason.END_OF_DATA, equity, settings,
            )
            trades.append(trade)
            curve.append(equity)

        return BacktestResult(
            symbol=settings.symbol,
            timeframe=settings.timeframe,
            strategy=strategy.name,
            bars_seen=len(closed),
            bars_traded=max(0, len(closed) - settings.warmup_bars),
            first_bar_at=closed[0].timestamp if closed else None,
            last_bar_at=closed[-1].timestamp if closed else None,
            starting_equity=D(settings.starting_equity),
            ending_equity=equity,
            costs=settings.costs,
            trades=tuple(trades),
            skipped=tuple(skipped),
            equity_curve=tuple(curve),
            open_at_end=open_at_end,
        )

    # -- pieces --------------------------------------------------------------

    def _ask(
        self,
        strategy: Strategy,
        visible: Sequence[Bar],
        instrument: InstrumentSpec,
        settings: BacktestSettings,
        bar: Bar,
    ) -> tuple[Proposal | None, RegimeReading]:
        report = self.structure.analyse(visible)
        regime = self.classifier.classify(visible, structure=report)
        context = StrategyContext(
            symbol=settings.symbol,
            market=instrument.market,
            timeframe=settings.timeframe,
            instrument=instrument,
            bars=visible,
            # No quote: bar data does not contain one, and synthesising a
            # bid/ask from the close would invent the spread.
            quote=None,
            structure=report,
            regime=regime,
            now=bar.timestamp,
        )
        return strategy.propose(context, TradingMode.BACKTEST), regime

    def _evaluate(
        self,
        proposal: Proposal,
        instrument: InstrumentSpec,
        settings: BacktestSettings,
        equity: Decimal,
        bar: Bar,
        visible: Sequence[Bar],
    ) -> RiskVerdict:
        request = OrderRequest(
            symbol=settings.symbol,
            market=instrument.market,
            side=proposal.side or Side.BUY,
            order_type=OrderType.MARKET,
            protective_stop=proposal.stop,
            targets=proposal.targets,
            strategy=proposal.strategy,
        )
        account = Account(
            account_id="backtest",
            currency=instrument.quote_currency or "USD",
            equity=equity,
            cash=equity,
            buying_power=equity,
            mode=TradingMode.BACKTEST,
            reconciled_at=bar.timestamp,
        )
        context = RiskContext(
            execution=replace(
                initial_state(TradingMode.PAPER), mode=TradingMode.BACKTEST
            ),
            account=account,
            instrument=instrument,
            limits=self.limits,
            state=fresh_state("backtest", equity, now=bar.timestamp),
            data_quality=DataQualityReport(
                symbol=settings.symbol, checked_at=bar.timestamp
            ),
            quote=self._assumed_quote(bar, settings),
            broker_healthy=True,
            estimated_slippage_bps=settings.costs.slippage_bps,
            now=bar.timestamp,
        )
        return self.engine.evaluate(request, context)

    @staticmethod
    def _assumed_quote(bar: Bar, settings: BacktestSettings) -> Quote:
        """A quote built from the bar close and the DECLARED spread.

        The risk engine refuses an order it cannot price, and it is right
        to: in live trading, sizing against a stale bar close is how a
        position gets opened at a price that no longer exists. Relaxing
        that check in backtest mode was the other option and it is the
        wrong one — a backtest whose risk engine is not the live risk
        engine measures a system that does not exist.

        So the quote is constructed from the spread the caller declared in
        `BarCosts`, which is the same number the cost model charges. That
        matters: two independently invented spreads could drift apart, and
        the result would charge one while sizing against the other. The
        replay adapter refuses to do this because it has no declared
        assumption to apply. Here there is one, and `BacktestResult`
        carries it so no statistic can be read without it.
        """
        half = bar.close * settings.costs.spread_bps / D(2) / D(10000)
        return Quote(
            symbol=settings.symbol,
            timestamp=bar.timestamp,
            bid=bar.close - half,
            ask=bar.close + half,
            bid_size=ZERO,
            ask_size=ZERO,
            received_at=bar.timestamp,
        )

    def _open(
        self,
        proposal: Proposal,
        verdict: RiskVerdict,
        fill_bar: Bar,
        fill_index: int,
        settings: BacktestSettings,
        instrument: InstrumentSpec,
        *,
        regime: RegimeReading,
    ) -> "_OpenPosition":
        side = proposal.side or Side.BUY
        # Costs move the entry against us, always.
        drag = settings.costs.per_side_bps() / D(10000)
        raw = fill_bar.open
        entry = raw * (D(1) + drag) if side is Side.BUY else raw * (D(1) - drag)
        quantity = verdict.approved_quantity
        assert proposal.stop is not None
        return _OpenPosition(
            side=side,
            quantity=quantity,
            entry_index=fill_index,
            entry_at=fill_bar.timestamp,
            entry_price=entry,
            stop=proposal.stop,
            target=proposal.targets[0] if proposal.targets else None,
            strategy=proposal.strategy,
            rationale=proposal.rationale,
            entry_cost=raw * quantity * drag,
            # The regime AT THE DECISION, not at the exit. Attributing a
            # trade to the conditions it ended in would answer a different
            # question from the one anybody asks of a breakdown.
            regime=str(regime.regime) if regime.confident else None,
        )

    @staticmethod
    def _exit(position: "_OpenPosition", bar: Bar) -> tuple[Decimal, ExitReason] | None:
        """Did this bar close the position, and at what price?

        When the bar's range covers both the stop and the target, the stop
        is taken. OHLCV does not record the order of ticks inside a bar, so
        either choice is an assumption, and only one of them can be wrong
        in the direction that flatters the result.
        """
        stop = position.stop
        target = position.target

        if position.side is Side.BUY:
            hit_stop = bar.low <= stop
            hit_target = target is not None and bar.high >= target
        else:
            hit_stop = bar.high >= stop
            hit_target = target is not None and bar.low <= target

        if hit_stop:
            return stop, ExitReason.STOP
        if hit_target:
            assert target is not None
            return target, ExitReason.TARGET
        return None

    def _close(
        self,
        position: "_OpenPosition",
        index: int,
        bar: Bar,
        price: Decimal,
        reason: ExitReason,
        equity: Decimal,
        settings: BacktestSettings,
    ) -> tuple[ClosedTrade, Decimal]:
        drag = settings.costs.per_side_bps() / D(10000)
        exit_price = (
            price * (D(1) - drag) if position.side is Side.BUY else price * (D(1) + drag)
        )
        exit_cost = price * position.quantity * drag
        gross = (
            (exit_price - position.entry_price) * position.quantity
            if position.side is Side.BUY
            else (position.entry_price - exit_price) * position.quantity
        )
        costs = position.entry_cost + exit_cost
        # The entry and exit prices already carry the drag, so the cost
        # total is reported rather than subtracted twice.
        pnl = gross
        risk = abs(position.entry_price - position.stop) * position.quantity
        trade = ClosedTrade(
            symbol=settings.symbol,
            strategy=position.strategy,
            side=position.side,
            quantity=position.quantity,
            entry_index=position.entry_index,
            entry_at=position.entry_at,
            entry_price=position.entry_price,
            exit_index=index,
            exit_at=bar.timestamp,
            exit_price=exit_price,
            reason=reason,
            planned_stop=position.stop,
            planned_target=position.target,
            costs=costs,
            pnl=pnl,
            r_multiple=(pnl / risk) if risk > ZERO else None,
            regime=position.regime,
            rationale=position.rationale,
        )
        return trade, equity + pnl


@dataclass
class _OpenPosition:
    side: Side
    quantity: Decimal
    entry_index: int
    entry_at: datetime
    entry_price: Decimal
    stop: Decimal
    target: Decimal | None
    strategy: str
    rationale: str
    entry_cost: Decimal
    #: None when the classifier had no confident reading. Not "UNKNOWN":
    #: a breakdown bucket named UNKNOWN reads as a regime, and this is the
    #: absence of one.
    regime: str | None = None
