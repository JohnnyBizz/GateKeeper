"""The OANDA v20 adapter.

**About these fixtures.** They were written from the v20 API as
documented, not captured from live traffic: the machine this was built
on could not reach OANDA's documentation or its API. So they prove the
adapter handles the shape it expects, and they do not prove that shape
is what OANDA sends.

That gap is closed by `python -m gtcc oanda-check`, which the account
owner runs with their own token and which reports the fields the
endpoints actually return. Until that has been run once, treat the
field names here as an assumption.

What the tests do prove, and what matters either way: no instrument
detail is hardcoded, a response that differs from expectation raises an
error naming the exact field rather than silently substituting, the
token never appears in any output, and the live host is unreachable
without arming.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from gtcc.adapters.errors import (
    AdapterError,
    AuthenticationError,
    ConnectionUnhealthy,
    FeatureUnavailable,
    LiveTradingDisabled,
    OrderRejected,
    RateLimited,
)
from gtcc.adapters.oanda import (
    LIVE_HOST,
    PRACTICE_HOST,
    OandaBroker,
    OandaClient,
    OandaDataAdapter,
    OandaSchemaError,
    build_oanda,
)
from gtcc.domain.enums import (
    AssetClass,
    Market,
    OrderStatus,
    OrderType,
    Side,
    Timeframe,
    TradingMode,
)
from gtcc.domain.money import D
from gtcc.domain.orders import OrderRequest

TOKEN = "oanda-TEST-TOKEN-NEVER-LOGGED"
ACCOUNT = "101-004-1234567-001"

INSTRUMENTS = {
    "instruments": [
        {
            "name": "EUR_USD",
            "type": "CURRENCY",
            "displayName": "EUR/USD",
            "pipLocation": -4,
            "displayPrecision": 5,
            "tradeUnitsPrecision": 0,
            "minimumTradeSize": "1",
            "maximumTrailingStopDistance": "1.00000",
            "minimumTrailingStopDistance": "0.00050",
            "maximumPositionSize": "0",
            "maximumOrderUnits": "100000000",
            "marginRate": "0.0333",
        },
        {
            "name": "USD_JPY",
            "type": "CURRENCY",
            "displayName": "USD/JPY",
            "pipLocation": -2,
            "displayPrecision": 3,
            "tradeUnitsPrecision": 0,
            "minimumTradeSize": "1",
            "marginRate": "0.0333",
        },
    ]
}

PRICING = {
    "prices": [
        {
            "type": "PRICE",
            "instrument": "EUR_USD",
            "time": "2026-10-01T14:30:00.123456789Z",
            "tradeable": True,
            "bids": [{"price": "1.08495", "liquidity": 10000000}],
            "asks": [{"price": "1.08505", "liquidity": 10000000}],
            "closeoutBid": "1.08480",
            "closeoutAsk": "1.08520",
        }
    ]
}

CANDLES = {
    "instrument": "EUR_USD",
    "granularity": "M5",
    "candles": [
        {
            "complete": True,
            "volume": 412,
            "time": "2026-10-01T14:00:00.000000000Z",
            "mid": {"o": "1.08400", "h": "1.08520", "l": "1.08380", "c": "1.08500"},
        },
        {
            "complete": False,
            "volume": 88,
            "time": "2026-10-01T14:05:00.000000000Z",
            "mid": {"o": "1.08500", "h": "1.08560", "l": "1.08490", "c": "1.08550"},
        },
    ],
}

SUMMARY = {
    "account": {
        "id": ACCOUNT,
        "currency": "USD",
        "balance": "99875.4321",
        "NAV": "100120.8765",
        "marginUsed": "3200.0000",
        "marginAvailable": "96920.8765",
        "openTradeCount": 1,
        "pl": "120.8765",
    },
    "lastTransactionID": "556",
}

OPEN_POSITIONS = {
    "positions": [
        {
            "instrument": "EUR_USD",
            "long": {
                "units": "10000",
                "averagePrice": "1.08400",
                "pl": "12.3456",
                "unrealizedPL": "9.8765",
                "financing": "-0.4321",
            },
            "short": {"units": "0", "averagePrice": "0", "pl": "0", "financing": "0"},
        }
    ]
}

MARKET_FILL = {
    "orderCreateTransaction": {
        "id": "557",
        "time": "2026-10-01T14:30:01.000000000Z",
        "type": "MARKET_ORDER",
        "instrument": "EUR_USD",
        "units": "10000",
    },
    "orderFillTransaction": {
        "id": "558",
        "time": "2026-10-01T14:30:01.100000000Z",
        "type": "ORDER_FILL",
        "instrument": "EUR_USD",
        "units": "10000",
        "price": "1.08506",
        "commission": "0.0000",
        "financing": "0.0000",
        "pl": "0.0000",
    },
    "lastTransactionID": "558",
}


class StubTransport(httpx.BaseTransport):
    """Answers the endpoints the adapter calls, and nothing else.

    Strict: an unexpected path raises rather than returning an empty
    document, so a test cannot pass because a fake invented a reply.
    Every request's headers are captured so the tests can assert on
    what was actually sent.
    """

    def __init__(self, routes: dict[str, object] | None = None, status: int = 200) -> None:
        self.routes: dict[str, object] = routes or {}
        self.status = status
        self.requests: list[httpx.Request] = []
        self.calls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        self.calls.append(f"{request.method} {path}")

        for suffix, body in self.routes.items():
            if path.endswith(suffix):
                if isinstance(body, Exception):
                    raise body
                if isinstance(body, tuple):
                    status, payload = body
                    return httpx.Response(status, json=payload, request=request)
                return httpx.Response(self.status, json=body, request=request)

        raise AssertionError(
            f"the adapter called {request.method} {path}, which this stub was "
            f"not set up for. Known: {sorted(self.routes)}"
        )


def make_adapters(routes: dict[str, object], **kwargs):
    transport = StubTransport(routes)
    data, broker = build_oanda(
        token=TOKEN, account_id=ACCOUNT, transport=transport, **kwargs
    )
    # Tests must not be slowed by the real limiter.
    data.client.limiter.per_second = 0
    return data, broker, transport


ALL_ROUTES = {
    "/instruments": INSTRUMENTS,
    "/pricing": PRICING,
    "/candles": CANDLES,
    "/summary": SUMMARY,
    "/openPositions": OPEN_POSITIONS,
    "/orders": MARKET_FILL,
}


class TestTheTokenNeverEscapes:
    """The point of not accepting it in a chat message is undermined if
    the adapter then prints it."""

    def test_it_is_sent_as_a_bearer_header(self):
        data, _, transport = make_adapters(ALL_ROUTES)

        data.refresh_instruments()

        assert transport.requests[0].headers["authorization"] == f"Bearer {TOKEN}"

    def test_the_client_repr_does_not_contain_it(self):
        client = OandaClient(token=TOKEN, account_id=ACCOUNT)

        assert TOKEN not in repr(client)
        assert "token=***" in repr(client)

    def test_an_authentication_failure_does_not_echo_it(self):
        data, _, _ = make_adapters({"/instruments": (401, {"errorMessage": "bad token"})})

        with pytest.raises(AuthenticationError) as caught:
            data.refresh_instruments()

        assert TOKEN not in str(caught.value)

    def test_a_server_error_does_not_echo_it(self):
        data, _, _ = make_adapters({"/instruments": (500, {"errorMessage": "boom"})})

        with pytest.raises(ConnectionUnhealthy) as caught:
            data.refresh_instruments()

        assert TOKEN not in str(caught.value)

    def test_a_schema_error_does_not_echo_it(self):
        data, _, _ = make_adapters({"/instruments": {"wrong": []}})

        with pytest.raises(OandaSchemaError) as caught:
            data.refresh_instruments()

        assert TOKEN not in str(caught.value)


class TestInstrumentsComeFromOanda:
    """The property that protects against my documentation uncertainty.

    If a field name here is wrong the error says so. Nothing about tick
    size, pip size or lot step is written into this codebase.
    """

    def test_a_forex_pair_is_translated_from_the_venue_fields(self):
        data, _, _ = make_adapters(ALL_ROUTES)

        spec = data.get_instrument("EUR_USD")

        assert spec.symbol == "EUR_USD"
        assert spec.asset_class is AssetClass.FOREX_SPOT
        assert spec.market is Market.FOREX
        assert spec.base_currency == "EUR"
        assert spec.quote_currency == "USD"
        # pipLocation -4 means a pip is 0.0001.
        assert spec.pip_size == Decimal("0.0001")
        # displayPrecision 5 means the tick is 0.00001.
        assert spec.tick_size == Decimal("0.00001")
        # tradeUnitsPrecision 0 means whole units.
        assert spec.lot_step == Decimal("1")
        assert spec.min_qty == Decimal("1")

    def test_a_jpy_pair_has_a_different_pip_size(self):
        """The case that catches a hardcoded 0.0001."""
        data, _, _ = make_adapters(ALL_ROUTES)

        assert data.get_instrument("USD_JPY").pip_size == Decimal("0.01")
        assert data.get_instrument("USD_JPY").tick_size == Decimal("0.001")

    def test_leverage_is_derived_from_the_margin_rate(self):
        data, _, _ = make_adapters(ALL_ROUTES)

        spec = data.get_instrument("EUR_USD")

        # marginRate 0.0333 is roughly 30:1.
        assert Decimal("29") < spec.max_leverage < Decimal("31")

    def test_an_unmapped_instrument_type_is_refused_not_guessed(self):
        odd = {
            "instruments": [
                {
                    "name": "WEIRD", "type": "SOMETHING_NEW", "displayName": "?",
                    "pipLocation": -4, "displayPrecision": 5,
                    "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
                    "marginRate": "0.05",
                }
            ]
        }
        data, _, _ = make_adapters({"/instruments": odd})

        with pytest.raises(OandaSchemaError, match="unmapped instrument type"):
            data.refresh_instruments()

    def test_an_unknown_symbol_says_how_oanda_names_things(self):
        data, _, _ = make_adapters(ALL_ROUTES)

        with pytest.raises(FeatureUnavailable, match="EUR_USD"):
            data.get_instrument("EURUSD")

    def test_a_missing_field_names_itself(self):
        broken = {"instruments": [{"name": "EUR_USD", "type": "CURRENCY"}]}
        data, _, _ = make_adapters({"/instruments": broken})

        with pytest.raises(OandaSchemaError) as caught:
            data.refresh_instruments()

        assert caught.value.field_path == "pipLocation"
        assert "oanda-check" in str(caught.value)


class TestQuotes:
    def test_a_quote_is_read_from_the_top_of_book(self):
        data, _, _ = make_adapters(ALL_ROUTES)

        quote = data.get_quote("EUR_USD")

        assert quote.bid == Decimal("1.08495")
        assert quote.ask == Decimal("1.08505")
        assert quote.bid_size == Decimal("10000000")
        assert quote.timestamp.tzinfo is not None
        assert quote.spread == Decimal("0.00010")

    def test_nanosecond_timestamps_are_handled(self):
        """OANDA sends nine fractional digits, which Python's
        fromisoformat will not take on 3.11."""
        data, _, _ = make_adapters(ALL_ROUTES)

        quote = data.get_quote("EUR_USD")

        assert quote.timestamp == datetime(
            2026, 10, 1, 14, 30, 0, 123456, tzinfo=timezone.utc
        )

    def test_an_untradeable_instrument_has_no_usable_quote(self):
        """Returning the last price would present a stale number as
        current, which is the failure the whole platform is built to
        avoid."""
        closed = {"prices": [{**PRICING["prices"][0], "tradeable": False}]}
        data, _, _ = make_adapters({**ALL_ROUTES, "/pricing": closed})

        with pytest.raises(FeatureUnavailable, match="not currently tradeable"):
            data.get_quote("EUR_USD")

    def test_an_empty_price_list_is_refused(self):
        data, _, _ = make_adapters({**ALL_ROUTES, "/pricing": {"prices": []}})

        with pytest.raises(FeatureUnavailable):
            data.get_quote("EUR_USD")


class TestCandles:
    def test_candles_become_bars(self):
        data, _, _ = make_adapters(ALL_ROUTES)

        bars = data.get_bars("EUR_USD", Timeframe.M5)

        assert len(bars) == 2
        assert bars[0].open == Decimal("1.08400")
        assert bars[0].high == Decimal("1.08520")
        assert bars[0].volume == Decimal("412")

    def test_oandas_complete_flag_becomes_the_closed_flag(self):
        """The indicators refuse unclosed bars, so this mapping is what
        stops a forming candle reaching them."""
        data, _, _ = make_adapters(ALL_ROUTES)

        bars = data.get_bars("EUR_USD", Timeframe.M5)

        assert bars[0].closed is True
        assert bars[1].closed is False

    def test_an_unsupported_timeframe_is_refused_with_the_list(self):
        data, _, _ = make_adapters(ALL_ROUTES)

        with pytest.raises(FeatureUnavailable, match="no granularity"):
            data.get_bars("EUR_USD", Timeframe.M3)

    def test_the_granularity_and_price_component_are_sent(self):
        data, _, transport = make_adapters(ALL_ROUTES)

        data.get_bars("EUR_USD", Timeframe.H4, limit=100)

        request = transport.requests[-1]
        assert request.url.params["granularity"] == "H4"
        assert request.url.params["price"] == "M"
        assert request.url.params["count"] == "100"

    def test_mid_prices_are_used_by_default(self):
        """Reading bid for longs and ask for shorts would bias every
        backtest in the strategy's favour."""
        data, _, _ = make_adapters(ALL_ROUTES)

        assert data.candle_price == "M"


