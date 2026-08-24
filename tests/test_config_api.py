"""Configuration handling and the HTTP/WebSocket API."""

from __future__ import annotations

from pathlib import Path
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

    def test_a_settings_file_it_cannot_read_does_not_stop_the_app(self, tmp_path):
        """It used to raise. But this file is one the user is told to edit by
        hand, and a stray character in YAML then meant a double-clicked
        GateKeeper died on a parser stack trace with nothing on screen saying
        which line to fix. Bad settings are worth losing; the app is not."""
        for name, text in (
            ("list.yaml", "- just\n- a\n- list\n"),
            ("torn.yaml", "not: [valid\n"),
            ("scalar.yaml", "42\n"),
        ):
            path = tmp_path / name
            path.write_text(text)
            config = load_config(path)
            assert config.get("market.payout") == DEFAULTS["market"]["payout"]

    def test_the_unreadable_file_is_kept_rather_than_deleted(self, tmp_path):
        """Whatever is wrong with it, the values in it are the user's own."""
        path = tmp_path / "config.yaml"
        path.write_text("not: [valid\n")
        load_config(path)
        kept = tmp_path / "config.yaml.unreadable"
        assert kept.exists() and kept.read_text() == "not: [valid\n"

    def test_an_empty_file_is_not_treated_as_broken(self, tmp_path):
        """A file with nothing in it is a file with no overrides."""
        path = tmp_path / "config.yaml"
        path.write_text("")
        load_config(path)
        assert not (tmp_path / "config.yaml.unreadable").exists()

    def test_a_readable_file_is_left_alone(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("market:\n  payout: 0.85\n")
        assert load_config(path).get("market.payout") == 0.85
        assert not (tmp_path / "config.yaml.unreadable").exists()

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
        assert "GateKeeper" in response.text

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


class TestConfigPersistence:
    """Settings changed in the app must survive a restart."""

    def test_save_round_trips(self, tmp_path):
        config = load_config(tmp_path / "missing.yaml")
        config.set("market.asset", "GBP/JPY")
        config.set("market.trade_duration", 600)
        target = config.save(tmp_path / "config.yaml")

        reloaded = load_config(target)
        assert reloaded.get("market.asset") == "GBP/JPY"
        assert reloaded.get("market.trade_duration") == 600

    def test_save_never_overwrites_the_shipped_example(self, tmp_path, monkeypatch):
        # The example is documentation; clobbering it would lose the comments
        # that explain every setting.
        from poa import config as config_module

        example = tmp_path / "config.example.yaml"
        example.write_text("market:\n  asset: EUR/USD\n")
        default = tmp_path / "config.yaml"
        monkeypatch.setattr(config_module, "EXAMPLE_CONFIG_PATH", example)
        monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", default)

        config = load_config(example)
        config.set("market.asset", "CHANGED")
        written = config.save()

        assert written == default
        assert "CHANGED" not in example.read_text()

    def test_save_is_atomic(self, tmp_path):
        # A half-written config that fails to parse would break the next start.
        config = load_config(tmp_path / "missing.yaml")
        target = config.save(tmp_path / "config.yaml")
        assert target.exists()
        assert not target.with_suffix(".yaml.tmp").exists()

    def test_ensure_creates_the_config_on_first_run(self, tmp_path, monkeypatch):
        from poa import config as config_module

        example = tmp_path / "config.example.yaml"
        example.write_text("market:\n  asset: EUR/USD\n")
        default = tmp_path / "config.yaml"
        monkeypatch.setattr(config_module, "EXAMPLE_CONFIG_PATH", example)
        monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", default)

        assert not default.exists()
        assert config_module.ensure_config_file() == default
        assert default.exists()

    def test_ensure_does_not_clobber_an_existing_config(self, tmp_path, monkeypatch):
        from poa import config as config_module

        example = tmp_path / "config.example.yaml"
        example.write_text("market:\n  asset: EUR/USD\n")
        default = tmp_path / "config.yaml"
        default.write_text("market:\n  asset: MINE\n")
        monkeypatch.setattr(config_module, "EXAMPLE_CONFIG_PATH", example)
        monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", default)

        config_module.ensure_config_file()
        assert "MINE" in default.read_text()


class TestPackagedDataDirectory:
    """A one-file build unpacks to a temp dir and deletes it on exit.

    Anything the user expects to keep — settings, the journal, the log — has to
    live somewhere else, or every session starts from nothing and the app looks
    like it is ignoring everything it was told.
    """

    def test_running_from_source_uses_the_project_directory(self):
        from poa.config import PROJECT_ROOT, data_root, frozen

        assert not frozen()
        assert data_root() == PROJECT_ROOT

    def test_a_frozen_build_writes_outside_the_bundle(self, monkeypatch, tmp_path):
        import sys

        from poa import config as module

        unpacked = tmp_path / "_MEI12345"
        unpacked.mkdir()
        home = tmp_path / "home"
        home.mkdir()

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(unpacked), raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(home))
        monkeypatch.setenv("XDG_DATA_HOME", str(home))
        monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: home))

        assert module.bundle_root() == unpacked
        data = module.data_root()
        assert unpacked not in data.parents and data != unpacked
        assert data.is_dir()

    def test_relative_paths_resolve_against_the_data_directory(
        self, monkeypatch, tmp_path
    ):
        from poa import config as module

        monkeypatch.setattr(module, "data_root", lambda: tmp_path)
        cfg = module.load_config()
        cfg.set("storage.database", "storage/journal.db")
        assert cfg.resolve_path("storage.database") == tmp_path / "storage/journal.db"

    def test_an_absolute_path_is_left_alone(self, tmp_path):
        from poa.config import load_config

        cfg = load_config()
        cfg.set("storage.database", str(tmp_path / "explicit.db"))
        assert load_config.__module__  # sanity
        assert cfg.resolve_path("storage.database") == tmp_path / "explicit.db"


