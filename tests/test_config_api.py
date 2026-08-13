"""Configuration handling and the HTTP/WebSocket API."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from poa.config import DEFAULTS, Config, _env_overrides, load_config
from poa.models import format_duration, format_price
from poa.server import create_app


class TestConfig:
    def test_defaults_load_without_a_file(self, tmp_path):
        config = load_config(tmp_path / "missing.yaml")
        assert config.get("market.chart_timeframe") == DEFAULTS["market"]["chart_timeframe"]

    def test_a_file_overlays_the_defaults(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("market:\n  asset: GBP/JPY\n  trade_duration: 600\n")
        config = load_config(path)
        assert config.get("market.asset") == "GBP/JPY"
        assert config.get("market.trade_duration") == 600
        # Untouched keys keep their defaults rather than disappearing.
        assert config.get("market.chart_timeframe") == 60

    def test_missing_keys_return_the_supplied_default(self):
        assert Config().get("nope.nothing", "fallback") == "fallback"

    def test_dotted_set_creates_intermediate_sections(self):
        config = Config()
        config.set("a.b.c", 5)
        assert config.get("a.b.c") == 5

    def test_env_overrides_are_parsed_and_typed(self):
        overrides = _env_overrides(
            {
                "POA_SERVER__PORT": "9000",
                "POA_ALERTS__ENABLED": "false",
                "POA_CAPTURE__POLL_SECONDS": "1.5",
                "POA_MARKET__ASSET": "GBP/USD",
                "UNRELATED": "x",
            }
        )
        assert overrides["server"]["port"] == 9000
        assert overrides["alerts"]["enabled"] is False
        assert overrides["capture"]["poll_seconds"] == pytest.approx(1.5)
        assert overrides["market"]["asset"] == "GBP/USD"
        assert "unrelated" not in overrides

    def test_relative_paths_resolve_against_the_project_root(self):
        config = Config()
        config.set("storage.database", "storage/journal.db")
        assert config.resolve_path("storage.database").is_absolute()

    def test_a_non_mapping_config_file_is_rejected(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(ValueError):
            load_config(path)

    def test_the_example_config_is_valid_and_complete(self):
        from poa.config import EXAMPLE_CONFIG_PATH

        config = load_config(EXAMPLE_CONFIG_PATH)
        for section in DEFAULTS:
            assert isinstance(config.section(section), dict)
        assert config.get("market.chart_timeframe") != config.get("market.trade_duration")


class TestFormatting:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (30, "30 SEC"),
            (60, "1 MIN"),
            (180, "3 MIN"),
            (1800, "30 MIN"),
            (3600, "1 HOUR"),
            (14400, "4 HOURS"),
            (90, "1M 30S"),
        ],
    )
    def test_durations_render_the_way_a_platform_labels_them(self, seconds, expected):
        assert format_duration(seconds) == expected

    def test_price_precision_scales_with_magnitude(self):
        assert format_price(1.08123456) == "1.08123"
        assert format_price(19234.5) == "19234.50"
        assert format_price(None) == "--"


@pytest.fixture
def client(tmp_path):
    config = load_config()
    config.set("storage.database", str(tmp_path / "journal.db"))
    config.set("storage.screenshot_dir", str(tmp_path / "shots"))
    config.set("logging.file", str(tmp_path / "poa.log"))
    config.set("capture.source", "synthetic")
    config.set("alerts.desktop_notifications", False)
    # autostart=False keeps the background thread out of the tests; ticks are
    # driven explicitly so assertions are deterministic.
    app = create_app(config, autostart=False)
    with TestClient(app) as test_client:
        yield test_client


class TestApi:
    def test_health_reports_the_source(self, client):
        payload = client.get("/api/health").json()
        assert payload["status"] == "ok"
        assert payload["source"] == "synthetic"

    def test_the_dashboard_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Technical Analysis Assistant" in response.text

    def test_static_assets_are_served(self, client):
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200

    def test_options_expose_timeframes_and_durations_separately(self, client):
        payload = client.get("/api/options").json()
        assert payload["chart_timeframes"]
        assert payload["trade_durations"]
        assert "chart_timeframe" in payload["current"]
        assert "trade_duration" in payload["current"]

    def test_a_manual_tick_produces_a_signal(self, client):
        payload = client.post("/api/engine/tick").json()
        assert payload["signal"] is not None
        assert payload["signal"]["direction"] in ("CALL", "PUT", "WAIT", "NO_TRADE")
        assert payload["candles"]

    def test_the_state_payload_is_json_clean(self, client):
        client.post("/api/engine/tick")
        text = json.dumps(client.get("/api/state").json())
        assert "NaN" not in text and "Infinity" not in text

    def test_settings_can_be_updated(self, client):
        response = client.post("/api/settings", json={"trade_duration": 300})
        assert response.status_code == 200
        assert response.json()["applied"]["trade_duration"] == 300
        assert client.get("/api/options").json()["current"]["trade_duration"] == 300

    def test_chart_timeframe_and_trade_duration_update_independently(self, client):
        client.post("/api/settings", json={"chart_timeframe": 300, "trade_duration": 900})
        current = client.get("/api/options").json()["current"]
        assert current["chart_timeframe"] == 300
        assert current["trade_duration"] == 900

    def test_an_invalid_setting_is_rejected(self, client):
        assert client.post("/api/settings", json={"trade_duration": -5}).status_code == 422
        assert client.post("/api/settings", json={"min_confidence": 500}).status_code == 422

    def test_an_empty_settings_body_is_a_no_op(self, client):
        assert client.post("/api/settings", json={}).json()["applied"] == {}

    def test_the_journal_endpoint_returns_entries(self, client):
        client.post("/api/engine/tick")
        payload = client.get("/api/journal").json()
        assert isinstance(payload["entries"], list)

    def test_an_unknown_journal_entry_is_a_404(self, client):
        assert client.get("/api/journal/does-not-exist").status_code == 404

    def test_statistics_are_available_from_the_start(self, client):
        stats = client.get("/api/statistics").json()
        assert "win_rate" in stats
        assert "breakeven_rate" in stats
        assert "disclaimer" in stats

    def test_the_disclaimer_disclaims(self, client):
        text = client.get("/api/disclaimer").json()["disclaimer"]
        assert "does not place trades" in text.lower()
        assert "wait" in text.lower()

    def test_a_backtest_can_be_run_over_synthetic_data(self, client):
        response = client.post(
            "/api/backtest",
            json={"synthetic_candles": 600, "trade_duration": 180, "step": 10, "window": 200},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["evaluated_bars"] > 0
        assert "statistics" in payload

    def test_a_backtest_over_a_missing_file_is_a_400(self, client):
        response = client.post("/api/backtest", json={"csv_path": "/nope/missing.csv"})
        assert response.status_code == 400

    def test_the_websocket_sends_state_on_connect(self, client):
        with client.websocket_connect("/ws") as websocket:
            payload = json.loads(websocket.receive_text())
            assert payload["type"] == "state"
            assert "options" in payload
            assert "disclaimer" in payload

    def test_the_websocket_answers_a_ping(self, client):
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_text()
            websocket.send_text("ping")
            assert json.loads(websocket.receive_text())["type"] == "pong"


class TestEngineDegradation:
    def test_a_capture_failure_clears_any_live_signal(self, tmp_path):
        from poa.engine import AnalysisEngine

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        engine = AnalysisEngine(config)
        try:
            engine.tick()

            # Break the source mid-flight, the way a closed chart window would.
            def explode():
                raise RuntimeError("chart window disappeared")

            engine.source.capture = explode  # type: ignore[method-assign]
            state = engine.tick()

            assert state.last_error is not None
            assert state.signal.direction.value in ("WAIT", "NO_TRADE")
            assert not state.signal.actionable
            assert state.candles == []
        finally:
            engine.close()

    def test_repeated_failures_are_counted(self, tmp_path):
        from poa.engine import AnalysisEngine

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        engine = AnalysisEngine(config)
        try:
            def explode():
                raise RuntimeError("nope")

            engine.source.capture = explode  # type: ignore[method-assign]
            engine.tick()
            engine.tick()
            assert engine.state.consecutive_errors >= 2
        finally:
            engine.close()
