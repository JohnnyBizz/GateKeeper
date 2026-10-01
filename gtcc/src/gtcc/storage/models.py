"""Database schema — specification section 32.

Money is stored as ``Numeric`` and never as a float. Timestamps are
timezone-aware and stored in UTC. Every table that records a decision
keeps enough context to reconstruct it later, which is the whole point
of section 31: a trade that cannot be explained six months on may as
well not have been recorded.

Phase 1 creates every table below. The ones the later phases fill —
candles, features, agent analyses, backtests — exist now so the
migration history stays linear and so a Phase 2 writer has somewhere to
put its rows without a schema change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

#: 28 significant digits with 10 after the point: enough for satoshis
#: and for an index future, without float drift.
MONEY = Numeric(28, 10)


class UtcDateTime(TypeDecorator):
    """A timestamp that is always timezone-aware UTC on the way out.

    PostgreSQL round-trips ``timestamptz`` with its offset intact.
    SQLite has no timestamp type at all and hands back a naive value,
    so the same comparison that works in production raises
    "can't compare offset-naive and offset-aware datetimes" in
    development. Rather than remember that at every call site, the
    conversion lives here: naive values are rejected going in and
    stamped UTC coming out.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetimes are not accepted; attach a timezone before storing"
            )
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# -- identity and audit --------------------------------------------------------


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    #: Argon2id. Never a reversible encoding, never logged.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="owner", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime())

    sessions: Mapped[list["Session"]] = relationship(back_populates="user")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    user: Mapped[User] = relationship(back_populates="sessions")


class AuditLog(Base):
    """Who did what. Append-only by convention; never updated in place."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(32), index=True)


# -- accounts and venues ---------------------------------------------------------


class TradingAccount(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    broker: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="PAPER")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    starting_equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("broker", "external_id", name="uq_account_broker_ext"),)


class BrokerConnection(Base, TimestampMixin):
    """A configured venue connection.

    No credential is stored here. The row names the environment variable
    or secret-store key that holds it, so the database can be dumped
    without leaking anything — specification section 29.
    """

    __tablename__ = "broker_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="PAPER")
    #: Name of the secret, not the secret.
    credential_ref: Mapped[str | None] = mapped_column(String(128))
    #: Recorded from the venue's own key-permission endpoint where one
    #: exists. A key that can withdraw should not be used for trading.
    withdrawal_permission: Mapped[bool | None] = mapped_column(Boolean)
    is_healthy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_health_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    last_health_detail: Mapped[str | None] = mapped_column(String(512))


class Instrument(Base, TimestampMixin):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(24), nullable=False)
    base_currency: Mapped[str | None] = mapped_column(String(16))
    quote_currency: Mapped[str] = mapped_column(String(16), nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    lot_step: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    min_qty: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    contract_size: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=1)
    tick_value: Mapped[Decimal | None] = mapped_column(MONEY)
    pip_size: Mapped[Decimal | None] = mapped_column(MONEY)
    max_leverage: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=1)
    maker_fee_bps: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    taker_fee_bps: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    session: Mapped[str] = mapped_column(String(16), default="24x7", nullable=False)

    __table_args__ = (UniqueConstraint("venue", "symbol", name="uq_instrument_venue_symbol"),)


# -- orders, fills, positions -------------------------------------------------------


class OrderRow(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(128), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    stop_price: Mapped[Decimal | None] = mapped_column(MONEY)
    average_fill_price: Mapped[Decimal | None] = mapped_column(MONEY)
    fees_paid: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    reject_reason: Mapped[str | None] = mapped_column(String(512))
    #: The full risk verdict, every check, as returned by the engine.
    risk_verdict: Mapped[dict] = mapped_column(JSON, default=dict)


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_fill_id: Mapped[str] = mapped_column(String(64), nullable=False)
    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.client_order_id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    liquidity: Mapped[str] = mapped_column(String(8), default="taker", nullable=False)
    filled_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

    __table_args__ = (UniqueConstraint("venue_fill_id", name="uq_fill_venue_id"),)


class PositionRow(Base, TimestampMixin):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    average_entry_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    mark_price: Mapped[Decimal | None] = mapped_column(MONEY)
    realised_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    fees_paid: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    protective_stop: Mapped[Decimal | None] = mapped_column(MONEY)
    strategy: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


# -- the journal -----------------------------------------------------------------------


class TradeJournalEntry(Base, TimestampMixin):
    """Specification section 24 — every considered setup, not only the taken ones.

    A rejected setup is a row here with ``outcome='REJECTED'`` and the
    risk verdict attached. Keeping them is what makes it possible to ask
    later whether the rejections were right.
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    considered_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    timeframe: Mapped[str | None] = mapped_column(String(8))
    mode: Mapped[str] = mapped_column(String(16), nullable=False)

    #: TAKEN, REJECTED_BY_RISK, REJECTED_BY_AI, EXPIRED, CANCELLED
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    planned_entry: Mapped[Decimal | None] = mapped_column(MONEY)
    planned_stop: Mapped[Decimal | None] = mapped_column(MONEY)
    planned_targets: Mapped[list] = mapped_column(JSON, default=list)
    planned_size: Mapped[Decimal | None] = mapped_column(MONEY)
    planned_risk: Mapped[Decimal | None] = mapped_column(MONEY)
    reward_risk: Mapped[Decimal | None] = mapped_column(MONEY)

    actual_entry: Mapped[Decimal | None] = mapped_column(MONEY)
    actual_exit: Mapped[Decimal | None] = mapped_column(MONEY)
    actual_size: Mapped[Decimal | None] = mapped_column(MONEY)
    fees: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    slippage: Mapped[Decimal | None] = mapped_column(MONEY)
    realised_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    r_multiple: Mapped[Decimal | None] = mapped_column(MONEY)
    #: Maximum favourable and adverse excursion while the trade was open.
    mfe: Mapped[Decimal | None] = mapped_column(MONEY)
    mae: Mapped[Decimal | None] = mapped_column(MONEY)
    exit_reason: Mapped[str | None] = mapped_column(String(64))

    regime: Mapped[str | None] = mapped_column(String(24), index=True)
    session: Mapped[str | None] = mapped_column(String(16), index=True)
    data_quality: Mapped[str | None] = mapped_column(String(16))

    #: Full decision context, so the row explains itself without joins.
    agent_outputs: Mapped[dict] = mapped_column(JSON, default=dict)
    ai_decision: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_verdict: Mapped[dict] = mapped_column(JSON, default=dict)
    indicators: Mapped[dict] = mapped_column(JSON, default=dict)
    market_structure: Mapped[dict] = mapped_column(JSON, default=dict)
    news_context: Mapped[list] = mapped_column(JSON, default=list)
    macro_context: Mapped[list] = mapped_column(JSON, default=list)
    order_ids: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    opened_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    __table_args__ = (Index("ix_trades_strategy_outcome", "strategy", "outcome"),)


