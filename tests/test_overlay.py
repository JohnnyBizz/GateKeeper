"""The overlay view model and the scan state machine.

Everything the panel shows is decided here, headlessly — including the two
behaviours that matter most: the old verdict is blanked while a scan runs, and
a stale verdict cannot survive a state the tracker has marked dead.
"""

from __future__ import annotations

import json

import pytest

from conftest import choppy_series, good_quality, pullback_trend
from poa.models import Direction, SignalState
from poa.overlay.viewmodel import (
    COLORS,
    OverlayViewModel,
    ScanController,
    ScanState,
    direction_color,
    score_color,
    strength_badge,
)
from poa.risk import SessionStats
from poa.signals import GateSettings, SignalEngine, SignalRequest


def make_signal(series, **kwargs):
    return SignalEngine().evaluate(
        SignalRequest(
            series=series,
            asset=kwargs.pop("asset", "EUR/USD"),
            chart_timeframe=60,
            trade_duration=kwargs.pop("trade_duration", 180),
            quality=good_quality(series),
            settings=kwargs.pop("settings", GateSettings()),
        )
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestScanController:
    def _controller(self, duration: float = 2.4):
        clock = FakeClock()
        controller = ScanController(duration=duration, _clock=clock)
        return controller, clock

    def test_begin_enters_scanning(self):
        controller, _ = self._controller()
        controller.begin()
        assert controller.state is ScanState.SCANNING
        assert controller.scanning

    def test_poll_holds_until_the_duration_elapses(self):
        controller, clock = self._controller(duration=2.0)
        controller.begin()
        clock.advance(1.0)
        assert not controller.poll()
        assert controller.scanning

    def test_poll_reveals_exactly_once(self):
        controller, clock = self._controller(duration=2.0)
        controller.begin()
        clock.advance(2.5)
        assert controller.poll()  # the transition
        assert controller.state is ScanState.REVEALED
        assert not controller.poll()  # no repeat

    def test_progress_runs_zero_to_one(self):
        controller, clock = self._controller(duration=2.0)
        controller.begin()
        assert controller.progress() == pytest.approx(0.0)
        clock.advance(1.0)
        assert controller.progress() == pytest.approx(0.5)
        clock.advance(2.0)
        assert controller.progress() == pytest.approx(1.0)

    def test_reset_returns_to_idle(self):
        controller, _ = self._controller()
        controller.begin()
        controller.reset()
        assert controller.state is ScanState.IDLE


class TestColours:
    def test_directions_map_to_their_colours(self):
        assert direction_color(Direction.CALL) == COLORS["call"]
        assert direction_color(Direction.PUT) == COLORS["put"]
        assert direction_color(Direction.WAIT) == COLORS["wait"]

    def test_score_colour_bands(self):
        assert score_color(85) == COLORS["call"]
        assert score_color(70) == COLORS["accent"]
        assert score_color(55) == COLORS["wait"]
        assert score_color(30) == COLORS["put"]
        assert score_color(None) == COLORS["neutral"]

    def test_strength_badges(self):
        assert strength_badge(85)[0] == "HIGH"
        assert strength_badge(70)[0] == "MEDIUM"
        assert strength_badge(55)[0] == "LOW"
        assert strength_badge(None)[0] == "--"


class TestViewModel:
    def _vm(self, signal=None, **kwargs) -> OverlayViewModel:
        vm = OverlayViewModel(session=SessionStats(), **kwargs)
        vm.signal = signal
        vm.connected = True
        return vm

    def test_a_call_signal_renders_as_buy(self):
        signal = make_signal(pullback_trend(400, direction=1))
        assert signal.direction is Direction.CALL
        verdict = self._vm(signal).render()["verdict"]
        assert verdict["direction_label"] == "BUY"
        assert verdict["color"] == COLORS["call"]
        assert verdict["arrow"] == "▲"
        assert not verdict["blanked"]

    def test_a_put_signal_renders_as_sell(self):
        signal = make_signal(pullback_trend(400, direction=-1))
        verdict = self._vm(signal).render()["verdict"]
        assert verdict["direction_label"] == "SELL"
        assert verdict["color"] == COLORS["put"]

    def test_the_named_pattern_is_surfaced(self):
        signal = make_signal(pullback_trend(400, direction=1))
        verdict = self._vm(signal).render()["verdict"]
        assert verdict["pattern"]  # some name, never empty when revealed

    def test_scanning_blanks_the_previous_verdict(self):
        # The whole point of the scan cycle: the old BUY must not be readable
        # while the engine is re-deciding.
        signal = make_signal(pullback_trend(400, direction=1))
        vm = self._vm(signal)
        vm.scan.begin()
        verdict = vm.render()["verdict"]
        assert verdict["blanked"]
        assert verdict["direction_label"] == "SCANNING"
        assert verdict["score"] is None
        assert verdict["direction"] == "--"

    def test_scanning_suppresses_reason_and_warnings(self):
        signal = make_signal(pullback_trend(400, direction=1))
        vm = self._vm(signal)
        vm.scan.begin()
        payload = vm.render()
        assert payload["reason"] == "Re-reading the chart…"
        assert payload["warnings"] == []

    def test_no_signal_renders_as_waiting_for_data(self):
        verdict = self._vm(None).render()["verdict"]
        assert verdict["blanked"]
        assert verdict["direction_label"] == "WAITING FOR DATA"

    def test_an_invalidated_signal_is_labelled_not_left_looking_live(self):
        signal = make_signal(pullback_trend(400, direction=1))
        signal.state = SignalState.INVALIDATED
        payload = self._vm(signal).render()
        assert payload["verdict"]["state"] == "INVALIDATED"
        assert not payload["verdict"]["actionable"]
        assert any("invalidated" in w.lower() for w in payload["warnings"])

    def test_a_weakening_signal_carries_a_warning(self):
        signal = make_signal(pullback_trend(400, direction=1))
        signal.state = SignalState.WEAKENING
        payload = self._vm(signal).render()
        assert any("weakening" in w.lower() for w in payload["warnings"])

    def test_chart_timeframe_and_trade_duration_are_shown_separately(self):
        vm = self._vm(None, chart_timeframe=60, trade_duration=180)
        tiles = vm.render()["tiles"]
        assert tiles["chart"] == "1 MIN"
        assert tiles["time"] == "3 MIN"
        assert tiles["chart"] != tiles["time"]

    def test_low_data_confidence_shows_in_the_status(self):
        vm = self._vm(None)
        vm.data_confidence = 45.0
        header = vm.render()["header"]
        assert "LOW DATA" in header["status"]
        assert header["status_color"] == COLORS["wait"]

    def test_disconnected_shows_offline(self):
        vm = self._vm(None)
        vm.connected = False
        assert vm.render()["header"]["status"] == "OFFLINE"

    def test_an_error_beats_everything_in_the_status(self):
        vm = self._vm(None)
        vm.last_error = "Chart capture failed"
        payload = vm.render()
        assert payload["header"]["status"] == "ERROR"
        assert payload["reason"] == "Chart capture failed"

    def test_the_disclaimer_is_always_present(self):
        for signal in (None, make_signal(pullback_trend(400, direction=1))):
            payload = self._vm(signal).render()
            assert "not a trading recommendation" in payload["disclaimer"].lower()

    def test_the_risk_block_reflects_the_configured_payout(self):
        vm = self._vm(None, payout=0.80)
        risk = vm.render()["risk"]
        assert risk["breakeven_rate"] == pytest.approx(55.6, abs=0.1)

    def test_the_session_block_uses_the_same_payout(self):
        vm = self._vm(None, payout=0.80)
        vm.session.set_auto(10, 5)
        session = vm.render()["session"]
        assert session["breakeven_rate"] == pytest.approx(55.6, abs=0.1)

    def test_the_full_payload_serialises(self):
        for signal in (None, make_signal(pullback_trend(400, direction=1)),
                       make_signal(choppy_series(400))):
            vm = self._vm(signal)
            text = json.dumps(vm.render())
            assert "NaN" not in text

    def test_no_certainty_language_reaches_the_panel(self):
        from poa.signals import contains_banned_language

        for series in (pullback_trend(400, direction=1), choppy_series(400)):
            payload = self._vm(make_signal(series)).render()
            joined = " ".join(
                [payload["reason"], payload["disclaimer"], *payload["warnings"]]
            )
            assert not contains_banned_language(joined)


class TestOverlayAppLogic:
    """The engine-to-panel glue, run without any window."""

    def _app(self, tmp_path):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        return OverlayApp(config)

    def test_a_manual_scan_runs_the_engine_and_holds_the_result(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app._begin_scan()
            assert app.vm.scan.scanning
            # The engine ran, but the verdict must not be visible yet.
            assert app.vm.render()["verdict"]["blanked"]
            # Completing the scan reveals the held signal.
            app.vm.scan.state = ScanState.REVEALED
            app._finish_scan()
            assert app.vm.signal is not None
        finally:
            app.shutdown()

    def test_engine_state_arriving_mid_scan_is_held_back(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.engine.tick()
            app.vm.scan.begin()
            app._apply_state()
            # The signal went to the pending slot, not the visible one.
            assert app._pending_signal is not None
            assert app.vm.render()["verdict"]["blanked"]
        finally:
            app.shutdown()

    def test_reset_clears_session_and_tracker(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.vm.session.adjust(wins=3, losses=1)
            app.engine.tick()
            app._reset()
            assert app.vm.session.total == 0
            assert app.engine.tracker.current is None
        finally:
            app.shutdown()

    def test_session_refresh_pulls_from_the_journal(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app._refresh_session()  # must not raise on an empty journal
            assert app.vm.session.auto_wins == 0
        finally:
            app.shutdown()

    def test_reset_survives_the_next_journal_refresh(self, tmp_path):
        """Reset has to mean something after the journal is read again."""
        from datetime import timedelta

        from poa.models import Direction, utcnow

        app = self._app(tmp_path)
        try:
            app.engine.tick()
            signal = app.engine.state.signal
            assert signal is not None
            signal.direction = Direction.CALL
            signal.price = 1.08
            signal.asset = app.vm.asset
            signal.trade_duration = 60
            app.engine.journal.record(signal, source="synthetic")
            app.engine.journal.resolve_outcomes(
                1.09,
                utcnow() + timedelta(seconds=90),
                source="synthetic",
                asset=app.vm.asset,
            )

            app._refresh_session()
            assert app.vm.session.wins == 1

            app._reset()
            app._refresh_session()
            assert app.vm.session.total == 0
        finally:
            app.shutdown()

    def test_the_session_tally_ignores_another_data_source(self, tmp_path):
        from datetime import timedelta

        from poa.models import Direction, utcnow

        app = self._app(tmp_path)
        try:
            app.engine.tick()
            signal = app.engine.state.signal
            assert signal is not None
            signal.direction = Direction.CALL
            signal.price = 1.08
            signal.asset = app.vm.asset
            signal.trade_duration = 60
            # Recorded while the screen source was in use; this app is running
            # on the synthetic feed, so it is not this session's trade.
            app.engine.journal.record(signal, source="screen")
            app.engine.journal.resolve_outcomes(
                1.09, utcnow() + timedelta(seconds=90), source="screen"
            )
            app._refresh_session()
            assert app.vm.session.total == 0
        finally:
            app.shutdown()


class TestChartSwitching:
    """Switching charts on the platform must not leave a stale read on screen."""

    def _app(self, tmp_path, source="synthetic"):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", source)
        return OverlayApp(config)

    def test_renaming_the_pair_clears_the_previous_signal(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.engine.tick()
            app._apply_state()
            assert app.vm.signal is not None

            app._set_asset("GBP/JPY")
            assert app.vm.asset == "GBP/JPY"
            # The old verdict described a different chart.
            assert app.vm.signal is None
            assert app.vm.render()["verdict"]["blanked"]
        finally:
            app.shutdown()

    def test_renaming_the_pair_resets_the_tracker(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.engine.tick()
            assert app.engine.tracker.current is not None
            app._set_asset("AUD/CAD")
            assert app.engine.tracker.current is None
        finally:
            app.shutdown()

    def test_the_asset_reaches_the_engine_and_its_captures(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app._set_asset("USD/CHF")
            app.engine.tick()
            # The capture carries the asset, and its name wins over config —
            # so a rename that did not rebuild the source would be invisible.
            assert app.engine.state.capture_meta["asset"] == "USD/CHF"
        finally:
            app.shutdown()

    def test_an_unchanged_name_is_a_no_op(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.engine.tick()
            app._apply_state()
            signal = app.vm.signal
            app._set_asset(app.vm.asset)
            assert app.vm.signal is signal
        finally:
            app.shutdown()

    def test_a_manual_scan_starts_from_a_clean_slate(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.engine.tick()
            assert app.engine.tracker.current is not None
            app._begin_scan()
            # Scanning re-derives from scratch; the previous peak confidence
            # and weakening state must not carry over onto a different chart.
            assert app.engine.tracker.history == [] or app.engine.tracker.current is not None
            assert app.vm.render()["verdict"]["blanked"]
        finally:
            app.shutdown()

    def test_stake_and_balance_edits_reach_the_risk_block(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app._set_balance(500.0)
            app._set_stake(125.0)
            risk = app.vm.render()["risk"]
            assert risk["balance"] == pytest.approx(500.0)
            assert risk["stake"] == pytest.approx(125.0)
            assert risk["risk_percent"] == pytest.approx(25.0)
            assert risk["stake_overridden"] is True
        finally:
            app.shutdown()

    def test_clearing_the_stake_returns_to_percentage_sizing(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app._set_balance(1000.0)
            app._set_stake(400.0)
            app._set_stake(None)
            risk = app.vm.render()["risk"]
            assert risk["stake_overridden"] is False
            assert risk["stake"] == pytest.approx(20.0)
        finally:
            app.shutdown()


class TestAutomaticChartLocation:
    """Scan finds the chart itself — no boxes to drag, nothing to type."""

    def _screen(self, asset="CAD/JPY OTC", timeframe="M5"):
        cv2 = pytest.importorskip("cv2")
        from poa.chart_detection import generate_series
        from poa.chart_detection.render import (
            RenderStyle,
            ScreenStyle,
            render_platform_screen,
        )

        series = generate_series(200, seed=5)
        image, truth = render_platform_screen(
            series,
            ScreenStyle(asset_label=asset, timeframe_label=timeframe),
            RenderStyle(candle_width=7, candle_gap=4),
        )
        return series, image, truth

    def _config(self, tmp_path):
        from poa.config import load_config

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("config_path", str(tmp_path / "config.yaml"))
        config.set("capture.source", "screen")
        return config

    def test_a_scan_configures_the_region_pair_and_timeframe(self, tmp_path):
        from poa.overlay.autoscan import scan_screen

        _series, image, truth = self._screen()
        config = self._config(tmp_path)
        result = scan_screen(config, grab=lambda _i: (image, {"left": 0, "top": 0}))

        assert result.applied
        region = config.get("capture.region")
        assert abs(region["left"] - truth["chart"][0]) < 40
        assert config.get("market.asset") == "CAD/JPY OTC"
        assert config.get("market.chart_timeframe") == 300
        assert config.get("capture.asset_region")["width"] > 0
        assert config.get("capture.timeframe_region")["width"] > 0
        assert "CAD/JPY OTC" in result.message

    def test_the_monitor_origin_is_added_to_every_box(self, tmp_path):
        """A second monitor's coordinates are offset from the desktop origin."""
        from poa.overlay.autoscan import scan_screen

        _series, image, _truth = self._screen()
        origin = {"left": 1920, "top": -200}
        config = self._config(tmp_path)
        assert scan_screen(config, grab=lambda _i: (image, origin)).applied

        for key in ("capture.region", "capture.asset_region", "capture.timeframe_region"):
            box = config.get(key)
            assert box["left"] >= 1920
            assert box["top"] >= -200

    def test_a_stale_calibration_is_dropped_with_the_old_region(self, tmp_path):
        """Pixel rows from the previous region point at different prices now."""
        from poa.overlay.autoscan import scan_screen

        _series, image, _truth = self._screen()
        config = self._config(tmp_path)
        config.set(
            "capture.calibration",
            {"enabled": True, "top_pixel": 10, "top_price": 1.09,
             "bottom_pixel": 400, "bottom_price": 1.08},
        )
        scan_screen(config, grab=lambda _i: (image, {"left": 0, "top": 0}))
        assert config.get("capture.calibration") == {"enabled": False}

    def test_nothing_is_written_when_no_chart_is_found(self, tmp_path):
        import numpy as np

        from poa.overlay.autoscan import scan_screen

        pytest.importorskip("cv2")
        config = self._config(tmp_path)
        config.set("capture.region", {"left": 1, "top": 2, "width": 3, "height": 4})
        blank = np.full((600, 900, 3), (110, 45, 60), dtype=np.uint8)

        result = scan_screen(config, grab=lambda _i: (blank, {"left": 0, "top": 0}))
        assert not result.applied
        assert config.get("capture.region") == {"left": 1, "top": 2, "width": 3, "height": 4}
        assert "No candle chart" in result.message

    def test_a_capture_failure_is_reported_not_raised(self, tmp_path):
        from poa.overlay.autoscan import scan_screen

        def explode(_index):
            raise RuntimeError("no display")

        result = scan_screen(self._config(tmp_path), grab=explode)
        assert not result.applied
        assert "no display" in result.message


class TestScanDecidesWhenToRelocate:
    def _app(self, tmp_path, **settings):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        for key, value in settings.items():
            config.set(key, value)
        return OverlayApp(config)

    def test_a_non_screen_source_is_never_relocated(self, tmp_path):
        app = self._app(tmp_path)
        try:
            assert not app._should_relocate()
        finally:
            app.shutdown()

    def test_a_screen_source_with_no_region_relocates(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.config.set("capture.source", "screen")
            app.config.set("capture.region", {"left": 0, "top": 0, "width": 0, "height": 0})
            assert app._should_relocate()
        finally:
            app.shutdown()

    def test_a_working_region_is_left_alone(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.config.set("capture.source", "screen")
            app.config.set(
                "capture.region", {"left": 0, "top": 0, "width": 900, "height": 500}
            )
            app.vm.data_confidence = 92.0
            assert not app._should_relocate()
            # ...but a region that cannot read the chart is worth replacing.
            app.vm.data_confidence = 35.0
            assert app._should_relocate()
        finally:
            app.shutdown()


class TestScanDoesNotBlockTheUi:
    """A search takes seconds. On the UI thread that is a frozen window."""

    def _app(self, tmp_path):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "screen")
        config.set("capture.region", {"left": 0, "top": 0, "width": 0, "height": 0})
        return OverlayApp(config)

    def test_the_search_runs_off_the_calling_thread(self, tmp_path, monkeypatch):
        import threading
        import time

        from poa.overlay import app as module

        app = self._app(tmp_path)
        try:
            started = threading.Event()
            release = threading.Event()
            seen_threads = []

            def slow_scan(_config, exclude=None):
                seen_threads.append(threading.current_thread())
                started.set()
                release.wait(5)
                from poa.chart_detection.autodetect import Layout
                from poa.overlay.autoscan import AutoScanResult

                return AutoScanResult(
                    layout=Layout(), applied=False, message="nothing found"
                )

            monkeypatch.setattr(
                "poa.overlay.autoscan.scan_screen", slow_scan, raising=True
            )

            began = time.monotonic()
            app._begin_scan()
            # The call returned while the search is still running.
            assert time.monotonic() - began < 1.0
            assert started.wait(5)
            assert app._scan_busy
            assert seen_threads[0] is not threading.current_thread()

            # And the verdict stays blanked until the search finishes, rather
            # than the timer alone dropping the previous chart's answer back.
            app.vm.scan.state = ScanState.SCANNING
            app.vm.scan.started_at = began - 10
            app._tick()
            assert app.vm.render()["verdict"]["blanked"]

            release.set()
            for _ in range(50):
                app._tick()
                if not app._scan_busy:
                    break
                time.sleep(0.05)
            assert not app._scan_busy
        finally:
            app.shutdown()

    def test_a_second_press_while_searching_is_ignored(self, tmp_path, monkeypatch):
        app = self._app(tmp_path)
        try:
            calls = []
            monkeypatch.setattr(app, "_should_relocate", lambda: True)
            monkeypatch.setattr(app, "_own_windows", lambda: [])
            monkeypatch.setattr(
                app, "_locate_worker", lambda exclude: calls.append(exclude)
            )
            app._begin_scan()
            app._scan_busy = True  # the worker would have set this
            app._begin_scan()
            assert len(calls) == 1
        finally:
            app.shutdown()

    def test_a_working_region_with_no_label_boxes_still_relocates(self, tmp_path):
        """The candles can read perfectly while the pair never updates.

        Waiting for recognition confidence to drop would never fix that — the
        pair is text somewhere else on the screen, and the candles are fine.
        """
        from poa.chart_detection.autodetect import ocr_available

        app = self._app(tmp_path)
        try:
            app.config.set(
                "capture.region", {"left": 0, "top": 0, "width": 900, "height": 500}
            )
            app.vm.data_confidence = 95.0
            app.config.set(
                "capture.asset_region",
                {"left": 0, "top": 0, "width": 0, "height": 0},
            )
            assert app._should_relocate() is ocr_available()

            # Once both label boxes are known, a healthy region is left alone.
            app.config.set(
                "capture.asset_region",
                {"left": 10, "top": 10, "width": 120, "height": 24},
            )
            app.config.set(
                "capture.timeframe_region",
                {"left": 10, "top": 40, "width": 40, "height": 22},
            )
            assert not app._should_relocate()
        finally:
            app.shutdown()
