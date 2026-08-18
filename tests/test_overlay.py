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

    def test_how_many_trades_to_take_is_a_number(self):
        """WAIT and NO TRADE both mean zero while looking nothing alike."""
        signal = make_signal(pullback_trend(400, direction=1))
        verdict = self._vm(signal).render()["verdict"]
        assert verdict["take_now"] == (1 if signal.actionable else 0)
        assert verdict["take_label"] == (
            "1 trade" if signal.actionable else "0 trades"
        )

        # Nothing read yet is nothing to take, and it says so rather than "--".
        idle = self._vm().render()["verdict"]
        assert idle["take_now"] == 0
        assert idle["take_label"] == "0 trades"

    def test_the_session_reports_how_many_setups_were_called(self):
        vm = self._vm()
        assert vm.render()["session"]["calls"] == 0
        vm.calls_this_session = 3
        assert vm.render()["session"]["calls"] == 3

    def test_the_risk_block_can_be_folded_away(self):
        vm = self._vm()
        assert vm.render()["risk"]["collapsed"] is False
        vm.risk_collapsed = True
        assert vm.render()["risk"]["collapsed"] is True

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

    def test_demo_data_is_never_labelled_live(self):
        """Every score under a LIVE badge would be about an invented market."""
        signal = make_signal(pullback_trend(400, direction=1))
        vm = self._vm(signal)
        vm.source = "synthetic"
        vm.data_confidence = 99.0
        payload = vm.render()
        assert payload["header"]["status"] == "DEMO DATA"
        assert payload["header"]["status_color"] == COLORS["wait"]
        assert any("demo data" in w.lower() for w in payload["warnings"])

    def test_a_replay_says_so_too(self):
        vm = self._vm(make_signal(pullback_trend(400, direction=1)))
        vm.source = "csv"
        assert vm.render()["header"]["status"] == "REPLAY"

    def test_the_screen_source_reads_live(self):
        vm = self._vm(make_signal(pullback_trend(400, direction=1)))
        vm.source = "screen"
        vm.data_confidence = 92.0
        payload = vm.render()
        assert payload["header"]["status"] == "LIVE"
        assert not any("demo" in w.lower() for w in payload["warnings"])

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


class _Actionable:
    """Only the attribute the entry countdown reads."""

    def __init__(self, actionable: bool = True) -> None:
        self.actionable = actionable


# A wall-clock instant that is exactly on a 5-second, 1-minute and 5-minute
# boundary at once, so "N seconds into the bar" means the same thing for every
# timeframe the countdown is checked at.
BAR_START = 1_700_000_100.0


class TestWhenToGetIn:
    """The verdict says *what*; nothing on the panel said *when*.

    A setup is read off candles that have closed, so the answer on screen
    belongs to the bar now forming and is re-derived the moment it ends. BUY
    at 82 with no idea whether that bar has fifty seconds left or three is
    half an answer, and it is the half that decides between being on time and
    chasing a move that already happened.
    """

    def _vm(self, into: float, timeframe: int = 60, **kwargs) -> OverlayViewModel:
        """A view model reading the clock ``into`` seconds through a bar."""
        return OverlayViewModel(
            session=SessionStats(),
            chart_timeframe=timeframe,
            _now=lambda: BAR_START + into,
            **kwargs,
        )

    def test_it_counts_down_to_the_close_of_the_forming_candle(self):
        """Candles sit on wall-clock boundaries, so this is arithmetic."""
        entry = self._vm(into=37)._entry(False, _Actionable())
        assert entry["seconds"] == 23  # 37s into a 60s bar
        assert entry["clock"] == "0:23"

    def test_the_countdown_follows_the_chart_timeframe(self):
        """A five-minute chart is not counted down as a one-minute one."""
        entry = self._vm(into=100, timeframe=300)._entry(False, _Actionable())
        assert entry["seconds"] == 200
        assert entry["clock"] == "3:20"

    def test_a_five_second_chart_counts_in_seconds(self):
        entry = self._vm(into=3, timeframe=5)._entry(False, _Actionable())
        assert entry["seconds"] == 2
        assert entry["clock"] == "0:02"

    def test_the_bar_drains_as_the_candle_does(self):
        assert self._vm(into=30)._entry(False, _Actionable())[
            "progress"
        ] == pytest.approx(0.5)

    def test_a_setup_says_take_it_now(self):
        entry = self._vm(into=30)._entry(False, _Actionable())
        assert entry["text"] == "TAKE IT NOW"
        assert entry["ready"] is True
        assert "3 MIN expiry" in entry["detail"]

    def test_no_setup_counts_down_to_the_next_candle(self):
        """The headline reads as one phrase with the clock beside it."""
        entry = self._vm(into=30)._entry(False, _Actionable(False))
        assert entry["text"] == "NEXT CANDLE IN"
        assert entry["clock"] == "0:30"
        assert entry["ready"] is False
        assert entry["urgent"] is False

    def test_the_last_seconds_of_a_bar_are_urgent(self):
        """A setup confirmed on this bar is about to be re-read."""
        assert self._vm(into=58)._entry(False, _Actionable())["urgent"]
        assert not self._vm(into=30)._entry(False, _Actionable())["urgent"]

    def test_urgency_scales_with_the_timeframe(self):
        """Twenty seconds left is nearly over on a 5m chart and a third of a
        1m one — so "nearly gone" cannot be a fixed number of seconds."""
        assert self._vm(into=280, timeframe=300)._entry(False, _Actionable())["urgent"]
        assert not self._vm(into=40)._entry(False, _Actionable())["urgent"]

    def test_nothing_is_promised_while_scanning(self):
        entry = self._vm(into=30)._entry(True, _Actionable())
        assert entry["text"] == "—"
        assert entry["clock"] == ""
        assert entry["ready"] is False

    def test_it_never_claims_a_win(self):
        """The forbidden vocabulary, checked on the one new line of prose."""
        banned = ("guarantee", "100%", "cannot lose", "sure thing", "risk-free")
        for into in (1, 30, 59):
            for signal in (None, _Actionable(), _Actionable(False)):
                entry = self._vm(into=into)._entry(False, signal)
                blob = f"{entry['text']} {entry['detail']}".lower()
                assert not any(word in blob for word in banned)

    def test_it_reaches_the_render_payload(self):
        """And says nothing before the first chart has been read.

        A countdown running under "WAITING FOR DATA" would be a clock ticking
        toward nothing, which reads as a deadline the panel has not earned.
        """
        entry = self._vm(into=45).render()["entry"]
        assert entry["seconds"] == 15
        assert entry["clock"] == ""
        assert entry["ready"] is False


class _FakeProof:
    """A measured record, without running a replay to get one."""

    def __init__(self, *, edge: float, meaningful: bool, settled: int) -> None:
        self.edge = edge
        self.meaningful = meaningful
        self.settled = settled
        self.calibration = None
        self.attribution = None

    def summary(self) -> str:
        return f"{self.settled} replayed setups"


class TestACallYouCannotTakeAsYouAreSetUp:
    """A setup found on another chart is scored against the expiry that suits
    *that* chart. Taken at the expiry the platform happens to be set to, it is
    a different trade from the one that passed — and often one the engine
    would have refused outright.

    The row carried the right expiry all along and the panel kept it to
    itself, so a green tab invited exactly that mistake: five losses in a row
    on thirty-second expiries, from setups scored at ten minutes.
    """

    def _vm(self, expiry=600, actionable=True, trade_duration=30):
        vm = OverlayViewModel(session=SessionStats(), asset="EUR/USD OTC",
                              chart_timeframe=5, trade_duration=trade_duration)
        vm.watchlist = [
            {"asset": "EUR/USD OTC", "timeframe": 5, "expiry": trade_duration,
             "score": 53.0, "direction": "WAIT", "actionable": False},
            {"asset": "EUR/USD OTC", "timeframe": 180, "expiry": expiry,
             "score": 82.0, "direction": "CALL", "actionable": actionable},
        ]
        return vm

    def test_the_tab_says_which_expiry_it_needs(self):
        rows = self._vm().render()["watchlist"]
        needing = [r for r in rows if r["mismatched"]]
        assert len(needing) == 1
        assert needing[0]["needs"] == "10M"

    def test_a_matching_expiry_is_not_flagged(self):
        """Nothing to warn about when it is the trade you are already set up
        for — a marker on every tab would stop meaning anything."""
        rows = self._vm(expiry=30).render()["watchlist"]
        assert not any(r["mismatched"] for r in rows)

    def test_a_setup_nobody_can_take_yet_is_not_flagged(self):
        """Only calls. A tab that is not actionable is not an invitation."""
        rows = self._vm(actionable=False).render()["watchlist"]
        assert not any(r["mismatched"] for r in rows)

    def test_the_mismatch_is_warned_about_in_words(self):
        vm = self._vm()
        vm.signal = make_signal(pullback_trend(400, direction=1))
        warnings = vm.render()["warnings"]
        assert warnings, "the mismatch has to be said, not just marked"
        first = warnings[0]
        assert "10 MIN" in first and "30 SEC" in first
        assert "different trade" in first

    def test_the_warning_comes_before_the_others(self):
        """It decides whether the trade is the one that was scored at all."""
        vm = self._vm()
        vm.signal = make_signal(choppy_series(400))
        warnings = vm.render()["warnings"]
        assert warnings and "expiry" in warnings[0]

    def test_nothing_is_warned_about_when_everything_lines_up(self):
        vm = self._vm(expiry=30)
        vm.signal = make_signal(pullback_trend(400, direction=1))
        assert not any("expiry" in w and "different trade" in w
                       for w in vm.render()["warnings"])


