"""What happens when a venue misbehaves.

The original fakes were permanently healthy and answered every symbol,
which meant the whole failure half of the system was untested. These
tests inject each failure a real adapter can produce and assert the
platform refuses safely rather than trading on bad information.

The shape of every assertion is the same: no order reaches the broker,
and the reason is named in the verdict or the latch.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from gtcc.adapters.base import AdapterHealth, BrokerAdapter, Capability
from gtcc.adapters.errors import (
    AdapterError,
    ConnectionUnhealthy,
    FeatureUnavailable,
    OrderRejected,
    RateLimited,
)
from gtcc.domain.enums import Market, OrderStatus, OrderType, Side, TradingMode
from gtcc.domain.market_data import Quote
from gtcc.domain.money import D
from gtcc.domain.orders import Account, Order, OrderRequest
from gtcc.risk.safety import TripReason
from tests.support import FixtureMisuse


def _order(symbol: str = "BTCUSDT", **overrides) -> OrderRequest:
    base = dict(
        symbol=symbol, market=Market.CRYPTO, side=Side.BUY,
        order_type=OrderType.MARKET, protective_stop=D("59000"),
        targets=(D("62500"),), strategy="breakout",
    )
    base.update(overrides)
    return OrderRequest(**base)


class FailingBroker(BrokerAdapter):
    """A broker whose every failure mode is a constructor switch.

    Strict by default: a call the test did not arrange raises rather
    than returning something plausible.
    """

    name = "failing-broker"
    mode = TradingMode.PAPER
    capabilities = frozenset({Capability.PAPER_TRADING})

    def __init__(
        self,
        *,
        account: Account,
        healthy: bool = True,
        health_detail: str = "ok",
        place_error: Exception | None = None,
        account_error: Exception | None = None,
        positions_error: Exception | None = None,
        orders: tuple[Order, ...] = (),
        stuck_order: Order | None = None,
    ) -> None:
        self._account = account
        self.healthy = healthy
        self.health_detail = health_detail
        self.place_error = place_error
        self.account_error = account_error
        self.positions_error = positions_error
        self._orders = orders
        self.stuck_order = stuck_order
        self.place_calls = 0

    def get_account(self) -> Account:
        if self.account_error is not None:
            raise self.account_error
        return self._account

    def get_balance(self) -> Decimal:
        return self._account.equity

    def get_positions(self):
        if self.positions_error is not None:
            raise self.positions_error
        return ()

    def place_order(self, request, *, quantity):
        self.place_calls += 1
        if self.place_error is not None:
            raise self.place_error
        if self.stuck_order is not None:
            return self.stuck_order
        raise FixtureMisuse(
            "place_order was called but this broker was not told what to do. "
            "A fake that invents a successful fill can make a broken test pass."
        )

    def cancel_order(self, order_id):
        raise FixtureMisuse(f"unexpected cancel_order({order_id!r})")

    def get_order(self, order_id):
        raise FixtureMisuse(f"unexpected get_order({order_id!r})")

    def get_orders(self, *, open_only: bool = False):
        return self._orders

    def get_fills(self, *, since=None):
        return ()

    def close_position(self, symbol):
        raise FixtureMisuse(f"unexpected close_position({symbol!r})")

    def health(self) -> AdapterHealth:
        return (
            AdapterHealth.ok(self.health_detail)
            if self.healthy
            else AdapterHealth.down(self.health_detail)
        )


@pytest.fixture
def failing_runtime(settings, limits, data_adapter, account, now):
    """A runtime whose broker and data adapter can be made to misbehave."""
    from gtcc.adapters.base import AdapterRegistry
    from gtcc.runtime import TradingRuntime

    broker = FailingBroker(account=account)
    registry = AdapterRegistry()
    registry.register_data(data_adapter)
    registry.register_broker(broker)
    runtime = TradingRuntime(
        settings=settings, limits=limits, registry=registry,
        broker_name=broker.name, data_name=data_adapter.name, clock=lambda: now,
    )
    return runtime, broker, data_adapter


class TestStrictFakesFailLoudly:
    """Regressions 23 to 25."""

    def test_an_unknown_symbol_fails_the_test_immediately(self, data_adapter):
        with pytest.raises(FixtureMisuse, match="was not given"):
            data_adapter.get_instrument("NEVER_CONFIGURED")

    def test_an_aapl_request_cannot_resolve_to_the_bitcoin_instrument(self, data_adapter):
        """The original fixture returned one spec for every symbol, so
        an AAPL order could be sized against a Bitcoin contract."""
        apple = data_adapter.get_instrument("AAPL")
        bitcoin = data_adapter.get_instrument("BTCUSDT")

        assert apple.symbol == "AAPL"
        assert bitcoin.symbol == "BTCUSDT"
        assert apple is not bitcoin
        assert apple.tick_size != bitcoin.lot_step or apple.asset_class != bitcoin.asset_class

    def test_a_symbol_mismatch_is_refused_by_the_risk_engine(self, context, equity_spec):
        """Production defence, not only a fixture one. An adapter
        mapping error would otherwise price one instrument with
        another's quote."""
        from dataclasses import replace

        from gtcc.risk.engine import Check, RiskEngine

        mismatched = replace(context, quote=replace(context.quote, symbol="BTCUSDT"))
        verdict = RiskEngine().evaluate(_order(symbol="AAPL", market=Market.STOCKS), mismatched)

        assert Check.SYMBOL_CONSISTENT in verdict.failure_codes
        assert verdict.approved_quantity == 0

    def test_matching_symbols_pass_the_consistency_check(self, context):
        from gtcc.risk.engine import Check, RiskEngine

        verdict = RiskEngine().evaluate(
            _order(symbol="AAPL", market=Market.STOCKS), context
        )

        assert Check.SYMBOL_CONSISTENT not in verdict.failure_codes


