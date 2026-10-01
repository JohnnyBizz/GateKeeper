"""Shared fixtures.

Nothing here reaches a network, a real broker or a real clock. Every
test that involves time passes an explicit ``now``, because a risk
engine whose verdict depends on when the suite ran is a risk engine
nobody can trust.

Two properties are deliberate and were absent from the first version.

**Fixtures are strict.** An earlier instrument source returned the same
crypto contract specification for every symbol, so an AAPL order could
be sized against a Bitcoin instrument and the test would pass. Lookups
now go through symbol-specific maps that raise
:class:`FixtureMisuse` on anything unexpected, which fails the test
immediately rather than quietly answering.

**Authentication is not bundled into convenience.** A single fixture
that signs in and attaches a CSRF token makes happy-path tests short
and makes it easy to forget the denial paths entirely. The clients are
split so that every privileged test states which identity it is using.
"""

from __future__ import annotations

import secrets
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gtcc.adapters.base import AdapterHealth, AdapterRegistry, Capability, MarketDataAdapter
from gtcc.adapters.errors import FeatureUnavailable
from gtcc.adapters.paper import PaperBroker
from gtcc.config import Settings, set_settings
from gtcc.data.quality import check_quote
from gtcc.domain.enums import AssetClass, Market, Timeframe, TradingMode
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Quote
from gtcc.domain.money import D
from gtcc.domain.orders import Account
from gtcc.execution.paper_engine import FillModel, PaperFillEngine
from gtcc.risk.engine import RiskContext
from gtcc.risk.limits import parse_limits
from gtcc.risk.safety import initial_state
from gtcc.risk.state import fresh_state
from gtcc.runtime import TradingRuntime
from gtcc.storage import db

from tests.support import (  # noqa: E402
    NOW,
    OWNER_EMAIL,
    OWNER_PASSWORD,
    VIEWER_EMAIL,
    VIEWER_PASSWORD,
    FixtureMisuse,
    StrictDataAdapter,
)

__all__ = ["FixtureMisuse", "StrictDataAdapter"]


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


def write_risk_config(directory) -> "Path":
    """Write LIMITS_RAW to a file and return the path.

    Any test that needs `Settings.risk_config_path` to point somewhere
    real uses this. The bootstrap wiring tests originally pointed at the
    repository's own config/risk.yaml, on the reasoning that real limits
    make a more honest end-to-end test. Two things were wrong with that.
    It broke CI, where the file did not exist, while passing locally,
    where it did — a test that depends on a developer's untracked file is
    not testing anything repeatable. And it coupled the assertions to the
    owner's private risk decisions, so tightening a limit would move the
    numbers under tests that say nothing about limits.
    """
    import yaml

    path = Path(directory) / "risk.yaml"
    path.write_text(yaml.safe_dump(LIMITS_RAW, sort_keys=False))
    return path


# -- instruments ----------------------------------------------------------


@pytest.fixture
def equity_spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="AAPL", market=Market.STOCKS, asset_class=AssetClass.EQUITY,
        quote_currency="USD", tick_size=D("0.01"), lot_step=D("1"),
        min_qty=D("1"), taker_fee_bps=D("1"), maker_fee_bps=D("0.5"),
    )


@pytest.fixture
def crypto_spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="BTCUSDT", market=Market.CRYPTO, asset_class=AssetClass.CRYPTO_SPOT,
        quote_currency="USDT", base_currency="BTC", tick_size=D("0.01"),
        lot_step=D("0.0001"), min_qty=D("0.0001"), allows_fractional=True,
        taker_fee_bps=D("5"), maker_fee_bps=D("2"),
    )


@pytest.fixture
def futures_spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="ES", market=Market.FUTURES, asset_class=AssetClass.FUTURE,
        quote_currency="USD", tick_size=D("0.25"), tick_value=D("12.50"),
        contract_size=D("50"), lot_step=D("1"), min_qty=D("1"), max_leverage=D("20"),
    )


@pytest.fixture
def forex_spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="EURUSD", market=Market.FOREX, asset_class=AssetClass.FOREX_SPOT,
        quote_currency="USD", base_currency="EUR", tick_size=D("0.00001"),
        pip_size=D("0.0001"), lot_step=D("1000"), min_qty=D("1000"), max_leverage=D("30"),
    )


@pytest.fixture
def quote(now) -> Quote:
    return Quote(
        symbol="AAPL", timestamp=now, bid=D("199.98"), ask=D("200.02"),
        bid_size=D("5000"), ask_size=D("5000"), received_at=now,
    )


@pytest.fixture
def btc_quote(now) -> Quote:
    return Quote(
        symbol="BTCUSDT", timestamp=now, bid=D("60000"), ask=D("60006"),
        bid_size=D("5"), ask_size=D("5"), received_at=now,
    )


@pytest.fixture
def account(now) -> Account:
    return Account(
        account_id="TEST-1", currency="USD", equity=D("100000"), cash=D("100000"),
        buying_power=D("200000"), mode=TradingMode.PAPER, reconciled_at=now,
    )


