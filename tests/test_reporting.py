"""The written account of a session.

The panel is a live instrument: it shows what is true at the moment you look
and forgets. Judging whether the thing is any good needs the whole session
laid out at once, in a file that outlives the process and can be read by
somebody who was not sitting in front of it.

One claim runs through every test here, because getting it wrong would make
the report worse than no report: **nothing in it was bought.** The assistant
places no trades. The return figure is what its calls would have returned had
each been taken, and the document is never allowed to read as though money
moved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from poa.reporting import SessionReport, build_report, collect, write_report

START = datetime(2026, 8, 16, 9, 12, tzinfo=timezone.utc)


def _call(minutes, asset="EUR/USD OTC", direction="CALL", score=78.0,
          outcome="win", price=1.19280, duration=180):
    return {
        "timestamp": (START + timedelta(minutes=minutes)).isoformat(),
        "asset": asset,
        "direction": direction,
        "overall_confidence": score,
        "trade_duration": duration,
        "price": price,
        "outcome": outcome,
    }


def _report(calls=(), manual=(), stake=1.0, payout=0.92, **kw):
    return SessionReport(
        started=START,
        ended=START + timedelta(hours=2),
        calls=list(calls),
        manual=list(manual),
        stake=stake,
        payout=payout,
        **kw,
    )


class TestATradeNobodyCalledHasNoScore:
    """Zero is the marker for "nobody could attribute this trade", not a
    score of nought. Printed as a number it reads as the assistant having
    rated the trade zero out of a hundred and then been proved right or wrong
    by it — a claim about the engine that nothing supports."""

    def test_an_unattributed_trade_shows_a_dash(self):
        text = build_report(_report(manual=[_call(5, score=0.0, outcome="loss")]))
        row = [line for line in text.splitlines() if "EUR/USD OTC" in line][-1]
        assert "—" in row
        assert " 0 " not in row

    def test_an_attributed_trade_still_shows_its_score(self):
        text = build_report(_report(manual=[_call(5, score=81.0)]))
        row = [line for line in text.splitlines() if "EUR/USD OTC" in line][-1]
        assert "81" in row

    def test_the_column_says_what_a_dash_means(self):
        text = build_report(_report(manual=[_call(5, score=0.0)]))
        assert "nothing of ours was live on it" in text

    def test_the_outcome_is_still_counted(self):
        """The trade is real. Only the score is unknown."""
        text = build_report(_report(manual=[_call(5, score=0.0, outcome="loss")]))
        assert "1 recorded" in text and "LOSS" in text


class TestItNeverReadsAsThoughMoneyMoved:
    """The assistant places no trades and never has.

    A report an investor reads as a record of executed trades, when it is a
    record of calls nobody was obliged to take, is worse than no report.
    """

    def test_the_header_says_so_before_any_number(self):
        text = build_report(_report([_call(30)]))
        head = text[: text.index("CALLS MADE")]
        assert "places no trades" in head
        assert "notional" in head.lower()

    def test_the_return_is_labelled_as_not_placed(self):
        text = build_report(_report([_call(30), _call(35)]))
        assert "(not placed)" in text

    def test_nothing_is_promised(self):
        forbidden = (
            "guaranteed", "guarantee", "100% accurate", "cannot lose",
            "risk-free", "sure thing", "certain profit",
        )
        text = build_report(
            _report([_call(30), _call(35, outcome="loss")])
        ).lower()
        for word in forbidden:
            assert word not in text

    def test_the_caveats_are_always_present(self):
        for calls in ([], [_call(30)]):
            text = build_report(_report(calls))
            assert "Nothing above was bought" in text
            assert "not a trading recommendation" in text


class TestTheCallsItMade:
    def test_each_call_is_listed_with_what_produced_it(self):
        text = build_report(_report([_call(36, score=78.0)]))
        assert "09:48" in text
        assert "EUR/USD OTC" in text
        assert "78" in text
        assert "3 MIN" in text
        assert "1.19280" in text

    def test_a_silent_session_says_what_that_means(self):
        """Zero calls is a finding, not an empty page."""
        text = build_report(_report([]))
        assert "No setup passed the gates this session." in text
        assert "Not the same as nothing happening" in text

    def test_unsettled_calls_are_shown_and_excluded_from_the_rate(self):
        report = _report([_call(30), _call(35), _call(118, outcome=None)])
        assert report.open == 1
        assert report.win_rate == 100.0  # the open one is not counted either way
        text = build_report(report)
        assert "UNSETTLED" in text
        assert "excluded from the rate" in text


class TestWhatTheCallsCameTo:
    def test_the_notional_return_is_priced_at_stake_and_payout(self):
        report = _report(
            [_call(30), _call(33), _call(36, outcome="loss")],
            stake=20.0, payout=0.92,
        )
        # Two wins at 0.92 of 20, one loss of 20.
        assert report.notional == pytest.approx(2 * 20 * 0.92 - 20)
        assert "+$16.80" in build_report(report)

    def test_a_losing_session_reports_a_loss(self):
        report = _report(
            [_call(30, outcome="loss"), _call(33, outcome="loss")], stake=10.0
        )
        assert report.notional == -20.0
        assert "-$20.00" in build_report(report)

    def test_the_rate_is_put_against_break_even_not_fifty(self):
        """At 92% a 50% win rate loses money. Against 50% it looks like a draw."""
        report = _report([_call(30), _call(33, outcome="loss")], payout=0.92)
        text = build_report(report)
        assert "52.1%" in text
        assert "below" in text

    def test_a_small_sample_says_it_is_a_small_sample(self):
        text = build_report(_report([_call(30), _call(33)]))
        assert "too few to read" in text

    def test_nothing_settled_is_not_reported_as_zero_percent(self):
        report = _report([_call(30, outcome=None)])
        assert report.win_rate is None
        assert "nothing settled" in build_report(report)


class TestYourOwnTradesAreKeptSeparate:
    """Conflating them would be the same lie in the other direction.

    A trade the user placed is not a call the assistant made, and a report
    that pooled them could not be used to judge either one.
    """

    def test_they_have_their_own_section(self):
        text = build_report(_report(calls=[_call(30)], manual=[_call(45)]))
        assert "TRADES YOU PLACED" in text
        assert text.index("CALLS MADE") < text.index("TRADES YOU PLACED")

    def test_they_do_not_move_the_assistant_s_rate(self):
        report = _report(
            calls=[_call(30, outcome="loss")],
            manual=[_call(45), _call(48), _call(51)],
        )
        assert report.win_rate == 0.0  # the three hand-placed wins are not its
        assert report.notional < 0

    def test_the_section_is_absent_when_there_are_none(self):
        assert "TRADES YOU PLACED" not in build_report(_report([_call(30)]))


class TestGatheringFromTheJournal:
    def _journal(self, tmp_path):
        from poa.storage.journal import Journal

        return Journal(str(tmp_path / "j.db"))

    def _file(self, journal, minutes, won=True, manual=True):
        journal.record_manual(
            asset="EUR/USD OTC", chart_timeframe=60, trade_duration=180,
            direction="CALL", direction_confidence=78.0, duration_confidence=80.0,
            won=won, source="feed", price=1.19280,
            timestamp=START + timedelta(minutes=minutes),
        )

    def test_hand_entered_trades_land_in_their_own_list(self, tmp_path):
        journal = self._journal(tmp_path)
        self._file(journal, 30)
        self._file(journal, 40, won=False)
        report = collect(journal, started=START, source="feed")
        assert len(report.manual) == 2
        assert report.calls == []

    def test_rows_from_before_the_session_are_left_out(self, tmp_path):
        """The journal outlives the app; the report describes one session."""
        journal = self._journal(tmp_path)
        self._file(journal, -600)  # ten hours before this session began
        self._file(journal, 30)
        report = collect(journal, started=START, source="feed")
        assert len(report.manual) == 1

    def test_another_data_source_is_a_different_experiment(self, tmp_path):
        journal = self._journal(tmp_path)
        self._file(journal, 30)
        report = collect(journal, started=START, source="synthetic")
        assert report.manual == []

    def test_they_come_out_in_the_order_they_happened(self, tmp_path):
        """The journal reads newest first; a report is read forwards."""
        journal = self._journal(tmp_path)
        for minutes in (60, 20, 40):
            self._file(journal, minutes)
        report = collect(journal, started=START, source="feed")
        stamps = [row["timestamp"] for row in report.manual]
        assert stamps == sorted(stamps)

    def test_a_broken_journal_does_not_take_the_report_with_it(self, tmp_path):
        class Broken:
            def recent(self, limit=0):
                raise RuntimeError("database is locked")

        report = collect(Broken(), started=START)
        assert report.calls == [] and report.manual == []
        assert "GATEKEEPER" in build_report(report)


class TestWritingItOut:
    def test_it_lands_in_the_folder_named_for_when_it_started(self, tmp_path):
        path = write_report(_report([_call(30)]), tmp_path / "reports")
        assert path.name == "session-2026-08-16-0912.txt"
        assert path.parent.name == "reports"

    def test_the_folder_is_created_if_it_is_not_there(self, tmp_path):
        path = write_report(_report(), tmp_path / "a" / "b" / "reports")
        assert path.exists()

    def test_the_file_reads_back_as_written(self, tmp_path):
        report = _report([_call(30)])
        path = write_report(report, tmp_path)
        assert path.read_text(encoding="utf-8") == build_report(report)

    def test_the_names_sort_into_the_order_they_happened(self, tmp_path):
        first = _report([_call(30)])
        second = SessionReport(
            started=START + timedelta(hours=5), ended=START + timedelta(hours=6)
        )
        names = sorted(
            p.name for p in (write_report(first, tmp_path), write_report(second, tmp_path))
        )
        assert names[0].endswith("0912.txt") and names[1].endswith("1412.txt")


class TestTheSessionEndWritesOne:
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

    def test_closing_the_app_writes_the_report(self, tmp_path):
        app = self._app(tmp_path)
        app.shutdown()
        written = list((tmp_path / "reports").glob("session-*.txt"))
        assert len(written) == 1
        assert "GATEKEEPER — SESSION REPORT" in written[0].read_text(encoding="utf-8")

    def test_a_report_that_cannot_be_written_does_not_block_the_exit(self, tmp_path):
        """Losing the report is bad. Hanging on the way out is worse."""
        app = self._app(tmp_path)
        app._report_dir = lambda: (_ for _ in ()).throw(OSError("read-only"))
        app.shutdown()  # must not raise

    def test_the_charts_looked_at_are_named(self, tmp_path):
        app = self._app(tmp_path)
        try:
            app._charts_seen.add("EUR/USD OTC 1 MIN")
            path = app.write_session_report()
            assert "EUR/USD OTC 1 MIN" in path.read_text(encoding="utf-8")
        finally:
            app.shutdown()


class TestTheReportCanActuallyBeFound:
    """The app kept score all along, in a folder Windows hides by default.

    "It already writes a report" was true and useless at the same time: the
    file lands under AppData\\Local, which is not shown in Explorer unless you
    have turned hidden files on or typed the path. A scorecard nobody can find
    does not measure anything.
    """

    def _app(self, tmp_path, monkeypatch):
        from poa.config import load_config
        from poa.overlay import app as app_module
        from poa.overlay.app import OverlayApp

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        config.set("capture.source", "synthetic")
        config.set("capture.source_chosen", True)
        monkeypatch.setattr(app_module, "data_root", lambda: tmp_path)
        return OverlayApp(config)

    def test_closing_the_app_opens_the_folder_the_report_is_in(
        self, tmp_path, monkeypatch
    ):
        app = self._app(tmp_path, monkeypatch)
        opened: list = []
        monkeypatch.setattr(app, "_reveal", lambda path: opened.append(path))

        app.shutdown()

        assert opened, "the session report was written somewhere nobody was shown"
        assert opened[0] == app._report_dir()

    def test_it_can_be_turned_off_once_you_know_the_path(
        self, tmp_path, monkeypatch
    ):
        app = self._app(tmp_path, monkeypatch)
        opened: list = []
        monkeypatch.setattr(app, "_reveal", lambda path: opened.append(path))
        app.config.set("overlay.reveal_report", False)

        app.shutdown()

        assert opened == []

    def test_a_report_that_could_not_be_written_opens_nothing(
        self, tmp_path, monkeypatch
    ):
        """An empty folder popping open would be a worse lie than silence."""
        app = self._app(tmp_path, monkeypatch)
        opened: list = []
        monkeypatch.setattr(app, "_reveal", lambda path: opened.append(path))
        monkeypatch.setattr(app, "write_session_report", lambda: None)

        app.shutdown()

        assert opened == []

    def test_the_report_lands_where_it_says_it_does(self, tmp_path, monkeypatch):
        app = self._app(tmp_path, monkeypatch)
        try:
            written = app.write_session_report()
            assert written is not None
            assert written.parent == app._report_dir()
            assert written.name.startswith("session-")
            assert written.suffix == ".txt"
            # Plain text, so it opens anywhere and pastes into a message.
            assert "GATEKEEPER" in written.read_text(encoding="utf-8")
        finally:
            app.shutdown()