class TestDataFeedFailures:
    def test_a_stale_quote_refuses_the_order_and_latches(self, failing_runtime):
        runtime, broker, data = failing_runtime
        data.quote_age = timedelta(minutes=30)

        result = runtime.submit(_order())

        assert result.placed is False
        assert broker.place_calls == 0
        assert TripReason.STALE_MARKET_DATA in runtime.ensure_execution().trip_reasons

    def test_a_crossed_quote_is_invalid_data(self, failing_runtime, now):
        runtime, broker, data = failing_runtime
        data._quotes["BTCUSDT"] = Quote(
            symbol="BTCUSDT", timestamp=now, bid=D("60010"), ask=D("60000"), received_at=now
        )

        result = runtime.submit(_order())

        assert result.placed is False
        assert broker.place_calls == 0
        assert runtime.ensure_execution().tripped

    def test_a_data_provider_exception_does_not_approve_the_order(self, failing_runtime):
        runtime, broker, data = failing_runtime
        data.quote_error = ConnectionUnhealthy("the feed went away")

        result = runtime.submit(_order())

        assert result.placed is False
        assert broker.place_calls == 0

    def test_rate_limiting_does_not_approve_the_order(self, failing_runtime):
        runtime, broker, data = failing_runtime
        data.quote_error = RateLimited("test-data", retry_after_seconds=30)

        assert runtime.submit(_order()).placed is False
        assert broker.place_calls == 0

    def test_an_unlisted_symbol_is_a_refusal_not_a_crash(self, failing_runtime):
        runtime, broker, _ = failing_runtime

        verdict = runtime.evaluate(_order(symbol="NOSUCHTHING"))

        assert verdict.action.value == "REJECT"
        assert broker.place_calls == 0

    def test_an_unhealthy_data_adapter_blocks_a_breaker_reset(self, failing_runtime):
        runtime, _, data = failing_runtime
        runtime.trip(TripReason.STALE_MARKET_DATA, "feed stale")
        data.healthy = False
        data.health_detail = "feed still down"

        from gtcc.risk.safety import LiveArmingError

        with pytest.raises(LiveArmingError, match="has not cleared"):
            runtime.reset_breaker(actor="owner@example.com")