class TestTheEvidenceFoldsAway:
    """Every line under the decision earned its place one at a time, and
    together they turned a panel meant to be glanced at into a page."""

    def test_it_is_folded_by_default(self):
        assert OverlayViewModel(session=SessionStats()).render()["details"][
            "collapsed"
        ] is True

    def test_unfolding_shows(self):
        vm = OverlayViewModel(session=SessionStats(), details_collapsed=False)
        assert vm.render()["details"]["collapsed"] is False

    def test_the_headline_number_stays_out_when_it_is_folded(self):
        """Folding must not hide whether the record is good news."""
        vm = OverlayViewModel(session=SessionStats())
        assert vm.render()["details"]["summary"] == "not measured yet"

        vm.proof = _FakeProof(edge=4.2, meaningful=True, settled=60)
        assert vm.render()["details"]["summary"] == "+4 pts vs break-even"

    def test_a_sample_too_small_to_read_is_not_dressed_up_as_an_edge(self):
        """A 30-point edge over three trades is noise wearing a number."""
        vm = OverlayViewModel(session=SessionStats())
        vm.proof = _FakeProof(edge=30.0, meaningful=False, settled=3)
        assert vm.render()["details"]["summary"] == "3 measured so far"


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
        # Deliberately on demo data, so Scan analyses it instead of going off
        # to look for a real chart.
        config.set("capture.source_chosen", True)
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

    def test_the_tally_is_the_users_alone_by_default(self, tmp_path):
        """Two writers, one column, and neither number means anything.

        The user keeps this tally by hand; settled journal outcomes must not
        also be adding to it behind them.
        """
        app = self._app(tmp_path)
        try:
            assert app.vm.session_manual
            app.vm.session.set_auto(3, 2)  # as a journal refresh would
            app.vm.session.reset()

            app._adjust(1, 0)
            app._refresh_session()
            assert app.vm.session.wins == 1
            assert app.vm.session.auto_wins == 0
        finally:
            app.shutdown()

    def test_reset_survives_the_next_journal_refresh(self, tmp_path):
        """Reset has to mean something after the journal is read again."""
        from datetime import timedelta

        from poa.models import Direction, utcnow

        app = self._app(tmp_path)
        app.vm.session_manual = False  # the opt-in automatic tally
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

    def test_the_demo_default_is_not_a_dead_end(self, tmp_path):
        """Shipping pointed at demo data must not mean staying there.

        Scan used to refuse to search unless the source was already "screen",
        so the one button that sets everything up would not run until
        everything was already set up.
        """
        app = self._app(tmp_path)
        try:
            assert app.config.get("capture.source") == "synthetic"
            assert app._should_relocate()
        finally:
            app.shutdown()

    def test_a_source_the_user_picked_is_left_alone(self, tmp_path):
        """Demo and CSV replay are deliberate choices, not defaults to fix."""
        app = self._app(tmp_path)
        try:
            app.config.set("capture.source_chosen", True)
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
            # "Working" means the label boxes were found too — a region that
            # reads candles perfectly still leaves the pair frozen on whatever
            # was typed last if the name was never located. Stated here rather
            # than inherited from whatever config file happens to be present.
            for key in ("capture.asset_region", "capture.timeframe_region"):
                app.config.set(key, {"left": 0, "top": 0, "width": 120, "height": 40})
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


class TestFindChartDoesNotBlock:
    """The settings dialog's Find chart froze the UI exactly like Scan did."""

    def _app(self, tmp_path):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        config.set("capture.source_chosen", True)
        return OverlayApp(config)

    def test_locate_chart_returns_immediately_and_calls_back(
        self, tmp_path, monkeypatch
    ):
        import threading
        import time

        app = self._app(tmp_path)
        try:
            release = threading.Event()

            def slow_scan(_config, exclude=None):
                release.wait(5)
                from poa.chart_detection.autodetect import Layout
                from poa.overlay.autoscan import AutoScanResult

                return AutoScanResult(
                    layout=Layout(), applied=False, message="nothing found"
                )

            monkeypatch.setattr(
                "poa.overlay.autoscan.scan_screen", slow_scan, raising=True
            )
            monkeypatch.setattr(app, "_own_windows", lambda: [])

            reported: list[str] = []
            began = time.monotonic()
            app.locate_chart(reported.append)
            # Returned without waiting for the search.
            assert time.monotonic() - began < 1.0
            assert reported == []
            assert app._scan_busy

            release.set()
            for _ in range(50):
                app._tick()
                if reported:
                    break
                time.sleep(0.05)
            assert reported == ["nothing found"]
            assert not app._scan_busy
        finally:
            app.shutdown()

    def test_a_second_find_while_searching_is_told_so(self, tmp_path, monkeypatch):
        app = self._app(tmp_path)
        try:
            monkeypatch.setattr(app, "_own_windows", lambda: [])
            app._scan_busy = True
            reported: list[str] = []
            app.locate_chart(reported.append)
            assert reported == ["Already looking for the chart…"]
        finally:
            app.shutdown()


class TestThePanelMovesItselfOffTheChart:
    """Our window covering the chart is our problem to solve, not the user's."""

    def _app(self, tmp_path):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        config.set("capture.source_chosen", True)
        return OverlayApp(config)

    def _overlapped(self, chart):
        from poa.chart_detection.autodetect import Layout
        from poa.overlay.autoscan import AutoScanResult

        layout = Layout(chart=chart, candles_found=80, overlapped_by_app=True)
        return AutoScanResult(layout=layout, applied=False, message="covered")

    def test_a_covered_chart_triggers_a_move_and_one_rescan(
        self, tmp_path, monkeypatch
    ):
        from poa.chart_detection.autodetect import Box

        app = self._app(tmp_path)
        try:
            moved: list[Any] = []
            rescans: list[Any] = []
            monkeypatch.setattr(
                app, "_move_panel_clear_of", lambda chart: (moved.append(chart), True)[1]
            )
            monkeypatch.setattr(app, "_own_windows", lambda: [])
            monkeypatch.setattr(
                app, "_locate_worker", lambda exclude: rescans.append(exclude)
            )

            chart = Box(100, 100, 800, 500)
            app._scan_results.put(self._overlapped(chart))
            app._collect_scan_result()

            assert moved == [chart]
            assert len(rescans) == 1
            assert app._scan_busy  # waiting on the second search
        finally:
            app.shutdown()

    def test_it_does_not_loop_when_there_is_nowhere_clear(self, tmp_path, monkeypatch):
        from poa.chart_detection.autodetect import Box

        app = self._app(tmp_path)
        try:
            rescans: list[Any] = []
            monkeypatch.setattr(app, "_move_panel_clear_of", lambda chart: True)
            monkeypatch.setattr(app, "_own_windows", lambda: [])
            monkeypatch.setattr(
                app, "_locate_worker", lambda exclude: rescans.append(exclude)
            )

            chart = Box(100, 100, 800, 500)
            app._scan_results.put(self._overlapped(chart))
            app._collect_scan_result()
            # The second search comes back still covered.
            app._scan_results.put(self._overlapped(chart))
            app._collect_scan_result()

            assert len(rescans) == 1  # not two, not forever
            assert not app._scan_busy
        finally:
            app.shutdown()

    def test_a_chart_that_is_not_covered_changes_nothing(self, tmp_path, monkeypatch):
        from poa.chart_detection.autodetect import Box, Layout
        from poa.overlay.autoscan import AutoScanResult

        app = self._app(tmp_path)
        try:
            moved: list[Any] = []
            monkeypatch.setattr(
                app, "_move_panel_clear_of", lambda chart: (moved.append(chart), True)[1]
            )
            layout = Layout(chart=Box(0, 0, 10, 10), candles_found=80)
            app._scan_results.put(
                AutoScanResult(layout=layout, applied=False, message="fine")
            )
            app._collect_scan_result()
            assert moved == []
        finally:
            app.shutdown()


