"""The deterministic risk engine.

This module has final authority over every order the platform places.
It is ordinary Python: no model call, no network, no clock of its own.
Give it the same inputs twice and it returns the same verdict twice,
which is what makes a rejection defensible three months later when
somebody asks why a trade did not happen.

Two properties are deliberate and should survive any refactor.

**Every check is recorded, pass or fail.** The verdict carries the full
list, with the limit and the observed value for each. Specification
section 14 asks the platform to display exactly why a setup scored what
it did; the same applies, more strongly, to why an order was refused.

**No input can skip a check.** There is no bypass flag, no "trusted
caller", and no path by which a language model's output reaches a broker
without passing through :meth:`RiskEngine.evaluate`. Grok's opinion
arrives as one more advisory field on the request and is never read
here at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Sequence

from gtcc.data.quality import DataQualityReport
from gtcc.domain.enums import (
    DataQuality,
    Market,
    OrderType,
    RiskAction,
    Side,
    Timeframe,
    TradingMode,
)
from gtcc.domain.events import EconomicEvent, EventImpact
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Quote, utcnow
from gtcc.domain.money import ONE, ZERO, D
from gtcc.domain.orders import Account, Order, OrderRequest, Position
from gtcc.risk.limits import RiskLimits
from gtcc.risk.safety import ExecutionState
from gtcc.risk.sizing import SizingResult, size_position
from gtcc.risk.state import RiskState


class Check(StrEnum):
    """Stable identifiers. Dashboards, alerts and tests key on these."""

    VENUE_REACHABLE = "VENUE_REACHABLE"
    INSTRUMENT_KNOWN = "INSTRUMENT_KNOWN"
    SYMBOL_CONSISTENT = "SYMBOL_CONSISTENT"
    EXECUTION_NOT_TRIPPED = "EXECUTION_NOT_TRIPPED"
    LIVE_MODE_PERMITTED = "LIVE_MODE_PERMITTED"
    KILL_SWITCH = "KILL_SWITCH"
    TRADING_NOT_PAUSED = "TRADING_NOT_PAUSED"
    SYMBOL_ENABLED = "SYMBOL_ENABLED"
    STRATEGY_ENABLED = "STRATEGY_ENABLED"
    MARKET_ENABLED = "MARKET_ENABLED"
    DATA_QUALITY = "DATA_QUALITY"
    QUOTE_AVAILABLE = "QUOTE_AVAILABLE"
    BROKER_HEALTHY = "BROKER_HEALTHY"
    ACCOUNT_RECONCILED = "ACCOUNT_RECONCILED"
    EXPOSURE_KNOWN = "EXPOSURE_KNOWN"
    SPREAD_WITHIN_LIMIT = "SPREAD_WITHIN_LIMIT"
    SLIPPAGE_WITHIN_LIMIT = "SLIPPAGE_WITHIN_LIMIT"
    EVENT_BLACKOUT = "EVENT_BLACKOUT"
    STOP_PRESENT = "STOP_PRESENT"
    STOP_ON_CORRECT_SIDE = "STOP_ON_CORRECT_SIDE"
    STOP_DISTANCE = "STOP_DISTANCE"
    TARGET_PRESENT = "TARGET_PRESENT"
    REWARD_RISK = "REWARD_RISK"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    POSITION_SIZEABLE = "POSITION_SIZEABLE"
    RISK_PER_TRADE = "RISK_PER_TRADE"
    POSITION_NOTIONAL = "POSITION_NOTIONAL"
    LEVERAGE = "LEVERAGE"
    ASSET_EXPOSURE = "ASSET_EXPOSURE"
    CORRELATED_EXPOSURE = "CORRELATED_EXPOSURE"
    SECTOR_EXPOSURE = "SECTOR_EXPOSURE"
    MARKET_EXPOSURE = "MARKET_EXPOSURE"
    BUYING_POWER = "BUYING_POWER"


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    #: The check could not run. A skip is never silently a pass: skips
    #: that matter (unknown exposure, missing calendar) have their own
    #: FAIL-ing companion check.
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class CheckResult:
    code: Check
    outcome: Outcome
    detail: str
    limit: str | None = None
    observed: str | None = None
    #: Set when this ceiling was the reason the size came down, to the
    #: quantity that fits under it. Structured rather than left in the
    #: detail string so a caller can report which limit bound without
    #: parsing English.
    reduced_to: Decimal | None = None

    @property
    def failed(self) -> bool:
        return self.outcome is Outcome.FAIL

    @property
    def reduced(self) -> bool:
        return self.reduced_to is not None


@dataclass(frozen=True, slots=True)
class RewardRisk:
    """Net reward-to-risk, after everything the trade actually costs."""

    entry: Decimal
    stop: Decimal
    target: Decimal
    gross_reward: Decimal
    gross_risk: Decimal
    fees: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal

    @property
    def costs(self) -> Decimal:
        return self.fees + self.spread_cost + self.slippage_cost

    @property
    def net_reward(self) -> Decimal:
        return self.gross_reward - self.costs

    @property
    def net_risk(self) -> Decimal:
        # Costs widen the loss as well as narrowing the win.
        return self.gross_risk + self.costs

    @property
    def ratio(self) -> Decimal:
        if self.net_risk <= ZERO:
            return ZERO
        return self.net_reward / self.net_risk


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Everything the engine is allowed to know.

    Assembled by the caller from live sources. The engine reads nothing
    else — no globals, no database, no clock.
    """

    #: Runtime execution state: current mode, whether a person armed
    #: live during this process's life, and any latched safety trip.
    #: The engine reads it and cannot change it.
    execution: ExecutionState
    account: Account
    instrument: InstrumentSpec
    limits: RiskLimits
    state: RiskState
    data_quality: DataQualityReport
    quote: Quote | None = None
    positions: tuple[Position, ...] = ()
    open_orders: tuple[Order, ...] = ()
    broker_healthy: bool = True
    #: Account-currency exposure per symbol, including the open positions
    #: above. Supply via :func:`exposure_from_positions` so a missing mark
    #: price becomes a refusal rather than a zero.
    exposure_by_symbol: Mapping[str, Decimal] = field(default_factory=dict)
    exposure_known: bool = True
    correlation_group_of: Mapping[str, str] = field(default_factory=dict)
    sector_of: Mapping[str, str] = field(default_factory=dict)
    upcoming_events: tuple[EconomicEvent, ...] = ()
    #: None means no calendar was consulted; the blackout check then
    #: reports SKIP rather than pretending the diary is empty.
    calendar_available: bool = False
    estimated_slippage_bps: Decimal = ZERO
    quote_to_account_rate: Decimal = ONE
    #: Strategies written and tested for event trading opt out of the
    #: macro blackout by declaring themselves here.
    event_strategy: bool = False
    strategy_timeframe: Timeframe | None = None
    now: datetime = field(default_factory=utcnow)

    @property
    def mode(self) -> TradingMode:
        return self.execution.mode

    @property
    def live_trading_enabled(self) -> bool:
        """Armed by a person during this process's life."""
        return self.execution.live_armed


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    """The engine's answer. Immutable, complete, and safe to persist."""

    action: RiskAction
    approved_quantity: Decimal
    checks: tuple[CheckResult, ...]
    sizing: SizingResult | None = None
    reward_risk: RewardRisk | None = None
    requested_quantity: Decimal | None = None
    #: Money at risk between entry and the protective stop AT THE APPROVED
    #: QUANTITY. ``sizing.projected_risk`` is the pre-cap number the risk
    #: budget asked for, which is the right input to the per-trade risk
    #: check and the wrong thing to show a human: once a face-value,
    #: leverage or exposure ceiling shrinks the position, the budget figure
    #: overstates the loss by however much the cap bit. Anything reporting
    #: "if the stop is hit you lose this much" must use this field.
    approved_risk: Decimal | None = None

    @classmethod
    def refused(cls, code: "Check", detail: str) -> "RiskVerdict":
        """A refusal that never reached the engine.

        Used when the context cannot be assembled at all — an unknown
        instrument, for instance. It still comes back as a verdict with a
        check attached, so every refusal in the system has the same
        auditable shape and the API has one response schema.
        """
        return cls(
            action=RiskAction.REJECT,
            approved_quantity=ZERO,
            checks=(CheckResult(code=code, outcome=Outcome.FAIL, detail=detail),),
        )

    @property
    def allowed(self) -> bool:
        return self.action in (RiskAction.ALLOW, RiskAction.REDUCE)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if check.failed)

    @property
    def binding_limits(self) -> tuple[CheckResult, ...]:
        """The ceilings that reduced the size, tightest last."""
        reduced = [check for check in self.checks if check.reduced]
        return tuple(sorted(reduced, key=lambda check: check.reduced_to or ZERO, reverse=True))

    @property
    def failure_codes(self) -> tuple[Check, ...]:
        return tuple(check.code for check in self.failures)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f"{check.code}: {check.detail}" for check in self.failures)

    def explain(self) -> str:
        if self.allowed:
            head = f"{self.action} {self.approved_quantity}"
            if self.action is RiskAction.REDUCE:
                head += f" (requested {self.requested_quantity})"
            return head
        return "REJECT — " + "; ".join(self.reasons)