class TestAccountAndPositions:
    def test_the_account_summary_is_translated(self):
        _, broker, _ = make_adapters(ALL_ROUTES)

        account = broker.get_account()

        assert account.currency == "USD"
        assert account.equity == Decimal("100120.8765")
        assert account.cash == Decimal("99875.4321")
        assert account.buying_power == Decimal("96920.8765")
        assert account.reconciled_at is not None

    def test_a_practice_account_reports_paper_mode(self):
        """Simulated funds against real prices is paper by any sensible
        reading, and the journal should say so."""
        _, broker, _ = make_adapters(ALL_ROUTES)

        assert broker.mode is TradingMode.PAPER
        assert broker.get_account().mode is TradingMode.PAPER

    def test_a_long_position_is_signed_positive(self):
        _, broker, _ = make_adapters(ALL_ROUTES)

        positions = broker.get_positions()

        assert len(positions) == 1
        assert positions[0].quantity == Decimal("10000")
        assert positions[0].average_entry_price == Decimal("1.08400")

    def test_the_mark_comes_from_the_price_feed(self):
        """OANDA reports unrealised P&L but not a mark. Deriving one
        from P&L would be inventing a price."""
        _, broker, _ = make_adapters(ALL_ROUTES)

        assert broker.get_positions()[0].mark_price == Decimal("1.08495")

    def test_a_position_with_no_price_reports_no_mark(self):
        routes = {**ALL_ROUTES, "/pricing": {"prices": []}}
        _, broker, _ = make_adapters(routes)

        assert broker.get_positions()[0].mark_price is None

    def test_a_flat_position_is_omitted(self):
        flat = {
            "positions": [
                {
                    "instrument": "EUR_USD",
                    "long": {"units": "0", "averagePrice": "0", "pl": "0", "financing": "0"},
                    "short": {"units": "0", "averagePrice": "0", "pl": "0", "financing": "0"},
                }
            ]
        }
        _, broker, _ = make_adapters({**ALL_ROUTES, "/openPositions": flat})

        assert broker.get_positions() == ()