class TestLoggingWithoutAConsole:
    def test_no_console_handler_when_there_is_no_stderr(self, monkeypatch, tmp_path):
        """A windowed build has sys.stderr set to None."""
        import logging
        import sys

        from poa import logging_setup

        monkeypatch.setattr(logging_setup, "_CONFIGURED", False)
        monkeypatch.setattr(sys, "stderr", None)
        root = logging.getLogger()
        existing = list(root.handlers)
        try:
            root.handlers = []
            logging_setup.setup_logging("INFO", tmp_path / "app.log")
            kinds = [type(h).__name__ for h in root.handlers]
            assert "StreamHandler" not in kinds
            assert any("File" in kind for kind in kinds)
            assert logging_setup.log_file() == tmp_path / "app.log"
        finally:
            root.handlers = existing
            logging_setup._CONFIGURED = True

    def test_thread_exceptions_reach_the_log(self, caplog):
        import threading

        from poa.logging_setup import install_crash_handlers

        install_crash_handlers()
        with caplog.at_level("CRITICAL"):
            worker = threading.Thread(target=lambda: 1 / 0, name="boom")
            worker.start()
            worker.join()
        assert any("unhandled exception in thread" in r.message for r in caplog.records)


class TestAChartSwitchIsNoticed:
    """A tracked signal must never outlive the chart it describes.

    Its peak confidence, its invalidation levels and its expiry all belong to
    one instrument on one timeframe. Carried across a switch they report one
    market's setup over another market's candles — which is the same class of
    mistake as naming the wrong pair, one layer deeper.
    """

    class _NamedSource:
        """A source that knows its own chart, as the feed does."""

        name = "feed"
        vision_based = False
        names_own_chart = True

        def __init__(self):
            self.asset = "EUR/USD OTC"
            self.timeframe = 60

        def capture(self):
            from poa.chart_detection.base import Capture
            from poa.chart_detection.quality import validate_series
            from tests.conftest import pullback_trend

            series = pullback_trend(300, direction=1)
            return Capture(
                series=series,
                quality=validate_series(series, source="feed"),
                asset=self.asset,
                timeframe_seconds=self.timeframe,
            )

        def start(self): pass
        def stop(self): pass

    def _engine(self, tmp_path):
        from poa.config import load_config
        from poa.engine import AnalysisEngine

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("alerts.desktop_notifications", False)
        engine = AnalysisEngine(config)
        engine.source = self._NamedSource()
        return engine

    def test_the_same_chart_twice_is_not_a_change(self, tmp_path):
        engine = self._engine(tmp_path)
        engine.tick()
        engine.tick()
        assert engine.state.capture_meta["chart_changed"] is False

    def test_a_new_instrument_restarts_the_analysis(self, tmp_path):
        engine = self._engine(tmp_path)
        engine.tick()
        engine.tracker.update(engine.state.signal)
        assert engine.tracker.current is not None

        engine.source.asset = "CAD/JPY OTC"
        engine.tick()
        assert engine.state.capture_meta["chart_changed"] is True

    def test_a_new_timeframe_is_a_new_chart_too(self, tmp_path):
        engine = self._engine(tmp_path)
        engine.tick()
        engine.source.timeframe = 300
        engine.tick()
        assert engine.state.capture_meta["chart_changed"] is True