def exposure_from_positions(
    positions: Sequence[Position],
    specs: Mapping[str, InstrumentSpec],
) -> tuple[dict[str, Decimal], bool]:
    """Account-currency exposure per symbol.

    Returns ``(exposure, known)``. *known* is False when any open
    position cannot be valued, which the engine turns into a refusal:
    specification section 42 forbids trading on an account state that
    cannot be reliably determined.
    """
    exposure: dict[str, Decimal] = {}
    known = True
    for position in positions:
        if position.is_flat:
            continue
        spec = specs.get(position.symbol)
        if spec is None or position.mark_price is None:
            known = False
            continue
        exposure[position.symbol] = exposure.get(position.symbol, ZERO) + spec.notional(
            position.mark_price, abs(position.quantity)
        )
    return exposure, known


class RiskEngine:
    """Stateless evaluator. One instance can serve every account."""

    def evaluate(self, request: OrderRequest, context: RiskContext) -> RiskVerdict:
        checks: list[CheckResult] = []
        add = checks.append

        self._check_mode(request, context, add)
        self._check_symbol_consistency(request, context, add)
        self._check_operator_switches(request, context, add)
        self._check_system_health(context, add)
        self._check_market_conditions(context, add)
        self._check_event_blackout(context, add)
        structure_ok = self._check_trade_structure(request, context, add)
        self._check_account_breakers(context, add)
        self._check_concurrency(request, context, add)

        sizing, reward_risk = None, None
        approved = ZERO
        requested = request.quantity

        if structure_ok:
            sizing = self._size(request, context)
            add(
                CheckResult(
                    code=Check.POSITION_SIZEABLE,
                    outcome=Outcome.PASS if sizing.is_tradeable else Outcome.FAIL,
                    detail=sizing.rejected_reason or f"sized {sizing.quantity}",
                    observed=str(sizing.quantity),
                )
            )
            if sizing.is_tradeable:
                # Caps first, then score the trade at the size that would
                # actually be sent. Scoring the pre-cap size would report a
                # cost base the order never incurs.
                approved = self._apply_size_limits(request, context, sizing, add)
                reward_risk = self._reward_risk(
                    request, context, sizing, quantity=approved or sizing.quantity
                )
                if reward_risk is None:
                    add(
                        CheckResult(
                            code=Check.REWARD_RISK,
                            outcome=Outcome.SKIP,
                            detail="reward:risk could not be computed",
                        )
                    )
                else:
                    passed = reward_risk.ratio >= context.limits.min_reward_risk
                    add(
                        CheckResult(
                            code=Check.REWARD_RISK,
                            outcome=Outcome.PASS if passed else Outcome.FAIL,
                            detail=(
                                f"net reward:risk {reward_risk.ratio:.2f} against the "
                                f"nearest target {reward_risk.target}, after "
                                f"{reward_risk.costs:.2f} of fees, spread and slippage"
                            ),
                            limit=str(context.limits.min_reward_risk),
                            observed=f"{reward_risk.ratio:.2f}",
                        )
                    )
        else:
            add(
                CheckResult(
                    code=Check.POSITION_SIZEABLE,
                    outcome=Outcome.SKIP,
                    detail="trade structure invalid, so no size was computed",
                )
            )

        action = self._decide(checks, approved, requested)
        if action is RiskAction.REJECT:
            approved = ZERO

        return RiskVerdict(
            action=action,
            approved_quantity=approved,
            checks=tuple(checks),
            sizing=sizing,
            reward_risk=reward_risk,
            requested_quantity=requested,
            approved_risk=None if sizing is None else sizing.risk_per_unit * approved,
        )

    # -- gate groups ---------------------------------------------------------

    def _check_mode(self, request: OrderRequest, ctx: RiskContext, add) -> None:
        """Nothing may execute while a safety breaker is latched, and a
        live order needs arming that happened in this process's life.

        These are two separate checks because they fail for different
        reasons and recover differently. A latched breaker stops orders
        in every mode, paper included, and clears only by an authorised
        reset. Live arming is about who permitted real money.
        """
        execution = ctx.execution

        add(
            CheckResult(
                code=Check.EXECUTION_NOT_TRIPPED,
                outcome=Outcome.FAIL if execution.tripped else Outcome.PASS,
                detail=(
                    "a safety breaker is latched and needs an authorised reset: "
                    + "; ".join(trip.describe() for trip in execution.trips)
                    if execution.tripped
                    else "no safety breaker is latched"
                ),
                observed=",".join(str(reason) for reason in execution.trip_reasons) or "clear",
            )
        )

        if execution.mode is TradingMode.LIVE and not execution.live_armed:
            add(
                CheckResult(
                    code=Check.LIVE_MODE_PERMITTED,
                    outcome=Outcome.FAIL,
                    detail=(
                        "mode is LIVE but nobody armed live execution in this "
                        "process; no order may reach a broker"
                    ),
                )
            )
            return
        if execution.mode is TradingMode.LIVE and ctx.state.breakers().live_execution_blocked:
            add(
                CheckResult(
                    code=Check.LIVE_MODE_PERMITTED,
                    outcome=Outcome.FAIL,
                    detail="live execution is disabled by a risk circuit breaker",
                )
            )
            return
        add(
            CheckResult(
                code=Check.LIVE_MODE_PERMITTED,
                outcome=Outcome.PASS,
                detail=(
                    f"mode {execution.mode}"
                    + (f", armed by {execution.armed_by}" if execution.live_armed else "")
                ),
                observed=str(execution.mode),
            )
        )

    def _check_operator_switches(self, request: OrderRequest, ctx: RiskContext, add) -> None:
        state = ctx.state
        add(
            _boolean(
                Check.KILL_SWITCH,
                not state.kill_switch,
                "kill switch is clear",
                "emergency kill switch is engaged",
            )
        )
        add(
            _boolean(
                Check.TRADING_NOT_PAUSED,
                not state.trading_paused,
                "trading is active",
                "trading is paused by the operator",
            )
        )
        add(
            _boolean(
                Check.SYMBOL_ENABLED,
                request.symbol not in state.disabled_symbols,
                f"{request.symbol} is enabled",
                f"{request.symbol} is disabled by the operator",
            )
        )
        add(
            _boolean(
                Check.STRATEGY_ENABLED,
                request.strategy not in state.disabled_strategies,
                f"strategy {request.strategy} is enabled",
                f"strategy {request.strategy} is disabled by the operator",
            )
        )
        add(
            _boolean(
                Check.MARKET_ENABLED,
                request.market not in state.disabled_markets,
                f"{request.market} is enabled",
                f"{request.market} is disabled by the operator",
            )
        )

    def _check_symbol_consistency(self, request: OrderRequest, ctx: RiskContext, add) -> None:
        """The order, the contract spec and the quote must name one symbol.

        A test fixture that returned one instrument for every symbol let
        an AAPL order be sized against a Bitcoin contract specification,
        and nothing in the engine noticed. In production the same shape
        of mistake is an adapter mapping error, and it would price and
        size a position against the wrong instrument entirely.
        """
        names = {("order", request.symbol), ("instrument", ctx.instrument.symbol)}
        if ctx.quote is not None:
            names.add(("quote", ctx.quote.symbol))
        distinct = {symbol for _, symbol in names}

        add(
            CheckResult(
                code=Check.SYMBOL_CONSISTENT,
                outcome=Outcome.PASS if len(distinct) == 1 else Outcome.FAIL,
                detail=(
                    f"order, instrument and quote all name {request.symbol}"
                    if len(distinct) == 1
                    else "symbol mismatch: "
                    + ", ".join(f"{role}={symbol}" for role, symbol in sorted(names))
                ),
                observed=",".join(sorted(distinct)),
            )
        )

    def _check_system_health(self, ctx: RiskContext, add) -> None:
        add(
            _boolean(
                Check.BROKER_HEALTHY,
                ctx.broker_healthy,
                "broker connection healthy",
                "broker connection is unhealthy; order refused",
            )
        )
        add(
            _boolean(
                Check.ACCOUNT_RECONCILED,
                ctx.account.reconciled_at is not None,
                f"account reconciled at {ctx.account.reconciled_at}",
                "account state has never been reconciled against the broker",
            )
        )
        add(
            _boolean(
                Check.EXPOSURE_KNOWN,
                ctx.exposure_known,
                "all open positions can be valued",
                "an open position has no mark price, so exposure is unknown",
            )
        )

    def _check_market_conditions(self, ctx: RiskContext, add) -> None:
        quality = ctx.data_quality.status
        add(
            CheckResult(
                code=Check.DATA_QUALITY,
                outcome=Outcome.PASS if quality.tradeable else Outcome.FAIL,
                detail=ctx.data_quality.summary(),
                limit=f"better than {DataQuality.INVALID}",
                observed=str(quality),
            )
        )

        quote = ctx.quote
        if quote is None:
            add(
                CheckResult(
                    code=Check.QUOTE_AVAILABLE,
                    outcome=Outcome.FAIL,
                    detail="no quote available, so the order cannot be priced",
                )
            )
            add(
                CheckResult(
                    code=Check.SPREAD_WITHIN_LIMIT,
                    outcome=Outcome.SKIP,
                    detail="no quote to measure the spread from",
                )
            )
        else:
            add(
                CheckResult(
                    code=Check.QUOTE_AVAILABLE,
                    outcome=Outcome.PASS,
                    detail=f"bid {quote.bid} ask {quote.ask}",
                )
            )
            within = quote.spread_bps <= ctx.limits.max_spread_bps
            add(
                CheckResult(
                    code=Check.SPREAD_WITHIN_LIMIT,
                    outcome=Outcome.PASS if within else Outcome.FAIL,
                    detail=f"spread {quote.spread_bps:.1f}bps",
                    limit=f"{ctx.limits.max_spread_bps}bps",
                    observed=f"{quote.spread_bps:.1f}bps",
                )
            )

        slippage_ok = ctx.estimated_slippage_bps <= ctx.limits.max_slippage_bps
        add(
            CheckResult(
                code=Check.SLIPPAGE_WITHIN_LIMIT,
                outcome=Outcome.PASS if slippage_ok else Outcome.FAIL,
                detail=f"estimated slippage {ctx.estimated_slippage_bps}bps",
                limit=f"{ctx.limits.max_slippage_bps}bps",
                observed=str(ctx.estimated_slippage_bps),
            )
        )

    def _check_event_blackout(self, ctx: RiskContext, add) -> None:
        window = ctx.limits.event_blackout_minutes
        if window <= 0:
            add(
                CheckResult(
                    code=Check.EVENT_BLACKOUT,
                    outcome=Outcome.SKIP,
                    detail="no event blackout configured",
                )
            )
            return
        if ctx.event_strategy:
            add(
                CheckResult(
                    code=Check.EVENT_BLACKOUT,
                    outcome=Outcome.SKIP,
                    detail="strategy is declared an event strategy and opts out",
                )
            )
            return
        timeframe = ctx.strategy_timeframe
        threshold = ctx.limits.event_blackout_applies_above_timeframe_seconds
        if timeframe is not None and timeframe.seconds > threshold:
            add(
                CheckResult(
                    code=Check.EVENT_BLACKOUT,
                    outcome=Outcome.SKIP,
                    detail=f"{timeframe} is longer-dated than the blackout applies to",
                )
            )
            return
        if not ctx.calendar_available:
            add(
                CheckResult(
                    code=Check.EVENT_BLACKOUT,
                    outcome=Outcome.SKIP,
                    detail=(
                        "no economic calendar was consulted; this is not evidence "
                        "that no event is scheduled"
                    ),
                )
            )
            return

        imminent = [
            event
            for event in ctx.upcoming_events
            if event.impact is EventImpact.HIGH
            and 0 <= event.minutes_until(ctx.now) <= window
            and event.affects(
                quote_currency=ctx.instrument.quote_currency,
                base_currency=ctx.instrument.base_currency,
                market=ctx.instrument.market,
            )
        ]
        if imminent:
            names = ", ".join(f"{e.name} in {e.minutes_until(ctx.now):.0f}m" for e in imminent[:3])
            add(
                CheckResult(
                    code=Check.EVENT_BLACKOUT,
                    outcome=Outcome.FAIL,
                    detail=f"high-impact event inside the {window}m blackout: {names}",
                    limit=f"{window}m",
                )
            )
        else:
            add(
                CheckResult(
                    code=Check.EVENT_BLACKOUT,
                    outcome=Outcome.PASS,
                    detail=f"no high-impact event within {window}m",
                )
            )

    def _check_trade_structure(self, request: OrderRequest, ctx: RiskContext, add) -> bool:
        """Entry, stop and target must make sense before anything is sized."""
        entry = self._entry_price(request, ctx)
        stop = request.protective_stop

        if stop is None:
            add(
                CheckResult(
                    code=Check.STOP_PRESENT,
                    outcome=Outcome.FAIL,
                    detail="no protective stop supplied; the trade has undefined risk",
                )
            )
            for code in (Check.STOP_ON_CORRECT_SIDE, Check.STOP_DISTANCE):
                add(CheckResult(code=code, outcome=Outcome.SKIP, detail="no stop to check"))
            self._target_check(request, add, skip="no stop, so no reward:risk")
            return False

        add(CheckResult(code=Check.STOP_PRESENT, outcome=Outcome.PASS, detail=f"stop {stop}"))

        if entry is None:
            for code in (Check.STOP_ON_CORRECT_SIDE, Check.STOP_DISTANCE):
                add(
                    CheckResult(
                        code=code,
                        outcome=Outcome.SKIP,
                        detail="no entry price available to compare the stop against",
                    )
                )
            self._target_check(request, add, skip="no entry price")
            return False

        correct_side = (
            stop < entry if request.side is Side.BUY else stop > entry
        )
        add(
            CheckResult(
                code=Check.STOP_ON_CORRECT_SIDE,
                outcome=Outcome.PASS if correct_side else Outcome.FAIL,
                detail=(
                    f"{request.side} entry {entry} with stop {stop}"
                    + ("" if correct_side else " — the stop is on the wrong side of the entry")
                ),
            )
        )

        ticks = abs(entry - stop) / ctx.instrument.tick_size
        far_enough = ticks >= ctx.limits.min_stop_distance_ticks
        add(
            CheckResult(
                code=Check.STOP_DISTANCE,
                outcome=Outcome.PASS if far_enough else Outcome.FAIL,
                detail=f"stop is {ticks:.0f} ticks away",
                limit=f"{ctx.limits.min_stop_distance_ticks} ticks",
                observed=f"{ticks:.0f}",
            )
        )

        has_target = bool(request.targets)
        add(
            CheckResult(
                code=Check.TARGET_PRESENT,
                outcome=Outcome.PASS if has_target else Outcome.FAIL,
                detail=(
                    f"{len(request.targets)} target(s)"
                    if has_target
                    else "no target supplied, so reward cannot be established"
                ),
            )
        )
        if not has_target:
            add(
                CheckResult(
                    code=Check.REWARD_RISK,
                    outcome=Outcome.SKIP,
                    detail="no target to measure reward against",
                )
            )

        return correct_side and far_enough and has_target

    def _target_check(self, request: OrderRequest, add, *, skip: str) -> None:
        add(
            CheckResult(
                code=Check.TARGET_PRESENT,
                outcome=Outcome.SKIP if not request.targets else Outcome.PASS,
                detail=skip if not request.targets else f"{len(request.targets)} target(s)",
            )
        )
        add(CheckResult(code=Check.REWARD_RISK, outcome=Outcome.SKIP, detail=skip))

    def _check_account_breakers(self, ctx: RiskContext, add) -> None:
        state, limits = ctx.state, ctx.limits

        daily = state.daily_loss_fraction()
        add(
            _threshold(
                Check.DAILY_LOSS_LIMIT,
                observed=daily,
                limit=limits.max_daily_loss,
                unit="of day-start equity lost",
                tripped=state.daily_breaker_tripped,
                tripped_detail="daily loss circuit breaker is tripped",
            )
        )

        weekly = state.weekly_loss_fraction()
        add(
            _threshold(
                Check.WEEKLY_LOSS_LIMIT,
                observed=weekly,
                limit=limits.max_weekly_loss,
                unit="of week-start equity lost",
                tripped=state.weekly_breaker_tripped,
                tripped_detail="weekly loss circuit breaker is tripped",
            )
        )

        drawdown = state.drawdown_fraction(ctx.account.equity)
        add(
            _threshold(
                Check.MAX_DRAWDOWN,
                observed=drawdown,
                limit=limits.max_drawdown,
                unit="below the equity high-water mark",
                tripped=state.drawdown_breaker_tripped,
                tripped_detail="drawdown circuit breaker is tripped",
            )
        )

        within = state.consecutive_losses < limits.max_consecutive_losses
        add(
            CheckResult(
                code=Check.CONSECUTIVE_LOSSES,
                outcome=Outcome.PASS if within else Outcome.FAIL,
                detail=f"{state.consecutive_losses} consecutive losing trades",
                limit=str(limits.max_consecutive_losses),
                observed=str(state.consecutive_losses),
            )
        )

    def _check_concurrency(self, request: OrderRequest, ctx: RiskContext, add) -> None:
        open_positions = [p for p in ctx.positions if not p.is_flat]
        already_in = any(p.symbol == request.symbol for p in open_positions)
        count = len(open_positions) + (0 if already_in else 1)
        within = count <= ctx.limits.max_open_positions
        add(
            CheckResult(
                code=Check.MAX_OPEN_POSITIONS,
                outcome=Outcome.PASS if within else Outcome.FAIL,
                detail=f"this order would make {count} open position(s)",
                limit=str(ctx.limits.max_open_positions),
                observed=str(count),
            )
        )

    # -- sizing --------------------------------------------------------------

    def _entry_price(self, request: OrderRequest, ctx: RiskContext) -> Decimal | None:
        """The price the trade is planned around.

        A market order is priced at the side of the book it will cross,
        never at the mid: assuming the mid is how a backtest invents an
        edge that does not survive contact with a broker.
        """
        if request.limit_price is not None:
            return request.limit_price
        if request.stop_price is not None and request.order_type in (
            OrderType.STOP,
            OrderType.STOP_LIMIT,
        ):
            return request.stop_price
        if ctx.quote is None:
            return None
        return ctx.quote.ask if request.side is Side.BUY else ctx.quote.bid

    def _size(self, request: OrderRequest, ctx: RiskContext) -> SizingResult:
        entry = self._entry_price(request, ctx)
        assert entry is not None and request.protective_stop is not None
        return size_position(
            instrument=ctx.instrument,
            equity=ctx.account.equity,
            risk_fraction=ctx.limits.max_risk_per_trade,
            entry=entry,
            stop=request.protective_stop,
            quote_to_account_rate=ctx.quote_to_account_rate,
            max_quantity=request.quantity,
        )

    def _reward_risk(
        self,
        request: OrderRequest,
        ctx: RiskContext,
        sizing: SizingResult,
        *,
        quantity: Decimal,
    ) -> RewardRisk | None:
        entry = self._entry_price(request, ctx)
        if entry is None or request.protective_stop is None or not request.targets:
            return None
        if quantity <= ZERO:
            return None

        # The NEAREST target, not the furthest. Section 15 forbids
        # inventing a distant target to satisfy the minimum ratio, and
        # taking the nearest one removes the incentive to try.
        target = min(request.targets, key=lambda t: abs(t - entry))

        # risk_per_unit is "account-currency move per unit between two
        # prices"; entry-to-target is the reward side of the same sum.
        reward_per_unit = ctx.instrument.risk_per_unit(
            entry, target, quote_to_account_rate=ctx.quote_to_account_rate
        )
        notional = ctx.instrument.notional(
            entry, quantity, quote_to_account_rate=ctx.quote_to_account_rate
        )
        gross_reward = reward_per_unit * quantity
        gross_risk = sizing.risk_per_unit * quantity

        spread_cost = ZERO
        if ctx.quote is not None and request.order_type is OrderType.MARKET:
            spread_cost = (
                ctx.quote.spread
                * quantity
                * ctx.instrument.contract_size
                * ctx.quote_to_account_rate
            )
        slippage_cost = notional * ctx.estimated_slippage_bps / D(10000)

        return RewardRisk(
            entry=entry,
            stop=request.protective_stop,
            target=target,
            gross_reward=gross_reward,
            gross_risk=gross_risk,
            fees=ctx.instrument.fee(notional) * D(2),
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
        )

    def _apply_size_limits(
        self, request: OrderRequest, ctx: RiskContext, sizing: SizingResult, add
    ) -> Decimal:
        """Shrink the size to the tightest binding limit, recording each."""
        equity = ctx.account.equity
        limits = ctx.limits
        instrument = ctx.instrument
        allowed = sizing.quantity

        risk_limit = equity * limits.max_risk_per_trade
        risk_ok = sizing.projected_risk <= risk_limit
        add(
            CheckResult(
                code=Check.RISK_PER_TRADE,
                outcome=Outcome.PASS if risk_ok else Outcome.FAIL,
                detail=f"position risks {sizing.projected_risk:.2f} at the stop",
                limit=f"{risk_limit:.2f}",
                observed=f"{sizing.projected_risk:.2f}",
            )
        )

        # Per-market where configured: see MarketOverride on why a single
        # notional ceiling cannot cover both cash equities and futures.
        notional_cap = equity * limits.position_notional_limit(request.market)
        allowed = self._cap(
            add,
            Check.POSITION_NOTIONAL,
            allowed=allowed,
            value=sizing.notional,
            cap=notional_cap,
            instrument=instrument,
            quantity_sized=sizing.quantity,
            detail="position notional",
        )

        leverage_ceiling = min(limits.leverage_limit(request.market), instrument.max_leverage)
        leverage_cap_notional = equity * leverage_ceiling
        allowed = self._cap(
            add,
            Check.LEVERAGE,
            allowed=allowed,
            value=sizing.notional,
            cap=leverage_cap_notional,
            instrument=instrument,
            quantity_sized=sizing.quantity,
            detail=(
                f"notional at {sizing.leverage:.2f}x leverage, ceiling "
                f"{leverage_ceiling}x —"
            ),
        )

        existing_symbol = D(ctx.exposure_by_symbol.get(request.symbol, ZERO))
        allowed = self._cap(
            add,
            Check.ASSET_EXPOSURE,
            allowed=allowed,
            value=existing_symbol + sizing.notional,
            cap=equity * limits.asset_exposure_limit(request.market),
            instrument=instrument,
            quantity_sized=sizing.quantity,
            detail=f"exposure to {request.symbol}",
            already=existing_symbol,
        )

        group = ctx.correlation_group_of.get(request.symbol)
        if group:
            existing_group = sum(
                (
                    D(value)
                    for symbol, value in ctx.exposure_by_symbol.items()
                    if ctx.correlation_group_of.get(symbol) == group
                ),
                ZERO,
            )
            allowed = self._cap(
                add,
                Check.CORRELATED_EXPOSURE,
                allowed=allowed,
                value=existing_group + sizing.notional,
                cap=equity * limits.max_correlated_exposure,
                instrument=instrument,
                quantity_sized=sizing.quantity,
                detail=f"exposure to correlation group {group}",
                already=existing_group,
            )
        else:
            add(
                CheckResult(
                    code=Check.CORRELATED_EXPOSURE,
                    outcome=Outcome.SKIP,
                    detail=f"{request.symbol} is not mapped to a correlation group",
                )
            )

        sector = ctx.sector_of.get(request.symbol)
        if sector:
            existing_sector = sum(
                (
                    D(value)
                    for symbol, value in ctx.exposure_by_symbol.items()
                    if ctx.sector_of.get(symbol) == sector
                ),
                ZERO,
            )
            allowed = self._cap(
                add,
                Check.SECTOR_EXPOSURE,
                allowed=allowed,
                value=existing_sector + sizing.notional,
                cap=equity * limits.max_sector_exposure,
                instrument=instrument,
                quantity_sized=sizing.quantity,
                detail=f"exposure to sector {sector}",
                already=existing_sector,
            )
        else:
            add(
                CheckResult(
                    code=Check.SECTOR_EXPOSURE,
                    outcome=Outcome.SKIP,
                    detail=f"{request.symbol} is not mapped to a sector",
                )
            )

        market_cap = limits.market_exposure_limit(request.market)
        if market_cap is not None:
            existing_market = sum(
                (
                    D(value)
                    for symbol, value in ctx.exposure_by_symbol.items()
                    if symbol == request.symbol
                ),
                ZERO,
            )
            allowed = self._cap(
                add,
                Check.MARKET_EXPOSURE,
                allowed=allowed,
                value=existing_market + sizing.notional,
                cap=equity * market_cap,
                instrument=instrument,
                quantity_sized=sizing.quantity,
                detail=f"exposure to {request.market}",
                already=existing_market,
            )
        else:
            add(
                CheckResult(
                    code=Check.MARKET_EXPOSURE,
                    outcome=Outcome.SKIP,
                    detail=f"no exposure limit configured for {request.market}",
                )
            )

        buying_power_ok = sizing.notional <= ctx.account.buying_power or (
            instrument.max_leverage > ONE
        )
        add(
            CheckResult(
                code=Check.BUYING_POWER,
                outcome=Outcome.PASS if buying_power_ok else Outcome.FAIL,
                detail=f"notional {sizing.notional:.2f}",
                limit=f"{ctx.account.buying_power:.2f}",
                observed=f"{sizing.notional:.2f}",
            )
        )

        return max(ZERO, allowed)

    def _cap(
        self,
        add,
        code: Check,
        *,
        allowed: Decimal,
        value: Decimal,
        cap: Decimal,
        instrument: InstrumentSpec,
        detail: str,
        quantity_sized: Decimal,
        already: Decimal = ZERO,
    ) -> Decimal:
        """Record a notional ceiling and shrink *allowed* to respect it."""
        if value <= cap:
            add(
                CheckResult(
                    code=code,
                    outcome=Outcome.PASS,
                    detail=f"{detail} {value:.2f}",
                    limit=f"{cap:.2f}",
                    observed=f"{value:.2f}",
                )
            )
            return allowed

        # Each ceiling is converted independently into the largest
        # quantity that fits under it, and the caller keeps the smallest.
        # Scaling the running quantity instead would compound every cap
        # against every earlier one and undersize the position.
        headroom = cap - already
        unit_notional = (value - already) / quantity_sized if quantity_sized > ZERO else ZERO
        if headroom <= ZERO or unit_notional <= ZERO:
            fits = ZERO
        else:
            fits = instrument.round_quantity(headroom / unit_notional)

        add(
            CheckResult(
                code=code,
                outcome=Outcome.FAIL if fits <= ZERO else Outcome.PASS,
                detail=(
                    f"{detail} {value:.2f} exceeds the limit; "
                    + (
                        f"size reduced to {fits}"
                        if fits > ZERO
                        else "no size fits inside the limit"
                    )
                ),
                limit=f"{cap:.2f}",
                observed=f"{value:.2f}",
                reduced_to=fits,
            )
        )
        return min(allowed, fits)

    # -- verdict --------------------------------------------------------------

    def _decide(
        self, checks: Sequence[CheckResult], approved: Decimal, requested: Decimal | None
    ) -> RiskAction:
        if any(check.failed for check in checks):
            return RiskAction.REJECT
        if approved <= ZERO:
            return RiskAction.REJECT
        if requested is not None and approved < requested:
            return RiskAction.REDUCE
        return RiskAction.ALLOW


def _boolean(code: Check, ok: bool, pass_detail: str, fail_detail: str) -> CheckResult:
    return CheckResult(
        code=code,
        outcome=Outcome.PASS if ok else Outcome.FAIL,
        detail=pass_detail if ok else fail_detail,
    )


def _threshold(
    code: Check,
    *,
    observed: Decimal,
    limit: Decimal,
    unit: str,
    tripped: bool,
    tripped_detail: str,
) -> CheckResult:
    if tripped:
        return CheckResult(
            code=code,
            outcome=Outcome.FAIL,
            detail=tripped_detail,
            limit=f"{limit * 100:.2f}%",
            observed=f"{observed * 100:.2f}%",
        )
    within = observed < limit
    return CheckResult(
        code=code,
        outcome=Outcome.PASS if within else Outcome.FAIL,
        detail=f"{observed * 100:.2f}% {unit}",
        limit=f"{limit * 100:.2f}%",
        observed=f"{observed * 100:.2f}%",
    )
