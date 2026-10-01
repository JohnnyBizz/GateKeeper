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
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Callable

from gtcc.adapters.base import AdapterRegistry, BrokerAdapter, MarketDataAdapter
from gtcc.adapters.paper import ProtectiveExit
from gtcc.adapters.errors import AdapterError, FeatureUnavailable
from gtcc.config import Settings
from gtcc.data.quality import DataQualityReport, check_quote
from gtcc.domain.enums import RiskAction, TradingMode
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Quote, utcnow
from gtcc.domain.money import ZERO, D
from gtcc.domain.orders import Account, Order, OrderRequest, new_id
from gtcc.journal.entry import TradeAnnotations, entry_for
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
from gtcc.risk.safety import (
    ExecutionState,
    LiveArmingError,
    Trip,
    TripReason,
    initial_state,
)
from gtcc.risk.state import RiskState, fresh_state
from gtcc.scanner import ScanSettings, Scanner
from gtcc.strategies.registry import StrategyRegistry

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
    #: Runtime execution state. Starts disarmed and untripped on every
    #: process start, whatever the environment says. Nothing in the
    #: configuration can arm it.
    execution: ExecutionState | None = None
    #: When set, latched trips are read on first use and written as they
    #: happen, so a safety stop survives a restart. Live arming is
    #: deliberately NOT persisted: a process must never come back
    #: trading because a row said it was armed.
    latch_store: object | None = None
    state: RiskState | None = None
    #: When set, the risk state is read on first use and written after
    #: every change, so a restart cannot clear a tripped breaker.
    state_store: object | None = None
    #: Set by the last reconciliation. A dirty result stops live trading.
    last_reconciliation: Reconciliation | None = None
    #: Strategies this deployment knows about. Consulted by the scanner and
    #: by analysis; a proposal from one is an opinion that still has to pass
    #: the risk engine like anything else.
    strategies: StrategyRegistry = field(default_factory=StrategyRegistry)
    #: When set, every considered setup is written here — taken or refused.
    #: See `_journal` for why a write failure is treated differently
    #: depending on whether an order actually reached the venue.
    journal_store: object | None = None

    # -- wiring ---------------------------------------------------------------

    def ensure_execution(self) -> ExecutionState:
        """The execution state for this process.

        Starts disarmed, always. Any trip that was latched when the
        last process died is read back, because the condition that
        tripped it has not been looked at by a person yet, and a
        restart is not a diagnosis.
        """
        if self.execution is None:
            state = initial_state(self.settings.mode)
            if self.latch_store is not None:
                try:
                    trips = self.latch_store.open_trips(self._account_id())
                except Exception as exc:  # pragma: no cover - storage failure
                    # Cannot read the latch. Assume the worst and latch,
                    # because the alternative is resuming on an unknown
                    # safety state.
                    log_event(
                        logger, logging.ERROR,
                        "could not read the execution latch; latching defensively",
                        error=str(exc),
                    )
                    trips = (
                        Trip(
                            reason=TripReason.ACCOUNT_STATE_UNKNOWN,
                            detail=f"the latch could not be read on startup: {exc}",
                            occurred_at=self.clock(),
                        ),
                    )
                if trips:
                    state = replace(state, trips=tuple(trips))
                    log_event(
                        logger, logging.WARNING,
                        "execution latch restored from storage",
                        reasons=[str(trip.reason) for trip in trips],
                    )
            self.execution = state
        return self.execution

    def _account_id(self) -> str:
        try:
            return self.broker.get_account().account_id
        except Exception:  # pragma: no cover - broker failure path
            return "unknown"

    @property
    def broker(self) -> BrokerAdapter:
        return self.registry.broker(self.broker_name)

    @property
    def data(self) -> MarketDataAdapter:
        return self.registry.data(self.data_name)

    def scanner(self, settings: ScanSettings | None = None) -> Scanner:
        """A scanner over this deployment's data adapter.

        It is handed the data adapter and the strategy registry, and
        nothing else. Passing it `self` would give analysis code a route
        to `submit`, and there is exactly one path to a broker by design.
        """
        return Scanner(self.data, registry=self.strategies, settings=settings)

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

        # These conditions do not merely fail one order: they latch
        # execution off until a person resets it. Recovering the broker
        # on the next poll must not quietly resume trading, because
        # whatever happened in between is unaccounted for.
        if not broker_health.healthy:
            self.trip(TripReason.BROKER_UNHEALTHY, broker_health.detail or "broker unhealthy")
        if self.last_reconciliation is not None and self.last_reconciliation.blocks_live_trading:
            self.trip(
                TripReason.RECONCILIATION_FAILED, self.last_reconciliation.summary()
            )
        if not data_quality.tradeable:
            self.trip(
                TripReason.STALE_MARKET_DATA
                if any("STALE" in str(code) for code in data_quality.codes)
                else TripReason.INVALID_MARKET_DATA,
                data_quality.summary(),
            )
        if not exposure_known:
            self.trip(
                TripReason.ACCOUNT_STATE_UNKNOWN,
                "an open position cannot be valued, so exposure is unknown",
            )

        return RiskContext(
            execution=self.ensure_execution(),
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
        except AdapterError as exc:
            # The venue could not be reached well enough to assemble a
            # context: its account endpoint failed, or a call timed out.
            # That is a refusal and a latched breaker, not an exception
            # for the caller to handle. A route that got a 500 here
            # would be a 500 the operator has to interpret, and the
            # breaker would never have tripped.
            log_event(
                logger, logging.ERROR, "order refused: venue unreachable",
                symbol=request.symbol, error=str(exc),
            )
            self.trip(TripReason.BROKER_UNHEALTHY, f"venue unreachable: {exc}")
            return None, RiskVerdict.refused(
                Check.VENUE_REACHABLE,
                f"the venue could not be reached to price or fund this order: {exc}",
            )

    def evaluate(self, request: OrderRequest, **kwargs) -> RiskVerdict:
        """Dry run: what would the risk engine say? Places nothing."""
        context, refusal = self._context_or_refusal(request, **kwargs)
        if refusal is not None:
            return refusal
        assert context is not None
        return self.engine.evaluate(request, context)

    def submit(
        self,
        request: OrderRequest,
        *,
        annotations: TradeAnnotations | None = None,
        **kwargs,
    ) -> SubmissionResult:
        """The only way an order reaches a broker.

        *annotations* carry the analysis context behind the setup for the
        journal. They are advisory data about why this order exists and
        reach no decision: the risk engine never sees them.
        """
        # Settle any protective exit the market has already reached, BEFORE
        # new risk is evaluated. This is a safety requirement rather than
        # housekeeping: an unsettled stop means today's realised loss is
        # understated, so a loss breaker that should already have latched has
        # not, and this order would be approved against a tally that is
        # missing the loss that should have stopped it.
        self.settle_protective_exits()

        context, refusal = self._context_or_refusal(request, **kwargs)
        if refusal is not None:
            # Journalled too. A setup refused because the venue was
            # unreachable or the symbol unknown is still a setup the
            # platform considered, and leaving it out would make the
            # journal a record of the days the plumbing worked.
            self._journal(
                request, refusal, order=None, placed=False,
                context=None, annotations=annotations,
            )
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
            self._journal(
                request, verdict, order=None, placed=False,
                context=context, annotations=annotations,
            )
            return SubmissionResult(
                verdict=verdict, order=None, placed=False, detail=verdict.explain()
            )

        order = self.oms.register(
            request, verdict, mode=self.ensure_execution().mode, now=self.clock()
        )
        self.oms.mark_submitted(order.client_order_id)

        try:
            placed = self.broker.place_order(request, quantity=verdict.approved_quantity)
        except AdapterError as exc:
            self.oms.mark_rejected(order.client_order_id, str(exc))
            log_event(
                logger, logging.WARNING, "broker rejected order",
                client_order_id=order.client_order_id, error=str(exc),
            )
            rejected = self.oms.get(order.client_order_id)
            self._journal(
                request, verdict, order=rejected, placed=False,
                context=context, annotations=annotations,
                venue_detail=f"the venue rejected this order: {exc}",
            )
            return SubmissionResult(
                verdict=verdict, order=rejected, placed=False, detail=str(exc),
            )

        tracked = self.oms.mark_accepted(
            order.client_order_id, placed.broker_order_id or "unknown"
        )
        for fill in placed.fills:
            tracked = self.oms.apply_fill(order.client_order_id, fill)

        self._journal(
            request, verdict, order=tracked, placed=True,
            context=context, annotations=annotations,
        )
        return SubmissionResult(
            verdict=verdict,
            order=tracked,
            placed=True,
            detail=f"{tracked.status}: filled {tracked.filled_quantity} of {tracked.quantity}",
        )

    # -- journal ----------------------------------------------------------------------

    def _journal(
        self,
        request: OrderRequest,
        verdict: RiskVerdict,
        *,
        order: Order | None,
        placed: bool,
        context: RiskContext | None,
        annotations: TradeAnnotations | None,
        venue_detail: str | None = None,
    ) -> None:
        """Record one considered setup. Never raises.

        The failure handling is deliberately asymmetric, because the two
        failures mean different things.

        A refusal that cannot be journalled is a lost record of something
        that never happened at a venue. It is bad for later analysis and
        harmless to the account, so it logs an error and trading continues.

        A PLACED order that cannot be journalled is a position at a venue
        with no local record of why it was opened. The next reconciliation
        will find an order it cannot explain, and the platform's view of
        its own account is now incomplete — which is the exact condition
        section 42 says must stop trading. So that latches the breaker.
        """
        if self.journal_store is None:
            return

        account_id = "unknown"
        equity = ZERO
        if context is not None:
            account_id = context.account.account_id
            equity = context.account.equity

        try:
            entry = entry_for(
                request,
                verdict,
                trade_id=new_id("trade"),
                account_external_id=account_id,
                now=self.clock(),
                mode=self.ensure_execution().mode,
                order=order,
                placed=placed,
                data_quality=(
                    str(context.data_quality.status) if context is not None else None
                ),
                annotations=annotations,
                venue_detail=venue_detail,
            )
            self.journal_store.record(entry, equity=equity)
        except Exception as exc:  # noqa: BLE001 - the handling is the point
            log_event(
                logger,
                logging.ERROR,
                "could not write the trade journal",
                symbol=request.symbol,
                placed=placed,
                error=str(exc),
            )
            if placed:
                self.trip(
                    TripReason.ACCOUNT_STATE_UNKNOWN,
                    (
                        f"an order was placed for {request.symbol} and could not be "
                        f"journalled ({exc}); the platform cannot account for its "
                        "own position"
                    ),
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

    # -- live arming and the latched breaker -------------------------------

    def arm_live(self, *, actor: str, confirmation: str) -> ExecutionState:
        """Arm live execution. Raises :class:`LiveArmingError` if refused.

        The confirmation is compared against a phrase supplied in this
        call, never against anything read from configuration, so there
        is no environment variable that can stand in for a person.
        """
        state = self.ensure_execution().arm_live(
            actor=actor,
            confirmation=confirmation,
            deployment_allows_live=self.settings.allow_live_trading,
            now=self.clock(),
        )
        self.execution = state
        log_event(
            logger, logging.WARNING, "live execution ARMED",
            actor=actor, armed_at=state.armed_at.isoformat() if state.armed_at else None,
        )
        return state

    def disarm_live(self, *, actor: str) -> ExecutionState:
        self.execution = self.ensure_execution().disarm_live(actor=actor, now=self.clock())
        log_event(logger, logging.WARNING, "live execution disarmed", actor=actor)
        return self.execution

    def trip(self, reason: TripReason, detail: str) -> ExecutionState:
        """Latch execution off. Idempotent for a reason already latched."""
        before = self.ensure_execution()
        state = before.trip(reason, detail, now=self.clock())
        self.execution = state
        if reason not in before.trip_reasons:
            log_event(
                logger, logging.ERROR, "safety breaker TRIPPED",
                reason=str(reason), detail=detail,
                occurred_at=state.trips[-1].occurred_at.isoformat(),
                was_live_armed=before.live_armed,
            )
            self._persist_trip(state.trips[-1])
        return state

    def _persist_trip(self, trip: Trip) -> None:
        """Write the trip. A failure here must not lose the latch.

        The in-memory state is already tripped by the time this runs,
        so a storage failure degrades to "latched until restart"
        rather than "not latched at all", and says so loudly.
        """
        if self.latch_store is None:
            return
        try:
            self.latch_store.record(
                self._account_id(), trip, self.broker.get_account().equity
            )
        except Exception as exc:  # pragma: no cover - storage failure path
            log_event(
                logger, logging.ERROR,
                "could not persist the execution latch; it will not survive a restart",
                error=str(exc), reason=str(trip.reason),
            )

    def system_healthy(self) -> tuple[bool, str]:
        """Is every dependency the breaker cares about healthy right now?

        Read before a reset is allowed, so a breaker cannot be cleared
        while the condition that tripped it is still true.
        """
        problems: list[str] = []
        try:
            health = self.broker.health()
            if not health.healthy:
                problems.append(f"broker: {health.detail or 'unhealthy'}")
        except Exception as exc:
            problems.append(f"broker: {exc}")
        try:
            data = self.data.health()
            if not data.healthy:
                problems.append(f"data: {data.detail or 'unhealthy'}")
        except Exception as exc:
            problems.append(f"data: {exc}")
        if self.last_reconciliation is not None and not self.last_reconciliation.clean:
            problems.append(f"reconciliation: {self.last_reconciliation.summary()}")
        if self.state is not None and not self.state.breakers().clear:
            problems.append("risk breakers: " + "; ".join(self.state.breakers().reasons))
        return (not problems), "; ".join(problems)

    def reset_breaker(self, *, actor: str) -> ExecutionState:
        """Clear the latch, leaving the process disarmed.

        Refused while anything is still unhealthy. Clearing does not
        re-arm: resuming live trading costs a second deliberate action.
        """
        healthy, detail = self.system_healthy()
        now = self.clock()
        state = self.ensure_execution().reset_breaker(
            actor=actor, healthy=healthy, unhealthy_detail=detail, now=now
        )
        self.execution = state
        if self.latch_store is not None:
            try:
                cleared = self.latch_store.clear(
                    self._account_id(), actor=actor, now=now,
                    equity=self.broker.get_account().equity,
                )
                log_event(
                    logger, logging.WARNING, "execution latch cleared in storage",
                    actor=actor, trips_cleared=cleared,
                )
            except Exception as exc:  # pragma: no cover - storage failure
                # The row is still open, so the next restart re-latches.
                # That is the safe direction, and it is said out loud.
                log_event(
                    logger, logging.ERROR,
                    "cleared the latch in memory but not in storage; "
                    "a restart will latch again",
                    error=str(exc), actor=actor,
                )
        log_event(
            logger, logging.WARNING, "safety breaker reset",
            actor=actor, mode=str(state.mode), live_armed=state.live_armed,
        )
        return state

    def set_mode(self, mode, *, actor: str) -> ExecutionState:
        """Switch between the non-live modes. LIVE is not reachable here."""
        self.execution = self.ensure_execution().set_mode(mode, actor=actor)
        log_event(logger, logging.INFO, "mode switched", actor=actor, mode=str(mode))
        return self.execution

    def engage_kill_switch(self, engaged: bool, *, actor: str = "operator") -> RiskState:
        state = self._persist(self.ensure_state().with_kill_switch(engaged))
        if engaged:
            self.trip(TripReason.KILL_SWITCH, f"kill switch engaged by {actor}")
        log_event(logger, logging.WARNING, "kill switch", engaged=engaged, actor=actor)
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
            # A loss breaker is a critical safety event, so it latches
            # execution as well as blocking new trades. Tomorrow's roll
            # clears the daily tally; it does not clear this.
            for tripped, reason in (
                (daily, TripReason.DAILY_LOSS_LIMIT),
                (weekly, TripReason.WEEKLY_LOSS_LIMIT),
                (drawdown, TripReason.MAX_DRAWDOWN),
            ):
                if tripped:
                    self.trip(reason, f"realised {realised_pnl} breached the limit")
        return self._persist(state)

    def settle_protective_exits(self) -> list[ProtectiveExit]:
        """Close positions the market has stopped out or taken to target.

        Drives the paper broker's protective exits, folds each result into
        the risk tally (which may trip a loss breaker) and writes the
        outcome onto the journal row that planned it.

        A broker with no protective simulation returns nothing, which is
        correct for a real venue: there the stop lives at the venue and the
        close arrives as a fill through reconciliation instead.
        """
        settle = getattr(self.broker, "settle_protective_exits", None)
        if settle is None:
            return []

        exits: list[ProtectiveExit] = settle()
        for closed in exits:
            log_event(
                logger, logging.INFO, "protective exit",
                symbol=closed.symbol, reason=str(closed.reason),
                realised_pnl=str(closed.realised_pnl),
                exit_price=str(closed.exit_price),
            )
            self.record_settled_trade(closed.realised_pnl)
            self._journal_outcome(closed)
        return exits

    def _journal_outcome(self, closed: ProtectiveExit) -> None:
        """Write a close onto the row that planned it. Never raises.

        An unmatched close is logged at ERROR rather than ignored: a
        position the platform closed and cannot find a plan for means the
        journal no longer describes what the account did.
        """
        if self.journal_store is None:
            return
        record_outcome = getattr(self.journal_store, "record_outcome", None)
        if record_outcome is None:  # pragma: no cover - older store
            return
        try:
            row_id = record_outcome(
                self._account_id(),
                symbol=closed.symbol,
                strategy=closed.strategy,
                opened_at=closed.opened_at,
                exit_price=closed.exit_price,
                exit_size=closed.quantity,
                realised_pnl=closed.realised_pnl,
                r_multiple=closed.r_multiple,
                exit_reason=str(closed.reason),
                closed_at=closed.closed_at,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            log_event(
                logger, logging.ERROR, "could not journal a closed position",
                symbol=closed.symbol, error=str(exc),
            )
            return
        if row_id is None:
            log_event(
                logger, logging.ERROR,
                "a position closed with no journal row to record it against",
                symbol=closed.symbol, strategy=closed.strategy,
                realised_pnl=str(closed.realised_pnl),
            )

    def account(self) -> Account:
        return self.broker.get_account()

    def _spec_for(self, symbol: str) -> InstrumentSpec | None:
        try:
            return self.data.get_instrument(symbol)
        except Exception:
            return None
