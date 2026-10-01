"""The HTTP surface, its security, and the single submission path."""

from __future__ import annotations

from decimal import Decimal

import pytest

from gtcc.config import LIVE_CONFIRMATION_PHRASE, Settings
from gtcc.domain.enums import Market, OrderType, RiskAction, Side, TradingMode
from gtcc.domain.money import D
from gtcc.domain.orders import OrderRequest


def _body(**overrides) -> dict:
    base = {
        "symbol": "BTCUSDT",
        "market": "CRYPTO",
        "side": "BUY",
        "order_type": "MARKET",
        "protective_stop": "59000",
        "targets": ["62500"],
        "strategy": "breakout",
    }
    base.update(overrides)
    return base


class TestAuthentication:
    def test_health_needs_no_session(self, api):
        api.cookies.clear()
        response = api.get("/api/health")

        assert response.status_code == 200
        assert response.json()["mode"] == "PAPER"
        assert response.json()["live_trading"] is False

    def test_an_unauthenticated_request_is_refused(self, api):
        api.cookies.clear()
        assert api.get("/api/account").status_code == 401

    def test_a_signed_in_request_succeeds(self, api):
        response = api.get("/api/auth/me")

        assert response.status_code == 200
        assert response.json()["email"] == "owner@example.com"

    def test_a_wrong_password_is_refused_without_saying_why(self, api):
        api.cookies.clear()
        response = api.post(
            "/api/auth/login", json={"email": "owner@example.com", "password": "wrong-password"}
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "email or password is incorrect"

    def test_an_unknown_account_gives_the_same_answer(self, api):
        """Different messages would enumerate accounts."""
        api.cookies.clear()
        response = api.post(
            "/api/auth/login", json={"email": "nobody@example.com", "password": "whatever-long"}
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "email or password is incorrect"

    def test_repeated_attempts_are_rate_limited(self, api):
        api.cookies.clear()
        codes = [
            api.post(
                "/api/auth/login", json={"email": "owner@example.com", "password": "nope-nope-nope"}
            ).status_code
            for _ in range(8)
        ]

        assert 429 in codes

    def test_signing_out_revokes_the_session(self, api):
        assert api.post("/api/auth/logout").status_code == 200
        assert api.get("/api/account").status_code == 401

    def test_the_session_cookie_is_http_only(self, api):
        api.cookies.clear()
        response = api.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "a-sufficiently-long-password"},
        )

        header = response.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "SameSite=strict" in header

    def test_the_password_is_never_returned(self, api):
        text = api.get("/api/auth/me").text
        assert "password" not in text.lower()


class TestCrossSiteRequestForgery:
    def test_an_unsafe_request_without_the_token_is_refused(self, api):
        del api.headers["X-CSRF-Token"]
        response = api.post("/api/orders", json=_body())

        assert response.status_code == 403
        assert "CSRF" in response.json()["detail"]

    def test_a_wrong_token_is_refused(self, api):
        api.headers["X-CSRF-Token"] = "not-the-token"
        assert api.post("/api/orders", json=_body()).status_code == 403

    def test_a_safe_request_needs_no_token(self, api):
        del api.headers["X-CSRF-Token"]
        assert api.get("/api/account").status_code == 200


class TestSecurityHeaders:
    def test_every_response_carries_the_hardening_headers(self, api):
        headers = api.get("/api/health").headers

        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    def test_a_correlation_id_is_returned_for_tracing(self, api):
        assert api.get("/api/health").headers["X-Correlation-Id"]


