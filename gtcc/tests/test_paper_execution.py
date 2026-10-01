"""Paper fills, the order state machine, and the ledger behind them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gtcc.domain.enums import Market, OrderStatus, OrderType, Side
from gtcc.domain.market_data import BookLevel, OrderBook, Quote
from gtcc.domain.money import D
from gtcc.domain.orders import Fill, Order, OrderRequest
from gtcc.execution.oms import (
    DiscrepancyKind,
    IllegalTransition,
    OrderManager,
    RiskNotApproved,
)
from gtcc.execution.paper_engine import FillModel, PaperFillEngine
from gtcc.risk.engine import RiskAction, RiskVerdict


def _order(**overrides) -> Order:
    base = dict(
        symbol="BTCUSDT",
        market=Market.CRYPTO,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=D("1"),
        status=OrderStatus.ACCEPTED,
    )
    base.update(overrides)
    return Order(**base)


class TestFillsAreNotFree:
    """A paper fill that flatters the strategy is worse than none."""

    def test_a_market_buy_lifts_the_ask_and_pays_slippage(self, crypto_spec, quote, now):
        book_quote = Quote(
            symbol="BTCUSDT", timestamp=now, bid=D("60000"), ask=D("60006"),
            bid_size=D("10"), ask_size=D("10"), received_at=now,
        )
        engine = PaperFillEngine(FillModel(base_slippage_bps=D("2")))

        outcome = engine.execute(_order(), crypto_spec, book_quote, now=now)

        assert outcome.status is OrderStatus.FILLED
        assert outcome.fills[0].price > book_quote.ask
        assert outcome.fills[0].price > book_quote.mid

    def test_a_market_sell_hits_the_bid(self, crypto_spec, now):
        quote = Quote(
            symbol="BTCUSDT", timestamp=now, bid=D("60000"), ask=D("60006"),
            bid_size=D("10"), ask_size=D("10"), received_at=now,
        )
        outcome = PaperFillEngine().execute(
            _order(side=Side.SELL), crypto_spec, quote, now=now
        )

        assert outcome.fills[0].price < quote.bid

    def test_commission_is_charged(self, crypto_spec, now):
        quote = Quote(
            symbol="BTCUSDT", timestamp=now, bid=D("60000"), ask=D("60006"),
            bid_size=D("10"), ask_size=D("10"), received_at=now,
        )
        outcome = PaperFillEngine().execute(_order(), crypto_spec, quote, now=now)

        assert outcome.fills[0].fee > 0

    def test_the_fill_is_stamped_after_the_latency(self, crypto_spec, now):
        quote = Quote(
            symbol="BTCUSDT", timestamp=now, bid=D("60000"), ask=D("60006"),
            bid_size=D("10"), ask_size=D("10"), received_at=now,
        )
        engine = PaperFillEngine(FillModel(latency_ms=250))

        outcome = engine.execute(_order(), crypto_spec, quote, now=now)

        assert outcome.fills[0].timestamp == now + timedelta(milliseconds=250)

    def test_size_beyond_the_participation_limit_fills_partially(self, crypto_spec, now):
        thin = Quote(
            symbol="BTCUSDT", timestamp=now, bid=D("60000"), ask=D("60006"),
            bid_size=D("1"), ask_size=D("1"), received_at=now,
        )
        engine = PaperFillEngine(FillModel(max_participation=D("0.25")))

        outcome = engine.execute(_order(quantity=D("5")), crypto_spec, thin, now=now)

        assert outcome.status is OrderStatus.PARTIALLY_FILLED
        assert outcome.filled_quantity == D("0.25")
        assert "partial" in outcome.reason

    def test_walking_the_book_degrades_the_average_price(self, crypto_spec, now):
        quote = Quote(
            symbol="BTCUSDT", timestamp=now, bid=D("60000"), ask=D("60006"),
            bid_size=D("100"), ask_size=D("100"), received_at=now,
        )
        book = OrderBook(
            symbol="BTCUSDT",
            timestamp=now,
            bids=(BookLevel(price=D("60000"), size=D("40")),),
            asks=(
                BookLevel(price=D("60006"), size=D("4")),
                BookLevel(price=D("60020"), size=D("4")),
                BookLevel(price=D("60080"), size=D("40")),
            ),
        )
        outcome = PaperFillEngine(FillModel(max_participation=D("1"))).execute(
            _order(quantity=D("10")), crypto_spec, quote, book=book, now=now
        )

        assert outcome.filled_quantity == D("10")
        assert outcome.average_price > D("60006")

    def test_a_limit_order_away_from_the_market_rests_rather_than_filling(
        self, crypto_spec, now
    ):
        quote = Quote(
            symbol="BTCUSDT", timestamp=now, bid=D("60000"), ask=D("60006"),
            bid_size=D("10"), ask_size=D("10"), received_at=now,
        )
        outcome = PaperFillEngine().execute(
            _order(order_type=OrderType.LIMIT, limit_price=D("59000")),
            crypto_spec, quote, now=now,
        )

        assert outcome.fills == ()
        assert outcome.status is OrderStatus.ACCEPTED
        assert "rests" in outcome.reason

    def test_a_limit_order_never_fills_worse_than_its_limit(self, crypto_spec, now):
        quote = Quote(
            symbol="BTCUSDT", timestamp=now, bid=D("60000"), ask=D("60006"),
            bid_size=D("10"), ask_size=D("10"), received_at=now,
        )
        outcome = PaperFillEngine(FillModel(base_slippage_bps=D("50"))).execute(
            _order(order_type=OrderType.LIMIT, limit_price=D("60006")),
            crypto_spec, quote, now=now,
        )

        assert outcome.fills[0].price <= D("60006")

    def test_a_crossed_quote_is_refused(self, crypto_spec, now):
        broken = Quote(
            symbol="BTCUSDT", timestamp=now, bid=D("60010"), ask=D("60000"), received_at=now
        )
        outcome = PaperFillEngine().execute(_order(), crypto_spec, broken, now=now)

        assert outcome.status is OrderStatus.REJECTED

    def test_an_equity_outside_its_session_does_not_fill(self, equity_spec, quote):
        from dataclasses import replace

        rth = replace(equity_spec, session="RTH")
        saturday = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
        order = _order(symbol="AAPL", market=Market.STOCKS)

        outcome = PaperFillEngine().execute(order, rth, quote, now=saturday)

        assert outcome.fills == ()
        assert "session" in outcome.reason

    def test_slippage_cannot_be_configured_negative(self):
        with pytest.raises(ValueError, match="rebate"):
            FillModel(base_slippage_bps=D("-1"))


class TestTheLedger:
    def test_a_buy_then_a_sell_realises_the_difference(self, paper_broker, crypto_spec):
        buy = OrderRequest(
            symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
            order_type=OrderType.MARKET, strategy="test",
        )
        paper_broker.place_order(buy, quantity=D("1"))
        assert paper_broker.get_positions()[0].quantity == D("1")

        close = paper_broker.close_position("BTCUSDT")

        assert close.status is OrderStatus.FILLED
        assert paper_broker.get_positions() == ()
        # Bought at the ask plus slippage, sold at the bid minus it, so
        # the round trip must lose money: that is the spread.
        assert paper_broker.get_balance() < D("100000")

    def test_adding_to_a_position_averages_the_entry(self, paper_broker):
        """Exact arithmetic, not a tolerance.

        The previous version of this test compared the average against
        the first fill with a loose relative tolerance, which would have
        passed for almost any implementation. Two equal fills average to
        the arithmetic mean of their prices, exactly, and that is what is
        asserted here.
        """
        request = OrderRequest(
            symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
            order_type=OrderType.MARKET, strategy="test",
        )
        first_order = paper_broker.place_order(request, quantity=D("1"))
        first_price = first_order.fills[0].price

        second_order = paper_broker.place_order(request, quantity=D("1"))
        second_price = second_order.fills[0].price

        position = paper_broker.get_positions()[0]
        assert position.quantity == D("2")
        assert position.average_entry_price == (first_price + second_price) / D("2")

    def test_averaging_weights_by_quantity(self, paper_broker, now):
        """Unequal fills weight by size, not by count."""
        from dataclasses import replace

        request = OrderRequest(
            symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
            order_type=OrderType.MARKET, strategy="test",
        )
        one = paper_broker.place_order(request, quantity=D("1"))
        more = paper_broker.place_order(
            replace(request, client_order_id="second"), quantity=D("3")
        )
        # The second order fills partially: participation is capped at a
        # fraction of displayed size. Weight by what actually filled,
        # which is the point of the test.
        fills = list(one.fills) + list(more.fills)
        assert len({fill.quantity for fill in fills}) > 1, "fills must differ in size"

        total = sum(fill.quantity for fill in fills)
        expected = sum(fill.price * fill.quantity for fill in fills) / total

        position = paper_broker.get_positions()[0]
        assert position.quantity == total
        assert position.average_entry_price == expected

    def test_fees_reduce_the_balance(self, paper_broker):
        before = paper_broker.get_balance()
        paper_broker.place_order(
            OrderRequest(
                symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
                order_type=OrderType.MARKET, strategy="test",
            ),
            quantity=D("1"),
        )

        assert paper_broker.get_balance() < before

    def test_closing_a_flat_symbol_is_refused(self, paper_broker):
        from gtcc.adapters.errors import OrderRejected

        with pytest.raises(OrderRejected):
            paper_broker.close_position("BTCUSDT")


class TestTheOrderStateMachine:
    def _approved(self, quantity: str = "1") -> RiskVerdict:
        return RiskVerdict(
            action=RiskAction.ALLOW, approved_quantity=D(quantity), checks=()
        )

    def _request(self) -> OrderRequest:
        return OrderRequest(
            symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
            order_type=OrderType.MARKET, strategy="test",
        )

    def test_an_unapproved_order_cannot_be_registered(self):
        oms = OrderManager()
        refused = RiskVerdict(
            action=RiskAction.REJECT, approved_quantity=D("0"), checks=()
        )

        with pytest.raises(RiskNotApproved):
            oms.register(self._request(), refused)

    def test_the_approved_quantity_wins_over_the_requested_one(self):
        oms = OrderManager()
        request = OrderRequest(
            symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
            order_type=OrderType.MARKET, quantity=D("100"), strategy="test",
        )
        verdict = RiskVerdict(
            action=RiskAction.REDUCE, approved_quantity=D("3"),
            checks=(), requested_quantity=D("100"),
        )

        order = oms.register(request, verdict)

        assert order.quantity == D("3")

    def test_submission_does_not_make_an_order_filled(self):
        """The transition that must not exist."""
        oms = OrderManager()
        order = oms.register(self._request(), self._approved())
        oms.mark_submitted(order.client_order_id)

        assert oms.get(order.client_order_id).status is OrderStatus.SUBMITTED
        assert oms.get(order.client_order_id).filled_quantity == 0

    def test_a_fill_is_what_makes_an_order_filled(self, now):
        oms = OrderManager()
        order = oms.register(self._request(), self._approved())
        oms.mark_submitted(order.client_order_id)
        oms.mark_accepted(order.client_order_id, "broker-1")

        updated = oms.apply_fill(
            order.client_order_id,
            Fill(
                order_id=order.client_order_id, symbol="BTCUSDT", side=Side.BUY,
                quantity=D("1"), price=D("60000"), fee=D("3"), timestamp=now,
            ),
        )

        assert updated.status is OrderStatus.FILLED

    def test_an_illegal_transition_raises(self):
        oms = OrderManager()
        order = oms.register(self._request(), self._approved())
        oms.mark_submitted(order.client_order_id)
        oms.mark_canceled(order.client_order_id)

        with pytest.raises(IllegalTransition):
            oms.mark_accepted(order.client_order_id, "broker-1")

    def test_overfilling_an_order_raises(self, now):
        oms = OrderManager()
        order = oms.register(self._request(), self._approved("1"))
        oms.mark_submitted(order.client_order_id)
        oms.mark_accepted(order.client_order_id, "b")

        with pytest.raises(ValueError, match="above the ordered"):
            oms.apply_fill(
                order.client_order_id,
                Fill(
                    order_id=order.client_order_id, symbol="BTCUSDT", side=Side.BUY,
                    quantity=D("2"), price=D("60000"), fee=D("1"), timestamp=now,
                ),
            )


class TestReconciliation:
    def test_a_clean_comparison_reports_clean(self):
        oms = OrderManager()
        assert oms.reconcile([]).clean

    def test_an_order_the_broker_has_never_heard_of_is_a_discrepancy(self):
        oms = OrderManager()
        order = oms.register(
            OrderRequest(
                symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
                order_type=OrderType.MARKET, strategy="test",
            ),
            RiskVerdict(action=RiskAction.ALLOW, approved_quantity=D("1"), checks=()),
        )
        oms.mark_submitted(order.client_order_id)

        result = oms.reconcile([])

        assert not result.clean
        assert result.blocks_live_trading
        assert result.discrepancies[0].kind is DiscrepancyKind.MISSING_AT_BROKER

    def test_an_order_only_the_broker_knows_about_is_a_discrepancy(self):
        oms = OrderManager()
        stray = _order(status=OrderStatus.ACCEPTED, client_order_id="stray-1")

        result = oms.reconcile([stray])

        assert result.discrepancies[0].kind is DiscrepancyKind.UNKNOWN_LOCALLY

    def test_a_quantity_disagreement_is_a_discrepancy(self, now):
        oms = OrderManager()
        order = oms.register(
            OrderRequest(
                symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
                order_type=OrderType.MARKET, strategy="test",
            ),
            RiskVerdict(action=RiskAction.ALLOW, approved_quantity=D("2"), checks=()),
        )
        oms.mark_submitted(order.client_order_id)
        oms.mark_accepted(order.client_order_id, "b")

        remote = _order(
            quantity=D("2"),
            status=OrderStatus.ACCEPTED,
            client_order_id=order.client_order_id,
            filled_quantity=D("1"),
        )
        result = oms.reconcile([remote])

        kinds = {d.kind for d in result.discrepancies}
        assert DiscrepancyKind.QUANTITY_MISMATCH in kinds
