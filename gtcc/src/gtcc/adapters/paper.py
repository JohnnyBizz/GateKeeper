"""The paper broker — the default venue, and the only one in Phase 1.

It keeps a real ledger: every fill moves cash, adjusts an average entry
price, realises profit and loss on the closing portion, and charges the
instrument's own fees. Equity is derived from that ledger and from
marks supplied by the data adapter, never asserted.

Accounting model, stated plainly because it bounds what the numbers
mean: equity is ``starting cash + realised P&L - fees + unrealised``.
Margin is not simulated, so a futures or perpetual position consumes no
buying power here. That makes this broker useful for measuring strategy
behaviour and useless for measuring margin calls, and it is documented
as a known limitation rather than papered over with a guess at a
venue's margin formula.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable, Sequence

from gtcc.adapters.base import AdapterHealth, BrokerAdapter, Capability
from gtcc.adapters.errors import OrderRejected
from gtcc.domain.enums import (
    AssetClass,
    Market,
    OrderStatus,
    OrderType,
    Side,
    TradingMode,
)
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import OrderBook, Quote, utcnow
from gtcc.domain.money import ZERO, D
from gtcc.domain.orders import Account, Fill, Order, OrderRequest, Position, new_id
from gtcc.execution.paper_engine import FillModel, PaperFillEngine

QuoteSource = Callable[[str], Quote]
BookSource = Callable[[str], OrderBook | None]
InstrumentSource = Callable[[str], InstrumentSpec]


@dataclass
class PaperBroker(BrokerAdapter):
    """A simulated venue with an honest ledger."""

    account_id: str = "paper-1"
    currency: str = "USD"
    starting_cash: Decimal = D("100000")
    quote_source: QuoteSource | None = None
    instrument_source: InstrumentSource | None = None
    book_source: BookSource | None = None
    fill_engine: PaperFillEngine = field(default_factory=PaperFillEngine)
    clock: Callable[[], datetime] = utcnow

    name: str = "paper"
    mode: TradingMode = TradingMode.PAPER
    capabilities: frozenset[Capability] = frozenset(
        {Capability.PAPER_TRADING, Capability.QUOTES}
    )

    _positions: dict[str, Position] = field(default_factory=dict, init=False)
    _orders: dict[str, Order] = field(default_factory=dict, init=False)
    _fills: list[Fill] = field(default_factory=list, init=False)
    _realised_pnl: Decimal = field(default=ZERO, init=False)
    _fees_paid: Decimal = field(default=ZERO, init=False)

    # -- account ---------------------------------------------------------------

    def get_account(self) -> Account:
        equity = self.equity()
        return Account(
            account_id=self.account_id,
            currency=self.currency,
            equity=equity,
            cash=self.starting_cash + self._realised_pnl - self._fees_paid,
            # No margin model, so buying power is simply equity. A real
            # adapter reports what the venue says instead of deriving it.
            buying_power=equity,
            mode=TradingMode.PAPER,
            reconciled_at=self.clock(),
        )

    def get_balance(self) -> Decimal:
        return self.starting_cash + self._realised_pnl - self._fees_paid

    def equity(self) -> Decimal:
        unrealised = ZERO
        for position in self._positions.values():
            if position.is_flat:
                continue
            spec = self._spec(position.symbol)
            marked = self._mark(position)
            if marked is None:
                # Cannot value it. Contributing zero would overstate
                # equity, so the position's own cost basis is held.
                continue
            unrealised += (marked - position.average_entry_price) * position.quantity * (
                spec.contract_size if spec else D(1)
            )
        return self.starting_cash + self._realised_pnl - self._fees_paid + unrealised

    def get_positions(self) -> Sequence[Position]:
        out = []
        for position in self._positions.values():
            if position.is_flat:
                continue
            mark = self._mark(position)
            out.append(position if mark is None else _with_mark(position, mark))
        return tuple(out)

    # -- orders -------------------------------------------------------------------

    def place_order(self, request: OrderRequest, *, quantity: Decimal) -> Order:
        if quantity <= ZERO:
            raise OrderRejected(self.name, "quantity must be positive", request.client_order_id)
        spec = self._spec(request.symbol)
        if spec is None:
            raise OrderRejected(
                self.name, f"no instrument specification for {request.symbol}",
                request.client_order_id,
            )
        if self.quote_source is None:
            raise OrderRejected(
                self.name, "no quote source configured; cannot price an order",
                request.client_order_id,
            )

        now = self.clock()
        order = Order(
            symbol=request.symbol,
            market=request.market,
            side=request.side,
            order_type=request.order_type,
            quantity=quantity,
            status=OrderStatus.ACCEPTED,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            time_in_force=request.time_in_force,
            client_order_id=request.client_order_id,
            broker_order_id=new_id("paper"),
            mode=TradingMode.PAPER,
            strategy=request.strategy,
            created_at=now,
            updated_at=now,
        )

        quote = self.quote_source(request.symbol)
        book = self.book_source(request.symbol) if self.book_source else None
        outcome = self.fill_engine.execute(order, spec, quote, book=book, now=now)

        if outcome.status is OrderStatus.REJECTED:
            order = _replace_status(order, OrderStatus.REJECTED, outcome.reason)
        for fill in outcome.fills:
            order = order.with_fill(fill)
            self._fills.append(fill)
            self._apply_to_position(fill, spec, request)

        self._orders[order.client_order_id] = order
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self._require(order_id)
        if order.status.is_terminal:
            raise OrderRejected(
                self.name, f"order is already {order.status}", order_id
            )
        order = _replace_status(order, OrderStatus.CANCELED, "canceled by request")
        self._orders[order_id] = order
        return order

    def get_order(self, order_id: str) -> Order:
        return self._require(order_id)

    def get_orders(self, *, open_only: bool = False) -> Sequence[Order]:
        orders = tuple(self._orders.values())
        return tuple(o for o in orders if o.status.is_open) if open_only else orders

    def get_fills(self, *, since: datetime | None = None) -> Sequence[Fill]:
        if since is None:
            return tuple(self._fills)
        return tuple(fill for fill in self._fills if fill.timestamp >= since)

    def close_position(self, symbol: str) -> Order:
        position = self._positions.get(symbol)
        if position is None or position.is_flat:
            raise OrderRejected(self.name, f"no open position in {symbol}")
        side = Side.SELL if position.quantity > ZERO else Side.BUY
        request = OrderRequest(
            symbol=symbol,
            market=position.market,
            side=side,
            order_type=OrderType.MARKET,
            strategy="close_position",
        )
        return self.place_order(request, quantity=abs(position.quantity))

    def health(self) -> AdapterHealth:
        if self.quote_source is None:
            return AdapterHealth.down("no quote source configured")
        return AdapterHealth.ok("paper broker ready")

    # -- ledger -----------------------------------------------------------------------

    def _apply_to_position(
        self, fill: Fill, spec: InstrumentSpec, request: OrderRequest
    ) -> None:
        """Fold a fill into the position, realising P&L on any reduction."""
        self._fees_paid += fill.fee
        signed = fill.quantity * (D(1) if fill.side is Side.BUY else D(-1))
        existing = self._positions.get(fill.symbol)

        if existing is None or existing.is_flat:
            self._positions[fill.symbol] = Position(
                symbol=fill.symbol,
                market=request.market,
                asset_class=spec.asset_class,
                quantity=signed,
                average_entry_price=fill.price,
                mark_price=fill.price,
                fees_paid=fill.fee,
                protective_stop=request.protective_stop,
                targets=request.targets,
                strategy=request.strategy,
            )
            return

        old_qty = existing.quantity
        new_qty = old_qty + signed

        if (old_qty > ZERO) == (signed > ZERO):
            # Adding to the position: weighted average entry.
            total_cost = existing.average_entry_price * abs(old_qty) + fill.price * fill.quantity
            average = total_cost / abs(new_qty)
            self._positions[fill.symbol] = _replace_position(
                existing, quantity=new_qty, average_entry_price=average, fill=fill
            )
            return

        # Reducing, closing or flipping.
        closed = min(abs(signed), abs(old_qty))
        direction = D(1) if old_qty > ZERO else D(-1)
        realised = (fill.price - existing.average_entry_price) * closed * direction
        realised *= spec.contract_size
        self._realised_pnl += realised

        if new_qty == ZERO:
            self._positions[fill.symbol] = _replace_position(
                existing,
                quantity=ZERO,
                average_entry_price=existing.average_entry_price,
                fill=fill,
                realised=realised,
            )
            return

        if (new_qty > ZERO) == (old_qty > ZERO):
            self._positions[fill.symbol] = _replace_position(
                existing,
                quantity=new_qty,
                average_entry_price=existing.average_entry_price,
                fill=fill,
                realised=realised,
            )
            return

        # Flipped through flat: the remainder opens at the fill price.
        self._positions[fill.symbol] = _replace_position(
            existing,
            quantity=new_qty,
            average_entry_price=fill.price,
            fill=fill,
            realised=realised,
        )

    def _spec(self, symbol: str) -> InstrumentSpec | None:
        if self.instrument_source is None:
            return None
        try:
            return self.instrument_source(symbol)
        except Exception:
            return None

    def _mark(self, position: Position) -> Decimal | None:
        if self.quote_source is None:
            return None
        try:
            quote = self.quote_source(position.symbol)
        except Exception:
            return None
        return quote.bid if position.quantity > ZERO else quote.ask

    def _require(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderRejected(self.name, "unknown order", order_id)
        return order


def _replace_status(order: Order, status: OrderStatus, reason: str) -> Order:
    from dataclasses import replace

    return replace(order, status=status, reject_reason=reason, updated_at=utcnow())


def _with_mark(position: Position, mark: Decimal) -> Position:
    from dataclasses import replace

    return replace(position, mark_price=mark)


def _replace_position(
    position: Position,
    *,
    quantity: Decimal,
    average_entry_price: Decimal,
    fill: Fill,
    realised: Decimal = ZERO,
) -> Position:
    from dataclasses import replace

    return replace(
        position,
        quantity=quantity,
        average_entry_price=average_entry_price,
        mark_price=fill.price,
        realised_pnl=position.realised_pnl + realised,
        fees_paid=position.fees_paid + fill.fee,
    )
