"""Request and response bodies. Validation happens here, not in routes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from gtcc.domain.enums import Market, OrderType, Side, Timeframe, TimeInForce, TradingMode


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
    #: Deployment permission: this server may OFFER live mode.
    deployment_allows_live: bool
    #: Runtime arming: a person armed live in THIS process. Always false
    #: immediately after a restart.
    live_armed: bool
    execution_tripped: bool
    trip_reasons: list[str] = Field(default_factory=list)
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
    reduced_to: Decimal | None = None


class VerdictOut(BaseModel):
    action: str
    approved_quantity: Decimal
    requested_quantity: Decimal | None
    reward_risk: Decimal | None
    #: Loss at the protective stop for the quantity actually approved. The
    #: engine also knows what the risk budget asked for before any ceiling
    #: cut the size, but reporting that number would overstate the loss on
    #: every capped trade, so it is not exposed here.
    risk_at_stop: Decimal | None
    #: Ceilings that reduced the size, so the caller can say which limit bound.
    binding_limits: list[str] = []
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
    """Switch between the non-live modes. LIVE is refused by the route."""

    target: TradingMode


class LiveArmIn(BaseModel):
    """Arm live execution.

    The phrase is supplied in the request by a person. It is never read
    from configuration, which is the whole point: an environment
    variable cannot stand in for somebody deciding.
    """

    confirmation: str = Field(min_length=1, max_length=128)


class ScanRequestIn(BaseModel):
    """What to scan. The symbol list is explicit, never "everything"."""

    symbols: list[str] = Field(min_length=1, max_length=200)
    timeframe: Timeframe = Timeframe.M15
    #: Ceiling on venue requests for the whole scan. A scan that runs out
    #: reports the symbols it never reached rather than omitting them.
    max_requests: int | None = Field(default=None, ge=1, le=5000)
    min_history: int = Field(default=60, ge=1, le=5000)
    sort_by: str = "SIGNAL"


class ScanProposalOut(BaseModel):
    strategy: str
    decision: str
    rationale: str
    entry: Decimal | None = None
    stop: Decimal | None = None
    targets: list[Decimal] = []
    conviction: int = 0


class ScanRowOut(BaseModel):
    symbol: str
    status: str
    detail: str = ""
    market: str | None = None
    price: Decimal | None = None
    change_pct: Decimal | None = None
    atr: Decimal | None = None
    atr_pct: Decimal | None = None
    relative_volume: Decimal | None = None
    spread_bps: Decimal | None = None
    trend: str | None = None
    regime: str | None = None
    structure_summary: str = ""
    data_quality: str | None = None
    bars_seen: int = 0
    signal: ScanProposalOut | None = None
    #: Why the strategies that proposed nothing proposed nothing. Present
    #: so that "no setup" and "nothing was eligible" stay distinguishable.
    silent_because: list[str] = []


class ScanResultOut(BaseModel):
    """A scan's rows plus an account of what was not looked at.

    `summary` and `not_analysed` exist so a caller cannot read this as
    "these are the only symbols with anything happening" when in fact the
    scan was cut short or several venues failed.
    """

    summary: str
    requested: int
    requests_made: int
    request_budget: int | None = None
    truncated: bool
    started_at: datetime
    finished_at: datetime
    rows: list[ScanRowOut]
    not_analysed: list[ScanRowOut]
