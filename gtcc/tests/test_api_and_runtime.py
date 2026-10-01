"""The HTTP surface, its security, and the single submission path."""

from __future__ import annotations

import pytest

from gtcc.config import Settings
from gtcc.domain.enums import Market, OrderType, RiskAction, Side, TradingMode
from gtcc.domain.money import D
from gtcc.domain.orders import OrderRequest
from gtcc.risk.safety import LIVE_CONFIRMATION_PHRASE


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


def _order(symbol: str = "BTCUSDT", **overrides) -> OrderRequest:
    base = dict(
        symbol=symbol, market=Market.CRYPTO, side=Side.BUY,
        order_type=OrderType.MARKET, protective_stop=D("59000"),
        targets=(D("62500"),), strategy="breakout",
    )
    base.update(overrides)
    return OrderRequest(**base)


class TestAuthentication:
    def test_health_needs_no_session(self, anonymous_api):
        response = anonymous_api.get("/api/health")

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "PAPER"
        assert body["live_armed"] is False
        assert body["deployment_allows_live"] is False

    def test_a_signed_in_request_succeeds(self, owner_api):
        response = owner_api.get("/api/auth/me")

        assert response.status_code == 200
        assert response.json()["email"] == "owner@example.com"

    def test_a_wrong_password_is_refused_without_saying_why(self, anonymous_api):
        response = anonymous_api.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "wrong-password-here"},
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "email or password is incorrect"

    def test_an_unknown_account_gives_the_same_answer(self, anonymous_api):
        """Different messages would enumerate accounts."""
        response = anonymous_api.post(
            "/api/auth/login",
            json={"email": "nobody@example.com", "password": "whatever-long-enough"},
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "email or password is incorrect"

    def test_repeated_attempts_are_rate_limited(self, anonymous_api):
        codes = [
            anonymous_api.post(
                "/api/auth/login",
                json={"email": "owner@example.com", "password": "nope-nope-nope"},
            ).status_code
            for _ in range(8)
        ]

        assert 429 in codes

    def test_signing_out_revokes_the_session(self, owner_api):
        assert owner_api.post("/api/auth/logout").status_code == 200
        assert owner_api.get("/api/account").status_code == 401

    def test_the_password_is_never_returned(self, owner_api):
        assert "password" not in owner_api.get("/api/auth/me").text.lower()


class TestEveryDangerousEndpointChecksIdentity:
    """Specification of the denial paths, per endpoint rather than in
    aggregate. A single signed-in fixture made it easy to test only the
    happy path and never notice a missing guard."""

    DANGEROUS = [
        ("post", "/api/orders", {"json": None}),
        ("post", "/api/control/kill-switch", {"json": {"enabled": True, "reason": "t"}}),
        ("post", "/api/control/pause", {"json": {"enabled": True, "reason": "t"}}),
        ("post", "/api/control/live/arm", {"json": {"confirmation": LIVE_CONFIRMATION_PHRASE}}),
        ("post", "/api/control/live/disarm", {"json": {}}),
        ("post", "/api/control/breaker/reset", {"json": {}}),
        ("post", "/api/control/mode", {"json": {"target": "BACKTEST"}}),
        ("post", "/api/control/reconcile", {"json": {}}),
        ("post", "/api/control/close-position/BTCUSDT", {"json": {}}),
    ]

    def _call(self, client, method, path, kwargs):
        payload = dict(kwargs)
        if payload.get("json") is None:
            payload["json"] = _body()
        return getattr(client, method)(path, **payload)

    @pytest.mark.parametrize("method,path,kwargs", DANGEROUS)
    def test_anonymous_is_denied(self, anonymous_api, method, path, kwargs):
        assert self._call(anonymous_api, method, path, kwargs).status_code == 401

    @pytest.mark.parametrize("method,path,kwargs", DANGEROUS)
    def test_a_missing_csrf_token_is_denied(self, owner_api_without_csrf, method, path, kwargs):
        response = self._call(owner_api_without_csrf, method, path, kwargs)

        assert response.status_code == 403
        assert "CSRF" in response.json()["detail"]

    @pytest.mark.parametrize("method,path,kwargs", DANGEROUS)
    def test_an_incorrect_csrf_token_is_denied(self, owner_api_bad_csrf, method, path, kwargs):
        assert self._call(owner_api_bad_csrf, method, path, kwargs).status_code == 403

    @pytest.mark.parametrize(
        "method,path,kwargs",
        [row for row in DANGEROUS if "/control/" in row[1]],
    )
    def test_the_wrong_role_is_denied(self, viewer_api, method, path, kwargs):
        response = self._call(viewer_api, method, path, kwargs)

        assert response.status_code == 403
        assert "owner" in response.json()["detail"]

    def test_the_owner_with_a_valid_token_is_allowed(self, owner_api):
        assert owner_api.post("/api/control/pause", json={"enabled": False}).status_code == 200

    def test_a_safe_request_needs_no_token(self, owner_api_without_csrf):
        assert owner_api_without_csrf.get("/api/account").status_code == 200


class TestProductionCookieSecurity:
    """The test settings disable secure cookies so TestClient works over
    http. That must not be the only configuration ever exercised."""

    @pytest.fixture
    def production_client(self, tmp_path, limits, paper_broker, data_adapter, now):
        import secrets

        from fastapi.testclient import TestClient

        from gtcc.adapters.base import AdapterRegistry
        from gtcc.config import set_settings
        from gtcc.runtime import TradingRuntime
        from tests.conftest import _build_app
        from tests.support import OWNER_EMAIL, OWNER_PASSWORD

        set_settings(None)
        settings = Settings(
            secret_key=secrets.token_urlsafe(48),
            database_url=f"sqlite:///{tmp_path / 'prod.db'}",
            environment="production",
            secure_cookies=True,
            log_format="json",
            log_level="WARNING",
        )
        set_settings(settings)
        registry = AdapterRegistry()
        registry.register_data(data_adapter)
        registry.register_broker(paper_broker)
        runtime = TradingRuntime(
            settings=settings, limits=limits, registry=registry,
            broker_name="paper", data_name=data_adapter.name, clock=lambda: now,
        )
        client = TestClient(_build_app(settings, runtime), base_url="https://testserver")
        yield client, OWNER_EMAIL, OWNER_PASSWORD
        set_settings(None)

    def test_the_session_cookie_is_secure_httponly_and_same_site(self, production_client):
        client, email, password = production_client

        response = client.post("/api/auth/login", json={"email": email, "password": password})

        assert response.status_code == 200
        header = response.headers["set-cookie"]
        assert "Secure" in header
        assert "HttpOnly" in header
        assert "SameSite=strict" in header
        assert "Path=/" in header

    def test_a_planted_session_cookie_is_never_adopted(self, production_client):
        """Session fixation.

        An attacker who can set a cookie must not be able to choose the
        session id the victim ends up with. Asserted on what the server
        sent, not on the client's cookie jar, which merges duplicates.
        """
        client, email, password = production_client
        client.cookies.set("gtcc_session", "planted-value")

        # The planted value authenticates nobody.
        assert client.get("/api/account").status_code == 401

        response = client.post(
            "/api/auth/login", json={"email": email, "password": password}
        )

        issued = response.headers["set-cookie"]
        assert "planted-value" not in issued
        assert issued.startswith("gtcc_session=")

    def test_two_sign_ins_receive_different_sessions(self, production_client):
        client, email, password = production_client

        first = client.post(
            "/api/auth/login", json={"email": email, "password": password}
        ).headers["set-cookie"]
        second = client.post(
            "/api/auth/login", json={"email": email, "password": password}
        ).headers["set-cookie"]

        assert first != second

    def test_logout_invalidates_the_session_server_side(self, production_client):
        client, email, password = production_client
        token = client.post(
            "/api/auth/login", json={"email": email, "password": password}
        ).json()["csrf_token"]
        client.headers["X-CSRF-Token"] = token
        cookie = client.cookies.get("gtcc_session")

        client.post("/api/auth/logout")

        # Replaying the exact cookie must fail: revocation is a row
        # update, not a reliance on the browser dropping it.
        client.cookies.set("gtcc_session", cookie)
        assert client.get("/api/account").status_code == 401


class TestSecurityHeaders:
    def test_every_response_carries_the_hardening_headers(self, anonymous_api):
        headers = anonymous_api.get("/api/health").headers

        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    def test_a_correlation_id_is_returned_for_tracing(self, anonymous_api):
        assert anonymous_api.get("/api/health").headers["X-Correlation-Id"]


class TestOrdersGoThroughRisk:
    def test_a_sound_order_is_placed_and_returns_its_verdict(self, owner_api):
        response = owner_api.post("/api/orders", json=_body())

        assert response.status_code == 200
        payload = response.json()
        assert payload["placed"] is True
        assert payload["verdict"]["action"] in ("ALLOW", "REDUCE")
        assert payload["order"]["status"] == "FILLED"
        assert len(payload["verdict"]["checks"]) > 20

    def test_an_order_without_a_stop_is_refused_with_reasons(self, owner_api):
        payload = owner_api.post("/api/orders", json=_body(protective_stop=None)).json()

        assert payload["placed"] is False
        assert payload["order"] is None
        assert any("STOP_PRESENT" in reason for reason in payload["verdict"]["reasons"])

    def test_the_dry_run_places_nothing(self, owner_api):
        before = len(owner_api.get("/api/orders").json())
        verdict = owner_api.post("/api/risk/evaluate", json=_body()).json()
        after = len(owner_api.get("/api/orders").json())

        assert verdict["action"] in ("ALLOW", "REDUCE")
        assert before == after

    def test_a_negative_quantity_is_refused_by_validation(self, owner_api):
        assert owner_api.post("/api/orders", json=_body(quantity="-5")).status_code == 422

    def test_the_kill_switch_stops_subsequent_orders(self, owner_api):
        owner_api.post("/api/control/kill-switch", json={"enabled": True, "reason": "test"})

        payload = owner_api.post("/api/orders", json=_body()).json()

        assert payload["placed"] is False


class TestLiveArmingIsARuntimeAct:
    """Blocker 1. Configuration may permit live trading; only a person
    can arm it, and only for the life of this process."""

    def test_a_permitting_deployment_does_not_arm_anything(self, live_runtime, live_settings):
        assert live_settings.allow_live_trading is True
        assert live_runtime.ensure_execution().live_armed is False
        assert live_runtime.ensure_execution().mode is TradingMode.PAPER
        assert live_runtime.ensure_execution().live_permitted is False

    def test_live_cannot_be_a_startup_mode(self):
        with pytest.raises(ValueError, match="not a startup mode"):
            Settings(mode=TradingMode.LIVE, secret_key="x" * 40)

    def test_there_is_no_confirmation_phrase_field_to_set(self):
        """The phrase used to be an environment-backed setting, which
        meant a stale variable satisfied the human-confirmation gate at
        boot. The field is gone entirely."""
        assert "live_confirmation" not in Settings.model_fields
        assert "live_trading" not in Settings.model_fields

    def test_an_environment_that_sets_the_old_variables_arms_nothing(self, monkeypatch, tmp_path):
        """The exact attack: the operator's environment still carries
        every variable the old design accepted."""
        monkeypatch.setenv("GTCC_SECRET_KEY", "x" * 40)
        monkeypatch.setenv("GTCC_LIVE_TRADING", "true")
        monkeypatch.setenv("GTCC_LIVE_CONFIRMATION", LIVE_CONFIRMATION_PHRASE)
        monkeypatch.setenv("GTCC_ALLOW_LIVE_TRADING", "true")
        monkeypatch.setenv("GTCC_DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")

        from gtcc.risk.safety import initial_state

        settings = Settings()
        execution = initial_state(settings.mode)

        assert settings.mode is TradingMode.PAPER
        assert execution.live_armed is False
        assert execution.live_permitted is False

    def test_arming_requires_the_exact_phrase(self, live_runtime):
        from gtcc.risk.safety import LiveArmingError

        for wrong in ("enable live trading", "ENABLE LIVE TRADING ", "", "yes"):
            with pytest.raises(LiveArmingError):
                live_runtime.arm_live(actor="owner@example.com", confirmation=wrong)

        assert live_runtime.ensure_execution().live_armed is False

    def test_arming_is_refused_without_deployment_permission(self, runtime):
        from gtcc.risk.safety import LiveArmingError

        with pytest.raises(LiveArmingError, match="does not permit live trading"):
            runtime.arm_live(actor="owner@example.com", confirmation=LIVE_CONFIRMATION_PHRASE)

    def test_a_valid_arming_records_who_and_when(self, live_runtime, now):
        state = live_runtime.arm_live(
            actor="owner@example.com", confirmation=LIVE_CONFIRMATION_PHRASE
        )

        assert state.live_armed is True
        assert state.live_permitted is True
        assert state.armed_by == "owner@example.com"
        assert state.armed_at == now

    def test_a_restart_comes_back_disarmed(self, live_settings, limits, paper_broker, data_adapter, now):
        """The central claim. A new process on the same configuration
        must not inherit the armed state."""
        from gtcc.adapters.base import AdapterRegistry
        from gtcc.runtime import TradingRuntime

        def build():
            registry = AdapterRegistry()
            registry.register_data(data_adapter)
            registry.register_broker(paper_broker)
            return TradingRuntime(
                settings=live_settings, limits=limits, registry=registry,
                broker_name="paper", data_name=data_adapter.name, clock=lambda: now,
            )

        first = build()
        first.arm_live(actor="owner@example.com", confirmation=LIVE_CONFIRMATION_PHRASE)
        assert first.ensure_execution().live_permitted is True

        restarted = build()

        assert restarted.ensure_execution().live_armed is False
        assert restarted.ensure_execution().mode is TradingMode.PAPER

    def test_settings_cannot_be_mutated_into_live(self, settings):
        """Blocker 2. The old API route assigned to settings.mode."""
        with pytest.raises(Exception):
            settings.mode = TradingMode.LIVE
        with pytest.raises(Exception):
            settings.allow_live_trading = True

        assert settings.mode is TradingMode.PAPER
        assert settings.allow_live_trading is False

    def test_the_mode_endpoint_cannot_reach_live(self, live_owner_api):
        response = live_owner_api.post("/api/control/mode", json={"target": "LIVE"})

        assert response.status_code == 400
        assert "not a mode" in response.json()["detail"]
        assert live_owner_api.get("/api/control/live").json()["live_armed"] is False

    def test_arming_through_the_api_requires_the_owner_and_the_phrase(self, live_owner_api):
        refused = live_owner_api.post(
            "/api/control/live/arm", json={"confirmation": "nope"}
        )
        assert refused.status_code == 409

        armed = live_owner_api.post(
            "/api/control/live/arm", json={"confirmation": LIVE_CONFIRMATION_PHRASE}
        )
        assert armed.status_code == 200
        assert armed.json()["live_armed"] is True
        assert armed.json()["armed_by"] == "owner@example.com"

    def test_arming_is_recorded_in_the_audit_log(self, live_owner_api, live_settings):
        from sqlalchemy import select

        from gtcc.storage import db
        from gtcc.storage.models import AuditLog

        live_owner_api.post(
            "/api/control/live/arm", json={"confirmation": LIVE_CONFIRMATION_PHRASE}
        )

        db.configure(live_settings.database_dsn)
        with db.session_scope() as session:
            rows = session.scalars(
                select(AuditLog).where(AuditLog.action == "control.live_armed")
            ).all()
            assert len(rows) == 1
            assert rows[0].user_id is not None
            assert rows[0].detail["armed_by"] == "owner@example.com"
            assert rows[0].occurred_at is not None


class TestTheLatchedBreaker:
    """Blocker 3. A critical failure latches execution off and recovery
    of the dependency does not undo it."""

    def test_a_broker_disconnect_trips_the_latch(self, runtime, data_adapter, paper_broker):
        paper_broker.quote_source = None  # makes the broker report unhealthy

        runtime.submit(_order())

        execution = runtime.ensure_execution()
        assert execution.tripped is True
        assert "BROKER_UNHEALTHY" in [str(r) for r in execution.trip_reasons]

    def test_stale_market_data_trips_the_latch(self, runtime, data_adapter):
        from datetime import timedelta

        data_adapter.quote_age = timedelta(minutes=10)

        runtime.submit(_order())

        assert "STALE_MARKET_DATA" in [
            str(r) for r in runtime.ensure_execution().trip_reasons
        ]

    def test_a_reconciliation_failure_trips_the_latch(self, runtime):
        from gtcc.domain.enums import OrderStatus
        from gtcc.domain.orders import Order

        stray = Order(
            symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
            order_type=OrderType.MARKET, quantity=D("1"),
            status=OrderStatus.ACCEPTED, client_order_id="ghost",
        )
        runtime.last_reconciliation = runtime.oms.reconcile([stray])

        runtime.submit(_order())

        assert "RECONCILIATION_FAILED" in [
            str(r) for r in runtime.ensure_execution().trip_reasons
        ]

    def test_a_loss_breaker_trips_the_latch(self, runtime):
        runtime.record_settled_trade(D("-2500"))

        assert "DAILY_LOSS_LIMIT" in [
            str(r) for r in runtime.ensure_execution().trip_reasons
        ]

    def test_recovery_does_not_clear_the_latch(self, runtime, data_adapter):
        from datetime import timedelta

        data_adapter.quote_age = timedelta(minutes=10)
        runtime.submit(_order())
        assert runtime.ensure_execution().tripped

        data_adapter.quote_age = None  # the feed recovers

        result = runtime.submit(_order())

        assert runtime.ensure_execution().tripped is True
        assert result.placed is False
        assert any("EXECUTION_NOT_TRIPPED" in reason for reason in result.verdict.reasons)

    def test_a_tripped_latch_blocks_paper_orders_too(self, runtime):
        """Paper results are the evidence base. Recording them while the
        feed is broken would poison the record."""
        runtime.trip_for_test = None
        from gtcc.risk.safety import TripReason

        runtime.trip(TripReason.OPERATOR, "manual")

        result = runtime.submit(_order())

        assert result.placed is False

    def test_a_reset_is_refused_while_still_unhealthy(self, runtime, paper_broker):
        from gtcc.risk.safety import LiveArmingError, TripReason

        runtime.trip(TripReason.BROKER_UNHEALTHY, "connection lost")
        paper_broker.quote_source = None  # still down

        with pytest.raises(LiveArmingError, match="has not cleared"):
            runtime.reset_breaker(actor="owner@example.com")

        assert runtime.ensure_execution().tripped is True

    def test_an_authorised_reset_clears_a_resolved_breaker(self, runtime):
        from gtcc.risk.safety import TripReason

        runtime.trip(TripReason.BROKER_UNHEALTHY, "connection lost")

        state = runtime.reset_breaker(actor="owner@example.com")

        assert state.tripped is False
        assert state.last_reset_by == "owner@example.com"
        assert state.last_reset_at is not None

    def test_a_reset_leaves_live_disarmed(self, live_runtime):
        """Resuming live after a safety event costs two actions, not one."""
        from gtcc.risk.safety import TripReason

        live_runtime.arm_live(actor="owner@example.com", confirmation=LIVE_CONFIRMATION_PHRASE)
        live_runtime.trip(TripReason.BROKER_UNHEALTHY, "connection lost")

        state = live_runtime.reset_breaker(actor="owner@example.com")

        assert state.tripped is False
        assert state.live_armed is False
        assert state.mode is TradingMode.PAPER

    def test_arming_is_refused_while_tripped(self, live_runtime):
        from gtcc.risk.safety import LiveArmingError, TripReason

        live_runtime.trip(TripReason.MAX_DRAWDOWN, "drawdown limit")

        with pytest.raises(LiveArmingError, match="latched"):
            live_runtime.arm_live(
                actor="owner@example.com", confirmation=LIVE_CONFIRMATION_PHRASE
            )

    def test_the_reset_endpoint_records_the_actor(self, owner_api, runtime, settings):
        from sqlalchemy import select

        from gtcc.risk.safety import TripReason
        from gtcc.storage import db
        from gtcc.storage.models import AuditLog

        runtime.trip(TripReason.OPERATOR, "manual trip for the test")

        response = owner_api.post("/api/control/breaker/reset")

        assert response.status_code == 200
        assert response.json()["tripped"] is False
        db.configure(settings.database_dsn)
        with db.session_scope() as session:
            row = session.scalars(
                select(AuditLog).where(AuditLog.action == "control.breaker_reset")
            ).one()
            assert row.user_id is not None
            assert "OPERATOR" in row.detail["cleared"]

    def test_health_reports_tripped_rather_than_ready(self, anonymous_api, runtime):
        from gtcc.risk.safety import TripReason

        runtime.trip(TripReason.BROKER_UNHEALTHY, "connection lost")

        body = anonymous_api.get("/api/health").json()

        assert body["status"] == "tripped"
        assert body["execution_tripped"] is True
        assert "BROKER_UNHEALTHY" in body["trip_reasons"]

    def test_grok_cannot_clear_the_breaker(self):
        """Structural: nothing in the AI package may touch safety state."""
        from pathlib import Path

        ai_dir = Path(__file__).resolve().parents[1] / "src" / "gtcc" / "ai"
        for path in ai_dir.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "reset_breaker" not in source
            assert "arm_live" not in source
            assert "gtcc.risk" not in source


class TestTheDashboardRenders:
    @pytest.mark.parametrize(
        "path", ["/", "/positions", "/risk", "/settings", "/scanner", "/journal"]
    )
    def test_each_page_renders_for_a_signed_in_owner(self, owner_api, path):
        response = owner_api.get(path)

        assert response.status_code == 200
        assert "GROK" in response.text

    def test_the_mode_is_shown_on_every_page(self, owner_api):
        assert "mode-PAPER" in owner_api.get("/").text

    def test_pages_for_later_phases_say_so_rather_than_showing_figures(self, owner_api):
        text = owner_api.get("/scanner").text

        assert "No data to show yet" in text
        assert "Phase 2" in text

    def test_a_tripped_breaker_is_visible_on_every_page(self, owner_api, runtime):
        from gtcc.risk.safety import TripReason

        runtime.trip(TripReason.BROKER_UNHEALTHY, "connection lost")

        text = owner_api.get("/").text

        assert "Execution is latched off" in text
        assert "BREAKER TRIPPED" in text

    def test_the_login_page_is_public(self, anonymous_api):
        assert anonymous_api.get("/login").status_code == 200


class TestNoSecretIsEverRendered:
    """Secrets must not reach settings output, pages, logs or errors."""

    MARKERS = ("SUPER_SECRET_DB_PASSWORD_12345", "REDIS_SECRET_99", "xai-SECRET_MODEL_KEY")

    @pytest.fixture
    def leaky_settings(self, tmp_path) -> Settings:
        return Settings(
            secret_key="s" * 40,
            database_url=(
                "postgresql://dbadmin:SUPER_SECRET_DB_PASSWORD_12345@db.internal:5432/gtcc"
            ),
            redis_url="redis://:REDIS_SECRET_99@cache.internal:6379/0",
            grok_api_key="xai-SECRET_MODEL_KEY",
            environment="development",
        )

    def test_the_public_view_contains_no_credential(self, leaky_settings):
        rendered = repr(leaky_settings.public_view())

        for marker in self.MARKERS:
            assert marker not in rendered
        assert "dbadmin" not in rendered

    def test_the_public_view_still_says_what_is_configured(self, leaky_settings):
        view = leaky_settings.public_view()

        assert view["database"]["scheme"] == "postgresql"
        assert view["database"]["host"] == "db.internal"
        assert view["database"]["credentials_present"] is True
        assert view["redis"]["configured"] is True
        assert view["grok_api_key_set"] is True

    def test_no_rendering_of_the_settings_object_leaks_a_url_credential(
        self, leaky_settings
    ):
        """The allowlist protects the endpoint. It does not protect a
        traceback that happens to include the settings object, or a
        debug log of it, which is why the URLs are SecretStr.
        """
        for label, rendered in (
            ("repr", repr(leaky_settings)),
            ("str", str(leaky_settings)),
            ("model_dump", repr(leaky_settings.model_dump())),
            ("model_dump_json", leaky_settings.model_dump_json()),
        ):
            for marker in self.MARKERS:
                assert marker not in rendered, f"{marker} leaked through {label}"

    def test_the_connection_string_is_still_reachable_for_use(self, leaky_settings):
        """Masking must not break the thing that needs the value."""
        assert leaky_settings.database_dsn.startswith("postgresql://")
        assert "SUPER_SECRET_DB_PASSWORD_12345" in leaky_settings.database_dsn

    def test_an_exception_rendering_settings_does_not_leak(self, leaky_settings):
        try:
            raise RuntimeError(f"could not connect: {leaky_settings!r}")
        except RuntimeError as exc:
            for marker in self.MARKERS:
                assert marker not in str(exc)

    def test_the_view_is_an_allowlist_not_a_denylist(self, leaky_settings):
        """A new setting must be invisible until somebody lists it."""
        view = leaky_settings.public_view()

        assert "database_url" not in view
        assert "redis_url" not in view
        assert "secret_key" not in view
        assert "grok_api_key" not in view
        assert "provider_keys" not in view

    def test_no_secret_reaches_the_settings_page(self, owner_api, settings):
        text = owner_api.get("/settings").text

        assert settings.secret_key.get_secret_value() not in text
        assert "database_url" not in text

    def test_log_redaction_covers_a_url_credential(self):
        from gtcc.logging_setup import scrub

        scrubbed = repr(
            scrub({"database_url": "postgresql://u:SUPER_SECRET_DB_PASSWORD_12345@h/d"})
        )

        assert "SUPER_SECRET_DB_PASSWORD_12345" not in scrubbed


class TestTheRuntimeIsTheOnlyPath:
    def test_submitting_through_the_runtime_calls_the_engine_first(self, runtime):
        result = runtime.submit(_order())

        assert result.placed
        assert result.verdict.action in (RiskAction.ALLOW, RiskAction.REDUCE)
        assert result.order.filled_quantity > 0

    def test_a_refused_verdict_never_reaches_the_broker(self, runtime):
        result = runtime.submit(_order(protective_stop=None))

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

        assert not runtime.submit(_order()).placed


class TestAnUnknownSymbolIsARefusalNotACrash:
    def test_the_api_returns_a_verdict_rather_than_an_error(self, owner_api):
        response = owner_api.post("/api/orders", json=_body(symbol="NOSUCHTHING"))

        assert response.status_code == 200
        payload = response.json()
        assert payload["placed"] is False
        assert any("INSTRUMENT_KNOWN" in r for r in payload["verdict"]["reasons"])

    def test_the_dry_run_refuses_the_same_way(self, runtime):
        verdict = runtime.evaluate(_order(symbol="NOSUCHTHING"))

        assert verdict.action is RiskAction.REJECT
        assert "no contract specification" in verdict.reasons[0]


class TestConfigurationGates:
    def test_the_defaults_are_paper_and_nothing_automatic(self, monkeypatch):
        monkeypatch.setenv("GTCC_SECRET_KEY", "x" * 40)
        settings = Settings()

        assert settings.mode is TradingMode.PAPER
        assert settings.allow_live_trading is False
        assert settings.automatic_execution is False

    def test_a_weak_secret_is_refused_outside_development(self, monkeypatch):
        monkeypatch.delenv("GTCC_SECRET_KEY", raising=False)

        with pytest.raises(ValueError, match="SECRET_KEY"):
            Settings(environment="production", secret_key="change-me")

    def test_a_weak_secret_is_refused_where_live_is_permitted(self, monkeypatch):
        monkeypatch.delenv("GTCC_SECRET_KEY", raising=False)

        with pytest.raises(ValueError, match="SECRET_KEY"):
            Settings(environment="development", allow_live_trading=True, secret_key="short")

    def test_no_grok_model_is_assumed(self, monkeypatch):
        monkeypatch.setenv("GTCC_SECRET_KEY", "x" * 40)

        assert Settings().grok_model == ""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("max_quote_age_seconds", -1),
            ("max_quote_age_seconds", 0),
            ("session_max_age_seconds", 0),
            ("session_max_age_seconds", -60),
            ("login_rate_limit_per_minute", 0),
            ("grok_timeout_seconds", -5),
            ("grok_max_retries", -1),
            ("max_clock_skew_seconds", -1),
            ("max_bar_age_multiple", 0),
            ("log_level", "CHATTY"),
        ],
    )
    def test_invalid_configuration_fails_at_startup(self, monkeypatch, field, value):
        monkeypatch.setenv("GTCC_SECRET_KEY", "x" * 40)

        with pytest.raises(ValueError):
            Settings(**{field: value})


