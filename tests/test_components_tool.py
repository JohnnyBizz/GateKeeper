"""The check that stopped an engine change being made out of noise.

Pooled across two recordings, ``ema_alignment`` scored 41.6% with an interval
clear of fifty — which reads as a component wired backwards and worth
flipping. Split apart it was 35.9% in one recording and 48.1% in the other:
the whole effect was one half-hour of one market.

Reweighting the engine on that would have been fitting noise and shipping it
as an improvement, which is the mistake this project has already made twice
with thresholds. These tests are about the arithmetic that catches it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from components import WORTH_FINDING, auc  # noqa: E402


class TestRankingPower:
    def test_a_component_that_always_argues_for_the_winner_scores_one(self):
        value, _error = auc([(9.0, True), (8.0, True), (1.0, False), (2.0, False)])
        assert value == 100.0

    def test_one_that_always_argues_for_the_loser_scores_zero(self):
        value, _error = auc([(1.0, True), (2.0, True), (9.0, False), (8.0, False)])
        assert value == 0.0

    def test_one_that_says_nothing_scores_fifty(self):
        """Every winner and loser carrying the same value is no information."""
        value, _error = auc([(5.0, True), (5.0, True), (5.0, False), (5.0, False)])
        assert value == 50.0

    def test_it_needs_both_outcomes_to_say_anything(self):
        assert auc([(1.0, True), (2.0, True)]) is None
        assert auc([]) is None

    def _overlapping(self, per_side: int, shift: float = 6.0):
        """Winners a little higher than losers, with the ranges overlapping.

        Perfectly separated groups have no variance at all, which is not what
        any real component looks like and would make the interval tests below
        pass for the wrong reason.
        """
        winners = [(float(i) + shift, True) for i in range(per_side)]
        losers = [(float(i), False) for i in range(per_side)]
        return winners + losers

    def test_the_interval_narrows_as_the_sample_grows(self):
        """Which is the whole reason a single recording cannot settle this."""
        assert auc(self._overlapping(20))[1] > auc(self._overlapping(400))[1]

    def test_a_hundred_calls_cannot_resolve_an_edge_worth_finding(self):
        """The finding that stopped the engine being changed.

        At the sample sizes these recordings hold, a component with a real
        55% edge produces an interval that still contains 50 — so "not
        significant" here means "not measured", not "not there".
        """
        # shift 2.5 over fifty a side is an AUC near 55 — the size worth
        # hunting for, and small enough that a hundred calls cannot see it.
        value, error = auc(self._overlapping(50, shift=2.5))

        # A small, real effect of about the size worth hunting for.
        assert 53.0 < value < 57.0
        # And at this sample size its interval still reaches back past 50, so
        # "not significant" here means "not measured", not "not there".
        assert value - 1.96 * error < 50.0
        assert 1.96 * error > (WORTH_FINDING - 50.0)


class TestItRefusesToPoolRecordings:
    def test_two_opposite_halves_average_to_a_finding_that_is_not_there(self):
        """Why the tool reports per recording and never pools first."""
        first = [(9.0, True)] * 20 + [(1.0, False)] * 20    # strongly predictive
        second = [(1.0, True)] * 20 + [(9.0, False)] * 20   # exactly backwards

        assert auc(first)[0] == 100.0
        assert auc(second)[0] == 0.0
        # Together they look like pure noise, and apart they are two findings
        # that cancel. Either way, pooling destroys the information that the
        # halves disagree — which is the thing worth knowing.
        assert auc(first + second)[0] == 50.0

    def test_agreement_across_recordings_is_what_the_tool_requires(self):
        """A real effect holds its direction; noise does not."""
        sides = []
        for rows in ([(9.0, True)] * 20 + [(1.0, False)] * 20,
                     [(8.0, True)] * 20 + [(2.0, False)] * 20):
            value, error = auc(rows)
            sides.append(1 if value - 1.96 * error > 50 else 0)
        assert sides == [1, 1]


class TestItReadsTheCallsActuallyMade:
    """Replaying a recording measures the engine against a market. This
    measures it against its own live behaviour, which is the thing being
    complained about.

    It needs nothing collected specially: the journal already stores every
    signal's component breakdown in its payload, so every session that has
    ever run is already in there and every future one adds to it. The answer
    accumulates while the app is simply used.
    """

    def _journal(self, tmp_path, sittings=((0, 12), (7200, 12))):
        import datetime

        from conftest import good_quality, trending_series
        from poa.signals import GateSettings, SignalEngine, SignalRequest
        from poa.storage.journal import Journal

        path = tmp_path / "journal.db"
        journal = Journal(path)
        series = trending_series(400, step=0.00012)
        base = datetime.datetime(2026, 8, 21, 10, 0, tzinfo=datetime.timezone.utc)
        for sitting, (offset, count) in enumerate(sittings):
            for i in range(count):
                signal = SignalEngine().evaluate(SignalRequest(
                    series=series, asset="EUR/USD", chart_timeframe=5,
                    trade_duration=30, quality=good_quality(series),
                    settings=GateSettings(min_confidence=0.0),
                ))
                signal.id = f"s{sitting}-{i}"
                signal.timestamp = base + datetime.timedelta(seconds=offset + i * 40)
                journal.record(signal, None, source="feed")
                journal._connection.execute(
                    "UPDATE signals SET outcome=? WHERE id=?",
                    ("win" if i % 2 else "loss", signal.id),
                )
        journal._connection.commit()
        return path

    def test_a_sitting_becomes_a_group(self, tmp_path):
        from components import from_journal

        groups = from_journal(self._journal(tmp_path))

        assert len(groups) == 2
        assert all(len(rows) == 12 for rows in groups.values())

    def test_each_call_carries_every_component(self, tmp_path):
        from components import from_journal

        groups = from_journal(self._journal(tmp_path))
        components, _won = next(iter(groups.values()))[0]

        for expected in ("macd", "rsi", "heikin_ashi", "ema_alignment"):
            assert expected in components

    def test_a_long_gap_splits_the_groups(self, tmp_path):
        """Two hours apart is two sessions; two minutes apart is one."""
        from components import from_journal

        together = from_journal(self._journal(tmp_path, sittings=((0, 8), (120, 8))))
        apart = from_journal(self._journal(tmp_path, sittings=((0, 8), (9999, 8))))

        assert len(together) == 1
        assert len(apart) == 2

    def test_unsettled_calls_are_not_counted(self, tmp_path):
        """A call whose expiry has not elapsed says nothing about anything."""
        from components import from_journal
        from poa.storage.journal import Journal

        path = self._journal(tmp_path)
        journal = Journal(path)
        journal._connection.execute("UPDATE signals SET outcome=NULL WHERE id LIKE 's0-%'")
        journal._connection.commit()

        groups = from_journal(path)

        assert sum(len(rows) for rows in groups.values()) == 12

    def test_one_session_is_refused_rather_than_reported(self, tmp_path, capsys):
        """The whole lesson of this file: one window cannot tell a finding
        from a coincidence, so it must not print a table that looks like one."""
        from components import _report_live

        code = _report_live(self._journal(tmp_path, sittings=((0, 12),)))

        assert code == 1
        assert "cannot tell a finding from a coincidence" in capsys.readouterr().out

    def test_two_sessions_get_the_replication_column(self, tmp_path, capsys):
        from components import _report_live

        code = _report_live(self._journal(tmp_path))
        out = capsys.readouterr().out

        assert code == 0
        assert "replicates?" in out
        assert "macd" in out

    def test_a_missing_journal_says_so_plainly(self, tmp_path, capsys):
        from components import _report_live

        assert _report_live(tmp_path / "nothing.db") == 1
        assert "Run the app first" in capsys.readouterr().out