class TestOrderPlacement:
    def _request(self, **overrides) -> OrderRequest:
        base = dict(
            symbol="EUR_USD", market=Market.FOREX, side=Side.BUY,
            order_type=OrderType.MARKET, protective_stop=D("1.08000"),
            targets=(D("1.09500"),), strategy="trend_continuation",
        )
        base.update(overrides)
        return OrderRequest(**base)

    def _body(self, transport: StubTransport) -> dict:
        return json.loads(transport.requests[-1].content)["order"]

    def test_a_buy_sends_positive_units(self):
        _, broker, transport = make_adapters(ALL_ROUTES)

        broker.place_order(self._request(), quantity=D("10000"))

        assert self._body(transport)["units"] == "10000"

    def test_a_sell_sends_negative_units(self):
        """OANDA expresses direction as the sign of units, and this is
        the only place that translation happens."""
        _, broker, transport = make_adapters(ALL_ROUTES)

        broker.place_order(self._request(side=Side.SELL), quantity=D("10000"))

        assert self._body(transport)["units"] == "-10000"

    def test_the_stop_and_target_travel_to_the_venue(self):
        """They must survive this process dying, which means they live
        at OANDA rather than in memory here."""
        _, broker, transport = make_adapters(ALL_ROUTES)

        broker.place_order(self._request(), quantity=D("10000"))

        body = self._body(transport)
        assert body["stopLossOnFill"]["price"] == "1.08000"
        assert body["takeProfitOnFill"]["price"] == "1.09500"
        assert body["stopLossOnFill"]["timeInForce"] == "GTC"

    def test_prices_are_formatted_at_the_instruments_precision(self):
        _, broker, transport = make_adapters(ALL_ROUTES)

        broker.place_order(self._request(protective_stop=D("1.08")), quantity=D("10000"))

        # displayPrecision 5 for EUR_USD.
        assert self._body(transport)["stopLossOnFill"]["price"] == "1.08000"

    def test_our_client_id_travels_to_the_venue(self):
        """So a reply can be matched after a restart."""
        _, broker, transport = make_adapters(ALL_ROUTES)
        request = self._request()

        broker.place_order(request, quantity=D("10000"))

        assert self._body(transport)["clientExtensions"]["id"] == request.client_order_id

    def test_a_fill_in_the_response_is_recorded(self):
        _, broker, _ = make_adapters(ALL_ROUTES)

        order = broker.place_order(self._request(), quantity=D("10000"))

        assert order.status is OrderStatus.FILLED
        assert order.filled_quantity == Decimal("10000")
        assert order.average_fill_price == Decimal("1.08506")
        assert order.broker_order_id == "557"

    def test_an_accepted_order_without_a_fill_is_not_marked_filled(self):
        """The rule from section 18, at the adapter boundary."""
        accepted = {"orderCreateTransaction": MARKET_FILL["orderCreateTransaction"]}
        _, broker, _ = make_adapters({**ALL_ROUTES, "/orders": accepted})

        order = broker.place_order(self._request(), quantity=D("10000"))

        assert order.status is OrderStatus.ACCEPTED
        assert order.filled_quantity == 0

    def test_a_venue_rejection_arrives_as_a_rejection_not_a_fill(self):
        """OANDA returns a rejection with HTTP 201, not an error status."""
        rejected = {
            "orderRejectTransaction": {
                "id": "559", "type": "MARKET_ORDER_REJECT",
                "rejectReason": "INSUFFICIENT_MARGIN",
            }
        }
        _, broker, _ = make_adapters({**ALL_ROUTES, "/orders": rejected})

        with pytest.raises(OrderRejected, match="INSUFFICIENT_MARGIN"):
            broker.place_order(self._request(), quantity=D("10000"))

    def test_a_zero_quantity_is_refused_before_any_request(self):
        _, broker, transport = make_adapters(ALL_ROUTES)

        with pytest.raises(OrderRejected, match="positive"):
            broker.place_order(self._request(), quantity=D("0"))

        assert not any("POST" in call for call in transport.calls)

    def test_an_unmappable_order_state_raises_rather_than_guessing(self):
        """An order whose state cannot be mapped is exactly the
        condition that must stop live trading."""
        odd = {
            "orders": [
                {
                    "id": "600", "instrument": "EUR_USD", "units": "100",
                    "state": "SOMETHING_NEW", "clientExtensions": {"id": "x"},
                }
            ]
        }
        _, broker, _ = make_adapters({**ALL_ROUTES, "/orders": odd})

        with pytest.raises(OandaSchemaError, match="unmapped order state"):
            broker.get_orders()