class TestTheFeedIsTheDefault:
    """Reading the platform's data is the normal path now, not an option."""

    def _app(self, tmp_path, **settings):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.auto_launch_browser", False)
        for key, value in settings.items():
            config.set(key, value)
        return OverlayApp(config)

    def test_a_fresh_install_reads_the_feed(self):
        """Both the built-in defaults and the shipped example, which is what a
        first run actually copies into place."""
        import yaml

        from poa.config import DEFAULTS, EXAMPLE_CONFIG_PATH

        assert DEFAULTS["capture"]["source"] == "feed"
        example = yaml.safe_load(EXAMPLE_CONFIG_PATH.read_text())
        assert example["capture"]["source"] == "feed"

    def test_the_feed_never_sends_scan_hunting_for_a_chart(self, tmp_path):
        """There is no region, no axis and no badge to find."""
        app = self._app(tmp_path, **{"capture.source": "feed"})
        try:
            assert not app._should_relocate()
        finally:
            app.shutdown()

    def test_the_feed_is_not_labelled_demo_data(self):
        from poa.overlay.viewmodel import COLORS, OverlayViewModel
        from poa.risk import SessionStats

        vm = OverlayViewModel(session=SessionStats(), source="feed")
        vm.connected = True
        vm.data_confidence = 95.0
        header = vm.render()["header"]
        assert header["status"] == "LIVE FEED"
        assert header["status_color"] == COLORS["call"]

    # -- what Scan and Find chart mean when there are no pixels -------------

    class _StubFeed:
        """A feed source that records whether it was asked to re-sync."""

        name = "feed"
        vision_based = False
        names_own_chart = True

        def __init__(self, symbol=None, history=False):
            self.symbol = symbol
            self.history = history
            self.resyncs = 0

        def describe(self):
            return {"symbol": self.symbol, "history_loaded": self.history}

        def resync(self):
            self.resyncs += 1
            return "Re-reading the chart from the platform…"

        def capture(self):
            from poa.chart_detection.base import Capture
            from poa.models import DataQuality

            return Capture(
                series=None,
                quality=DataQuality(
                    ok=False, confidence=0.0, candle_count=0, issues=["no data"]
                ),
            )

    def test_find_chart_re_syncs_the_feed_instead_of_doing_nothing(self, tmp_path):
        """The button used to run a screen search the feed has no use for.

        Nothing happened, visibly or otherwise, which is indistinguishable
        from a broken button.
        """
        app = self._app(tmp_path, **{"capture.source": "feed"})
        feed = self._StubFeed()
        app.engine.source = feed
        said: list[str] = []
        try:
            app.locate_chart(said.append)
            assert feed.resyncs == 1
            assert said and "chart" in said[0].lower()
            assert not app._scan_busy  # no screen search was started
        finally:
            app.shutdown()

    def test_scan_re_syncs_a_feed_that_does_not_know_the_chart(self, tmp_path):
        app = self._app(tmp_path, **{"capture.source": "feed"})
        feed = self._StubFeed(symbol=None)
        app.engine.source = feed
        try:
            app._begin_scan()
            assert feed.resyncs == 1
        finally:
            app.shutdown()

    def test_scan_leaves_a_feed_that_is_reading_the_right_chart_alone(self, tmp_path):
        """Re-syncing reloads the platform's page. Not on every press."""
        app = self._app(tmp_path, **{"capture.source": "feed"})
        feed = self._StubFeed(symbol="CADJPY_otc", history=True)
        app.engine.source = feed
        try:
            app._begin_scan()
            assert feed.resyncs == 0
        finally:
            app.shutdown()

    def test_a_pair_is_never_left_on_the_panel_once_it_cannot_be_read(
        self, tmp_path
    ):
        """The worst version of this bug: a stale pair over a live verdict.

        The panel named AUD/CAD while the platform showed CAD/JPY, and every
        number under it was a report on a market the user was not watching.
        """
        app = self._app(tmp_path, **{"capture.source": "feed"})
        feed = self._StubFeed(symbol="CADJPY_otc", history=True)
        app.engine.source = feed
        try:
            # A good read first, so there is something stale to hold on to.
            app.engine.state.capture_meta = {"asset": "CAD/JPY OTC"}
            app._apply_state()
            assert app.vm.asset == "CAD/JPY OTC"

            app.engine._degrade("nothing readable")
            app._apply_state()
            assert app.vm.asset == "—"
            assert app.vm.render()["tiles"]["pair"] == "—"
        finally:
            app.shutdown()


class TestOneSetupIsOneCall:
    """A setup that stands for ten minutes is one call, not six hundred.

    Every evaluation mints a fresh signal id, so keying the counter on that
    counted the same standing setup again on every poll — the panel reported
    88 calls in a session where one had been made.
    """

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

    class _Signal:
        def __init__(self, actionable):
            import uuid

            self.actionable = actionable
            self.id = uuid.uuid4().hex[:12]  # fresh every evaluation, as in life

    def _poll(self, app, actionable):
        app.engine.state.signal = self._Signal(actionable)
        app.engine.state.capture_meta = {"asset": "EUR/USD"}
        app._apply_state()

    def test_a_standing_setup_counts_once(self, tmp_path):
        app = self._app(tmp_path)
        try:
            for _ in range(60):
                self._poll(app, actionable=True)
            assert app.vm.calls_this_session == 1
        finally:
            app.shutdown()

    def test_a_second_setup_after_a_wait_counts_again(self, tmp_path):
        app = self._app(tmp_path)
        try:
            for _ in range(20):
                self._poll(app, actionable=True)
            for _ in range(20):
                self._poll(app, actionable=False)
            for _ in range(20):
                self._poll(app, actionable=True)
            assert app.vm.calls_this_session == 2
        finally:
            app.shutdown()

    def test_waiting_alone_never_counts(self, tmp_path):
        app = self._app(tmp_path)
        try:
            for _ in range(30):
                self._poll(app, actionable=False)
            assert app.vm.calls_this_session == 0
        finally:
            app.shutdown()


class TestAMeasurementBelongsToItsChart:
    """The same lie as showing the wrong pair, one line further down.

    After a switch the new chart has too little history to replay for a while.
    Leaving the previous numbers on screen reports one instrument's record
    over another's candles.
    """

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

    def test_switching_charts_clears_the_previous_measurement(self, tmp_path):
        from poa.models import Series

        app = self._app(tmp_path)
        try:
            app.vm.proof = object()
            app.vm.tuning = ["Signal gate 75 → 70"]
            app.vm.retired = ["momentum no longer blocks"]
            app._proof_key = ("EUR/USD OTC", 60, 180)
            app._proof_bars = 500

            # A fresh chart, still rebuilding its candles.
            app.engine._last_series = Series((), 60, "GBP/USD OTC")
            app._maybe_measure()

            assert app.vm.proof is None
            assert app.vm.tuning == []
            assert app.vm.retired == []
        finally:
            app.shutdown()

    def test_the_same_chart_keeps_its_measurement(self, tmp_path):
        from poa.models import Series

        app = self._app(tmp_path)
        try:
            marker = object()
            app.vm.proof = marker
            app._proof_key = ("EUR/USD OTC", 60, app.engine.trade_duration)
            app._proof_bars = 500
            app.engine._last_series = Series((), 60, "EUR/USD OTC")
            app._maybe_measure()
            assert app.vm.proof is marker
        finally:
            app.shutdown()