@pytest.fixture
def context(limits, equity_spec, quote, account, now) -> RiskContext:
    """A context in which a reasonable order is approved.

    Every rejection test starts from this and changes exactly one
    thing, so a failure names its own cause.
    """
    return RiskContext(
        execution=initial_state(TradingMode.PAPER),
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


# -- strict fakes ----------------------------------------------------------


@pytest.fixture
def instruments(crypto_spec, equity_spec) -> dict[str, InstrumentSpec]:
    return {"BTCUSDT": crypto_spec, "AAPL": equity_spec}


@pytest.fixture
def quotes(btc_quote, quote) -> dict[str, Quote]:
    return {"BTCUSDT": btc_quote, "AAPL": quote}


@pytest.fixture
def data_adapter(instruments, quotes, now) -> StrictDataAdapter:
    return StrictDataAdapter(instruments=instruments, quotes=quotes, now=now)


@pytest.fixture
def paper_broker(data_adapter, now) -> PaperBroker:
    return PaperBroker(
        account_id="TEST-1",
        starting_cash=D("100000"),
        quote_source=data_adapter.get_quote,
        instrument_source=data_adapter.get_instrument,
        fill_engine=PaperFillEngine(FillModel(base_slippage_bps=D("1"))),
        clock=lambda: now,
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
        risk_config_path=write_risk_config(tmp_path),
    )
    set_settings(created)
    yield created
    set_settings(None)


@pytest.fixture
def runtime(settings, limits, paper_broker, data_adapter, now) -> TradingRuntime:
    registry = AdapterRegistry()
    registry.register_data(data_adapter)
    registry.register_broker(paper_broker)
    return TradingRuntime(
        settings=settings,
        limits=limits,
        registry=registry,
        broker_name="paper",
        data_name=data_adapter.name,
        clock=lambda: now,
    )


@pytest.fixture
def live_settings(tmp_path) -> Settings:
    """A deployment that PERMITS live trading but has armed nothing."""
    set_settings(None)
    created = Settings(
        secret_key=secrets.token_urlsafe(48),
        database_url=f"sqlite:///{tmp_path / 'live.db'}",
        environment="development",
        allow_live_trading=True,
        secure_cookies=False,
        log_format="text",
        log_level="WARNING",
        risk_config_path=write_risk_config(tmp_path),
    )
    set_settings(created)
    yield created
    set_settings(None)


@pytest.fixture
def live_runtime(live_settings, limits, paper_broker, data_adapter, now) -> TradingRuntime:
    registry = AdapterRegistry()
    registry.register_data(data_adapter)
    registry.register_broker(paper_broker)
    return TradingRuntime(
        settings=live_settings,
        limits=limits,
        registry=registry,
        broker_name="paper",
        data_name=data_adapter.name,
        clock=lambda: now,
    )


# -- API clients, one identity each ------------------------------------------


def _build_app(settings: Settings, runtime: TradingRuntime):
    from gtcc.api.app import create_app
    from gtcc.api.security import hash_password
    from gtcc.storage.models import User

    db.configure(settings.database_dsn)
    db.create_all()
    with db.session_scope() as session:
        session.add(
            User(email=OWNER_EMAIL, password_hash=hash_password(OWNER_PASSWORD), role="owner")
        )
        session.add(
            User(email=VIEWER_EMAIL, password_hash=hash_password(VIEWER_PASSWORD), role="viewer")
        )
    return create_app(settings=settings, runtime=runtime)


def _sign_in(client, email: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


@pytest.fixture
def app(settings, runtime):
    return _build_app(settings, runtime)


@pytest.fixture
def anonymous_api(app):
    """No session at all. Every privileged call must be refused."""
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def owner_api(app):
    """Signed in as the owner, with a valid CSRF token attached."""
    from fastapi.testclient import TestClient

    client = TestClient(app)
    client.headers["X-CSRF-Token"] = _sign_in(client, OWNER_EMAIL, OWNER_PASSWORD)
    return client


@pytest.fixture
def owner_api_without_csrf(app):
    """Signed in as the owner, deliberately missing the CSRF token."""
    from fastapi.testclient import TestClient

    client = TestClient(app)
    _sign_in(client, OWNER_EMAIL, OWNER_PASSWORD)
    return client


@pytest.fixture
def owner_api_bad_csrf(app):
    """Signed in as the owner, with a token that is not theirs."""
    from fastapi.testclient import TestClient

    client = TestClient(app)
    _sign_in(client, OWNER_EMAIL, OWNER_PASSWORD)
    client.headers["X-CSRF-Token"] = "not-the-session-token"
    return client


@pytest.fixture
def viewer_api(app):
    """Signed in, valid CSRF, but without the owner role."""
    from fastapi.testclient import TestClient

    client = TestClient(app)
    client.headers["X-CSRF-Token"] = _sign_in(client, VIEWER_EMAIL, VIEWER_PASSWORD)
    return client


@pytest.fixture
def live_owner_api(live_settings, live_runtime):
    """Owner client on a deployment that permits live, armed nothing."""
    from fastapi.testclient import TestClient

    client = TestClient(_build_app(live_settings, live_runtime))
    client.headers["X-CSRF-Token"] = _sign_in(client, OWNER_EMAIL, OWNER_PASSWORD)
    return client