class TestBrokerFailures:
    def test_an_unhealthy_broker_refuses_and_latches(self, failing_runtime):
        runtime, broker, _ = failing_runtime
        broker.healthy = False
        broker.health_detail = "session expired"

        result = runtime.submit(_order())

        assert result.placed is False
        assert broker.place_calls == 0
        assert TripReason.BROKER_UNHEALTHY in runtime.ensure_execution().trip_reasons

    def test_a_broker_exception_during_placement_does_not_report_a_fill(self, failing_runtime):
        runtime, broker, _ = failing_runtime
        broker.place_error = OrderRejected("failing-broker", "insufficient margin")

        result = runtime.submit(_order())

        assert result.placed is False
        assert "insufficient margin" in result.detail
        tracked = runtime.oms.get(result.order.client_order_id)
        assert tracked.status is OrderStatus.REJECTED
        assert tracked.filled_quantity == 0

    def test_an_account_call_that_raises_refuses_rather_than_propagating(
        self, failing_runtime
    ):
        """An unreachable venue is a refusal and a latched breaker.

        This used to assert that the exception escaped `submit`. It no
        longer does, deliberately: an API route that received the
        exception would turn a venue outage into a 500 for the operator
        to interpret, and the breaker would never have tripped. The
        refusal carries a named check instead, like every other one.
        """
        runtime, broker, _ = failing_runtime
        broker.account_error = ConnectionUnhealthy("account endpoint down")

        result = runtime.submit(_order())

        assert result.placed is False
        assert broker.place_calls == 0
        assert "VENUE_REACHABLE" in result.verdict.explain()
        assert TripReason.BROKER_UNHEALTHY in runtime.ensure_execution().trip_reasons

    def test_an_order_the_broker_never_heard_of_trips_reconciliation(self, failing_runtime):
        runtime, broker, _ = failing_runtime
        from gtcc.risk.engine import RiskAction, RiskVerdict

        tracked = runtime.oms.register(
            _order(),
            RiskVerdict(action=RiskAction.ALLOW, approved_quantity=D("1"), checks=()),
        )
        runtime.oms.mark_submitted(tracked.client_order_id)

        reconciliation = runtime.reconcile()

        assert reconciliation.blocks_live_trading
        result = runtime.submit(_order())
        assert result.placed is False
        assert TripReason.RECONCILIATION_FAILED in runtime.ensure_execution().trip_reasons

    def test_a_partial_broker_response_is_not_treated_as_filled(self, failing_runtime, now):
        """A broker that accepts without filling must leave the order
        open, not complete."""
        runtime, broker, _ = failing_runtime
        broker.stuck_order = Order(
            symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
            order_type=OrderType.MARKET, quantity=D("1"),
            status=OrderStatus.ACCEPTED, broker_order_id="venue-1",
        )

        result = runtime.submit(_order())

        assert result.order.filled_quantity == 0
        assert result.order.status is not OrderStatus.FILLED

    def test_an_unvaluable_position_latches_account_state_unknown(
        self, settings, limits, data_adapter, account, now
    ):
        from gtcc.adapters.base import AdapterRegistry
        from gtcc.domain.enums import AssetClass
        from gtcc.domain.orders import Position
        from gtcc.runtime import TradingRuntime

        class MysteryPositionBroker(FailingBroker):
            def get_positions(self):
                return (
                    Position(
                        symbol="MYSTERY", market=Market.CRYPTO,
                        asset_class=AssetClass.CRYPTO_SPOT, quantity=D("1"),
                        average_entry_price=D("100"), mark_price=None,
                    ),
                )

        broker = MysteryPositionBroker(account=account)
        registry = AdapterRegistry()
        registry.register_data(data_adapter)
        registry.register_broker(broker)
        runtime = TradingRuntime(
            settings=settings, limits=limits, registry=registry,
            broker_name=broker.name, data_name=data_adapter.name, clock=lambda: now,
        )

        result = runtime.submit(_order())

        assert result.placed is False
        assert TripReason.ACCOUNT_STATE_UNKNOWN in runtime.ensure_execution().trip_reasons


class TestCapabilityNegotiation:
    def test_an_absent_feed_raises_rather_than_returning_empty(self, data_adapter):
        """Section 7: unavailable is not the same as zero."""
        with pytest.raises(FeatureUnavailable):
            data_adapter.get_order_book("BTCUSDT")
        with pytest.raises(FeatureUnavailable):
            data_adapter.get_funding_rate("BTCUSDT")

    def test_capabilities_are_declared_not_guessed(self, data_adapter):
        assert data_adapter.supports(Capability.QUOTES)
        assert not data_adapter.supports(Capability.ORDER_BOOK)
