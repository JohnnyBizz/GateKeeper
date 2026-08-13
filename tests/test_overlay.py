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