class RiskEvent(Base):
    """Breaker trips, kill-switch flips, rejections worth alerting on."""

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, nullable=False, index=True
    )
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    code: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    detail: Mapped[str] = mapped_column(String(1024), nullable=False)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class RiskStateRow(Base, TimestampMixin):
    """Persisted so a restart cannot clear a tripped breaker."""

    __tablename__ = "risk_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id"), unique=True, nullable=False
    )
    day_start_equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    week_start_equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    peak_equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    realised_pnl_today: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    realised_pnl_week: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trades_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_day: Mapped[str] = mapped_column(String(10), nullable=False)
    week_start: Mapped[str] = mapped_column(String(10), nullable=False)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trading_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    daily_breaker_tripped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    weekly_breaker_tripped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    drawdown_breaker_tripped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    disabled_symbols: Mapped[list] = mapped_column(JSON, default=list)
    disabled_strategies: Mapped[list] = mapped_column(JSON, default=list)
    disabled_markets: Mapped[list] = mapped_column(JSON, default=list)


# -- research tables, written from Phase 2 onward -------------------------------------


class Candle(Base):
    __tablename__ = "candles"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    open: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    high: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    low: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    close: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    volume: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("venue", "symbol", "timeframe", "opened_at", name="uq_candle"),
        Index("ix_candles_lookup", "symbol", "timeframe", "opened_at"),
    )


class Strategy(Base, TimestampMixin):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    allowed_regimes: Mapped[list] = mapped_column(JSON, default=list)
    #: Nothing may be enabled for paper or live until it has passed
    #: out-of-sample testing — specification section 25.
    validation_status: Mapped[str] = mapped_column(
        String(24), default="UNTESTED", nullable=False
    )


class AgentAnalysis(Base):
    __tablename__ = "agent_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    produced_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, nullable=False, index=True
    )
    trade_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    conclusion: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(MONEY)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    data_quality: Mapped[str | None] = mapped_column(String(16))


class AIDecisionRow(Base):
    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    produced_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, nullable=False, index=True
    )
    trade_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rejection_reasons: Mapped[list] = mapped_column(JSON, default=list)
    decision: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[Decimal | None] = mapped_column(MONEY)
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_response: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class NewsItem(Base):
    """Every row carries its source and timestamp. Nothing is summarised
    into the database without a link back to what was summarised."""

    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    published_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), nullable=False, index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, nullable=False
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str | None] = mapped_column(String(1024))
    headline: Mapped[str] = mapped_column(String(512), nullable=False)
    symbols: Mapped[list] = mapped_column(JSON, default=list)
    importance: Mapped[str | None] = mapped_column(String(16))
    summary: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("source", "url", name="uq_news_source_url"),)


class EconomicEventRow(Base):
    __tablename__ = "economic_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(
        UtcDateTime(), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    impact: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    currencies: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    actual: Mapped[str | None] = mapped_column(String(64))
    forecast: Mapped[str | None] = mapped_column(String(64))
    previous: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_event_source_id"),)


class BacktestRun(Base, TimestampMixin):
    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbols: Mapped[list] = mapped_column(JSON, default=list)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    period_start: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    period_end: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    #: IN_SAMPLE, VALIDATION, OUT_OF_SAMPLE, WALK_FORWARD
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_model: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Hash of code and data, so a result can be tied to what produced it.
    fingerprint: Mapped[str | None] = mapped_column(String(64))


class PerformanceSnapshot(Base):
    __tablename__ = "performance_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_value: Mapped[str | None] = mapped_column(String(64))
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
