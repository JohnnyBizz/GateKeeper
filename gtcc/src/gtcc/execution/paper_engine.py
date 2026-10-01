"""Paper execution — specification section 19.

The default mode, and the one that decides whether anything measured
here means anything. A paper engine that fills at the mid, instantly,
in full, at any size, produces an equity curve that no broker will
reproduce. Everything this module does is in service of the opposite:
a paper fill should be slightly disappointing, in the same ways a real
one is.

What is modelled:

* **Crossing the spread.** A market buy lifts the ask. It never gets
  the mid and never gets the bid.
* **Slippage.** A base cost in basis points, plus an impact term that
  grows with order size against displayed depth.
* **Depth.** When an order book is supplied the order walks it level by
  level and the average price degrades accordingly.
* **Partial fills.** Size beyond the participation limit does not fill.
* **Commission.** Per the instrument's own fee schedule.
* **Latency.** Fills are stamped after the configured delay, so a
  strategy cannot act on its own fill before it would have heard.
* **Market hours.** An equity order outside the session does not fill.

What is NOT modelled, and is documented rather than faked: queue
position for resting limit orders, hidden liquidity, auction mechanics,
and borrow availability for shorts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from gtcc.domain.enums import AssetClass, OrderStatus, OrderType, Side
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import OrderBook, Quote
from gtcc.domain.money import ONE, ZERO, D, ceil_to_tick, floor_to_tick
from gtcc.domain.pricing import normalise_entry_limit, respects_limit
from gtcc.domain.orders import Fill, Order

#: US regular trading hours, in exchange local time. Only used for
#: instruments whose spec says ``session="RTH"``.
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)


@dataclass(frozen=True, slots=True)
class FillModel:
    """Execution friction. Every field is a cost, never a benefit."""

    #: Unavoidable slippage even on a small order in a liquid book.
    base_slippage_bps: Decimal = D("1.0")
    #: Extra slippage proportional to (order size / displayed top-of-book).
    impact_coefficient: Decimal = D("2.0")
    #: Fraction of displayed size this account may take in one order.
    max_participation: Decimal = D("0.25")
    #: Round-trip delay between sending and hearing back.
    latency_ms: int = 150
    allow_partial_fills: bool = True
    #: Reject rather than fill when the book cannot cover the order at all.
    reject_if_no_liquidity: bool = True

    def __post_init__(self) -> None:
        if self.base_slippage_bps < ZERO:
            raise ValueError("slippage cannot be negative — that would be a rebate")
        if not (ZERO < self.max_participation <= ONE):
            raise ValueError("max_participation must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class FillOutcome:
    """What the simulated venue did with the order."""

    fills: tuple[Fill, ...]
    status: OrderStatus
    reason: str = ""

    @property
    def filled_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.fills), ZERO)

    @property
    def average_price(self) -> Decimal | None:
        total = self.filled_quantity
        if total <= ZERO:
            return None
        return sum((f.price * f.quantity for f in self.fills), ZERO) / total


@dataclass
class PaperFillEngine:
    """Simulates one venue's execution behaviour."""

    model: FillModel = field(default_factory=FillModel)

    def execute(
        self,
        order: Order,
        instrument: InstrumentSpec,
        quote: Quote,
        *,
        book: OrderBook | None = None,
        now: datetime,
    ) -> FillOutcome:
        if order.remaining_quantity <= ZERO:
            return FillOutcome(fills=(), status=order.status, reason="nothing left to fill")

        if not self._session_open(instrument, now):
            return FillOutcome(
                fills=(),
                status=OrderStatus.ACCEPTED,
                reason=(
                    f"{instrument.symbol} is outside its trading session; the order "
                    "rests until the session opens"
                ),
            )

        if quote.is_crossed or quote.bid <= ZERO or quote.ask <= ZERO:
            return FillOutcome(
                fills=(),
                status=OrderStatus.REJECTED,
                reason="quote is not usable (crossed or non-positive)",
            )

        marketable, reference = self._marketability(order, quote)
        if not marketable:
            return FillOutcome(
                fills=(),
                status=OrderStatus.ACCEPTED,
                reason=(
                    f"limit {order.limit_price} is away from the market "
                    f"(bid {quote.bid} / ask {quote.ask}); order rests"
                ),
            )

        quantity, price, note = self._price_and_size(order, instrument, quote, book, reference)

        if quantity <= ZERO:
            if self.model.reject_if_no_liquidity:
                return FillOutcome(
                    fills=(), status=OrderStatus.REJECTED, reason=note or "no liquidity available"
                )
            return FillOutcome(fills=(), status=OrderStatus.ACCEPTED, reason=note)

        # Put the fill on the tick grid in the venue's favour: a buy pays
        # up to the next tick, a sell receives down to the previous one.
        # Rounding to nearest here would hand the strategy a fraction of
        # a tick it would not get from a real venue.
        price = (
            ceil_to_tick(price, instrument.tick_size)
            if order.side is Side.BUY
            else floor_to_tick(price, instrument.tick_size)
        )

        # The limit is applied AFTER rounding, not before. Clamping first
        # and rounding second let a buy limit of 100.00 round up to
        # 100.01 and fill a cent above the price the trader set.
        if order.limit_price is not None:
            limit = normalise_entry_limit(order.limit_price, instrument.tick_size, order.side)
            price = min(price, limit) if order.side is Side.BUY else max(price, limit)
            if not respects_limit(price, limit, order.side):  # pragma: no cover - guard
                raise AssertionError(
                    f"paper fill at {price} violates the {order.side} limit {limit}"
                )
        notional = instrument.notional(price, quantity)
        fee = instrument.fee(notional, maker=order.order_type is OrderType.LIMIT)

        fill = Fill(
            order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=fee,
            timestamp=now + timedelta(milliseconds=self.model.latency_ms),
            liquidity="maker" if order.order_type is OrderType.LIMIT else "taker",
        )
        status = (
            OrderStatus.FILLED
            if quantity >= order.remaining_quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        return FillOutcome(fills=(fill,), status=status, reason=note)

    # -- internals -----------------------------------------------------------

    def _session_open(self, instrument: InstrumentSpec, now: datetime) -> bool:
        if instrument.session != "RTH":
            return True
        if now.weekday() >= 5:
            return False
        return _RTH_OPEN <= now.timetz().replace(tzinfo=None) < _RTH_CLOSE

    def _marketability(self, order: Order, quote: Quote) -> tuple[bool, Decimal]:
        """Can this order trade now, and against which side of the book?"""
        touch = quote.ask if order.side is Side.BUY else quote.bid
        if order.order_type is OrderType.MARKET:
            return True, touch
        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            if order.side is Side.BUY:
                return quote.ask <= order.limit_price, min(quote.ask, order.limit_price)
            return quote.bid >= order.limit_price, max(quote.bid, order.limit_price)
        if order.order_type in (OrderType.STOP, OrderType.STOP_LOSS):
            assert order.stop_price is not None
            triggered = (
                quote.ask >= order.stop_price
                if order.side is Side.BUY
                else quote.bid <= order.stop_price
            )
            # A triggered stop becomes a market order and crosses.
            return triggered, touch
        if order.order_type is OrderType.STOP_LIMIT:
            assert order.stop_price is not None and order.limit_price is not None
            triggered = (
                quote.ask >= order.stop_price
                if order.side is Side.BUY
                else quote.bid <= order.stop_price
            )
            if not triggered:
                return False, touch
            if order.side is Side.BUY:
                return quote.ask <= order.limit_price, min(quote.ask, order.limit_price)
            return quote.bid >= order.limit_price, max(quote.bid, order.limit_price)
        # TAKE_PROFIT and TRAILING_STOP are managed by the position
        # manager, not filled directly here.
        return False, touch

    def _price_and_size(
        self,
        order: Order,
        instrument: InstrumentSpec,
        quote: Quote,
        book: OrderBook | None,
        reference: Decimal,
    ) -> tuple[Decimal, Decimal, str]:
        wanted = order.remaining_quantity

        if book is not None:
            levels = book.asks if order.side is Side.BUY else book.bids
            if levels:
                return self._walk_book(order, instrument, levels, wanted)

        displayed = quote.ask_size if order.side is Side.BUY else quote.bid_size
        available = wanted
        note = ""
        if displayed > ZERO:
            cap = displayed * self.model.max_participation
            if cap < wanted:
                if not self.model.allow_partial_fills:
                    return ZERO, reference, (
                        f"only {cap} of {wanted} available at the touch and partial "
                        "fills are disabled"
                    )
                available = instrument.round_quantity(cap)
                note = f"partial fill: {available} of {wanted} available at the touch"

        slippage_bps = self._slippage_bps(available, displayed)
        price = self._apply_slippage(reference, order.side, slippage_bps)
        return available, price, note

    def _walk_book(
        self,
        order: Order,
        instrument: InstrumentSpec,
        levels: tuple,
        wanted: Decimal,
    ) -> tuple[Decimal, Decimal, str]:
        """Consume depth level by level; the average price is what you get."""
        remaining = wanted
        cost = ZERO
        taken = ZERO
        for level in levels:
            takeable = min(remaining, level.size * self.model.max_participation)
            if takeable <= ZERO:
                continue
            cost += takeable * level.price
            taken += takeable
            remaining -= takeable
            if remaining <= ZERO:
                break
        if taken <= ZERO:
            return ZERO, ZERO, "the visible book has no size on that side"
        taken = instrument.round_quantity(taken)
        if taken <= ZERO:
            return ZERO, ZERO, "available depth is below one lot"
        average = cost / taken
        note = "" if taken >= wanted else f"partial fill: {taken} of {wanted} from visible depth"
        return taken, average, note

    def _slippage_bps(self, quantity: Decimal, displayed: Decimal) -> Decimal:
        if displayed <= ZERO:
            # No depth information. Charge the base cost; do not pretend
            # the absence of data means the absence of impact.
            return self.model.base_slippage_bps
        participation = quantity / displayed
        return self.model.base_slippage_bps + self.model.impact_coefficient * participation * D(10)

    def _apply_slippage(self, price: Decimal, side: Side, slippage_bps: Decimal) -> Decimal:
        adjustment = price * slippage_bps / D(10000)
        return price + adjustment if side is Side.BUY else price - adjustment
