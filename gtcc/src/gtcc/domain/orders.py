"""Orders, fills, positions and accounts.

These are the platform's own representations. Adapters translate to and
from whatever shape their venue uses, so nothing above the adapter layer
ever sees a broker-specific payload.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from gtcc.domain.enums import (
    AssetClass,
    Market,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    TradingMode,
)
from gtcc.domain.market_data import utcnow
from gtcc.domain.money import ZERO, D


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _require_finite(owner: object, **values: Decimal | None) -> None:
    """Refuse NaN and the infinities at the domain boundary.

    NaN propagates through arithmetic and then raises at whichever
    comparison reaches it first, far from the feed that produced it.
    Infinity is worse: it compares and multiplies without complaint, so
    an infinite price passes every range check downstream. Catching
    both where the object is built turns a bad feed into a loud error
    at the edge.
    """
    for name, value in values.items():
        if value is None:
            continue
        if not isinstance(value, Decimal):
            raise TypeError(
                f"{type(owner).__name__}.{name} must be a Decimal, got {type(value).__name__}"
            )
        if not value.is_finite():
            raise ValueError(f"{type(owner).__name__}.{name} is not a finite number: {value}")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """What a strategy or an operator wants to do.

    This is an intent, not an order. It has no broker id and no status
    because nothing has agreed to it yet. The risk engine consumes one of
    these and may approve it, shrink it, or refuse it.
    """

    symbol: str
    market: Market
    side: Side
    order_type: OrderType
    quantity: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    take_profit: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    strategy: str = "manual"
    #: The intended protective stop for the resulting position. Distinct
    #: from stop_price, which is the trigger of a stop entry order.
    protective_stop: Decimal | None = None
    targets: tuple[Decimal, ...] = ()
    client_order_id: str = field(default_factory=lambda: new_id("req"))
    created_at: datetime = field(default_factory=utcnow)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_finite(
            self, quantity=self.quantity, limit_price=self.limit_price,
            stop_price=self.stop_price, take_profit=self.take_profit,
            protective_stop=self.protective_stop,
        )
        for index, target in enumerate(self.targets):
            _require_finite(self, **{f"targets[{index}]": target})

    @property
    def reference_price(self) -> Decimal | None:
        """The price the trade is planned around, when one is known."""
        return self.limit_price or self.stop_price


@dataclass(frozen=True, slots=True)
class Fill:
    """A confirmed execution. Only a broker creates these."""

    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    timestamp: datetime
    liquidity: str = "taker"
    venue_fill_id: str = field(default_factory=lambda: new_id("fill"))

    def __post_init__(self) -> None:
        _require_finite(self, quantity=self.quantity, price=self.price, fee=self.fee)
        if self.quantity <= ZERO:
            raise ValueError(f"a fill of {self.quantity} is not an execution")
        if self.price <= ZERO:
            raise ValueError(f"a fill at {self.price} is not a price")

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class Order:
    """An order and everything that has happened to it."""

    symbol: str
    market: Market
    side: Side
    order_type: OrderType
    quantity: Decimal
    status: OrderStatus = OrderStatus.REQUESTED
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    filled_quantity: Decimal = ZERO
    average_fill_price: Decimal | None = None
    fees_paid: Decimal = ZERO
    fills: tuple[Fill, ...] = ()
    #: Our id, generated before submission so a reply can always be matched.
    client_order_id: str = field(default_factory=lambda: new_id("ord"))
    #: The venue's id, once the venue has acknowledged the order.
    broker_order_id: str | None = None
    mode: TradingMode = TradingMode.PAPER
    strategy: str = "manual"
    reject_reason: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        _require_finite(
            self, quantity=self.quantity, limit_price=self.limit_price,
            stop_price=self.stop_price, filled_quantity=self.filled_quantity,
            average_fill_price=self.average_fill_price, fees_paid=self.fees_paid,
        )

    @property
    def remaining_quantity(self) -> Decimal:
        return max(ZERO, self.quantity - self.filled_quantity)

    @property
    def is_complete(self) -> bool:
        return self.status.is_terminal

    def with_fill(self, fill: Fill) -> "Order":
        """Return a copy that includes *fill*.

        The resulting status is derived from the filled quantity, never
        asserted by the caller.
        """
        filled = self.filled_quantity + fill.quantity
        if filled > self.quantity:
            raise ValueError(
                f"{self.client_order_id}: fill of {fill.quantity} would take filled "
                f"quantity to {filled}, above the ordered {self.quantity}"
            )
        prior_notional = (self.average_fill_price or ZERO) * self.filled_quantity
        average = (prior_notional + fill.price * fill.quantity) / filled if filled > ZERO else None
        status = OrderStatus.FILLED if filled >= self.quantity else OrderStatus.PARTIALLY_FILLED
        return replace(
            self,
            filled_quantity=filled,
            average_fill_price=average,
            fees_paid=self.fees_paid + fill.fee,
            fills=self.fills + (fill,),
            status=status,
            updated_at=fill.timestamp,
        )


@dataclass(frozen=True, slots=True)
class Position:
    """A net position in one symbol.

    *quantity* is signed: positive is long, negative is short.
    """

    symbol: str
    market: Market
    asset_class: AssetClass
    quantity: Decimal
    average_entry_price: Decimal
    mark_price: Decimal | None = None
    realised_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO
    protective_stop: Decimal | None = None
    targets: tuple[Decimal, ...] = ()
    opened_at: datetime = field(default_factory=utcnow)
    strategy: str = "manual"

    def __post_init__(self) -> None:
        _require_finite(
            self, quantity=self.quantity,
            average_entry_price=self.average_entry_price,
            mark_price=self.mark_price, realised_pnl=self.realised_pnl,
            fees_paid=self.fees_paid, protective_stop=self.protective_stop,
        )

    @property
    def is_flat(self) -> bool:
        return self.quantity == ZERO

    @property
    def side(self) -> Side | None:
        if self.quantity > ZERO:
            return Side.BUY
        if self.quantity < ZERO:
            return Side.SELL
        return None

    def unrealised_pnl(self, contract_size: Decimal = D(1)) -> Decimal | None:
        """None when there is no mark price. Never guess one."""
        if self.mark_price is None or self.is_flat:
            return None
        return (self.mark_price - self.average_entry_price) * self.quantity * contract_size

    def notional(self, contract_size: Decimal = D(1)) -> Decimal | None:
        if self.mark_price is None:
            return None
        return abs(self.quantity) * self.mark_price * contract_size


@dataclass(frozen=True, slots=True)
class Account:
    """A broker account snapshot, as reported by the broker.

    *reconciled_at* is when we last matched this against the venue. The
    risk engine refuses to trade on a snapshot it cannot date.
    """

    account_id: str
    currency: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    margin_used: Decimal = ZERO
    mode: TradingMode = TradingMode.PAPER
    reconciled_at: datetime | None = None
    #: High-water mark of equity, for drawdown. Maintained by the risk store.
    peak_equity: Decimal | None = None

    def __post_init__(self) -> None:
        _require_finite(
            self, equity=self.equity, cash=self.cash,
            buying_power=self.buying_power, margin_used=self.margin_used,
            peak_equity=self.peak_equity,
        )

    @property
    def drawdown(self) -> Decimal:
        """Fraction below the high-water mark, 0 when at or above it."""
        peak = self.peak_equity or self.equity
        if peak <= ZERO:
            return ZERO
        return max(ZERO, (peak - self.equity) / peak)