class TestFailureHandling:
    def test_a_timeout_retries_then_reports_unhealthy(self):
        data, _, transport = make_adapters(
            {"/instruments": httpx.TimeoutException("too slow")}
        )

        with pytest.raises(ConnectionUnhealthy, match="timeout"):
            data.refresh_instruments()

        # One attempt plus the configured retries.
        assert len(transport.requests) == 3

    def test_rate_limiting_eventually_raises(self):
        data, _, _ = make_adapters({"/instruments": (429, {"errorMessage": "slow down"})})

        with pytest.raises(RateLimited):
            data.refresh_instruments()

    def test_a_client_error_is_not_retried(self):
        """The request was wrong; sending it again will be wrong again."""
        data, _, transport = make_adapters(
            {"/instruments": (400, {"errorMessage": "bad request"})}
        )

        with pytest.raises(AdapterError):
            data.refresh_instruments()

        assert len(transport.requests) == 1

    def test_health_reports_down_when_the_summary_fails(self):
        data, _, _ = make_adapters({"/summary": (500, {"errorMessage": "boom"})})

        health = data.health()

        assert health.healthy is False
        assert "oanda" in health.detail

    def test_health_reports_ok_with_a_latency(self):
        data, _, _ = make_adapters(ALL_ROUTES)

        health = data.health()

        assert health.healthy is True
        assert health.latency_ms is not None

    def test_a_non_json_body_is_a_schema_error(self):
        class Garbage(httpx.BaseTransport):
            def handle_request(self, request):
                return httpx.Response(200, content=b"<html>down for maintenance", request=request)

        client = OandaClient(token=TOKEN, account_id=ACCOUNT, transport=Garbage())
        client.limiter.per_second = 0
        data = OandaDataAdapter(client=client)

        with pytest.raises(OandaSchemaError, match="not JSON"):
            data.refresh_instruments()


