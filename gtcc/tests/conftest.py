"""Shared fixtures.

Nothing here reaches a network, a real broker or a real clock. Every
test that involves time passes an explicit ``now``, because a risk
engine whose verdict depends on when the suite ran is a risk engine
nobody can trust.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gtcc.adapters.base import AdapterRegistry
from gtcc.adapters.paper import PaperBroker
from gtcc.config import Settings, set_settings
from gtcc.data.quality import check_quote
from gtcc.domain.enums import AssetClass, Market, TradingMode
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Quote
from gtcc.domain.money import D
from gtcc.domain.orders import Account
from gtcc.execution.paper_engine import FillModel, PaperFillEngine
from gtcc.risk.engine import RiskContext
from gtcc.risk.limits import parse_limits
from gtcc.risk.state import fresh_state
from gtcc.runtime import TradingRuntime
from gtcc.storage import db

NOW = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)

#: Deliberately explicit rather than loaded from the example file: a
#: test that silently inherits the shipped numbers stops testing the
#: thing it names when somebody edits the example.
LIMITS_RAW = {
    "per_trade": {
        "max_risk_pct": 0.5,
        "max_position_notional_pct": 25,
        "max_leverage": 2,
        "min_reward_risk": 1.5,
        "min_stop_distance_ticks": 8,
        "max_spread_bps": 15,
        "max_slippage_bps": 10,
    },
    "drawdown": {
        "max_daily_loss_pct": 2,
        "max_weekly_loss_pct": 5,
        "max_drawdown_pct": 10,
        "max_consecutive_losses": 4,
    },
    "concentration": {
        "max_open_positions": 5,
        "max_exposure_per_asset_pct": 25,
        "max_correlated_exposure_pct": 40,
        "max_sector_exposure_pct": 40,
        "max_market_exposure_pct": {"CRYPTO": 50, "STOCKS": 60},
    },
    "market_overrides": {
        "FUTURES": {"max_position_notional_pct": 600, "max_leverage": 6},
    },
    "events": {"blackout_minutes": 15, "applies_to_timeframes_under_seconds": 3600},
}


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def limits():
    return parse_limits(LIMITS_RAW)


@pytest.fixture
def equity_spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="AAPL",
        market=Market.STOCKS,
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        tick_size=D("0.01"),
        lot_step=D("1"),
        min_qty=D("1"),
        taker_fee_bps=D("1"),
        maker_fee_bps=D("0.5"),
    )


@pytest.fixture
def crypto_spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="BTCUSDT",
        market=Market.CRYPTO,
        asset_class=AssetClass.CRYPTO_SPOT,
        quote_currency="USDT",
        base_currency="BTC",
        tick_size=D("0.01"),
        lot_step=D("0.0001"),
        min_qty=D("0.0001"),
        allows_fractional=True,
        taker_fee_bps=D("5"),
        maker_fee_bps=D("2"),
    )


@pytest.fixture
def futures_spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="ES",
        market=Market.FUTURES,
        asset_class=AssetClass.FUTURE,
        quote_currency="USD",
        tick_size=D("0.25"),
        tick_value=D("12.50"),
        contract_size=D("50"),
        lot_step=D("1"),
        min_qty=D("1"),
        max_leverage=D("20"),
    )


@pytest.fixture
def forex_spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="EURUSD",
        market=Market.FOREX,
        asset_class=AssetClass.FOREX_SPOT,
        quote_currency="USD",
        base_currency="EUR",
        tick_size=D("0.00001"),
        pip_size=D("0.0001"),
        lot_step=D("1000"),
        min_qty=D("1000"),
        max_leverage=D("30"),
    )


@pytest.fixture
def quote(now) -> Quote:
    return Quote(
        symbol="AAPL",
        timestamp=now,
        bid=D("199.98"),
        ask=D("200.02"),
        bid_size=D("5000"),
        ask_size=D("5000"),
        received_at=now,
    )


@pytest.fixture
def account(now) -> Account:
    return Account(
        account_id="TEST-1",
        currency="USD",
        equity=D("100000"),
        cash=D("100000"),
        buying_power=D("200000"),
        mode=TradingMode.PAPER,
        reconciled_at=now,
    )


@pytest.fixture
def context(limits, equity_spec, quote, account, now) -> RiskContext:
    """A context in which a reasonable order is approved.

    Every rejection test starts from this and changes exactly one
    thing, so a failure names its own cause.
    """
    return RiskContext(
        mode=TradingMode.PAPER,
        live_trading_enabled=False,
        account=account,
        instrument=equity_spec,
        limits=limits,
        state=fresh_state("TEST-1", D("100000"), now=now),
        data_quality=check_quote(quote, now=now),
        quote=quote,
        broker_healthy=True,
        estimated_slippage_bps=D("2"),
        now=now,
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    set_settings(None)
    created = Settings(
        secret_key=secrets.token_urlsafe(48),
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        environment="development",
        secure_cookies=False,
        log_format="text",
        log_level="WARNING",
    )
    set_settings(created)
    yield created
    set_settings(None)


@pytest.fixture
def paper_broker(crypto_spec, quote, now) -> PaperBroker:
    btc_quote = Quote(
        symbol="BTCUSDT",
        timestamp=now,
        bid=D("60000"),
        ask=D("60006"),
        bid_size=D("5"),
        ask_size=D("5"),
        received_at=now,
    )
    quotes = {"BTCUSDT": btc_quote, "AAPL": quote}
    return PaperBroker(
        account_id="TEST-1",
        starting_cash=D("100000"),
        quote_source=lambda symbol: quotes[symbol],
        instrument_source=lambda symbol: crypto_spec,
        fill_engine=PaperFillEngine(FillModel(base_slippage_bps=D("1"))),
        clock=lambda: now,
    )


@pytest.fixture
def runtime(settings, limits, paper_broker, crypto_spec, now) -> TradingRuntime:
    from gtcc.adapters.base import AdapterHealth, Capability, MarketDataAdapter

    class _Data(MarketDataAdapter):
        name = "test-data"
        capabilities = frozenset({Capability.QUOTES, Capability.BARS})

        def get_instrument(self, symbol):
            return crypto_spec

        def get_quote(self, symbol):
            return paper_broker.quote_source(symbol)

        def get_bars(self, symbol, timeframe, *, limit=500, start=None, end=None):
            return []

        def health(self):
            return AdapterHealth.ok("test data adapter")

    registry = AdapterRegistry()
    registry.register_data(_Data())
    registry.register_broker(paper_broker)
    return TradingRuntime(
        settings=settings,
        limits=limits,
        registry=registry,
        broker_name="paper",
        data_name="test-data",
        clock=lambda: now,
    )


@pytest.fixture
def api(settings, runtime):
    """A signed-in API client with its CSRF token attached."""
    from fastapi.testclient import TestClient

    from gtcc.api.app import create_app
    from gtcc.api.security import hash_password
    from gtcc.storage.models import User

    db.configure(settings.database_url)
    db.create_all()
    with db.session_scope() as session:
        session.add(
            User(
                email="owner@example.com",
                password_hash=hash_password("a-sufficiently-long-password"),
                role="owner",
            )
        )

    app = create_app(settings=settings, runtime=runtime)
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        json={"email": "owner@example.com", "password": "a-sufficiently-long-password"},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client
