"""Shadow strategies racing the live one over the same charts.

Two promises hold this feature together. One setup is one row per rulebook
— the transition rule the live watchlist earned the hard way, applied to
every experiment. And an experiment can never leak: not into the session
report's calls, not into the calibration record, not into the cooldown,
not into the ledger's live tables. Shadows share the journal so settlement
works unchanged, and everything else filters them out by label.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import good_quality, pullback_trend
from poa.models import Direction
from poa.signals import GateSettings, SignalEngine, SignalRequest
from poa.signals.racing import (
    DEFAULT_ROSTER,
    Experiment,
    ShadowBook,
    roster_from_config,
)
from poa.storage.journal import Journal


def _real_signal(direction=1):
    series = pullback_trend(400, direction=direction)
    signal = SignalEngine().evaluate(SignalRequest(
        series=series, asset="EUR/USD OTC", chart_timeframe=60,
        trade_duration=180, quality=good_quality(series),
        settings=GateSettings(),
    ))
    assert signal.actionable
    return signal


class _FakeEngine:
    """Just enough engine for the book: a journal, settings, one answer."""

    def __init__(self, journal, signal):
        self.journal = journal
        self.signal = signal
        self.source = SimpleNamespace(name="feed")
        self.asked = []

    def gate_settings(self):
        return GateSettings()

    def evaluate_series(self, series, asset, timeframe, calibration=None,
                        trade_duration=None, settings=None):
        import uuid

        self.asked.append((trade_duration, settings))
        if self.signal is None:
            return None
        # A real evaluation mints a fresh signal every time; the fake has to
        # as well, or the journal's primary key hides transition bugs.
        fresh = replace(self.signal, id=uuid.uuid4().hex[:12])
        if trade_duration:
            return replace(fresh, trade_duration=int(trade_duration))
        return fresh


def _experiment_rows(journal):
    return journal._connection.execute(
        "SELECT experiment, direction, trade_duration FROM signals "
        "WHERE experiment IS NOT NULL ORDER BY experiment"
    ).fetchall()


class TestTheRoster:
    def test_absent_means_the_shipped_roster(self):
        assert roster_from_config({}) == DEFAULT_ROSTER
        assert len(DEFAULT_ROSTER) == 6

    def test_the_mirror_is_the_live_rulebook_reversed(self):
        # 423 pooled calls put the live strategy's Wilson upper bound below
        # a coin flip; the mirror is the pre-registered test of whether the
        # complement survives out of sample. Same thresholds, same expiry —
        # only the side flips.
        mirror = next(e for e in DEFAULT_ROSTER if e.label == "mirror")
        assert mirror.invert is True
        assert mirror.overrides == ()
        assert mirror.trade_duration is None

    def test_both_horizons_are_always_in_the_race(self):
        # Whichever expiry the live strategy drives, the other horizon keeps
        # its measurement — and the matching one doubles as a control group.
        durations = {e.trade_duration for e in DEFAULT_ROSTER}
        assert {30, 180} <= durations

    def test_an_empty_list_turns_racing_off(self):
        assert roster_from_config({"experiments": []}) == ()

    def test_a_custom_roster_is_read(self):
        roster = roster_from_config({"experiments": [
            {"label": "mine", "overrides": {"min_shown_confidence": 80},
             "trade_duration": 60, "invert": True},
        ]})
        assert len(roster) == 1
        assert roster[0].label == "mine"
        assert roster[0].trade_duration == 60
        assert roster[0].invert is True
        assert roster[0].settings(GateSettings()).min_shown_confidence == 80

    def test_an_unknown_setting_skips_that_experiment_only(self):
        roster = roster_from_config({"experiments": [
            {"label": "broken", "overrides": {"win_more": 100}},
            {"label": "fine"},
        ]})
        assert [e.label for e in roster] == ["fine"]

    def test_a_label_less_entry_is_skipped(self):
        assert roster_from_config({"experiments": [{"overrides": {}}]}) == ()

    def test_a_yaml_typo_skips_the_entry_instead_of_the_whole_app(self):
        # trade_duration: 60s once raised out of the constructor and the
        # overlay never opened. A typo costs one experiment, never a session.
        roster = roster_from_config({"experiments": [
            {"label": "typo", "trade_duration": "60s"},
            {"label": "negative", "trade_duration": -30},
            {"label": "bad-override", "overrides": {"min_shown_confidence": "high"}},
            {"label": "fine", "trade_duration": "60"},
        ]})
        assert [e.label for e in roster] == ["fine"]
        assert roster[0].trade_duration == 60

    def test_a_quoted_number_in_an_override_still_counts(self):
        roster = roster_from_config({"experiments": [
            {"label": "quoted", "overrides": {"min_shown_confidence": "80"}},
        ]})
        assert roster[0].settings(GateSettings()).min_shown_confidence == 80.0

    def test_the_fade_experiment_is_the_inversion_the_record_suggested(self):
        fade = next(e for e in DEFAULT_ROSTER if e.label == "fade-overheat")
        settings = fade.settings(GateSettings())
        assert fade.invert is True
        assert settings.overheat_ceiling == 0.0  # it hunts exactly the 90+
        assert settings.min_shown_confidence == 90.0

    def test_the_three_minute_experiment_waives_only_the_fit_gate(self):
        three = next(e for e in DEFAULT_ROSTER if e.label == "three-minute")
        settings = three.settings(GateSettings())
        assert three.trade_duration == 180
        assert settings.min_duration_compatibility == 0.0
        # Everything else stays the live rulebook.
        assert settings.min_confidence == GateSettings().min_confidence


class TestOneSetupIsOneRowPerRulebook:
    def _book(self, *experiments):
        return ShadowBook(tuple(experiments))

    def test_a_standing_setup_is_recorded_once(self, tmp_path):
        journal = Journal(str(tmp_path / "j.db"))
        try:
            engine = _FakeEngine(journal, _real_signal())
            book = self._book(Experiment("solo"))
            for _ in range(5):  # five sweeps, one standing setup
                book.sweep_chart(engine, "EUR/USD OTC", 60, object(), None)
            assert len(_experiment_rows(journal)) == 1
        finally:
            journal.close()

    def test_a_setup_that_closes_and_returns_is_two_rows(self, tmp_path):
        journal = Journal(str(tmp_path / "j.db"))
        try:
            signal = _real_signal()
            engine = _FakeEngine(journal, signal)
            book = self._book(Experiment("solo"))
            book.sweep_chart(engine, "EUR/USD OTC", 60, object(), None)
            engine.signal = None  # the setup dies...
            book.sweep_chart(engine, "EUR/USD OTC", 60, object(), None)
            engine.signal = signal  # ...and re-arms
            book.sweep_chart(engine, "EUR/USD OTC", 60, object(), None)
            assert len(_experiment_rows(journal)) == 2
        finally:
            journal.close()

    def test_the_inverted_experiment_records_the_opposite_call(self, tmp_path):
        journal = Journal(str(tmp_path / "j.db"))
        try:
            signal = _real_signal(direction=1)
            assert signal.direction is Direction.CALL
            engine = _FakeEngine(journal, signal)
            self._book(Experiment("fade", invert=True)).sweep_chart(
                engine, "EUR/USD OTC", 60, object(), None
            )
            rows = _experiment_rows(journal)
            assert rows[0]["direction"] == "PUT"  # the read said CALL
        finally:
            journal.close()

    def test_the_duration_override_lands_on_the_row(self, tmp_path):
        journal = Journal(str(tmp_path / "j.db"))
        try:
            engine = _FakeEngine(journal, _real_signal())
            self._book(Experiment("slow", trade_duration=180)).sweep_chart(
                engine, "EUR/USD OTC", 60, object(), None
            )
            assert _experiment_rows(journal)[0]["trade_duration"] == 180
        finally:
            journal.close()

    def test_an_expiry_shorter_than_the_bars_sits_out(self, tmp_path):
        # A 30-second trade on a one-minute chart would be settled by a bar
        # that closes after the trade ended — a price it did not settle at.
        journal = Journal(str(tmp_path / "j.db"))
        try:
            engine = _FakeEngine(journal, _real_signal())
            book = self._book(Experiment("fast", trade_duration=30))
            book.sweep_chart(engine, "EUR/USD OTC", 60, object(), None)
            assert engine.asked == []       # not even evaluated
            assert _experiment_rows(journal) == []

            # The same experiment runs fine on bars its expiry can cover.
            book.sweep_chart(engine, "EUR/USD OTC", 5, object(), None)
            assert len(_experiment_rows(journal)) == 1
        finally:
            journal.close()

    def test_an_empty_roster_asks_nothing(self, tmp_path):
        journal = Journal(str(tmp_path / "j.db"))
        try:
            engine = _FakeEngine(journal, _real_signal())
            ShadowBook(()).sweep_chart(engine, "EUR/USD OTC", 60, object(), None)
            assert engine.asked == []
        finally:
            journal.close()


class TestShadowsNeverLeak:
    """The whole feature stands on this class."""

    def _journal_with_shadow_loss(self, tmp_path):
        from datetime import timedelta

        from poa.models import utcnow

        journal = Journal(str(tmp_path / "j.db"))
        stamp = (utcnow() - timedelta(minutes=1)).isoformat()
        journal._connection.execute(
            "INSERT INTO signals (id, timestamp, asset, chart_timeframe, "
            "trade_duration, direction, state, direction_confidence, "
            "duration_confidence, overall_confidence, setup_quality, "
            "outcome, outcome_at, source, experiment) VALUES ('sh1', ?, "
            "'EUR/USD OTC', 5, 30, 'CALL', 'SETTLED', 88, 80, 87, 'STRONG', "
            "'loss', ?, 'feed', 'fade-overheat')",
            (stamp, stamp),
        )
        journal._connection.commit()
        return journal

    def test_a_shadow_loss_cannot_stand_the_live_pair_down(self, tmp_path):
        journal = self._journal_with_shadow_loss(tmp_path)
        try:
            assert journal.last_loss_at("EUR/USD OTC", source="feed") is None
        finally:
            journal.close()

    def test_the_session_tally_does_not_count_shadow_calls(self, tmp_path):
        journal = self._journal_with_shadow_loss(tmp_path)
        try:
            stats = journal.statistics(asset="EUR/USD OTC", source="feed")
            assert stats.get("losses", 0) == 0
        finally:
            journal.close()

    def test_the_session_report_does_not_list_shadow_calls(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        from poa.reporting import build_report, collect

        journal = self._journal_with_shadow_loss(tmp_path)
        try:
            started = datetime.now(timezone.utc) - timedelta(hours=1)
            report = collect(journal, started=started, source="feed")
            assert report.calls == []
            assert "CALLS MADE" in build_report(report)
        finally:
            journal.close()

    def test_recent_hides_shadows_unless_asked(self, tmp_path):
        # The dashboard's journal view and the session report both read
        # recent(); a deliberately inverted experiment row presented there
        # as one of the tool's calls would be indistinguishable from a lie.
        journal = self._journal_with_shadow_loss(tmp_path)
        try:
            assert journal.recent(limit=10) == []
            shadows = journal.recent(limit=10, include_experiments=True)
            assert len(shadows) == 1
        finally:
            journal.close()

    def test_the_ledger_races_them_and_keeps_the_live_tables_clean(self, tmp_path):
        from poa.reporting.ledger import collect_ledger, ledger_lines

        journal = self._journal_with_shadow_loss(tmp_path)
        try:
            journal._connection.execute(
                "INSERT INTO signals (id, timestamp, asset, chart_timeframe, "
                "trade_duration, direction, state, direction_confidence, "
                "duration_confidence, overall_confidence, setup_quality, "
                "outcome, source) SELECT 'live1', timestamp, asset, "
                "chart_timeframe, trade_duration, direction, state, "
                "direction_confidence, duration_confidence, "
                "overall_confidence, setup_quality, 'win', source "
                "FROM signals WHERE id = 'sh1'"
            )
            journal._connection.commit()
            ledger = collect_ledger(journal, source="feed")
        finally:
            journal.close()

        assert ledger["overall"][0].settled == 1  # the live call only
        race = {row.label: row for row in ledger["race"]}
        assert race["live strategy"].wins == 1
        assert race["fade-overheat"].losses == 1
        text = "\n".join(ledger_lines(ledger))
        assert "THE RACE" in text
        assert "fade-overheat" in text