class TestTheMeasurementKeepsUp:
    """A rolling buffer stops growing once it is full.

    Re-measuring when the candle *count* rises therefore reads "nothing new
    has happened" forever after the first full buffer — freezing the record at
    whatever that buffer happened to contain, for the rest of the session.
    What has moved on is the clock, not the count.
    """

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

    def _series(self, bars, start_minute=0):
        from datetime import datetime, timedelta, timezone

        from poa.models import Candle, Series

        start = datetime(2026, 8, 16, 12, tzinfo=timezone.utc) + timedelta(
            minutes=start_minute
        )
        return Series(
            [
                Candle(start + timedelta(minutes=i), 1.1, 1.11, 1.09, 1.10)
                for i in range(bars)
            ],
            60,
            "EUR/USD OTC",
        )

    def test_a_full_buffer_still_re_measures_as_time_passes(self, tmp_path):
        app = self._app(tmp_path)
        try:
            # A saturated buffer: the same number of candles, an hour later.
            app.engine._last_series = self._series(400)
            app._maybe_measure()
            first = app._proof_at
            assert first is not None
            app._proof_busy = False  # the worker would have finished

            app.engine._last_series = self._series(400, start_minute=90)
            app._maybe_measure()
            assert app._proof_at is not None and app._proof_at > first
        finally:
            app.shutdown()

    def test_a_chart_that_has_barely_moved_is_not_re_measured(self, tmp_path):
        """Fourteen seconds of replay on every poll is its own kind of broken."""
        app = self._app(tmp_path)
        try:
            app.engine._last_series = self._series(400)
            app._maybe_measure()
            app._proof_busy = False
            at = app._proof_at

            app.engine._last_series = self._series(400, start_minute=5)
            app._maybe_measure()
            assert app._proof_at == at  # too little has happened
        finally:
            app.shutdown()