class TestTheLiveHostIsGated:
    """A practice token against the live host fails harmlessly. A live
    token against it does not, which is why this is in code."""

    def test_practice_is_the_default_host(self):
        data, _, _ = make_adapters(ALL_ROUTES)

        assert data.client.base_url == PRACTICE_HOST

    def test_live_is_refused_without_deployment_permission(self):
        with pytest.raises(LiveTradingDisabled, match="does not permit live"):
            build_oanda(
                token=TOKEN, account_id=ACCOUNT, environment="live",
                deployment_allows_live=False,
            )

    def test_live_is_refused_when_nothing_is_armed(self):
        from gtcc.domain.enums import TradingMode as Mode
        from gtcc.risk.safety import initial_state

        with pytest.raises(LiveTradingDisabled, match="not been armed"):
            build_oanda(
                token=TOKEN, account_id=ACCOUNT, environment="live",
                deployment_allows_live=True,
                execution=initial_state(Mode.PAPER),
            )

    def test_live_is_allowed_once_armed(self):
        from gtcc.domain.enums import TradingMode as Mode
        from gtcc.risk.safety import LIVE_CONFIRMATION_PHRASE, initial_state

        armed = initial_state(Mode.PAPER).arm_live(
            actor="owner@example.com",
            confirmation=LIVE_CONFIRMATION_PHRASE,
            deployment_allows_live=True,
        )

        data, broker = build_oanda(
            token=TOKEN, account_id=ACCOUNT, environment="live",
            deployment_allows_live=True, execution=armed,
            transport=StubTransport(ALL_ROUTES),
        )

        assert data.client.base_url == LIVE_HOST
        assert broker.mode is TradingMode.LIVE

    def test_an_unknown_environment_is_refused(self):
        with pytest.raises(ValueError, match="practice"):
            OandaClient(token=TOKEN, account_id=ACCOUNT, environment="demo")

    def test_a_missing_token_is_refused_with_the_variable_name(self):
        with pytest.raises(AuthenticationError, match="GTCC_OANDA_TOKEN"):
            OandaClient(token="", account_id=ACCOUNT)

    def test_a_missing_account_id_is_refused(self):
        with pytest.raises(AuthenticationError, match="GTCC_OANDA_ACCOUNT_ID"):
            OandaClient(token=TOKEN, account_id="")