class TestOrdersGoThroughRisk:
    def test_a_sound_order_is_placed_and_returns_its_verdict(self, api):
        response = api.post("/api/orders", json=_body())

        assert response.status_code == 200
        payload = response.json()
        assert payload["placed"] is True
        assert payload["verdict"]["action"] in ("ALLOW", "REDUCE")
        assert payload["order"]["status"] == "FILLED"
        assert len(payload["verdict"]["checks"]) > 20

    def test_an_order_without_a_stop_is_refused_with_reasons(self, api):
        response = api.post("/api/orders", json=_body(protective_stop=None))

        payload = response.json()
        assert payload["placed"] is False
        assert payload["order"] is None
        assert any("STOP_PRESENT" in reason for reason in payload["verdict"]["reasons"])

    def test_the_dry_run_places_nothing(self, api):
        before = len(api.get("/api/orders").json())
        verdict = api.post("/api/risk/evaluate", json=_body()).json()
        after = len(api.get("/api/orders").json())

        assert verdict["action"] in ("ALLOW", "REDUCE")
        assert before == after

    def test_a_rejected_order_is_still_auditable(self, api):
        response = api.post("/api/orders", json=_body(protective_stop=None))
        checks = response.json()["verdict"]["checks"]

        assert any(check["code"] == "STOP_PRESENT" for check in checks)
        assert all("detail" in check for check in checks)

    def test_a_negative_quantity_is_refused_by_validation(self, api):
        assert api.post("/api/orders", json=_body(quantity="-5")).status_code == 422

    def test_the_kill_switch_stops_subsequent_orders(self, api):
        api.post("/api/control/kill-switch", json={"enabled": True, "reason": "test"})
        response = api.post("/api/orders", json=_body())

        assert response.json()["placed"] is False
        assert any("KILL_SWITCH" in reason for reason in response.json()["verdict"]["reasons"])


class TestOperatorControls:
    def test_pausing_and_resuming_is_reflected_in_state(self, api):
        api.post("/api/control/pause", json={"enabled": True, "reason": "test"})
        assert api.get("/api/risk/state").json()["trading_paused"] is True

        api.post("/api/control/pause", json={"enabled": False, "reason": "test"})
        assert api.get("/api/risk/state").json()["trading_paused"] is False

    def test_switching_to_live_is_refused_when_the_deployment_forbids_it(self, api):
        response = api.post(
            "/api/control/mode",
            json={"target": "LIVE", "confirmation": LIVE_CONFIRMATION_PHRASE},
        )

        assert response.status_code == 409
        assert "GTCC_LIVE_TRADING" in response.json()["detail"]

    def test_switching_between_paper_and_backtest_is_allowed(self, api):
        response = api.post("/api/control/mode", json={"target": "BACKTEST"})

        assert response.status_code == 200
        assert response.json()["mode"] == "BACKTEST"

    def test_reconciliation_is_reported(self, api):
        response = api.post("/api/control/reconcile")

        assert response.status_code == 200
        assert "clean" in response.json()


class TestTheDashboardRenders:
    @pytest.mark.parametrize(
        "path", ["/", "/positions", "/risk", "/settings", "/scanner", "/journal"]
    )
    def test_each_page_renders_for_a_signed_in_owner(self, api, path):
        response = api.get(path)

        assert response.status_code == 200
        assert "GROK" in response.text

    def test_the_mode_is_shown_on_every_page(self, api):
        assert "mode-PAPER" in api.get("/").text

    def test_pages_for_later_phases_say_so_rather_than_showing_figures(self, api):
        text = api.get("/scanner").text

        assert "No data to show yet" in text
        assert "Phase 2" in text

    def test_no_secret_reaches_the_settings_page(self, api, settings):
        text = api.get("/settings").text

        assert settings.secret_key.get_secret_value() not in text
        assert "***" in text

    def test_the_login_page_is_public(self, api):
        api.cookies.clear()
        assert api.get("/login").status_code == 200


