"""The broker and market-data interfaces — specification section 34.

Everything above this layer speaks only in platform types. A strategy
cannot tell whether its quotes come from an exchange, a broker or a
replayed CSV, and nothing in the risk engine or the order manager
imports a vendor SDK. That is what makes a venue swappable.

Capability negotiation is explicit. An adapter declares what it can do;
asking for anything else raises
:class:`~gtcc.adapters.errors.FeatureUnavailable` rather than returning
an empty structure that reads like "no imbalance" instead of "no data".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Callable, Iterable, Sequence

from gtcc.adapters.errors import FeatureUnavailable
from gtcc.domain.enums import Timeframe, TradingMode
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, OrderBook, Quote, Trade, utcnow
from gtcc.domain.orders import Account, Fill, Order, OrderRequest, Position


class Capability(StrEnum):
    QUOTES = "QUOTES"
    BARS = "BARS"
    ORDER_BOOK = "ORDER_BOOK"
    TRADES = "TRADES"
    STREAMING = "STREAMING"
    HISTORICAL = "HISTORICAL"
    FUNDING = "FUNDING"
    OPEN_INTEREST = "OPEN_INTEREST"
    LIQUIDATIONS = "LIQUIDATIONS"
    OPTION_CHAIN = "OPTION_CHAIN"
    EXTENDED_HOURS = "EXTENDED_HOURS"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    MARKET_CALENDAR = "MARKET_CALENDAR"
    PAPER_TRADING = "PAPER_TRADING"
    LIVE_TRADING = "LIVE_TRADING"
    TRAILING_STOP = "TRAILING_STOP"
    BRACKET_ORDERS = "BRACKET_ORDERS"


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    """Whether this connection may be traded on right now."""

    healthy: bool
    checked_at: datetime
    detail: str = ""
    latency_ms: float | None = None
    #: Last time the venue confirmed our account state.
    last_reconciled_at: datetime | None = None

    @classmethod
    def ok(cls, detail: str = "", latency_ms: float | None = None) -> "AdapterHealth":
        return cls(healthy=True, checked_at=utcnow(), detail=detail, latency_ms=latency_ms)

    @classmethod
    def down(cls, detail: str) -> "AdapterHealth":
        return cls(healthy=False, checked_at=utcnow(), detail=detail)


class MarketDataAdapter(ABC):
    """Read-only access to a venue's market data."""

    name: str = "unnamed"
    capabilities: frozenset[Capability] = frozenset()

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise FeatureUnavailable(self.name, capability)

    @abstractmethod
    def get_instrument(self, symbol: str) -> InstrumentSpec:
        """Venue-reported contract specification.

        An adapter that cannot supply this must not offer the symbol:
        sizing without a real tick size and lot step is guesswork.
        """

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        limit: int = 500,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]: ...

    @abstractmethod
    def health(self) -> AdapterHealth: ...

    def list_symbols(self) -> Sequence[str]:
        raise FeatureUnavailable(self.name, "symbol listing")

    def get_order_book(self, symbol: str, *, depth: int = 20) -> OrderBook:
        raise FeatureUnavailable(self.name, Capability.ORDER_BOOK)

    def get_trades(self, symbol: str, *, limit: int = 200) -> Sequence[Trade]:
        raise FeatureUnavailable(self.name, Capability.TRADES)

    def get_funding_rate(self, symbol: str) -> Decimal:
        raise FeatureUnavailable(self.name, Capability.FUNDING)

    def get_open_interest(self, symbol: str) -> Decimal:
        raise FeatureUnavailable(self.name, Capability.OPEN_INTEREST)

    def stream_market_data(
        self, symbols: Iterable[str], handler: Callable[[object], None]
    ) -> None:
        raise FeatureUnavailable(self.name, Capability.STREAMING)


class BrokerAdapter(ABC):
    """Account and order access at one venue.

    Implementations never decide whether a trade is wise. By the time an
    order reaches here the risk engine has already approved it, and the
    adapter's only remaining judgement is whether the venue accepted it.
    """

    name: str = "unnamed"
    capabilities: frozenset[Capability] = frozenset()
    mode: TradingMode = TradingMode.PAPER

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_balance(self) -> Decimal: ...

    @abstractmethod
    def get_positions(self) -> Sequence[Position]: ...

    @abstractmethod
    def place_order(self, request: OrderRequest, *, quantity: Decimal) -> Order:
        """Submit an order the risk engine has approved.

        *quantity* is the engine's approved size, which may be smaller
        than the request's. It is passed separately so an adapter can
        never accidentally use the unapproved number.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> Order: ...

    @abstractmethod
    def get_order(self, order_id: str) -> Order: ...

    @abstractmethod
    def get_orders(self, *, open_only: bool = False) -> Sequence[Order]: ...

    @abstractmethod
    def get_fills(self, *, since: datetime | None = None) -> Sequence[Fill]: ...

    @abstractmethod
    def close_position(self, symbol: str) -> Order: ...

    @abstractmethod
    def health(self) -> AdapterHealth: ...


@dataclass
class AdapterRegistry:
    """The adapters this deployment has configured."""

    market_data: dict[str, MarketDataAdapter] = field(default_factory=dict)
    brokers: dict[str, BrokerAdapter] = field(default_factory=dict)

    def register_data(self, adapter: MarketDataAdapter) -> None:
        self.market_data[adapter.name] = adapter

    def register_broker(self, adapter: BrokerAdapter) -> None:
        self.brokers[adapter.name] = adapter

    def data(self, name: str) -> MarketDataAdapter:
        try:
            return self.market_data[name]
        except KeyError:
            raise KeyError(
                f"no market data adapter named {name!r}; configured: "
                f"{sorted(self.market_data)}"
            ) from None

    def broker(self, name: str) -> BrokerAdapter:
        try:
            return self.brokers[name]
        except KeyError:
            raise KeyError(
                f"no broker adapter named {name!r}; configured: {sorted(self.brokers)}"
            ) from None

    def live_brokers(self) -> list[str]:
        return [
            name for name, adapter in self.brokers.items() if adapter.mode is TradingMode.LIVE
        ]