class TestDepthIsForMeasuringNotDeciding:
    """Keeping thousands of candles must not slow down every poll.

    The replay's sample scales almost linearly with history, so depth is worth
    having. The live read's longest lookback is a 200-period EMA, so depth buys
    it nothing and costs real time — 5000 candles takes 110ms against 17ms for
    600, on a loop that runs every couple of seconds.
    """

    class _Deep:
        name = "feed"
        vision_based = False
        names_own_chart = True

        def __init__(self, bars):
            from tests.conftest import pullback_trend

            self.series = pullback_trend(bars, direction=1)

        def capture(self):
            from poa.chart_detection.base import Capture
            from poa.chart_detection.quality import validate_series

            return Capture(
                series=self.series,
                quality=validate_series(self.series, source="feed"),
                asset="EUR/USD OTC",
                timeframe_seconds=60,
            )

        def start(self): pass
        def stop(self): pass

    def _engine(self, tmp_path, bars):
        from poa.config import load_config
        from poa.engine import AnalysisEngine

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("alerts.desktop_notifications", False)
        engine = AnalysisEngine(config)
        engine.source = self._Deep(bars)
        return engine

    def test_the_live_read_looks_at_a_bounded_tail(self, tmp_path):
        engine = self._engine(tmp_path, 3000)
        try:
            engine.tick()
            depth = int(engine.config.get("market.analysis_candles", 600))
            # The signal was made from the tail …
            assert len(engine.state.signal.mtf.current.series) <= depth
        finally:
            engine.close()

    def test_the_replay_still_gets_every_candle(self, tmp_path):
        engine = self._engine(tmp_path, 3000)
        try:
            engine.tick()
            held = len(engine.source.series)
            depth = int(engine.config.get("market.analysis_candles", 600))
            assert held > depth  # the point of the test
            assert len(engine.latest_series()) == held
        finally:
            engine.close()

    def test_a_short_history_is_not_truncated(self, tmp_path):
        engine = self._engine(tmp_path, 200)
        try:
            engine.tick()
            assert len(engine.latest_series()) == len(engine.source.series)
        finally:
            engine.close()