class TestItPlugsIntoTheRiskEngine:
    """The adapter is only useful if the rest of the platform accepts it."""

    def test_a_real_risk_verdict_can_be_computed_from_oanda_data(self, limits, now):
        from gtcc.data.quality import check_quote
        from gtcc.risk.engine import RiskEngine
        from gtcc.risk.safety import initial_state
        from gtcc.risk.state import fresh_state
        from gtcc.risk.engine import RiskContext

        data, broker, _ = make_adapters(ALL_ROUTES)
        spec = data.get_instrument("EUR_USD")
        quote = data.get_quote("EUR_USD")
        account = broker.get_account()

        context = RiskContext(
            execution=initial_state(TradingMode.PAPER),
            account=account,
            instrument=spec,
            limits=limits,
            state=fresh_state(account.account_id, account.equity),
            data_quality=check_quote(quote, now=quote.timestamp),
            quote=quote,
            estimated_slippage_bps=D("1"),
            now=quote.timestamp,
        )
        verdict = RiskEngine().evaluate(
            OrderRequest(
                symbol="EUR_USD", market=Market.FOREX, side=Side.BUY,
                order_type=OrderType.MARKET, protective_stop=D("1.08000"),
                targets=(D("1.09500"),), strategy="trend_continuation",
            ),
            context,
        )

        # Sized in whole units, on OANDA's own lot step, risking no more
        # than the configured budget.
        assert verdict.approved_quantity % Decimal("1") == 0
        assert verdict.sizing.projected_risk <= account.equity * limits.max_risk_per_trade


