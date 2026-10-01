"""The application container.

Holds the objects a running deployment needs and, more importantly,
owns the one path by which an order may be placed:
:meth:`TradingRuntime.submit`. Routes, strategies, agents and operators
all go through it, and it always calls the risk engine first.

Keeping that path singular is the enforcement mechanism for
specification sections 1 and 16. There is no second way to reach a
broker, so there is no second thing to audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable

from gtcc.adapters.base import AdapterRegistry, BrokerAdapter, MarketDataAdapter
from gtcc.adapters.errors import AdapterError, FeatureUnavailable
from gtcc.config import Settings
from gtcc.data.quality import DataQualityReport, check_quote
from gtcc.domain.enums import RiskAction, TradingMode
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Quote, utcnow
from gtcc.domain.money import ZERO, D
from gtcc.domain.orders import Account, Order, OrderRequest
from gtcc.execution.oms import OrderManager, Reconciliation
from gtcc.logging_setup import log_event
from gtcc.risk.engine import (
    Check,
    RiskContext,
    RiskEngine,
    RiskVerdict,
    exposure_from_positions,
)
from gtcc.risk.limits import RiskLimits
from gtcc.risk.state import RiskState, fresh_state

logger = logging.getLogger("gtcc.runtime")


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """What happened to a submission attempt, approved or not."""

    verdict: RiskVerdict
    order: Order | None
    placed: bool
    detail: str

    @property
    def rejected(self) -> bool:
        return not self.placed


class TradingHalted(RuntimeError):
    """Submission refused before the risk engine, by a system condition."""


@dataclass
class TradingRuntime:
    settings: Settings
    limits: RiskLimits
    registry: AdapterRegistry = field(default_factory=AdapterRegistry)
    oms: OrderManager = field(default_factory=OrderManager)
    engine: RiskEngine = field(default_factory=RiskEngine)
    broker_name: str = "paper"
    data_name: str = "paper"
    clock: Callable[[], datetime] = utcnow
    state: RiskState | None = None
    #: When set, the risk state is read on first use and written after
    #: every change, so a restart cannot clear a tripped breaker.
    state_store: object | None = None
    #: Set by the last reconciliation. A dirty result stops live trading.
    last_reconciliation: Reconciliation | None = None

    # -- wiring ---------------------------------------------------------------

    @property
    def broker(self) -> BrokerAdapter:
        return self.registry.broker(self.broker_name)

    @property
    def data(self) -> MarketDataAdapter:
        return self.registry.data(self.data_name)

    def ensure_state(self) -> RiskState:
        if self.state is None:
            account = self.broker.get_account()
            if self.state_store is not None:
                self.state = self.state_store.load(account.account_id, account.equity)
            else:
                self.state = fresh_state(account.account_id, account.equity, now=self.clock())
        return self.state

    def _persist(self, state: RiskState) -> RiskState:
        """Record a state change. Writing is best-effort by design.

        A storage failure must not leave the process trading on a state
        it has forgotten, so the in-memory value is kept either way and
        the failure is logged loudly rather than swallowed.
        """
        self.state = state
        if self.state_store is not None:
            try:
                self.state_store.save(state, self.broker.get_account().equity)
            except Exception as exc:  # pragma: no cover - storage failure path
                log_event(
                    logger, logging.ERROR, "could not persist risk state",
                    error=str(exc), account_id=state.account_id,
                )
        return state

    # -- the single submission path ---------------------------------------------

    def build_context(
        self,
        request: OrderRequest,
        *,
        instrument: InstrumentSpec | None = None,
        quote: Quote | None = None,
        data_quality: DataQualityReport | None = None,
    ) -> RiskContext:
        now = self.clock()
        broker = self.broker
        data = self.data

        instrument = instrument or data.get_instrument(request.symbol)

        broker_health = broker.health()
        healthy = broker_health.healthy

        if quote is None:
            try:
                quote = data.get_quote(request.symbol)
            except (FeatureUnavailable, AdapterError) as exc:
                log_event(
                    logger, logging.WARNING, "quote unavailable",
                    symbol=request.symbol, error=str(exc),
                )
                quote = None

        if data_quality is None:
            if quote is None:
                data_quality = DataQualityReport(
                    symbol=request.symbol, checked_at=now, unavailable_feeds=("quotes",)
                )
            else:
                data_quality = check_quote(
                    quote,
                    now=now,
                    max_age_seconds=self.settings.max_quote_age_seconds,
                    max_clock_skew_seconds=self.settings.max_clock_skew_seconds,
                )

        positions = tuple(broker.get_positions())
        specs = {position.symbol: self._spec_for(position.symbol) for position in positions}
        exposure, exposure_known = exposure_from_positions(
            positions, {k: v for k, v in specs.items() if v is not None}
        )
        if any(spec is None for spec in specs.values()):
            exposure_known = False

        account = broker.get_account()
        state = self.ensure_state().mark_equity(account.equity)
        self.state = state

        # A dirty reconciliation means our view of the account is not
        # trustworthy, which the engine treats as an unhealthy broker.
        if self.last_reconciliation is not None and self.last_reconciliation.blocks_live_trading:
            healthy = False

        return RiskContext(
            mode=self.settings.mode,
            live_trading_enabled=self.settings.live_trading,
            account=account,
            instrument=instrument,
            limits=self.limits,
            state=state,
            data_quality=data_quality,
            quote=quote,
            positions=positions,
            open_orders=tuple(self.oms.open_orders()),
            broker_healthy=healthy,
            exposure_by_symbol=exposure,
            exposure_known=exposure_known,
            estimated_slippage_bps=D(self.settings.max_quote_age_seconds) * ZERO,
            now=now,
        )

    def _context_or_refusal(
        self, request: OrderRequest, **kwargs
    ) -> tuple[RiskContext | None, RiskVerdict | None]:
        """Assemble a context, or explain why the order cannot be judged.

        An unknown symbol is a refusal, not a server error: without a
        contract specification there is no tick size and no lot step, so
        nothing can be sized. It comes back as a verdict so the caller
        sees the same shape as every other rejection.
        """
        try:
            return self.build_context(request, **kwargs), None
        except (FeatureUnavailable, KeyError) as exc:
            log_event(
                logger, logging.WARNING, "order refused: instrument unknown",
                symbol=request.symbol, error=str(exc),
            )
            return None, RiskVerdict.refused(
                Check.INSTRUMENT_KNOWN,
                f"no contract specification for {request.symbol}: {exc}",
            )

    def evaluate(self, request: OrderRequest, **kwargs) -> RiskVerdict:
        """Dry run: what would the risk engine say? Places nothing."""
        context, refusal = self._context_or_refusal(request, **kwargs)
        if refusal is not None:
            return refusal
        assert context is not None
        return self.engine.evaluate(request, context)

    def submit(self, request: OrderRequest, **kwargs) -> SubmissionResult:
        """The only way an order reaches a broker."""
        context, refusal = self._context_or_refusal(request, **kwargs)
        if refusal is not None:
            return SubmissionResult(
                verdict=refusal, order=None, placed=False, detail=refusal.explain()
            )
        assert context is not None
        verdict = self.engine.evaluate(request, context)

        log_event(
            logger,
            logging.INFO,
            "risk verdict",
            symbol=request.symbol,
            strategy=request.strategy,
            action=str(verdict.action),
            approved_quantity=str(verdict.approved_quantity),
            failures=[str(code) for code in verdict.failure_codes],
            client_order_id=request.client_order_id,
        )

        if not verdict.allowed:
            return SubmissionResult(
                verdict=verdict, order=None, placed=False, detail=verdict.explain()
            )

        order = self.oms.register(request, verdict, mode=self.settings.mode)
        self.oms.mark_submitted(order.client_order_id)

        try:
            placed = self.broker.place_order(request, quantity=verdict.approved_quantity)
        except AdapterError as exc:
            self.oms.mark_rejected(order.client_order_id, str(exc))
            log_event(
                logger, logging.WARNING, "broker rejected order",
                client_order_id=order.client_order_id, error=str(exc),
            )
            return SubmissionResult(
                verdict=verdict, order=self.oms.get(order.client_order_id),
                placed=False, detail=str(exc),
            )

        tracked = self.oms.mark_accepted(
            order.client_order_id, placed.broker_order_id or "unknown"
        )
        for fill in placed.fills:
            tracked = self.oms.apply_fill(order.client_order_id, fill)

        return SubmissionResult(
            verdict=verdict,
            order=tracked,
            placed=True,
            detail=f"{tracked.status}: filled {tracked.filled_quantity} of {tracked.quantity}",
        )

    # -- operations -------------------------------------------------------------------

    def reconcile(self) -> Reconciliation:
        result = self.oms.reconcile(self.broker.get_orders(), now=self.clock())
        self.last_reconciliation = result
        if not result.clean:
            log_event(
                logger, logging.ERROR, "order reconciliation found differences",
                detail=result.summary(),
            )
        return result

    def engage_kill_switch(self, engaged: bool) -> RiskState:
        state = self._persist(self.ensure_state().with_kill_switch(engaged))
        log_event(logger, logging.WARNING, "kill switch", engaged=engaged)
        return state

    def pause(self, paused: bool) -> RiskState:
        state = self._persist(self.ensure_state().paused(paused))
        log_event(logger, logging.WARNING, "trading paused", paused=paused)
        return state

    def record_settled_trade(self, realised_pnl: Decimal) -> RiskState:
        """Fold a closed trade into the tally and trip any breaker it crosses."""
        state = self.ensure_state().record_settled_trade(realised_pnl, now=self.clock())
        equity = self.broker.get_account().equity

        daily = state.daily_loss_fraction() >= self.limits.max_daily_loss
        weekly = state.weekly_loss_fraction() >= self.limits.max_weekly_loss
        drawdown = state.drawdown_fraction(equity) >= self.limits.max_drawdown
        if daily or weekly or drawdown:
            state = state.trip(daily=daily, weekly=weekly, drawdown=drawdown)
            log_event(
                logger, logging.ERROR, "circuit breaker tripped",
                daily=daily, weekly=weekly, drawdown=drawdown,
                realised_pnl=str(realised_pnl),
            )
        return self._persist(state)

    def account(self) -> Account:
        return self.broker.get_account()

    def _spec_for(self, symbol: str) -> InstrumentSpec | None:
        try:
            return self.data.get_instrument(symbol)
        except Exception:
            return None