class TestASettingsFileIsBroughtUpToDate:
    """A changed default reaches nobody who has already run the app.

    ``save()`` writes every key, so the file on disk holds the old value and
    overlays the new one forever. A default is only a default on a machine
    that has never started GateKeeper — which is not the machine that needs
    the change.
    """

    def _write(self, tmp_path, data):
        import yaml

        path = tmp_path / "config.yaml"
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle)
        return path

    def test_an_old_file_gets_the_new_gate(self, tmp_path):
        from poa.config import load_config

        path = self._write(tmp_path, {"signals": {"min_confidence": 75}})
        config = load_config(path)

        # The gate that matters is on the number the panel shows.
        assert config.get("signals.min_shown_confidence") == 62
        assert config.get("signals.auto_tune") is False

    def test_the_85_is_taken_off_the_wrong_gate(self, tmp_path):
        """An earlier migration put it on min_confidence, which gates the
        direction score rather than the displayed number — so "trade at 85"
        gated something the user could not see and left the panel free to
        show 80.1."""
        from poa.config import load_config

        path = self._write(tmp_path, {"signals": {"min_confidence": 85}})
        config = load_config(path)

        assert config.get("signals.min_confidence") == 75
        assert config.get("signals.min_shown_confidence") == 62

    def test_and_the_change_is_written_back(self, tmp_path):
        """Otherwise it is applied again, and again, every single start."""
        import yaml

        from poa.config import CONFIG_VERSION, load_config

        path = self._write(tmp_path, {"signals": {"min_confidence": 75}})
        load_config(path)

        with path.open(encoding="utf-8") as handle:
            saved = yaml.safe_load(handle)
        assert saved["signals"]["min_shown_confidence"] == 62
        assert saved["config_version"] == CONFIG_VERSION

    def test_a_number_the_user_chose_is_left_alone(self, tmp_path):
        """Only the old default moves. Anything else is somebody's decision."""
        from poa.config import load_config

        path = self._write(tmp_path, {"signals": {"min_confidence": 62}})
        config = load_config(path)

        assert config.get("signals.min_confidence") == 62

    def test_it_runs_once_and_not_every_start(self, tmp_path):
        """A migration that reapplies itself would undo the next change."""
        from poa.config import load_config

        path = self._write(tmp_path, {"signals": {"min_confidence": 75}})
        load_config(path)

        # The user then decides 85 is too quiet and goes back down.
        config = load_config(path)
        config.set("signals.min_confidence", 70)
        config.set("signals.auto_tune", True)
        config.save(path)

        again = load_config(path)
        assert again.get("signals.min_confidence") == 70
        assert again.get("signals.auto_tune") is True

    def test_a_fresh_install_needs_no_migration(self, tmp_path):
        from poa.config import CONFIG_VERSION, load_config

        config = load_config(tmp_path / "nothing-here.yaml")

        assert config.get("signals.min_shown_confidence") == 62
        assert config.data["config_version"] == CONFIG_VERSION

    def test_an_unreadable_file_does_not_take_the_migration_with_it(self, tmp_path):
        """Defaults already cover it, and saving over it would lose the file."""
        from poa.config import load_config

        path = tmp_path / "config.yaml"
        path.write_text("signals: [this is not a mapping\n", encoding="utf-8")

        config = load_config(path)
        assert config.get("signals.min_shown_confidence") == 62


