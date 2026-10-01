"""Account, risk, order and control endpoints.

Every order endpoint goes through :meth:`TradingRuntime.submit`, which
calls the risk engine first. There is no "force" parameter and no
endpoint that places an order without a verdict attached to the reply.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session as DbSession

from gtcc.api.deps import AppContext, Principal, current_principal, get_context, get_db, require_owner
from gtcc.api.schemas import (
    AccountOut,
    LiveArmIn,
    CheckOut,
    LimitsOut,
    ModeSwitchIn,
    OrderOut,
    OrderRequestIn,
    PositionOut,
    RiskStateOut,
    SubmissionOut,
    ToggleIn,
    VerdictOut,
)
from gtcc.api.security import audit
from gtcc.risk.safety import LIVE_CONFIRMATION_PHRASE, ExecutionState, LiveArmingError
from gtcc.domain.enums import TradingMode
from gtcc.domain.money import D
from gtcc.domain.orders import Order, OrderRequest, Position
from gtcc.risk.engine import RiskVerdict
from gtcc.risk.state import RiskState

router = APIRouter(prefix="/api", tags=["trading"])


# -- serialisation helpers ------------------------------------------------------


def _verdict_out(verdict: RiskVerdict) -> VerdictOut:
    return VerdictOut(
        action=str(verdict.action),
        approved_quantity=verdict.approved_quantity,
        requested_quantity=verdict.requested_quantity,
        reward_risk=verdict.reward_risk.ratio if verdict.reward_risk else None,
        projected_risk=verdict.sizing.projected_risk if verdict.sizing else None,
        checks=[
            CheckOut(
                code=str(check.code),
                outcome=str(check.outcome),
                detail=check.detail,
                limit=check.limit,
                observed=check.observed,
            )
            for check in verdict.checks
        ],
        reasons=list(verdict.reasons),
        explanation=verdict.explain(),
    )


def _order_out(order: Order) -> OrderOut:
    return OrderOut(
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        symbol=order.symbol,
        side=order.side,
        order_type=order.order_type,
        status=str(order.status),
        mode=order.mode,
        strategy=order.strategy,
        quantity=order.quantity,
        filled_quantity=order.filled_quantity,
        average_fill_price=order.average_fill_price,
        fees_paid=order.fees_paid,
        reject_reason=order.reject_reason,
        created_at=order.created_at,
    )


def _position_out(position: Position) -> PositionOut:
    return PositionOut(
        symbol=position.symbol,
        market=position.market,
        quantity=position.quantity,
        average_entry_price=position.average_entry_price,
        mark_price=position.mark_price,
        unrealised_pnl=position.unrealised_pnl(),
        realised_pnl=position.realised_pnl,
        protective_stop=position.protective_stop,
        strategy=position.strategy,
    )


def _state_out(state: RiskState, equity: Decimal) -> RiskStateOut:
    breakers = state.breakers()
    return RiskStateOut(
        account_id=state.account_id,
        day_start_equity=state.day_start_equity,
        peak_equity=state.peak_equity,
        realised_pnl_today=state.realised_pnl_today,
        realised_pnl_week=state.realised_pnl_week,
        daily_loss_pct=state.daily_loss_fraction() * D(100),
        weekly_loss_pct=state.weekly_loss_fraction() * D(100),
        drawdown_pct=state.drawdown_fraction(equity) * D(100),
        consecutive_losses=state.consecutive_losses,
        trades_today=state.trades_today,
        kill_switch=state.kill_switch,
        trading_paused=state.trading_paused,
        daily_breaker_tripped=state.daily_breaker_tripped,
        weekly_breaker_tripped=state.weekly_breaker_tripped,
        drawdown_breaker_tripped=state.drawdown_breaker_tripped,
        new_trades_blocked=breakers.new_trades_blocked,
        live_execution_blocked=breakers.live_execution_blocked,
        breaker_reasons=list(breakers.reasons),
        disabled_symbols=sorted(state.disabled_symbols),
        disabled_strategies=sorted(state.disabled_strategies),
        disabled_markets=sorted(str(m) for m in state.disabled_markets),
    )


def _to_domain(body: OrderRequestIn) -> OrderRequest:
    return OrderRequest(
        symbol=body.symbol,
        market=body.market,
        side=body.side,
        order_type=body.order_type,
        quantity=body.quantity,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        protective_stop=body.protective_stop,
        targets=tuple(body.targets),
        time_in_force=body.time_in_force,
        strategy=body.strategy,
    )


# -- account ---------------------------------------------------------------------


@router.get("/account", response_model=AccountOut)
def get_account(
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> AccountOut:
    account = context.runtime.account()
    state = context.runtime.ensure_state()
    return AccountOut(
        account_id=account.account_id,
        currency=account.currency,
        equity=account.equity,
        cash=account.cash,
        buying_power=account.buying_power,
        mode=account.mode,
        reconciled_at=account.reconciled_at,
        drawdown=state.drawdown_fraction(account.equity) * D(100),
    )


@router.get("/positions", response_model=list[PositionOut])
def get_positions(
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> list[PositionOut]:
    return [_position_out(p) for p in context.runtime.broker.get_positions()]


# -- risk ---------------------------------------------------------------------------


@router.get("/risk/state", response_model=RiskStateOut)
def get_risk_state(
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> RiskStateOut:
    account = context.runtime.account()
    return _state_out(context.runtime.ensure_state(), account.equity)


@router.get("/risk/limits", response_model=LimitsOut)
def get_risk_limits(
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> LimitsOut:
    limits = context.runtime.limits
    return LimitsOut(
        max_risk_per_trade_pct=limits.max_risk_per_trade * D(100),
        max_position_notional_pct=limits.max_position_notional * D(100),
        max_leverage=limits.max_leverage,
        min_reward_risk=limits.min_reward_risk,
        max_spread_bps=limits.max_spread_bps,
        max_daily_loss_pct=limits.max_daily_loss * D(100),
        max_weekly_loss_pct=limits.max_weekly_loss * D(100),
        max_drawdown_pct=limits.max_drawdown * D(100),
        max_consecutive_losses=limits.max_consecutive_losses,
        max_open_positions=limits.max_open_positions,
        is_example=limits.is_example,
    )


@router.post("/risk/evaluate", response_model=VerdictOut)
def evaluate_order(
    body: OrderRequestIn,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> VerdictOut:
    """Dry run. Returns the verdict without placing anything."""
    return _verdict_out(context.runtime.evaluate(_to_domain(body)))


# -- orders -------------------------------------------------------------------------


@router.get("/orders", response_model=list[OrderOut])
def list_orders(
    open_only: bool = False,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> list[OrderOut]:
    orders = context.runtime.broker.get_orders(open_only=open_only)
    return [_order_out(order) for order in orders]


@router.post("/orders", response_model=SubmissionOut)
def submit_order(
    body: OrderRequestIn,
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
    db: DbSession = Depends(get_db),
) -> SubmissionOut:
    result = context.runtime.submit(_to_domain(body))
    audit(
        db,
        "order.submit",
        user_id=principal.user.id,
        ip_address=request.client.host if request.client else None,
        detail={
            "symbol": body.symbol,
            "strategy": body.strategy,
            "action": str(result.verdict.action),
            "placed": result.placed,
            "failures": [str(code) for code in result.verdict.failure_codes],
        },
    )
    return SubmissionOut(
        placed=result.placed,
        detail=result.detail,
        verdict=_verdict_out(result.verdict),
        order=_order_out(result.order) if result.order else None,
    )


@router.post("/orders/{client_order_id}/cancel", response_model=OrderOut)
def cancel_order(
    client_order_id: str,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> OrderOut:
    try:
        order = context.runtime.broker.cancel_order(client_order_id)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _order_out(order)


# -- operator controls (specification section 37) -----------------------------------


@router.post("/control/kill-switch", response_model=RiskStateOut)
def kill_switch(
    body: ToggleIn,
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(require_owner),
    db: DbSession = Depends(get_db),
) -> RiskStateOut:
    state = context.runtime.engage_kill_switch(
        body.enabled, actor=principal.user.email
    )
    audit(
        db,
        "control.kill_switch",
        user_id=principal.user.id,
        ip_address=request.client.host if request.client else None,
        detail={"enabled": body.enabled, "reason": body.reason},
    )
    return _state_out(state, context.runtime.account().equity)


@router.post("/control/pause", response_model=RiskStateOut)
def pause_trading(
    body: ToggleIn,
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(require_owner),
    db: DbSession = Depends(get_db),
) -> RiskStateOut:
    state = context.runtime.pause(body.enabled)
    audit(
        db,
        "control.pause",
        user_id=principal.user.id,
        ip_address=request.client.host if request.client else None,
        detail={"paused": body.enabled, "reason": body.reason},
    )
    return _state_out(state, context.runtime.account().equity)


@router.post("/control/close-position/{symbol}", response_model=OrderOut)
def close_position(
    symbol: str,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(require_owner),
) -> OrderOut:
    try:
        order = context.runtime.broker.close_position(symbol)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _order_out(order)


@router.post("/control/mode", response_model=dict)
def switch_mode(
    body: ModeSwitchIn,
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(require_owner),
    db: DbSession = Depends(get_db),
) -> dict:
    """Switch between BACKTEST and PAPER.

    LIVE is deliberately not reachable here. It is not a mode you
    select; it is a state a person arms, with a phrase, through
    ``/api/control/live/arm``. Keeping it off this endpoint means there
    is exactly one door into live execution.
    """
    runtime = context.runtime
    previous = runtime.ensure_execution().mode
    try:
        state = runtime.set_mode(body.target, actor=principal.user.email)
    except LiveArmingError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    audit(
        db,
        "control.mode_switch",
        user_id=principal.user.id,
        ip_address=request.client.host if request.client else None,
        detail={"from": str(previous), "to": str(body.target)},
    )
    return {"mode": str(state.mode), "previous": str(previous)}


def _execution_out(state: ExecutionState) -> dict:
    return {
        "mode": str(state.mode),
        "live_armed": state.live_armed,
        "live_permitted": state.live_permitted,
        "armed_by": state.armed_by,
        "armed_at": state.armed_at.isoformat() if state.armed_at else None,
        "tripped": state.tripped,
        "new_trades_blocked": state.new_trades_blocked,
        "trips": [
            {
                "reason": str(trip.reason),
                "detail": trip.detail,
                "occurred_at": trip.occurred_at.isoformat(),
            }
            for trip in state.trips
        ],
        "last_reset_at": state.last_reset_at.isoformat() if state.last_reset_at else None,
        "last_reset_by": state.last_reset_by,
        "summary": state.describe(),
    }


@router.get("/control/live", response_model=dict)
def live_status(
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> dict:
    state = context.runtime.ensure_execution()
    return {
        "deployment_allows_live": context.settings.allow_live_trading,
        "confirmation_phrase_required": LIVE_CONFIRMATION_PHRASE,
        **_execution_out(state),
    }


@router.post("/control/live/arm", response_model=dict)
def arm_live(
    body: LiveArmIn,
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(require_owner),
    db: DbSession = Depends(get_db),
) -> dict:
    """Arm live execution for the lifetime of this process.

    Requires an authenticated owner, deployment permission, the exact
    confirmation phrase, and no latched breaker. A restart disarms it;
    there is no configuration that re-arms it.
    """
    try:
        state = context.runtime.arm_live(
            actor=principal.user.email, confirmation=body.confirmation
        )
    except LiveArmingError as exc:
        audit(
            db,
            "control.live_arm_refused",
            user_id=principal.user.id,
            ip_address=request.client.host if request.client else None,
            detail={"reason": str(exc)},
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit(
        db,
        "control.live_armed",
        user_id=principal.user.id,
        ip_address=request.client.host if request.client else None,
        detail={
            "armed_by": state.armed_by,
            "armed_at": state.armed_at.isoformat() if state.armed_at else None,
        },
    )
    return _execution_out(state)


@router.post("/control/live/disarm", response_model=dict)
def disarm_live(
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(require_owner),
    db: DbSession = Depends(get_db),
) -> dict:
    state = context.runtime.disarm_live(actor=principal.user.email)
    audit(
        db,
        "control.live_disarmed",
        user_id=principal.user.id,
        ip_address=request.client.host if request.client else None,
    )
    return _execution_out(state)


@router.post("/control/breaker/reset", response_model=dict)
def reset_breaker(
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(require_owner),
    db: DbSession = Depends(get_db),
) -> dict:
    """Clear a latched safety breaker.

    Refused while anything is still unhealthy, and the reset leaves the
    process disarmed: resuming live trading needs a separate arm with
    the phrase.
    """
    before = context.runtime.ensure_execution()
    try:
        state = context.runtime.reset_breaker(actor=principal.user.email)
    except LiveArmingError as exc:
        audit(
            db,
            "control.breaker_reset_refused",
            user_id=principal.user.id,
            ip_address=request.client.host if request.client else None,
            detail={"reason": str(exc)},
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit(
        db,
        "control.breaker_reset",
        user_id=principal.user.id,
        ip_address=request.client.host if request.client else None,
        detail={"cleared": [str(trip.reason) for trip in before.trips]},
    )
    return _execution_out(state)


@router.post("/control/reconcile", response_model=dict)
def reconcile(
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(require_owner),
) -> dict:
    result = context.runtime.reconcile()
    return {
        "clean": result.clean,
        "blocks_live_trading": result.blocks_live_trading,
        "summary": result.summary(),
        "discrepancies": [
            {"kind": str(d.kind), "client_order_id": d.client_order_id, "detail": d.detail}
            for d in result.discrepancies
        ],
    }