class TestTheRuntimeIsTheOnlyPath:
    def test_submitting_through_the_runtime_calls_the_engine_first(self, runtime):
        request = OrderRequest(
            symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
            order_type=OrderType.MARKET, protective_stop=D("59000"),
            targets=(D("62500"),), strategy="breakout",
        )
        result = runtime.submit(request)

        assert result.placed
        assert result.verdict.action in (RiskAction.ALLOW, RiskAction.REDUCE)
        assert result.order.filled_quantity > 0

    def test_a_refused_verdict_never_reaches_the_broker(self, runtime):
        request = OrderRequest(
            symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
            order_type=OrderType.MARKET, protective_stop=None, strategy="breakout",
        )
        result = runtime.submit(request)

        assert not result.placed
        assert result.order is None
        assert runtime.broker.get_orders() == ()

    def test_a_settled_loss_trips_the_daily_breaker(self, runtime):
        runtime.record_settled_trade(D("-2500"))
        state = runtime.ensure_state()

        assert state.daily_breaker_tripped
        assert state.breakers().new_trades_blocked

    def test_a_tripped_breaker_refuses_the_next_order(self, runtime):
        runtime.record_settled_trade(D("-2500"))
        result = runtime.submit(
            OrderRequest(
                symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
                order_type=OrderType.MARKET, protective_stop=D("59000"),
                targets=(D("62500"),), strategy="breakout",
            )
        )

        assert not result.placed

    def test_a_dirty_reconciliation_marks_the_broker_unhealthy(self, runtime):
        from gtcc.domain.enums import OrderStatus
        from gtcc.domain.orders import Order

        stray = Order(
            symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
            order_type=OrderType.MARKET, quantity=D("1"),
            status=OrderStatus.ACCEPTED, client_order_id="ghost",
        )
        runtime.oms.reconcile([stray])
        runtime.last_reconciliation = runtime.oms.reconcile([stray])

        result = runtime.submit(
            OrderRequest(
                symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
                order_type=OrderType.MARKET, protective_stop=D("59000"),
                targets=(D("62500"),), strategy="breakout",
            )
        )

        assert not result.placed
        assert any("BROKER_HEALTHY" in reason for reason in result.verdict.reasons)


class TestConfigurationGates:
    def test_the_defaults_are_paper_and_nothing_automatic(self, monkeypatch):
        monkeypatch.setenv("GTCC_SECRET_KEY", "x" * 40)
        settings = Settings()

        assert settings.mode is TradingMode.PAPER
        assert settings.live_trading is False
        assert settings.automatic_execution is False

    def test_live_mode_needs_the_live_flag(self, monkeypatch):
        monkeypatch.setenv("GTCC_SECRET_KEY", "x" * 40)

        with pytest.raises(ValueError, match="GTCC_LIVE_TRADING"):
            Settings(mode=TradingMode.LIVE)

    def test_live_mode_needs_the_confirmation_phrase(self, monkeypatch):
        monkeypatch.setenv("GTCC_SECRET_KEY", "x" * 40)

        with pytest.raises(ValueError, match="LIVE_CONFIRMATION"):
            Settings(mode=TradingMode.LIVE, live_trading=True)

    def test_live_mode_is_reachable_when_both_gates_are_satisfied(self, monkeypatch):
        monkeypatch.setenv("GTCC_SECRET_KEY", "x" * 40)
        settings = Settings(
            mode=TradingMode.LIVE,
            live_trading=True,
            live_confirmation=LIVE_CONFIRMATION_PHRASE,
        )

        assert settings.is_live

    def test_a_weak_secret_is_refused_outside_development(self, monkeypatch):
        monkeypatch.delenv("GTCC_SECRET_KEY", raising=False)

        with pytest.raises(ValueError, match="SECRET_KEY"):
            Settings(environment="production", secret_key="change-me")

    def test_no_grok_model_is_assumed(self, monkeypatch):
        monkeypatch.setenv("GTCC_SECRET_KEY", "x" * 40)

        assert Settings().grok_model == ""

    def test_secrets_are_redacted_in_the_settings_view(self, monkeypatch):
        monkeypatch.setenv("GTCC_SECRET_KEY", "x" * 40)
        view = Settings(grok_api_key="xai-realkeyvalue").redacted()

        assert view["grok_api_key"] == "***set***"
        assert view["secret_key"] == "***set***"
        assert "xai-realkeyvalue" not in str(view)