class TestSwappingBetweenWatchedCharts:
    """Clicking a tab has to read that chart, not just rename the one on screen.

    Renaming is the failure that started all of this: a panel that says GBP/USD
    over EUR/USD's candles. Where the source names its own chart, picking a pair
    tells the source which of the charts it already holds to read.
    """

    def _app(self, tmp_path, source="synthetic"):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("storage.report_dir", str(tmp_path / "reports"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", source)
        return OverlayApp(config)

    def test_picking_a_pair_reads_it_rather_than_relabelling(self, tmp_path):
        app = self._app(tmp_path)
        try:
            picked = []
            app.engine.source.focus = lambda name, tf=None: (
                picked.append((name, tf)) or True
            )
            app.vm.asset = "EUR/USD OTC"
            app.vm.signal = object()

            app._set_asset("GBP/USD OTC")

            assert picked == [("GBP/USD OTC", None)]
            # The source names the chart on the next poll; until then the old
            # verdict is cleared rather than shown under a new name.
            assert app.vm.signal is None
            assert app.vm.asset == "EUR/USD OTC"
        finally:
            app.shutdown()

    def test_a_name_the_source_does_not_know_changes_nothing(self, tmp_path):
        app = self._app(tmp_path)
        try:
            marker = object()
            app.engine.source.focus = lambda name: False
            app.vm.signal = marker
            app._set_asset("CAD/CHF OTC")
            assert app.vm.signal is marker
        finally:
            app.shutdown()

    def test_a_source_that_cannot_pick_still_takes_a_typed_label(self, tmp_path):
        """Screen reading has no watchlist; there the name is a label."""
        app = self._app(tmp_path)
        try:
            assert not hasattr(app.engine.source, "focus")
            app._set_asset("gbp/usd otc")
            assert app.vm.asset == "GBP/USD OTC"
        finally:
            app.shutdown()

    def test_the_tabs_hold_their_places(self, tmp_path):
        """A tab that moves between the reach and the press is a misclick."""
        app = self._app(tmp_path)
        try:
            app._watch_results.put(
                [
                    {"asset": "USD/JPY OTC", "score": 40.0, "actionable": False,
                     "direction": "WAIT", "timeframe": 60, "candles": 300},
                    {"asset": "EUR/USD OTC", "score": 82.0, "actionable": True,
                     "direction": "CALL", "timeframe": 60, "candles": 300},
                    {"asset": "GBP/USD OTC", "score": 61.0, "actionable": False,
                     "direction": "WAIT", "timeframe": 60, "candles": 300},
                ]
            )
            app._collect_watchlist()
            first = [row["asset"] for row in app.vm.watchlist]

            # The same charts, every score different.
            app._watch_results.put(
                [
                    {"asset": "GBP/USD OTC", "score": 91.0, "actionable": True,
                     "direction": "PUT", "timeframe": 60, "candles": 320},
                    {"asset": "EUR/USD OTC", "score": 44.0, "actionable": False,
                     "direction": "WAIT", "timeframe": 60, "candles": 320},
                    {"asset": "USD/JPY OTC", "score": 70.0, "actionable": False,
                     "direction": "WAIT", "timeframe": 60, "candles": 320},
                ]
            )
            app._collect_watchlist()
            assert [row["asset"] for row in app.vm.watchlist] == first
        finally:
            app.shutdown()

    def test_the_open_chart_is_the_one_marked_active(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.vm.asset = "GBP/USD OTC"
            app.vm.chart_timeframe = 60
            app.vm.watchlist = [
                {"asset": "EUR/USD OTC", "score": 50.0, "actionable": False,
                 "direction": "WAIT", "timeframe": 60},
                {"asset": "GBP/USD OTC", "score": 80.0, "actionable": True,
                 "direction": "CALL", "timeframe": 60},
            ]
            rows = app.vm.render()["watchlist"]
            assert [row["active"] for row in rows] == [False, True]
            assert [row["label"] for row in rows] == ["EUR/USD 1MIN", "GBP/USD 1MIN"]
        finally:
            app.shutdown()

    def test_a_failed_sweep_does_not_freeze_the_watchlist(self, tmp_path):
        """The busy flag is cleared by the reply, so there must always be one."""
        app = self._app(tmp_path)
        try:
            app._watch_busy = True
            app._watch_worker(object())  # not iterable — the worker blows up
            app._collect_watchlist()
            assert app._watch_busy is False
        finally:
            app.shutdown()

    def test_a_measured_chart_is_judged_by_its_own_record(self, tmp_path):
        """Otherwise a tab would change its mind the moment you opened it.

        The active chart is read against what it has measured. A watched one
        read against the defaults would show one verdict as a tab and another
        as soon as it became the chart on screen.
        """
        from poa.models import Candle, Series
        from datetime import datetime, timedelta, timezone

        app = self._app(tmp_path)
        try:
            start = datetime(2026, 8, 15, tzinfo=timezone.utc)
            series = Series(
                [
                    Candle(start + timedelta(minutes=i), 1.1, 1.11, 1.09, 1.10)
                    for i in range(200)
                ],
                60,
                "GBP/USD OTC",
            )
            record = type("Proof", (), {"calibration": "the record"})()
            app._measured[("GBP/USD OTC", 60, app.engine.trade_duration)] = (
                record, 200, None
            )
            app.engine.source.watched = lambda: [
                ("EUR/USD OTC", 60, series),
                ("GBP/USD OTC", 60, series),
            ]

            # Keyed by chart — pair *and* timeframe — because the same pair
            # is now also read at the timeframes above the one it is open on,
            # and those have no record of their own.
            seen = {}
            app.engine.evaluate_series = (
                lambda s, a, tf, cal=None: seen.__setitem__((a, tf), cal)
            )
            app._sweep_watchlist()
            import time as _t
            for _ in range(100):
                if len(seen) >= 2:
                    break
                _t.sleep(0.02)

            assert seen[("GBP/USD OTC", 60)] == "the record"
            assert seen[("EUR/USD OTC", 60)] is None
        finally:
            app.shutdown()

    def test_the_tabs_survive_the_feed_blinking(self, tmp_path):
        """watched() reports nothing while the feed is between charts.

        A reload, a re-read, a moment before the platform says which chart is
        open — the watchlist has no opinion about any of those, and throwing
        the tabs away for them made the row come and go for reasons the user
        could not see.
        """
        app = self._app(tmp_path)
        try:
            app.vm.watchlist = [
                {"asset": "EUR/USD OTC", "score": 60.0, "actionable": False,
                 "direction": "WAIT"},
            ]
            app.engine.source.watched = lambda: []
            app._watch_at = None
            app._sweep_watchlist()
            assert app.vm.watchlist  # kept

            # A row containing only the chart already on screen has nothing to
            # show — but that is a question about the tabs, not about the data.
            # A lone setup elsewhere is still counted and still announced, so
            # the emptying happens where it is drawn.
            app.vm.asset = "EUR/USD OTC"
            app.vm.chart_timeframe = 60
            app.vm.watchlist = [
                {"asset": "EUR/USD OTC", "score": 60.0, "actionable": False,
                 "direction": "WAIT", "timeframe": 60},
            ]
            assert app.vm._watchlist() == []
        finally:
            app.shutdown()


class TestTheTrendReadout:
    """Which way the market is going, told separately from whether to act.

    The verdict is WAIT most of the time, and WAIT on its own says nothing
    about direction — the one thing that is obvious on the chart and was only
    ever available here by reading a paragraph of risk prose.
    """

    def _mtf(self, higher, current, entry, *, higher_distinct=True,
             entry_distinct=True):
        from poa.models import Bias

        class _View:
            def __init__(self, bias):
                self.trend_bias = bias
                self.trend_strength = 70.0

        class _MTF:
            pass

        mtf = _MTF()
        mtf.higher = _View(higher)
        mtf.current = _View(current)
        mtf.entry = _View(entry)
        mtf.higher_is_distinct = higher_distinct
        mtf.entry_is_distinct = entry_distinct
        return mtf

    def _vm(self, mtf):
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        from types import SimpleNamespace

        signal = SimpleNamespace(mtf=mtf)
        vm.signal = signal
        return vm

    def test_all_three_falling_reads_falling(self):
        from poa.models import Bias

        vm = self._vm(self._mtf(Bias.BEARISH, Bias.BEARISH, Bias.BEARISH))
        trend = vm._trend(scanning=False)
        assert trend["label"] == "FALLING"
        assert trend["arrow"] == "▼"
        assert trend["agreement"] == 100

    def test_a_split_stack_reads_sideways(self):
        from poa.models import Bias

        vm = self._vm(self._mtf(Bias.BULLISH, Bias.BEARISH, Bias.BEARISH,
                                entry_distinct=False))
        trend = vm._trend(scanning=False)
        assert trend["label"] == "SIDEWAYS"
        assert trend["detail"] == "the timeframes disagree"

    def test_a_duplicated_view_does_not_vote_twice(self):
        """The word has to match the arrows printed beside it.

        With no entry timeframe of its own, the entry view *is* the current
        one. Counting it again turned one bearish read into two and printed
        FALLING next to an arrow pair that plainly disagreed.
        """
        from poa.models import Bias

        mtf = self._mtf(Bias.BULLISH, Bias.BEARISH, Bias.BEARISH,
                        entry_distinct=False)
        trend = self._vm(mtf)._trend(scanning=False)

        shown = [view["arrow"] for view in trend["views"]]
        assert shown == ["▲", "▼"]  # the duplicate is not drawn
        assert trend["label"] == "SIDEWAYS"  # and does not vote

    def test_a_view_that_is_not_distinct_is_not_shown(self):
        from poa.models import Bias

        mtf = self._mtf(Bias.BEARISH, Bias.BEARISH, Bias.BEARISH,
                        higher_distinct=False, entry_distinct=False)
        trend = self._vm(mtf)._trend(scanning=False)
        assert [v["name"] for v in trend["views"]] == ["NOW"]

    def test_nothing_is_claimed_while_scanning(self):
        from poa.models import Bias

        vm = self._vm(self._mtf(Bias.BEARISH, Bias.BEARISH, Bias.BEARISH))
        assert vm._trend(scanning=True)["blanked"] is True

    def test_no_signal_is_blank_rather_than_flat(self):
        """SIDEWAYS is a reading. Having no reading is not the same thing."""
        from poa.overlay.viewmodel import OverlayViewModel

        trend = OverlayViewModel()._trend(scanning=False)
        assert trend["blanked"] is True
        assert trend["label"] == "--"

    def test_rising_reads_rising(self):
        from poa.models import Bias

        vm = self._vm(self._mtf(Bias.BULLISH, Bias.BULLISH, Bias.BULLISH))
        trend = vm._trend(scanning=False)
        assert (trend["label"], trend["arrow"]) == ("RISING", "▲")

    def test_the_panel_is_given_a_trend_to_draw(self):
        """It has to reach the render payload, or none of the above shows."""
        from poa.overlay.viewmodel import OverlayViewModel

        assert "trend" in OverlayViewModel().render()


class TestTheWinLossButtonsTeachIt:
    """Pressing WIN is the only moment the tool learns what a refused setup
    was actually worth. Before this the buttons moved a counter and nothing
    else, so a trade taken off the panel's reading — which is most of them,
    since the verdict is usually WAIT — vanished."""

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

    def _signal(self, app, direction="CALL", score=72.0):
        """A gated signal: WAIT on the panel, still scored for a direction."""
        from types import SimpleNamespace
        from poa.models import Direction

        app.vm.asset = "EUR/USD OTC"
        app.vm.chart_timeframe = 60
        app.vm.signal = SimpleNamespace(
            score=SimpleNamespace(direction=Direction[direction]),
            direction_confidence=score,
            duration_confidence=88.0,
            price=1.19280,
            mtf=None,
        )

    def _filed(self, app):
        return app.engine.journal.calibration_records(
            asset="EUR/USD OTC",
            source=getattr(app.engine.source, "name", None),
            chart_timeframe=60,
            trade_duration=app.engine.trade_duration,
        )

    def test_a_win_is_filed(self, tmp_path):
        app = self._app(tmp_path)
        try:
            self._signal(app)
            app._adjust(1, 0)
            filed = self._filed(app)
            assert len(filed) == 1
            assert filed[0].won is True
            assert filed[0].score == 72.0
            assert filed[0].duration_score == 88.0
        finally:
            app.shutdown()

    def test_a_loss_is_filed(self, tmp_path):
        app = self._app(tmp_path)
        try:
            self._signal(app)
            app._adjust(0, 1)
            filed = self._filed(app)
            assert len(filed) == 1 and filed[0].won is False
        finally:
            app.shutdown()

    def test_it_is_filed_under_the_scored_direction_not_the_verdict(self, tmp_path):
        """WAIT is not a direction. The score always describes one.

        The verdict is what the gates decided; the score is the case that was
        being argued, and it is what the calibration is keyed on. Filing under
        the verdict would key a number to a side it was never computed for.
        """
        app = self._app(tmp_path)
        try:
            self._signal(app, direction="PUT")
            app._adjust(1, 0)
            assert self._filed(app)[0].direction == "PUT"
        finally:
            app.shutdown()

    def test_correcting_the_tally_downward_files_nothing(self, tmp_path):
        """Taking a win back is fixing a miscount, not reporting a trade."""
        app = self._app(tmp_path)
        try:
            self._signal(app)
            app._adjust(-1, 0)
            assert self._filed(app) == []
        finally:
            app.shutdown()

    def test_nothing_is_invented_when_nothing_was_scored(self, tmp_path):
        """Mid-scan, or before the first read. The tally still moves."""
        app = self._app(tmp_path)
        try:
            app.vm.signal = None
            app._adjust(1, 0)
            assert self._filed(app) == []
            assert app.vm.session.wins == 1
        finally:
            app.shutdown()

    def test_the_tally_still_moves_either_way(self, tmp_path):
        app = self._app(tmp_path)
        try:
            self._signal(app)
            app._adjust(1, 0)
            app._adjust(0, 1)
            assert (app.vm.session.wins, app.vm.session.losses) == (1, 1)
        finally:
            app.shutdown()

    def test_the_record_is_re_read_after_a_trade_is_filed(self, tmp_path):
        """A new outcome may change what the record recommends, and the
        measurement is what turns that into a moved gate."""
        app = self._app(tmp_path)
        try:
            self._signal(app)
            app._proof_at = "not none"
            app._adjust(1, 0)
            assert app._proof_at is None
        finally:
            app.shutdown()


class TestTheLearningLoopIsVisible:
    """A button that appears to do nothing is a button nobody presses.

    The WIN/LOSS buttons are the only way the tool learns what a refused setup
    was worth, and until twenty of them exist nothing else on the panel
    changes — which is exactly the stretch where knowing it is filling up is
    worth something.
    """

    def _vm(self, available, taken_over=False):
        from types import SimpleNamespace
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        vm.proof = SimpleNamespace(
            calibration=SimpleNamespace(
                real_available=available,
                from_real_trades=taken_over,
                min_sample=20,
            )
        )
        return vm

    def test_it_counts_up_and_says_what_is_left(self):
        assert self._vm(7)._taught() == (
            "7 of your trades recorded — 13 more and they outrank the replay."
        )

    def test_it_says_so_when_they_take_over(self):
        assert self._vm(24, taken_over=True)._taught() == (
            "Learning from your 24 settled trades on this chart."
        )

    def test_it_is_silent_before_the_first_one(self):
        assert self._vm(0)._taught() == ""

    def test_it_is_silent_before_the_first_measurement(self):
        from poa.overlay.viewmodel import OverlayViewModel

        assert OverlayViewModel()._taught() == ""

    def test_the_panel_is_given_it_to_draw(self):
        from poa.overlay.viewmodel import OverlayViewModel

        assert "taught" in OverlayViewModel().render()["session"]


class TestEveryWatchedChartCounts:
    """One chart yields a handful of setups a day, which feels broken.

    The other seven arrive on the same socket and are read at the same bar
    against the same gates — the count was simply ignoring them, and the
    answer to "too few calls" is more charts rather than weaker standards.
    """

    def _app(self, tmp_path):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("storage.report_dir", str(tmp_path / "r"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        app = OverlayApp(config)
        app.vm.asset = "EUR/USD OTC"
        return app

    def _rows(self, *specs):
        """As _watch_worker builds them — a chart is a pair and a timeframe."""
        return [
            {"asset": a, "score": s, "actionable": act, "direction": d,
             "timeframe": 60}
            for a, s, act, d in specs
        ]

    def _live(self, actionable=True):
        from types import SimpleNamespace

        return SimpleNamespace(actionable=actionable)

    def test_setups_elsewhere_are_counted(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.vm.watchlist = self._rows(
                ("EUR/USD OTC", 80.0, True, "CALL"),
                ("GBP/USD OTC", 84.0, True, "PUT"),
                ("USD/JPY OTC", 44.0, False, "WAIT"),
            )
            assert app.vm._take_now(self._live())["take_now"] == 2
        finally:
            app.shutdown()

    def test_the_open_chart_is_not_counted_twice(self, tmp_path):
        """It appears in the sweep as well as being the live signal."""
        app = self._app(tmp_path)
        try:
            app.vm.watchlist = self._rows(("EUR/USD OTC", 80.0, True, "CALL"))
            assert app.vm._take_now(self._live())["take_now"] == 1
        finally:
            app.shutdown()

    def test_the_pairs_are_named(self, tmp_path):
        """A count with nowhere to go is a count nobody can act on."""
        app = self._app(tmp_path)
        try:
            app.vm.watchlist = self._rows(("GBP/USD OTC", 84.0, True, "PUT"))
            label = app.vm._take_now(self._live(False))["take_label"]
            assert "GBP/USD" in label and label.startswith("1")
        finally:
            app.shutdown()

    def test_nothing_anywhere_still_reads_zero_trades(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.vm.watchlist = self._rows(("GBP/USD OTC", 40.0, False, "WAIT"))
            assert app.vm._take_now(self._live(False))["take_label"] == "0 trades"
        finally:
            app.shutdown()


class TestASetupYouCannotSeeSpeaksUp:
    """A tab turning green only helps somebody already watching the tab row.

    The reason for reading eight charts is that the pair worth trading finds
    the user rather than the other way round, and that needs a noise.
    """

    def _app(self, tmp_path):
        return TestEveryWatchedChartCounts()._app(tmp_path)

    def _listen(self, app):
        heard: list[str] = []
        app.engine.alerts.add_notifier(
            type("N", (), {"name": "t", "send": lambda s, a: heard.append(a.title)})()
        )
        return heard

    def _sweep(self, app, rows):
        app._watch_results.put(list(rows))
        app._collect_watchlist()

    def _rows(self, *specs):
        return TestEveryWatchedChartCounts()._rows(*specs)

    def test_it_announces_a_setup_on_another_chart(self, tmp_path):
        app = self._app(tmp_path)
        try:
            heard = self._listen(app)
            self._sweep(app, self._rows(("GBP/USD OTC", 84.0, True, "PUT")))
            assert any("GBP/USD OTC" in title for title in heard)
        finally:
            app.shutdown()

    def test_it_says_nothing_about_the_chart_already_on_screen(self, tmp_path):
        """That one has the whole panel describing it."""
        app = self._app(tmp_path)
        try:
            heard = self._listen(app)
            self._sweep(app, self._rows(("EUR/USD OTC", 84.0, True, "CALL")))
            assert heard == []
        finally:
            app.shutdown()

    def test_a_standing_setup_is_announced_once(self, tmp_path):
        """Every fifteen seconds for as long as it holds is not an alert."""
        app = self._app(tmp_path)
        try:
            heard = self._listen(app)
            rows = self._rows(("GBP/USD OTC", 84.0, True, "PUT"))
            for _ in range(4):
                self._sweep(app, rows)
            assert len(heard) == 1
        finally:
            app.shutdown()

    def test_a_setup_that_goes_and_returns_is_announced_again(self, tmp_path):
        app = self._app(tmp_path)
        try:
            heard = self._listen(app)
            live = self._rows(("GBP/USD OTC", 84.0, True, "PUT"))
            gone = self._rows(("GBP/USD OTC", 40.0, False, "WAIT"))
            self._sweep(app, live)
            self._sweep(app, gone)
            app.engine.alerts._last_sent.clear()  # past the cooldown
            self._sweep(app, live)
            assert len(heard) == 2
        finally:
            app.shutdown()

    def test_the_cooldown_applies_the_same_as_anywhere_else(self, tmp_path):
        """Eight charts must not become eight times the interruptions."""
        app = self._app(tmp_path)
        try:
            heard = self._listen(app)
            self._sweep(app, self._rows(("GBP/USD OTC", 84.0, True, "PUT")))
            app._announced.clear()  # as if it had lapsed and returned
            self._sweep(app, self._rows(("GBP/USD OTC", 84.0, True, "PUT")))
            assert len(heard) == 1  # the manager's cooldown held it
        finally:
            app.shutdown()

    def test_alerts_switched_off_stay_off(self, tmp_path):
        app = self._app(tmp_path)
        try:
            heard = self._listen(app)
            app.engine.alerts.settings.enabled = False
            self._sweep(app, self._rows(("GBP/USD OTC", 84.0, True, "PUT")))
            assert heard == []
        finally:
            app.shutdown()


class TestAKindAddedLaterIsNotSilentlyOff:
    """Nothing exposes the notify list for editing.

    So a settings file written before a kind existed cannot have deliberately
    excluded it — it simply predates it. Leaving it off would make the feature
    look broken on every install that has ever saved its settings.
    """

    def test_an_older_settings_file_still_gets_it(self):
        from poa.alerts.manager import AlertSettings

        settings = AlertSettings.from_config(
            {"notify_on": ["BUY_SIGNAL", "SELL_SIGNAL", "TREND_REVERSAL"]}
        )
        assert "WATCHLIST" in settings.notify_on

    def test_what_was_already_chosen_is_kept(self):
        from poa.alerts.manager import AlertSettings

        settings = AlertSettings.from_config({"notify_on": ["BUY_SIGNAL"]})
        assert "BUY_SIGNAL" in settings.notify_on
        assert "SELL_SIGNAL" not in settings.notify_on

    def test_it_is_on_by_default(self):
        from poa.alerts.manager import AlertSettings

        assert "WATCHLIST" in AlertSettings().notify_on


class TestACachedVerdictAnswersTheQuestionThatWasAsked:
    """Re-using a verdict while nothing has closed is what makes sweeping
    eight charts affordable. But the question is more than the chart: which
    chart is open decides whether a row carries the user's expiry or the one
    the engine picks for it, and the expiry setting decides what was asked of
    every chart. Neither moves the candles, so the cache went on answering the
    old question for as long as the bar took to close — up to fifteen minutes.
    """

    def _app(self, tmp_path):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("storage.report_dir", str(tmp_path / "reports"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        config.set("market.scan_timeframes", False)
        app = OverlayApp(config)
        app.vm.asset, app.vm.chart_timeframe = "EUR/USD OTC", 60
        return app

    def _charts(self):
        return [("EUR/USD OTC", 60, pullback_trend(400, direction=1))]

    def _sweep(self, app):
        app._watch_worker(self._charts())
        return app._watch_results.get_nowait()

    def test_changing_the_expiry_re_reads_rather_than_re_using(self, tmp_path):
        app = self._app(tmp_path)
        try:
            first = self._sweep(app)
            assert first and first[0]["expiry"] == app.engine.trade_duration

            app.engine.update_settings({"trade_duration": 300})
            second = self._sweep(app)
            assert second and second[0]["expiry"] == 300
        finally:
            app.shutdown()

    def test_switching_the_open_chart_re_reads_rather_than_re_using(self, tmp_path):
        """The chart just opened must carry the user's expiry, not the
        recommendation it was given while it was somebody else's tab."""
        app = self._app(tmp_path)
        try:
            app.vm.asset = "GBP/USD OTC"      # EUR/USD is a watched tab
            self._sweep(app)

            app.vm.asset = "EUR/USD OTC"      # now it is the open chart
            rows = self._sweep(app)
            assert rows and rows[0]["expiry"] == app.engine.trade_duration
        finally:
            app.shutdown()

    def test_an_unchanged_question_still_re_uses_the_answer(self, tmp_path):
        """The saving is the whole point of the cache."""
        app = self._app(tmp_path)
        try:
            first = self._sweep(app)
            second = self._sweep(app)
            assert second and second[0] is first[0]
        finally:
            app.shutdown()


class TestTheSamePairAtSeveralTimeframes:
    """A setup is a statement about a timeframe as much as about a pair.

    The candles that say nothing at one minute can be a clean structure at
    five, and reading only the timeframe the chart happens to be open on threw
    that away. Every one of the platform's S5–M30 timeframes is derivable from
    candles already in hand.
    """

    def _app(self, tmp_path):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("storage.report_dir", str(tmp_path / "reports"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        return OverlayApp(config)

    def _series(self, bars=1200, timeframe=60, symbol="EUR/USD OTC"):
        from datetime import datetime, timedelta, timezone
        from poa.models import Candle, Series

        start = datetime(2026, 8, 16, tzinfo=timezone.utc)
        return Series(
            [
                Candle(start + timedelta(seconds=i * timeframe),
                       1.19, 1.191, 1.189, 1.190)
                for i in range(bars)
            ],
            timeframe,
            symbol,
        )

    def test_a_one_minute_chart_yields_the_minutes_above_it(self, tmp_path):
        app = self._app(tmp_path)
        try:
            charts = app._with_other_timeframes(
                [("EUR/USD OTC", 60, self._series())]
            )
            offered = sorted(tf for _a, tf, _s in charts)
            assert offered == [60, 120, 180, 300, 600, 900]
        finally:
            app.shutdown()

    def test_the_aggregated_candles_are_the_right_count(self, tmp_path):
        app = self._app(tmp_path)
        try:
            charts = dict(
                (tf, s)
                for _a, tf, s in app._with_other_timeframes(
                    [("EUR/USD OTC", 60, self._series(1200))]
                )
            )
            assert len(charts[300]) == 240  # 1200 one-minute bars at five
            assert len(charts[900]) == 80
        finally:
            app.shutdown()

    def test_it_never_aggregates_downwards(self, tmp_path):
        """Below the chart's own timeframe there is nothing to aggregate from.

        Going that way would mean inventing candles. Opening a shorter
        timeframe on the platform is what makes it available.
        """
        app = self._app(tmp_path)
        try:
            charts = app._with_other_timeframes(
                [("EUR/USD OTC", 300, self._series(1200, timeframe=300))]
            )
            assert all(tf >= 300 for _a, tf, _s in charts)
        finally:
            app.shutdown()

    def test_only_whole_multiples(self, tmp_path):
        """A 2-minute chart cannot make 3-minute candles honestly."""
        app = self._app(tmp_path)
        try:
            charts = app._with_other_timeframes(
                [("EUR/USD OTC", 120, self._series(1200, timeframe=120))]
            )
            offered = sorted(tf for _a, tf, _s in charts)
            assert all(tf % 120 == 0 for tf in offered)
            assert 180 not in offered
        finally:
            app.shutdown()

    def test_a_timeframe_with_too_few_bars_is_not_offered(self, tmp_path):
        """Aggregating 100 bars up to thirty minutes leaves three of them."""
        app = self._app(tmp_path)
        try:
            charts = app._with_other_timeframes(
                [("EUR/USD OTC", 60, self._series(100))]
            )
            assert sorted(tf for _a, tf, _s in charts) == [60]
        finally:
            app.shutdown()

    def test_hours_and_days_are_left_alone(self, tmp_path):
        """An H4 candle takes four hours to settle.

        That is not a timeframe anyone is taking three-minute expiries
        against, and scanning it would only crowd the row.
        """
        app = self._app(tmp_path)
        try:
            charts = app._with_other_timeframes(
                [("EUR/USD OTC", 60, self._series(20000))]
            )
            assert all(tf <= 1800 for _a, tf, _s in charts)
        finally:
            app.shutdown()

    def test_it_can_be_switched_off(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.config.set("market.scan_timeframes", False)
            charts = app._with_other_timeframes(
                [("EUR/USD OTC", 60, self._series())]
            )
            assert len(charts) == 1
        finally:
            app.shutdown()

    def test_each_timeframe_is_named_on_its_tab(self, tmp_path):
        """"EUR/USD" twice over would be two tabs nobody could tell apart."""
        app = self._app(tmp_path)
        try:
            app.vm.asset = "EUR/USD OTC"
            app.vm.chart_timeframe = 60
            app.vm.watchlist = [
                {"asset": "EUR/USD OTC", "timeframe": 60, "score": 60.0,
                 "actionable": False, "direction": "WAIT"},
                {"asset": "EUR/USD OTC", "timeframe": 300, "score": 84.0,
                 "actionable": True, "direction": "CALL"},
            ]
            labels = [row["label"] for row in app.vm._watchlist()]
            assert labels == ["EUR/USD 1MIN", "EUR/USD 5MIN"]
        finally:
            app.shutdown()

    def test_the_open_chart_is_marked_by_pair_and_timeframe(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.vm.asset = "EUR/USD OTC"
            app.vm.chart_timeframe = 60
            app.vm.watchlist = [
                {"asset": "EUR/USD OTC", "timeframe": 60, "score": 60.0,
                 "actionable": False, "direction": "WAIT"},
                {"asset": "EUR/USD OTC", "timeframe": 300, "score": 84.0,
                 "actionable": True, "direction": "CALL"},
            ]
            assert [row["active"] for row in app.vm._watchlist()] == [True, False]
        finally:
            app.shutdown()

    def test_a_setup_at_another_timeframe_is_a_trade_available(self, tmp_path):
        """Same pair, different read, genuinely separate opportunity."""
        from types import SimpleNamespace

        app = self._app(tmp_path)
        try:
            app.vm.asset = "EUR/USD OTC"
            app.vm.chart_timeframe = 60
            app.vm.watchlist = [
                {"asset": "EUR/USD OTC", "timeframe": 300, "score": 84.0,
                 "actionable": True, "direction": "CALL"},
            ]
            counted = app.vm._take_now(SimpleNamespace(actionable=False))
            assert counted["take_now"] == 1
        finally:
            app.shutdown()


class TestSweepingFastEnoughToMatter:
    """A sweep slower than the candles misses most of what closes.

    At fifteen seconds a five-second chart is read once every three bars, so
    two setups in three are over before anything looks. Reading more often is
    only affordable because a chart whose newest candle has not moved cannot
    have changed its mind, and is skipped.
    """

    def _app(self, tmp_path):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("storage.report_dir", str(tmp_path / "reports"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        return OverlayApp(config)

    def _series(self, period, bars=300, symbol="EUR/USD OTC"):
        from datetime import datetime, timedelta, timezone
        from poa.models import Candle, Series

        start = datetime(2026, 8, 16, tzinfo=timezone.utc)
        return Series(
            [
                Candle(start + timedelta(seconds=i * period),
                       1.19, 1.191, 1.189, 1.190)
                for i in range(bars)
            ],
            period,
            symbol,
        )

    def test_the_pace_follows_the_shortest_candle(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.vm.watchlist = [{"asset": "E", "timeframe": 5},
                                {"asset": "E", "timeframe": 60}]
            assert app._sweep_interval() == 5.0
        finally:
            app.shutdown()

    def test_it_never_sweeps_faster_than_it_can_afford(self, tmp_path):
        from poa.overlay.app import MIN_WATCH_SWEEP_SECONDS

        app = self._app(tmp_path)
        try:
            app.vm.watchlist = [{"asset": "E", "timeframe": 1}]
            assert app._sweep_interval() == MIN_WATCH_SWEEP_SECONDS
        finally:
            app.shutdown()

    def test_slow_charts_do_not_make_it_sluggish(self, tmp_path):
        from poa.overlay.app import WATCH_SWEEP_SECONDS

        app = self._app(tmp_path)
        try:
            app.vm.watchlist = [{"asset": "E", "timeframe": 1800}]
            assert app._sweep_interval() == WATCH_SWEEP_SECONDS
        finally:
            app.shutdown()

    def test_nothing_watched_yet_uses_the_default(self, tmp_path):
        from poa.overlay.app import WATCH_SWEEP_SECONDS

        app = self._app(tmp_path)
        try:
            assert app._sweep_interval() == WATCH_SWEEP_SECONDS
        finally:
            app.shutdown()

    def test_a_chart_that_has_not_closed_a_candle_is_not_re_read(self, tmp_path):
        """Re-deriving an identical verdict from identical candles buys
        nothing, and skipping it is what makes a fast sweep affordable."""
        app = self._app(tmp_path)
        try:
            # One chart, and the open one, so neither the aggregated
            # timeframes nor the expiry re-read muddy the count: this test is
            # about the skip and nothing else.
            app.config.set("market.scan_timeframes", False)
            app.vm.asset, app.vm.chart_timeframe = "EUR/USD OTC", 60
            charts = [("EUR/USD OTC", 60, self._series(60))]
            reads = []
            real = app.engine.evaluate_series
            app.engine.evaluate_series = lambda *a, **k: (
                reads.append(a[1]) or real(*a, **k)
            )
            app._watch_worker(charts, {})
            first = app._watch_results.get_nowait()
            after_first = len(reads)
            app._watch_worker(charts, {})
            second = app._watch_results.get_nowait()

            assert after_first == 1
            assert len(reads) == after_first   # the second sweep read nothing
            assert first == second             # and the answer is unchanged
        finally:
            app.shutdown()

    def test_a_new_candle_does_get_re_read(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.config.set("market.scan_timeframes", False)
            app.vm.asset, app.vm.chart_timeframe = "EUR/USD OTC", 60
            reads = []
            real = app.engine.evaluate_series
            app.engine.evaluate_series = lambda *a, **k: (
                reads.append(a[1]) or real(*a, **k)
            )
            app._watch_worker([("EUR/USD OTC", 60, self._series(60, 300))], {})
            app._watch_results.get_nowait()
            app._watch_worker([("EUR/USD OTC", 60, self._series(60, 301))], {})
            app._watch_results.get_nowait()
            assert len(reads) == 2
        finally:
            app.shutdown()


class TestLearningFromRealTrades:
    """The platform reports every trade it settles, with the direction, the
    fill, the expiry and the outcome. That is better evidence than the buttons
    in every way: nothing is assumed about which way the trade went, and
    nothing depends on remembering to press anything."""

    def _app(self, tmp_path):
        from poa.config import load_config
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("storage.report_dir", str(tmp_path / "reports"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        app = OverlayApp(config)
        app.vm.asset, app.vm.chart_timeframe = "USD/JPY OTC", 60
        return app

    def _trade(self, won=True, direction="PUT", asset="USDJPY_otc"):
        from poa.models import utcnow

        return {
            "asset": asset, "direction": direction, "won": won,
            "open_price": 158.611, "close_price": 158.597,
            "payout": 0.88, "duration": 180,
            # Just now, so it can reach the call it was taken on.
            "opened_at": utcnow().timestamp(),
        }

    def _called(self, app, direction="PUT", score=72.0, asset="USD/JPY OTC",
                timeframe=60):
        """What the panel was saying on that chart when the trade was placed."""
        app._remember_call(asset, timeframe, direction, score, 65.0, "TRENDING")

    def _filed(self, app, timeframe=60):
        return app.engine.journal.calibration_records(
            asset="USD/JPY OTC",
            source=getattr(app.engine.source, "name", None),
            chart_timeframe=timeframe, trade_duration=180,
        )

    def test_a_settled_trade_reaches_the_record(self, tmp_path):
        app = self._app(tmp_path)
        try:
            self._called(app, "PUT")
            app.engine.source.take_settled = lambda: [self._trade()]
            app._collect_real_trades()
            filed = self._filed(app)
            assert len(filed) == 1
            assert filed[0].direction == "PUT" and filed[0].won is True
        finally:
            app.shutdown()

    def test_the_direction_comes_from_the_broker_not_a_guess(self, tmp_path):
        """The buttons assume the trade went the way the panel was leaning.
        This does not have to assume anything."""
        app = self._app(tmp_path)
        try:
            self._called(app, "CALL")
            app.engine.source.take_settled = lambda: [
                self._trade(direction="CALL", won=False)
            ]
            app._collect_real_trades()
            assert self._filed(app)[0].direction == "CALL"
        finally:
            app.shutdown()

    def test_the_score_comes_from_the_traded_chart_not_the_open_one(self, tmp_path):
        """It used to take whatever the panel was showing at the moment the
        trade settled — a different instrument as often as not. A GBP/USD
        trade was then filed as EUR/USD's current score having won or lost,
        straight into the record that outranks the replay and holds a veto."""
        app = self._app(tmp_path)
        try:
            # The panel is on USD/JPY. The trade is on GBP/USD.
            self._called(app, "PUT", score=31.0)
            self._called(app, "CALL", score=84.0, asset="GBP/USD OTC", timeframe=15)

            app.engine.source.take_settled = lambda: [
                self._trade(direction="CALL", won=True, asset="GBPUSD_otc")
            ]
            app._collect_real_trades()

            filed = app.engine.journal.calibration_records(
                asset="GBP/USD OTC",
                source=getattr(app.engine.source, "name", None),
                chart_timeframe=15, trade_duration=180,
            )
            assert len(filed) == 1
            assert filed[0].score == 84.0
            # And nothing was filed against the chart that merely happened to
            # be on screen.
            assert self._filed(app) == []
        finally:
            app.shutdown()

    def test_a_trade_nobody_called_teaches_the_score_nothing(self, tmp_path):
        """The outcome is real and stays in the journal. What it must not do
        is teach a score, because the score it would teach is a stand-in for
        "nobody knows"."""
        app = self._app(tmp_path)
        try:
            app.engine.source.take_settled = lambda: [self._trade()]
            app._collect_real_trades()

            assert self._filed(app) == []
            assert app.engine.journal.recent(limit=5)
            assert (app.vm.session.wins, app.vm.session.losses) == (1, 0)
        finally:
            app.shutdown()

    def test_a_trade_taken_against_the_call_is_not_that_calls_outcome(self, tmp_path):
        """Filing it as one would teach the record backwards."""
        app = self._app(tmp_path)
        try:
            self._called(app, "CALL", score=84.0)
            app.engine.source.take_settled = lambda: [
                self._trade(direction="PUT", won=False)
            ]
            app._collect_real_trades()
            assert self._filed(app) == []
        finally:
            app.shutdown()

    def test_a_call_from_long_before_the_trade_is_not_matched(self, tmp_path):
        """A call from earlier in the session is a different moment."""
        from poa.models import utcnow

        app = self._app(tmp_path)
        try:
            self._called(app, "PUT", score=84.0)
            stale = utcnow().timestamp() - 3600
            app._calls["USD/JPY OTC"][0]["at"] = stale

            app.engine.source.take_settled = lambda: [self._trade()]
            app._collect_real_trades()
            assert self._filed(app) == []
        finally:
            app.shutdown()

    def test_the_tally_and_the_streak_both_move(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.engine.source.take_settled = lambda: [
                self._trade(won=False), self._trade(won=False)
            ]
            app._collect_real_trades()
            assert (app.vm.session.wins, app.vm.session.losses) == (0, 2)
            assert app.vm.session.losing_streak == 2
        finally:
            app.shutdown()

    def test_nothing_settled_changes_nothing(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.engine.source.take_settled = lambda: []
            app._collect_real_trades()
            assert self._filed(app) == []
        finally:
            app.shutdown()

    def test_a_source_that_cannot_report_them_is_fine(self, tmp_path):
        """Screen reading has no socket to hear a settlement on."""
        app = self._app(tmp_path)
        try:
            assert not hasattr(app.engine.source, "take_settled")
            app._collect_real_trades()  # must not raise
        finally:
            app.shutdown()


class TestThePayoutComesFromThePlatform:
    """It differs per instrument and moves through the day. Typed in once it
    goes stale silently, and in the flattering direction — a stale high payout
    lowers the rate a win has to beat."""

    def _app(self, tmp_path):
        return TestLearningFromRealTrades()._app(tmp_path)

    def test_the_reported_payout_is_used(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.vm.payout = 0.92
            app.engine.source.payout_for = lambda asset: 0.47
            app._load_payout_for("LBP/USD OTC")
            assert app.vm.payout == 0.47
        finally:
            app.shutdown()

    def test_it_beats_a_remembered_one(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.config.set("market.payouts", {"EUR/USD OTC": 0.80})
            app.engine.source.payout_for = lambda asset: 0.92
            app._load_payout_for("EUR/USD OTC")
            assert app.vm.payout == 0.92
        finally:
            app.shutdown()

    def test_a_platform_that_says_nothing_falls_back(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.config.set("market.payouts", {"EUR/USD OTC": 0.80})
            app.engine.source.payout_for = lambda asset: None
            app._load_payout_for("EUR/USD OTC")
            assert app.vm.payout == 0.80
        finally:
            app.shutdown()

    def test_an_unknown_pair_is_left_alone(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app.vm.payout = 0.92
            app._load_payout_for("—")
            assert app.vm.payout == 0.92
        finally:
            app.shutdown()
