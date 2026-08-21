"""Signal lifecycle tracking and alert suppression.

The behaviour under test is what stops the dashboard from showing a stale BUY
after the setup has fallen apart, and what stops the user being notified about
the same unchanged setup every two seconds.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import choppy_series, good_quality, pullback_trend
from poa.alerts import Alert, AlertManager, AlertSettings, Notifier
from poa.models import Direction, SignalState, utcnow
from poa.signals import GateSettings, SignalEngine, SignalRequest
from poa.signals.tracker import SignalTracker, TrackerSettings


class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def make_signal(series, **kwargs):
    request = SignalRequest(
        series=series,
        asset=kwargs.pop("asset", "EUR/USD"),
        chart_timeframe=60,
        trade_duration=kwargs.pop("trade_duration", 180),
        quality=good_quality(series),
        settings=kwargs.pop("settings", GateSettings()),
    )
    return SignalEngine().evaluate(request)


class TestTracker:
    def test_the_first_update_is_always_material(self):
        tracker = SignalTracker()
        change = tracker.update(make_signal(pullback_trend(400, direction=1)))
        assert change.material
        assert change.kind == "new"

    def test_an_unchanged_signal_is_not_material(self):
        tracker = SignalTracker()
        series = pullback_trend(400, direction=1)
        tracker.update(make_signal(series))
        change = tracker.update(make_signal(series))
        assert not change.material
        assert change.kind == "none"

    def test_losing_a_direction_invalidates_the_signal(self):
        tracker = SignalTracker()
        directional = make_signal(pullback_trend(400, direction=1))
        assert directional.direction is Direction.CALL
        tracker.update(directional)

        change = tracker.update(make_signal(choppy_series(400)))
        assert change.material
        assert tracker.current.state is SignalState.INVALIDATED

    def test_a_confidence_slide_marks_the_setup_weakening(self):
        tracker = SignalTracker(TrackerSettings(weakening_drop=5, invalidation_drop=90))
        signal = make_signal(pullback_trend(400, direction=1))
        tracker.update(signal)

        weaker = make_signal(pullback_trend(400, direction=1))
        weaker.direction_confidence -= 20
        weaker.duration.selected_score -= 20
        change = tracker.update(weaker)
        assert tracker.current.state is SignalState.WEAKENING
        assert "weakening" in change.description.lower()

    def test_a_large_slide_invalidates_rather_than_weakens(self):
        tracker = SignalTracker(TrackerSettings(weakening_drop=5, invalidation_drop=10))
        tracker.update(make_signal(pullback_trend(400, direction=1)))

        weaker = make_signal(pullback_trend(400, direction=1))
        weaker.direction_confidence -= 40
        weaker.duration.selected_score -= 40
        tracker.update(weaker)
        assert tracker.current.state is SignalState.INVALIDATED

    def test_peak_confidence_is_remembered(self):
        tracker = SignalTracker(TrackerSettings(weakening_drop=90, invalidation_drop=95))
        first = make_signal(pullback_trend(400, direction=1))
        tracker.update(first)
        peak = first.overall_confidence

        weaker = make_signal(pullback_trend(400, direction=1))
        weaker.direction_confidence -= 10
        tracker.update(weaker)
        assert tracker.current.peak_confidence == pytest.approx(peak)

    def test_an_elapsed_window_expires_the_previous_signal(self):
        tracker = SignalTracker()
        signal = make_signal(pullback_trend(400, direction=1), trade_duration=60)
        tracker.update(signal)

        later = utcnow() + timedelta(seconds=120)
        change = tracker.update(make_signal(pullback_trend(400, direction=1)), now=later)
        assert change.material
        assert change.new_state is SignalState.EXPIRED

    def test_history_is_bounded(self):
        tracker = SignalTracker()
        tracker.max_history = 5
        for index in range(20):
            signal = make_signal(pullback_trend(400, direction=1))
            signal.direction = Direction.CALL if index % 2 else Direction.PUT
            tracker.update(signal)
        assert len(tracker.history) <= 5

    def test_cooldown_is_respected(self):
        tracker = SignalTracker(TrackerSettings(cooldown_seconds=60))
        assert not tracker.within_cooldown()
        tracker.mark_emitted()
        assert tracker.within_cooldown()
        assert not tracker.within_cooldown(utcnow() + timedelta(seconds=120))

    def test_reset_clears_everything(self):
        tracker = SignalTracker()
        tracker.update(make_signal(pullback_trend(400, direction=1)))
        tracker.reset()
        assert tracker.current is None
        assert tracker.history == []
        assert tracker.open_direction is None


class TestOneCallPerSetup:
    """A setup that stays live is one call, however often it says so.

    Every material update on a running setup used to be journalled as a fresh
    call: the expiry window re-arming, the score drifting ten points, the
    regime being relabelled. A twenty-eight-minute session filed fifty-seven
    calls that way — four in one minute on one pair, at four prices a
    hundredth of a percent apart. Those settle together, so the win rate
    counted one right-or-wrong answer four times over and diluted every
    independent one beside it.
    """

    def _live(self, direction=1, **kwargs):
        return make_signal(pullback_trend(400, direction=direction), **kwargs)

    def test_the_first_live_setup_opens_a_call(self):
        tracker = SignalTracker()
        assert tracker.update(self._live()).opens_a_call

    def test_an_elapsed_window_does_not_open_a_second_call(self):
        """The bug, in one test. The window re-arms; the setup has not changed
        and nothing new has been named."""
        tracker = SignalTracker()
        assert tracker.update(self._live(trade_duration=60)).opens_a_call

        later = utcnow() + timedelta(seconds=120)
        change = tracker.update(self._live(), now=later)
        assert change.material, "the panel still wants to know the window elapsed"
        assert not change.opens_a_call, "but it is not a second call"

    def test_a_run_of_updates_opens_exactly_one_call(self):
        tracker = SignalTracker()
        now = utcnow()
        opened = sum(
            int(
                tracker.update(
                    self._live(trade_duration=60),
                    now=now + timedelta(seconds=60 * step),
                ).opens_a_call
            )
            # Once per expiry window, which is when the re-arm fires.
            for step in range(12)
        )
        assert opened == 1, f"one setup, {opened} calls"

    def test_dropping_below_the_gate_ends_the_call(self):
        tracker = SignalTracker()
        assert tracker.update(self._live()).opens_a_call
        tracker.update(make_signal(choppy_series(400)))
        assert tracker.open_direction is None

    def test_setting_up_again_afterwards_is_a_new_call(self):
        """The other half. Suppressing repeats must not suppress the genuine
        second opportunity, or the tool goes quiet for the rest of the run."""
        tracker = SignalTracker()
        assert tracker.update(self._live()).opens_a_call
        tracker.update(make_signal(choppy_series(400)))
        assert tracker.update(self._live()).opens_a_call

    def test_a_flip_to_the_other_side_is_a_new_call(self):
        tracker = SignalTracker()
        assert tracker.update(self._live(direction=1)).opens_a_call
        assert tracker.update(self._live(direction=-1)).opens_a_call

    def test_a_setup_that_never_goes_live_opens_nothing(self):
        tracker = SignalTracker()
        assert not tracker.update(make_signal(choppy_series(400))).opens_a_call


class TestAlerts:
    def _manager(self, **kwargs):
        notifier = RecordingNotifier()
        settings = AlertSettings(desktop_notifications=False, sound=False, **kwargs)
        return AlertManager(settings, [notifier]), notifier

    def test_a_new_call_produces_a_buy_alert(self):
        manager, notifier = self._manager(min_confidence=0, cooldown_seconds=0)
        tracker = SignalTracker()
        signal = make_signal(pullback_trend(400, direction=1))
        change = tracker.update(signal)
        manager.evaluate(signal, change)
        assert any(a.kind == "BUY_SIGNAL" for a in notifier.sent)

    def test_a_new_put_produces_a_sell_alert(self):
        manager, notifier = self._manager(min_confidence=0, cooldown_seconds=0)
        tracker = SignalTracker()
        signal = make_signal(pullback_trend(400, direction=-1))
        change = tracker.update(signal)
        manager.evaluate(signal, change)
        assert any(a.kind == "SELL_SIGNAL" for a in notifier.sent)

    def test_immaterial_changes_produce_nothing(self):
        manager, notifier = self._manager(min_confidence=0, cooldown_seconds=0)
        tracker = SignalTracker()
        series = pullback_trend(400, direction=1)
        first = make_signal(series)
        manager.evaluate(first, tracker.update(first))
        before = len(notifier.sent)

        second = make_signal(series)
        manager.evaluate(second, tracker.update(second))
        assert len(notifier.sent) == before

    def test_the_cooldown_suppresses_repeats(self):
        manager, notifier = self._manager(min_confidence=0, cooldown_seconds=600)
        tracker = SignalTracker()
        signal = make_signal(pullback_trend(400, direction=1))
        manager.evaluate(signal, tracker.update(signal))
        count = len(notifier.sent)

        # A fresh tracker makes the same signal look new again; only the
        # cooldown should stop it being sent twice.
        repeat = make_signal(pullback_trend(400, direction=1))
        manager.evaluate(repeat, SignalTracker().update(repeat))
        assert len(notifier.sent) == count

    def test_low_confidence_directional_alerts_are_filtered(self):
        manager, notifier = self._manager(min_confidence=99, cooldown_seconds=0)
        tracker = SignalTracker()
        signal = make_signal(pullback_trend(400, direction=1))
        manager.evaluate(signal, tracker.update(signal))
        assert not any(a.kind in ("BUY_SIGNAL", "SELL_SIGNAL") for a in notifier.sent)

    def test_disabled_alert_kinds_are_dropped(self):
        manager, notifier = self._manager(
            min_confidence=0, cooldown_seconds=0, notify_on=("SETUP_INVALIDATED",)
        )
        tracker = SignalTracker()
        signal = make_signal(pullback_trend(400, direction=1))
        manager.evaluate(signal, tracker.update(signal))
        assert not any(a.kind == "BUY_SIGNAL" for a in notifier.sent)

    def test_disabling_alerts_stops_everything(self):
        manager, notifier = self._manager(enabled=False)
        tracker = SignalTracker()
        signal = make_signal(pullback_trend(400, direction=1))
        manager.evaluate(signal, tracker.update(signal))
        assert notifier.sent == []

    def test_invalidation_produces_an_alert(self):
        manager, notifier = self._manager(min_confidence=0, cooldown_seconds=0)
        tracker = SignalTracker()
        directional = make_signal(pullback_trend(400, direction=1))
        manager.evaluate(directional, tracker.update(directional))

        gone = make_signal(choppy_series(400))
        change = tracker.update(gone)
        manager.evaluate(gone, change)
        assert any(a.kind == "SETUP_INVALIDATED" for a in notifier.sent)

    def test_a_notifier_that_raises_does_not_break_the_loop(self):
        class Broken(Notifier):
            name = "broken"

            def send(self, alert):
                raise RuntimeError("boom")

        working = RecordingNotifier()
        manager = AlertManager(
            AlertSettings(desktop_notifications=False, min_confidence=0, cooldown_seconds=0),
            [Broken(), working],
        )
        tracker = SignalTracker()
        signal = make_signal(pullback_trend(400, direction=1))
        manager.evaluate(signal, tracker.update(signal))
        assert working.sent  # the failure was contained
