"""Request and response bodies. Validation happens here, not in routes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from gtcc.domain.enums import Market, OrderType, Side, TimeInForce, TradingMode


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    email: str
    role: str
    last_login_at: datetime | None = None


class HealthOut(BaseModel):
    status: str
    mode: TradingMode
    live_trading: bool
    automatic_execution: bool
    broker_healthy: bool
    data_healthy: bool
    risk_limits_loaded: bool
    risk_limits_are_example: bool
    detail: str = ""


class AccountOut(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    account_id: str
    currency: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    mode: TradingMode
    reconciled_at: datetime | None
    drawdown: Decimal


class PositionOut(BaseModel):
    symbol: str
    market: Market
    quantity: Decimal
    average_entry_price: Decimal
    mark_price: Decimal | None
    unrealised_pnl: Decimal | None
    realised_pnl: Decimal
    protective_stop: Decimal | None
    strategy: str


class CheckOut(BaseModel):
    code: str
    outcome: str
    detail: str
    limit: str | None = None
    observed: str | None = None


class VerdictOut(BaseModel):
    action: str
    approved_quantity: Decimal
    requested_quantity: Decimal | None
    reward_risk: Decimal | None
    projected_risk: Decimal | None
    checks: list[CheckOut]
    reasons: list[str]
    explanation: str


class OrderRequestIn(BaseModel):
    """An order intent. Size is optional: when omitted the risk engine
    derives it from the configured risk budget, which is the preferred
    path — a caller-supplied size can only ever be reduced."""

    symbol: str = Field(min_length=1, max_length=64)
    market: Market
    side: Side
    order_type: OrderType = OrderType.MARKET
    quantity: Decimal | None = Field(default=None, gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    protective_stop: Decimal | None = Field(default=None, gt=0)
    targets: list[Decimal] = Field(default_factory=list, max_length=10)
    time_in_force: TimeInForce = TimeInForce.GTC
    strategy: str = Field(default="manual", max_length=64)

    @field_validator("targets")
    @classmethod
    def _targets_positive(cls, value: list[Decimal]) -> list[Decimal]:
        if any(target <= 0 for target in value):
            raise ValueError("targets must be positive prices")
        return value


class OrderOut(BaseModel):
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: Side
    order_type: OrderType
    status: str
    mode: TradingMode
    strategy: str
    quantity: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    fees_paid: Decimal
    reject_reason: str | None
    created_at: datetime


class SubmissionOut(BaseModel):
    placed: bool
    detail: str
    verdict: VerdictOut
    order: OrderOut | None


class RiskStateOut(BaseModel):
    account_id: str
    day_start_equity: Decimal
    peak_equity: Decimal
    realised_pnl_today: Decimal
    realised_pnl_week: Decimal
    daily_loss_pct: Decimal
    weekly_loss_pct: Decimal
    drawdown_pct: Decimal
    consecutive_losses: int
    trades_today: int
    kill_switch: bool
    trading_paused: bool
    daily_breaker_tripped: bool
    weekly_breaker_tripped: bool
    drawdown_breaker_tripped: bool
    new_trades_blocked: bool
    live_execution_blocked: bool
    breaker_reasons: list[str]
    disabled_symbols: list[str]
    disabled_strategies: list[str]
    disabled_markets: list[str]


class LimitsOut(BaseModel):
    max_risk_per_trade_pct: Decimal
    max_position_notional_pct: Decimal
    max_leverage: Decimal
    min_reward_risk: Decimal
    max_spread_bps: Decimal
    max_daily_loss_pct: Decimal
    max_weekly_loss_pct: Decimal
    max_drawdown_pct: Decimal
    max_consecutive_losses: int
    max_open_positions: int
    is_example: bool


class ToggleIn(BaseModel):
    enabled: bool
    reason: str = Field(default="", max_length=256)


class ModeSwitchIn(BaseModel):
    target: TradingMode
    confirmation: str = ""