class TestWatchingOnlyTheChartsYouTrade:
    """Reading the same pair at eleven lengths is not eleven times the news.

    It is ten extra rows to scroll past, each a verdict about a chart nobody
    is looking at — and acting on one of those by mistake is a trade the
    engine never scored for that expiry.
    """

    def _app_charts(self, tmp_path, monkeypatch, wanted):
        from poa.config import load_config
        from poa.models import Candle, Series
        from poa.overlay import app as app_module
        from poa.overlay.app import OverlayApp
        import datetime

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        config.set("capture.source_chosen", True)
        config.set("market.scan_timeframes", wanted)
        config.set("market.min_candles", 20)
        monkeypatch.setattr(app_module, "data_root", lambda: tmp_path)

        start = datetime.datetime(2026, 8, 20, tzinfo=datetime.timezone.utc)
        candles = [
            Candle(timestamp=start + datetime.timedelta(seconds=5 * n),
                   open=1.1, high=1.2, low=1.0, close=1.1 + n * 0.0001)
            for n in range(1200)
        ]
        series = Series(candles=candles, symbol="EUR/USD", timeframe_seconds=5)

        app = OverlayApp(config)
        try:
            return {tf for _a, tf, _s in app._with_other_timeframes(
                [("EUR/USD", 5, series)])}
        finally:
            app.shutdown()

    def test_a_list_watches_only_what_is_on_it(self, tmp_path, monkeypatch):
        found = self._app_charts(tmp_path, monkeypatch, [5, 60])

        assert 60 in found
        # The chart itself always survives; the rest were asked not to.
        assert found <= {5, 60}

    def test_true_still_watches_everything(self, tmp_path, monkeypatch):
        """The old setting has to keep meaning what it meant."""
        found = self._app_charts(tmp_path, monkeypatch, True)

        assert len(found) > 3
        assert {10, 15, 30, 60} <= found

    def test_false_watches_only_the_open_chart(self, tmp_path, monkeypatch):
        assert self._app_charts(tmp_path, monkeypatch, False) == {5}

    def test_an_old_config_is_narrowed_by_the_migration(self, tmp_path):
        import yaml

        from poa.config import load_config

        path = tmp_path / "config.yaml"
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump({"market": {"scan_timeframes": True}}, handle)

        assert load_config(path).get("market.scan_timeframes") == [5, 60]

    def test_but_a_list_the_user_wrote_is_kept(self, tmp_path):
        import yaml

        from poa.config import load_config

        path = tmp_path / "config.yaml"
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump({"market": {"scan_timeframes": [15, 300]}}, handle)

        assert load_config(path).get("market.scan_timeframes") == [15, 300]


class TestTheShownGateMovesToSixtyTwo:
    """Set for rate, not accuracy, and the trade-off was measured first.

    On the recorded market 62 is a call about once a minute per pair against
    once every two at 85, winning 66.7% in one half hour and 54.8% in another
    — and beating always-buy in neither. It changes how often the tool speaks,
    not how often it is right, and that is what it was chosen for.

    85 was the previous default and nobody picked it: it was fitted to
    twenty-nine calls and did not survive the next batch.
    """

    def _config(self, tmp_path, signals=None):
        import yaml

        from poa.config import load_config

        path = tmp_path / "config.yaml"
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump({"signals": signals} if signals else {}, handle)
        return load_config(path)

    def test_a_file_that_never_had_the_key_gets_sixty_two(self, tmp_path):
        assert self._config(tmp_path).get("signals.min_shown_confidence") == 62

    def test_the_old_default_is_moved_on(self, tmp_path):
        """85 was never a choice, so leaving it would strand every install."""
        config = self._config(tmp_path, {"min_shown_confidence": 85})

        assert config.get("signals.min_shown_confidence") == 62

    def test_a_number_the_user_picked_is_left_alone(self, tmp_path):
        config = self._config(tmp_path, {"min_shown_confidence": 90})

        assert config.get("signals.min_shown_confidence") == 90

    def test_the_internal_gate_is_untouched(self, tmp_path):
        """Only the displayed number moved. The direction score still gates
        at 75, which is what the panel's score is derived from."""
        config = self._config(tmp_path, {"min_shown_confidence": 85})

        assert config.get("signals.min_confidence") == 75

    def test_the_shipped_default_and_the_example_file_agree(self):
        """A config.example.yaml saying 85 while the code says 62 is how
        somebody ends up debugging a setting that was never in effect."""
        import yaml

        from poa.config import DEFAULTS

        example = yaml.safe_load(
            (Path(__file__).resolve().parent.parent / "config.example.yaml")
            .read_text(encoding="utf-8")
        )
        assert (
            example["signals"]["min_shown_confidence"]
            == DEFAULTS["signals"]["min_shown_confidence"]
            == 62
        )