class TestRiskStateSurvivesARestart:
    """A tripped breaker that a restart clears is not a breaker.

    The process can die for reasons that correlate with a bad day, and
    coming back with a clean daily tally is the worst possible moment to
    forget trading was stopped.
    """

    @pytest.fixture
    def store(self, settings):
        from gtcc.storage import db
        from gtcc.storage.repositories import RiskStateRepository

        db.configure(settings.database_url)
        db.create_all()
        return RiskStateRepository(db.session_scope)

    def test_a_tripped_breaker_is_read_back_by_a_new_runtime(self, runtime, store):
        runtime.state_store = store
        runtime.ensure_state()
        runtime.record_settled_trade(D("-2500"))
        assert runtime.ensure_state().daily_breaker_tripped

        restarted = runtime.__class__(
            settings=runtime.settings,
            limits=runtime.limits,
            registry=runtime.registry,
            broker_name=runtime.broker_name,
            data_name=runtime.data_name,
            clock=runtime.clock,
            state_store=store,
        )

        recovered = restarted.ensure_state()
        assert recovered.daily_breaker_tripped
        assert recovered.realised_pnl_today == D("-2500")
        assert recovered.breakers().new_trades_blocked

    def test_the_kill_switch_survives_a_restart(self, runtime, store):
        runtime.state_store = store
        runtime.engage_kill_switch(True)

        restarted = runtime.__class__(
            settings=runtime.settings,
            limits=runtime.limits,
            registry=runtime.registry,
            broker_name=runtime.broker_name,
            data_name=runtime.data_name,
            clock=runtime.clock,
            state_store=store,
        )

        assert restarted.ensure_state().kill_switch

    def test_disabled_symbols_round_trip(self, runtime, store):
        runtime.state_store = store
        runtime.state = runtime.ensure_state().disable_symbol("BTCUSDT")
        store.save(runtime.state, D("100000"))

        reloaded = store.load("TEST-1", D("100000"))

        assert "BTCUSDT" in reloaded.disabled_symbols

    def test_a_storage_failure_does_not_lose_the_in_memory_state(self, runtime):
        class _Broken:
            def load(self, account_id, equity):
                from gtcc.risk.state import fresh_state

                return fresh_state(account_id, equity)

            def save(self, state, equity):
                raise RuntimeError("disk on fire")

        runtime.state_store = _Broken()
        runtime.record_settled_trade(D("-2500"))

        # The write failed; the breaker must still be tripped in memory.
        assert runtime.ensure_state().daily_breaker_tripped


class TestAnUnknownSymbolIsARefusalNotACrash:
    """Without a contract specification there is no tick size and no lot
    step, so nothing can be sized. That is a refusal with a reason, not a
    500 and not a guess at the instrument's shape."""

    def test_the_api_returns_a_verdict_rather_than_an_error(self, api, runtime):
        from gtcc.adapters.errors import FeatureUnavailable

        def _refuse(symbol):
            raise FeatureUnavailable("test-data", "instrument specification", symbol)

        runtime.registry.data(runtime.data_name).get_instrument = _refuse

        response = api.post("/api/orders", json=_body(symbol="NOSUCHTHING"))

        assert response.status_code == 200
        payload = response.json()
        assert payload["placed"] is False
        assert any("INSTRUMENT_KNOWN" in reason for reason in payload["verdict"]["reasons"])

    def test_the_dry_run_refuses_the_same_way(self, runtime):
        from gtcc.adapters.errors import FeatureUnavailable

        def _refuse(symbol):
            raise FeatureUnavailable("test-data", "instrument specification", symbol)

        runtime.registry.data(runtime.data_name).get_instrument = _refuse

        verdict = runtime.evaluate(
            OrderRequest(
                symbol="NOSUCHTHING", market=Market.CRYPTO, side=Side.BUY,
                order_type=OrderType.MARKET, protective_stop=D("1"),
                targets=(D("2"),), strategy="test",
            )
        )

        assert verdict.action is RiskAction.REJECT
        assert "no contract specification" in verdict.reasons[0]