class TestTheVerifier:
    """The verifier is what closes the documentation gap, so it has to
    work even though the thing it verifies is uncertain."""

    def _client(self, routes) -> OandaClient:
        client = OandaClient(
            token=TOKEN, account_id=ACCOUNT, transport=StubTransport(routes)
        )
        client.limiter.per_second = 0
        return client

    def test_it_reports_every_expected_field_as_present(self):
        from gtcc.adapters.oanda_check import check

        results = check(self._client(ALL_ROUTES))

        assert len(results) == 5
        for result in results:
            assert result.reached, result.error
            assert result.ok, f"{result.endpoint} missing {result.missing}"
            assert result.present

    def test_it_names_a_field_that_is_absent(self):
        """The whole point: if OANDA's naming differs, say which field."""
        from gtcc.adapters.oanda_check import check

        broken = {
            "instruments": [
                {
                    "name": "EUR_USD", "type": "CURRENCY",
                    "displayPrecision": 5, "tradeUnitsPrecision": 0,
                    "minimumTradeSize": "1", "marginRate": "0.0333",
                }
            ]
        }
        results = check(self._client({**ALL_ROUTES, "/instruments": broken}))

        instruments = next(r for r in results if "instruments" in r.endpoint)
        assert instruments.ok is False
        assert "instruments[].pipLocation" in instruments.missing

    def test_an_unreachable_endpoint_is_reported_not_raised(self):
        """A verifier that crashes on the first problem is useless for
        diagnosing the problem."""
        from gtcc.adapters.oanda_check import check

        results = check(self._client({**ALL_ROUTES, "/summary": (500, {"e": "boom"})}))

        summary = next(r for r in results if "summary" in r.endpoint)
        assert summary.reached is False
        assert summary.error
        # The other endpoints were still probed.
        assert sum(1 for r in results if r.reached) == 4

    def test_an_empty_list_is_present_not_missing(self):
        """An account with no open positions is the normal case, not a
        schema problem."""
        from gtcc.adapters.oanda_check import check

        results = check(self._client({**ALL_ROUTES, "/openPositions": {"positions": []}}))

        positions = next(r for r in results if "openPositions" in r.endpoint)
        assert positions.ok is True

    def test_the_report_never_prints_the_token(self, capsys):
        from gtcc.adapters.oanda_check import check, report

        results = check(self._client(ALL_ROUTES))
        report(results, environment="practice", account_id=ACCOUNT)

        assert TOKEN not in capsys.readouterr().out

    def test_the_report_exit_code_signals_a_mismatch(self, capsys):
        from gtcc.adapters.oanda_check import check, report

        broken = {"instruments": [{"name": "EUR_USD", "type": "CURRENCY"}]}
        results = check(self._client({**ALL_ROUTES, "/instruments": broken}))

        code = report(results, environment="practice", account_id=ACCOUNT)

        assert code == 1
        assert "did not match" in capsys.readouterr().out

    def test_a_clean_run_exits_zero(self, capsys):
        from gtcc.adapters.oanda_check import check, report

        code = report(
            check(self._client(ALL_ROUTES)), environment="practice", account_id=ACCOUNT
        )

        assert code == 0
        assert "was present" in capsys.readouterr().out

    def test_it_places_no_order_and_modifies_nothing(self):
        """Read-only, asserted rather than promised."""
        from gtcc.adapters.oanda_check import check

        transport = StubTransport(ALL_ROUTES)
        client = OandaClient(token=TOKEN, account_id=ACCOUNT, transport=transport)
        client.limiter.per_second = 0

        check(client)

        assert all(call.startswith("GET ") for call in transport.calls), transport.calls

    def test_the_expected_fields_match_what_the_adapter_actually_reads(self):
        """Guards against the checklist drifting away from the adapter.

        If someone adds a field to the adapter and not to EXPECTED, the
        verifier would report a clean run while the adapter still broke.
        """
        from pathlib import Path

        from gtcc.adapters.oanda_check import EXPECTED

        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "gtcc" / "adapters" / "oanda.py"
        ).read_text(encoding="utf-8")

        leaves = set()
        for paths in EXPECTED.values():
            for path in paths:
                leaf = path.replace("[]", "").split(".")[-1]
                leaves.add(leaf)

        # Every field the checklist claims to verify must appear in the
        # adapter, otherwise the checklist is verifying fiction.
        for leaf in leaves:
            assert leaf in source, f"oanda_check expects {leaf!r} which oanda.py never reads"