class TestTheWaitingIsCutForShortTrades:
    """Every delay was tuned when a call arrived every few minutes.

    On a thirty-second trade off a five-second chart each one is a slice of
    the trade spent doing nothing: two seconds before the open chart is
    re-read, 2.4 watching a scan animation, a minute of silence on a chart
    that just signalled, two minutes on the alert.

    And one that was not about speed at all — alerts gated at 75 while the
    panel showed calls at 62, so every call between the two appeared without
    a sound. On a thirty-second trade a silent call is a missed one.
    """

    def _config(self, tmp_path, data=None):
        import yaml

        from poa.config import load_config

        path = tmp_path / "config.yaml"
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(data or {}, handle)
        return load_config(path)

    def test_the_old_defaults_are_all_moved_on(self, tmp_path):
        config = self._config(tmp_path, {
            "capture": {"poll_seconds": 2.0},
            "overlay": {"scan_seconds": 2.4},
            "signals": {"cooldown_seconds": 60, "min_shown_confidence": 85},
            "alerts": {"cooldown_seconds": 120, "min_confidence": 75},
        })

        assert config.get("capture.poll_seconds") == 0.5
        assert config.get("overlay.scan_seconds") == 0.6
        assert config.get("signals.cooldown_seconds") == 15
        assert config.get("alerts.cooldown_seconds") == 20
        assert config.get("alerts.min_confidence") == 62

    def test_a_number_the_user_chose_is_left_alone(self, tmp_path):
        """Somebody who set a slow poll on purpose keeps it."""
        config = self._config(tmp_path, {
            "capture": {"poll_seconds": 5.0},
            "alerts": {"cooldown_seconds": 300},
        })

        assert config.get("capture.poll_seconds") == 5.0
        assert config.get("alerts.cooldown_seconds") == 300

    def test_alerts_fire_at_the_gate_the_panel_shows(self, tmp_path):
        """The two must agree or the tool calls without saying so."""
        config = self._config(tmp_path)

        assert (
            config.get("alerts.min_confidence")
            == config.get("signals.min_shown_confidence")
        )

    def test_a_corrupt_value_does_not_stop_the_migration(self, tmp_path):
        config = self._config(tmp_path, {
            "capture": {"poll_seconds": "soon"},
            "alerts": {"cooldown_seconds": 120},
        })

        # The unreadable one is left for the loader's own defaulting; the rest
        # still move, because one bad key is not a reason to strand the file.
        assert config.get("alerts.cooldown_seconds") == 20


class TestTheSilentDefaultIsMovedOn:
    """Sound was off by default for as long as Windows had none to offer.

    No bundled player, and a desktop path that needed a package the build
    never carried: every Windows install was silent however the settings
    were set. Both channels now need nothing installed, so the old silent
    default moves on — and a silence chosen after the move stays chosen.
    """

    def _config(self, tmp_path, data):
        import yaml

        from poa.config import load_config

        path = tmp_path / "config.yaml"
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle)
        return load_config(path)

    def test_an_older_file_still_on_silent_gets_sound(self, tmp_path):
        config = self._config(
            tmp_path, {"config_version": 2, "alerts": {"sound": False}}
        )
        assert config.get("alerts.sound") is True

    def test_an_older_file_that_never_chose_lands_on_the_new_default(self, tmp_path):
        config = self._config(tmp_path, {"config_version": 2, "alerts": {}})
        assert config.get("alerts.sound") is True

    def test_silence_chosen_after_the_move_is_kept(self, tmp_path):
        config = self._config(
            tmp_path, {"config_version": 3, "alerts": {"sound": False}}
        )
        assert config.get("alerts.sound") is False

    def test_the_default_is_now_on(self):
        from poa.config import DEFAULTS

        assert DEFAULTS["alerts"]["sound"] is True