class TestTheLatchSurvivesARestartButArmingDoesNot:
    """The asymmetry is the whole design.

    A process may have died *because* of the condition that tripped the
    breaker, so coming back clear would hide it. Live arming is the
    opposite: it is a decision a person made about a running process,
    and a restart must invalidate it. One store, two opposite rules.
    """

    @pytest.fixture
    def stores(self, settings):
        from gtcc.storage import db
        from gtcc.storage.repositories import (
            ExecutionLatchRepository,
            RiskStateRepository,
        )

        db.configure(settings.database_dsn)
        db.create_all()
        return (
            RiskStateRepository(db.session_scope),
            ExecutionLatchRepository(db.session_scope),
        )

    def _runtime(self, settings, limits, paper_broker, data_adapter, now, stores):
        from gtcc.adapters.base import AdapterRegistry
        from gtcc.runtime import TradingRuntime

        registry = AdapterRegistry()
        registry.register_data(data_adapter)
        registry.register_broker(paper_broker)
        state_store, latch_store = stores
        return TradingRuntime(
            settings=settings, limits=limits, registry=registry,
            broker_name="paper", data_name=data_adapter.name, clock=lambda: now,
            state_store=state_store, latch_store=latch_store,
        )

    def test_a_latched_trip_is_still_latched_after_a_restart(
        self, settings, limits, paper_broker, data_adapter, now, stores
    ):
        from gtcc.risk.safety import TripReason

        first = self._runtime(settings, limits, paper_broker, data_adapter, now, stores)
        first.trip(TripReason.BROKER_UNHEALTHY, "connection lost mid-session")
        assert first.ensure_execution().tripped

        restarted = self._runtime(settings, limits, paper_broker, data_adapter, now, stores)

        execution = restarted.ensure_execution()
        assert execution.tripped is True
        assert TripReason.BROKER_UNHEALTHY in execution.trip_reasons
        assert execution.trips[0].detail == "connection lost mid-session"
        assert execution.new_trades_blocked

    def test_the_original_trip_time_is_preserved_across_the_restart(
        self, settings, limits, paper_broker, data_adapter, now, stores
    ):
        """When the problem started matters more than when it was noticed."""
        from gtcc.risk.safety import TripReason

        first = self._runtime(settings, limits, paper_broker, data_adapter, now, stores)
        first.trip(TripReason.MAX_DRAWDOWN, "drawdown limit")
        original = first.ensure_execution().trips[0].occurred_at

        restarted = self._runtime(settings, limits, paper_broker, data_adapter, now, stores)

        assert restarted.ensure_execution().trips[0].occurred_at == original

    def test_a_restart_still_comes_back_disarmed(
        self, live_settings, limits, paper_broker, data_adapter, now
    ):
        """The other half: arming is never restored, latch or no latch."""
        from gtcc.storage import db
        from gtcc.storage.repositories import ExecutionLatchRepository

        db.configure(live_settings.database_dsn)
        db.create_all()
        latch = ExecutionLatchRepository(db.session_scope)

        def build():
            from gtcc.adapters.base import AdapterRegistry
            from gtcc.runtime import TradingRuntime

            registry = AdapterRegistry()
            registry.register_data(data_adapter)
            registry.register_broker(paper_broker)
            return TradingRuntime(
                settings=live_settings, limits=limits, registry=registry,
                broker_name="paper", data_name=data_adapter.name,
                clock=lambda: now, latch_store=latch,
            )

        first = build()
        first.arm_live(actor="owner@example.com", confirmation=LIVE_CONFIRMATION_PHRASE)
        assert first.ensure_execution().live_permitted is True

        restarted = build()

        assert restarted.ensure_execution().live_armed is False
        assert restarted.ensure_execution().tripped is False

    def test_an_authorised_reset_clears_it_permanently(
        self, settings, limits, paper_broker, data_adapter, now, stores
    ):
        from gtcc.risk.safety import TripReason

        first = self._runtime(settings, limits, paper_broker, data_adapter, now, stores)
        first.trip(TripReason.OPERATOR, "manual")
        first.reset_breaker(actor="owner@example.com")

        restarted = self._runtime(settings, limits, paper_broker, data_adapter, now, stores)

        assert restarted.ensure_execution().tripped is False

    def test_the_cleared_trip_keeps_its_history(
        self, settings, limits, paper_broker, data_adapter, now, stores
    ):
        """Cleared is not deleted. Who cleared what, and when, is the
        record somebody will want after an incident."""
        from gtcc.risk.safety import TripReason

        _, latch = stores
        runtime = self._runtime(settings, limits, paper_broker, data_adapter, now, stores)
        runtime.trip(TripReason.STALE_MARKET_DATA, "feed stale")
        runtime.reset_breaker(actor="owner@example.com")

        history = latch.history("TEST-1")

        assert len(history) == 1
        assert history[0].reason == "STALE_MARKET_DATA"
        assert history[0].cleared_by == "owner@example.com"
        assert history[0].cleared_at is not None

    def test_repeating_the_same_condition_does_not_grow_the_table(
        self, settings, limits, paper_broker, data_adapter, now, stores
    ):
        """A dependency failing on every poll must not write a row each
        time, and the first occurrence is the one to keep."""
        from gtcc.risk.safety import TripReason

        _, latch = stores
        runtime = self._runtime(settings, limits, paper_broker, data_adapter, now, stores)
        for _ in range(5):
            runtime.trip(TripReason.BROKER_UNHEALTHY, "still down")

        assert len(latch.history("TEST-1")) == 1

    def test_two_different_conditions_are_both_recorded(
        self, settings, limits, paper_broker, data_adapter, now, stores
    ):
        from gtcc.risk.safety import TripReason

        _, latch = stores
        runtime = self._runtime(settings, limits, paper_broker, data_adapter, now, stores)
        runtime.trip(TripReason.BROKER_UNHEALTHY, "down")
        runtime.trip(TripReason.STALE_MARKET_DATA, "stale")

        assert len(latch.history("TEST-1")) == 2
        assert len(runtime.ensure_execution().trips) == 2

    def test_an_unreadable_latch_latches_defensively(
        self, settings, limits, paper_broker, data_adapter, now
    ):
        """If the latch cannot be read, the safe assumption is that it
        was set. Resuming on an unknown safety state is the one answer
        that is definitely wrong."""
        from gtcc.adapters.base import AdapterRegistry
        from gtcc.risk.safety import TripReason
        from gtcc.runtime import TradingRuntime

        class _Unreadable:
            def open_trips(self, account_id):
                raise RuntimeError("the database is unreachable")

        registry = AdapterRegistry()
        registry.register_data(data_adapter)
        registry.register_broker(paper_broker)
        runtime = TradingRuntime(
            settings=settings, limits=limits, registry=registry,
            broker_name="paper", data_name=data_adapter.name,
            clock=lambda: now, latch_store=_Unreadable(),
        )

        execution = runtime.ensure_execution()

        assert execution.tripped is True
        assert TripReason.ACCOUNT_STATE_UNKNOWN in execution.trip_reasons

    def test_a_storage_failure_does_not_lose_the_in_memory_latch(
        self, settings, limits, paper_broker, data_adapter, now
    ):
        from gtcc.adapters.base import AdapterRegistry
        from gtcc.risk.safety import TripReason
        from gtcc.runtime import TradingRuntime

        class _WriteOnlyFails:
            def open_trips(self, account_id):
                return ()

            def record(self, account_id, trip, equity):
                raise RuntimeError("disk full")

        registry = AdapterRegistry()
        registry.register_data(data_adapter)
        registry.register_broker(paper_broker)
        runtime = TradingRuntime(
            settings=settings, limits=limits, registry=registry,
            broker_name="paper", data_name=data_adapter.name,
            clock=lambda: now, latch_store=_WriteOnlyFails(),
        )

        runtime.trip(TripReason.BROKER_UNHEALTHY, "down")

        # Degrades to "latched until restart", never to "not latched".
        assert runtime.ensure_execution().tripped is True
