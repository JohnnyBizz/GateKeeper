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


class TestAnOutcomeIsNamedAsWhatItWas:
    """Five ties in one session were reported as "expiry had not elapsed".

    They were made between 11:41 and 12:20 on thirty-second expiries in a
    session that ran to 12:46, so every one of them had elapsed by minutes at
    the least. A tie is a refund and a void is undecidable, and neither is an
    unfinished trade. The same mislabel was fixed once for voids and survived
    here for everything that was not a win or a loss.
    """

    def test_a_tie_is_called_a_tie(self):
        report = _report([_call(30), _call(35, outcome="flat")])
        assert report.flat == 1
        assert report.open == 0, "it expired; saying otherwise is untrue"
        text = build_report(report)
        assert "Flat" in text
        assert "a refund" in text

    def test_a_tie_moves_the_rate_neither_way(self):
        report = _report([_call(30), _call(35, outcome="flat")])
        assert report.win_rate == 100.0

    def test_an_undecidable_call_says_that_rather_than_unfinished(self):
        report = _report([_call(30), _call(35, outcome="void")])
        assert report.undecided == 1
        assert report.open == 0
        text = build_report(report)
        assert "Could not be settled" in text
        assert "nothing could decide them" in text

    def test_an_unreadable_outcome_counts_with_the_undecidable(self):
        report = _report([_call(30), _call(35, outcome="unknown")])
        assert report.undecided == 1
        assert report.open == 0

    def test_a_call_that_really_has_not_expired_still_says_so(self):
        """The label is right for exactly one thing and keeps it."""
        report = _report([_call(30), _call(118, outcome=None)])
        assert report.open == 1
        assert report.flat == 0 and report.undecided == 0
        assert "expiry had not elapsed" in build_report(report)

    def test_the_four_do_not_overlap(self):
        report = _report([
            _call(30), _call(31, outcome="loss"), _call(32, outcome="flat"),
            _call(33, outcome="void"), _call(34, outcome=None),
        ])
        assert (report.wins, report.losses) == (1, 1)
        assert (report.flat, report.undecided, report.open) == (1, 1, 1)
        assert report.wins + report.losses + report.flat + report.undecided \
            + report.open == len(report.calls)


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


class TestAnEmptySessionSaysWhetherThatWasExpected:
    """Ten reports in a row read "0 calls" and every one was taken as a fault.

    Measured on real recordings at the shipped gates, the tool produces a
    directional call every two or three minutes on a 5 SEC chart and every
    eighteen to two hundred minutes on a 1 MIN one. An hour on the slow chart
    is *expected* to be silent — and a report that could not say so sent
    everybody hunting a bug that was not there, twice.
    """

    def _report(self, charts, minutes):
        from datetime import datetime, timedelta, timezone

        from poa.reporting.session import SessionReport, build_report

        started = datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)
        return build_report(SessionReport(
            started=started,
            ended=started + timedelta(minutes=minutes),
            source="feed",
            charts=list(charts),
            calls=[],
            payout=0.92,
        ))

    def test_an_hour_on_a_minute_chart_is_expected_to_be_quiet(self):
        text = self._report(["EUR/USD 1 MIN"], 53)

        assert "expected outcome, not a fault" in text
        assert "open the 5 SEC chart" in text

    def test_and_it_says_where_the_calls_actually_are(self):
        text = self._report(["EUR/USD 1 MIN"], 53)

        # The number that makes the advice actionable rather than vague.
        assert "every 120 minutes" in text or "every 2 minutes" in text
        assert "chart you have OPEN" in text

    def test_a_quiet_hour_on_a_fast_chart_is_worth_investigating(self):
        """Same silence, opposite meaning — 5 SEC should have spoken ~20 times."""
        text = self._report(["EUR/USD 5 SEC"], 60)

        assert "fewer than expected" in text
        assert "expected outcome, not a fault" not in text

    def test_the_fastest_chart_being_read_is_the_one_judged(self):
        """A 5 SEC chart in the list means calls were available, 1 MIN or not."""
        text = self._report(["EUR/USD 1 MIN", "EUR/USD 5 SEC"], 60)

        assert "fewer than expected" in text

    def test_a_session_with_no_recognisable_chart_says_nothing_extra(self):
        text = self._report([], 30)

        assert "expected outcome" not in text
        assert "No setup passed the gates" in text

    def test_a_session_with_calls_is_left_alone(self):
        from datetime import datetime, timedelta, timezone

        from poa.reporting.session import SessionReport, build_report

        started = datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)
        text = build_report(SessionReport(
            started=started,
            ended=started + timedelta(minutes=53),
            charts=["EUR/USD 1 MIN"],
            calls=[{"timestamp": started, "asset": "EUR/USD", "direction": "PUT",
                    "overall_confidence": 88.0, "trade_duration": 180,
                    "price": 1.16, "outcome": "win"}],
            payout=0.92,
        ))

        assert "expected outcome, not a fault" not in text


class TestTheReportAsksTheChartQuestionSeparately:
    """Payout decides whether a rate is worth money. It says nothing about
    whether the reading of the chart was any good, and mixing the two hid the
    thing that matters — a session can clear break-even because the payout was
    generous and the market trended, with the analysis contributing nothing.
    """

    def _scored(self, score, outcome, asset="EUR/USD OTC", direction="CALL"):
        return _call(30, asset=asset, direction=direction, score=score,
                     outcome=outcome)

    def _session(self):
        """A score wired backwards: the high band loses, the low band wins."""
        calls = [self._scored(86, "win", asset=f"P{i}") for i in range(9)]
        calls += [self._scored(86, "loss", asset=f"Q{i}") for i in range(3)]
        calls += [self._scored(93, "loss", asset=f"R{i}") for i in range(9)]
        calls += [self._scored(93, "win", asset=f"S{i}") for i in range(3)]
        return _report(calls)

    def test_it_says_how_many_reads_the_calls_really_were(self):
        text = build_report(_report([
            self._scored(90, "win", asset="EUR/USD OTC", direction="PUT"),
            self._scored(90, "win", asset="EUR/USD OTC", direction="PUT"),
            self._scored(90, "loss", asset="AUD/CAD OTC", direction="CALL"),
        ]))
        assert "3 settled, in 2 episodes" in text

    def test_a_score_pulling_the_wrong_way_is_named_as_that(self):
        text = build_report(self._session())
        assert "Ranking power (AUC)" in text
        assert "worse than nothing" in text

    def test_the_band_column_shows_the_direction_of_travel(self):
        text = build_report(self._session())
        assert "BY SCORE BAND" in text
        assert "85-89" in text and "90-94" in text

    def test_a_pair_called_only_one_way_is_flagged(self):
        calls = [self._scored(90, "loss", asset="AUD/USD OTC") for _ in range(4)]
        calls += [self._scored(90, "win", asset="EUR/USD OTC", direction="PUT")]
        text = build_report(_report(calls))
        assert "one-way" in text
        assert "4C/0P" in text

    def test_the_chart_question_does_not_mention_payout(self):
        """It is asked without reference to what a win pays, on purpose."""
        from poa.reporting.session import _edge_section

        block = "\n".join(_edge_section(self._session()))
        assert "payout" not in block.lower()
        assert "break-even" not in block.lower()

    def test_too_few_calls_says_nothing_rather_than_something_thin(self):
        from poa.reporting.session import _edge_section

        assert _edge_section(_report([self._scored(90, "win")])) == []


class TestTheLedgerPoolsEverySession:
    """A session report judges one sitting, and one sitting is nearly always
    too small to judge anything. The ledger is the part that grows: every
    settled call the journal holds, split by expiry, score band, pair and
    hour — the axes where FINDINGS.md's open questions get answered one
    session at a time. Same honesty bar as everywhere else: under twenty
    calls a row shows its count and no rate."""

    def _journal(self, tmp_path):
        from poa.storage.journal import Journal

        return Journal(str(tmp_path / "j.db"))

    def _settled(self, journal, id, *, minutes=0, asset="EUR/USD OTC",
                 duration=30, shown=87.0, outcome="win", notes=None,
                 source="feed"):
        stamp = (START + timedelta(minutes=minutes)).isoformat()
        journal._connection.execute(
            "INSERT INTO signals (id, timestamp, asset, chart_timeframe, "
            "trade_duration, direction, state, direction_confidence, "
            "duration_confidence, overall_confidence, setup_quality, "
            "outcome, notes, source) VALUES (?, ?, ?, 5, ?, 'CALL', "
            "'SETTLED', 86, 80, ?, 'STRONG', ?, ?, ?)",
            (id, stamp, asset, duration, shown, outcome, notes, source),
        )
        journal._connection.commit()

    def _filled(self, tmp_path):
        journal = self._journal(tmp_path)
        for i in range(25):
            self._settled(journal, f"a{i}", minutes=i,
                          outcome="win" if i % 2 == 0 else "loss")
        for i in range(3):
            self._settled(journal, f"b{i}", minutes=120 + i, duration=180,
                          asset="AUD/CHF OTC", shown=76.0)
        self._settled(journal, "m1", notes="manual")          # the user's
        self._settled(journal, "s1", source="screen")         # another source
        return journal

    def test_it_pools_the_tools_own_calls_and_nothing_else(self, tmp_path):
        from poa.reporting.ledger import collect_ledger

        journal = self._filled(tmp_path)
        try:
            ledger = collect_ledger(journal, source="feed")
            assert ledger is not None
            assert ledger["overall"][0].settled == 28  # not 30
        finally:
            journal.close()

    def test_a_small_row_shows_its_count_and_no_rate(self, tmp_path):
        from poa.reporting.ledger import collect_ledger, ledger_lines

        journal = self._filled(tmp_path)
        try:
            lines = "\n".join(ledger_lines(collect_ledger(journal, source="feed")))
        finally:
            journal.close()
        three_min = next(l for l in lines.splitlines() if "3 MIN" in l)
        assert "3 calls" in three_min
        assert "%" not in three_min
        thirty = next(l for l in lines.splitlines() if "30 SEC" in l)
        assert "52.0%" in thirty and "(13W/12L)" in thirty

    def test_it_splits_along_the_open_questions(self, tmp_path):
        from poa.reporting.ledger import collect_ledger

        journal = self._filled(tmp_path)
        try:
            ledger = collect_ledger(journal, source="feed")
        finally:
            journal.close()
        assert [r.label for r in ledger["expiry"]] == ["30 SEC", "3 MIN"]
        assert "85-89" in [r.label for r in ledger["band"]]
        assert ledger["pair"][0].label == "EUR/USD OTC"  # most called first
        assert ledger["hour"][0].label == "09:00"

    def test_the_live_record_prints_once_when_the_race_runs(self, tmp_path):
        # The race table leads with the live strategy; the 13:41 report of
        # 2026-08-24 then closed the section by appending the same row
        # again — the one record printed twice, reading as two statements.
        import sqlite3

        from poa.reporting.ledger import collect_ledger, ledger_lines

        journal = self._filled(tmp_path)
        try:
            journal._connection.execute(
                "UPDATE signals SET experiment = 'mirror' WHERE id = 'a1'"
            )
            journal._connection.commit()
            lines = ledger_lines(collect_ledger(journal, source="feed"))
        finally:
            journal.close()
        assert sum("live strategy" in line for line in lines) == 1

    def test_without_shadows_the_closing_summary_still_prints(self, tmp_path):
        from poa.reporting.ledger import collect_ledger, ledger_lines

        journal = self._filled(tmp_path)
        try:
            lines = ledger_lines(collect_ledger(journal, source="feed"))
        finally:
            journal.close()
        assert sum("live strategy" in line for line in lines) == 1


class TestTheSessionOpensOnWhatTheRecordSays:
    """"Search for any positive calls" — done honestly: any cell of the
    pooled record qualifies, at any rate, but only with twenty-plus settled
    behind it, because a positive rate over a handful is how every
    fitted-then-failed threshold in FINDINGS.md got chosen. Said at the top
    of the record section and on the panel at start, so the refinement loop
    is visible rather than taken on faith."""

    def _rows(self, label_rate_pairs):
        from poa.reporting.ledger import LedgerRow

        rows = []
        for label, wins, losses in label_rate_pairs:
            row = LedgerRow(label)
            row.wins, row.losses = wins, losses
            rows.append(row)
        return rows

    def _ledger(self):
        return {
            "overall": self._rows([("live strategy", 30, 40)]),
            "expiry": self._rows([("30 SEC", 25, 30), ("3 MIN", 5, 9)]),
            "band": self._rows([("85-89", 12, 15)]),
            "pair": self._rows([("AUD/CHF OTC", 13, 11)]),
            "hour": self._rows([("14:00", 30, 17), ("22:00", 17, 49)]),
            "race": [],
        }

    def test_cells_split_by_break_even_with_the_sample_bar_held(self):
        from poa.reporting.ledger import record_cells

        above, below = record_cells(self._ledger(), 52.1)
        assert [label for label, _ in above] == [
            "14:00 UTC", "AUD/CHF OTC",
        ]  # 63.8 then 54.2 — and 3 MIN (14 settled) never qualifies
        assert below[0][0] == "22:00 UTC"  # worst first

    def test_the_highlights_lead_with_working_and_failing(self):
        from poa.reporting.ledger import record_highlights

        lines = "\n".join(record_highlights(self._ledger(), 52.1))
        assert "WHAT THE RECORD SAYS" in lines
        assert "Working" in lines and "14:00 UTC  63.8% over 47" in lines
        assert "Failing" in lines and "22:00 UTC  25.8% over 66" in lines

    def test_nothing_meaningful_says_nothing_at_all(self):
        from poa.reporting.ledger import record_highlights

        thin = {"expiry": self._rows([("30 SEC", 5, 4)])}
        assert record_highlights(thin, 52.1) == []
        assert record_highlights(None, 52.1) == []

    def test_no_winner_is_stated_not_padded(self):
        from poa.reporting.ledger import record_highlights, record_summary

        losing = {"hour": self._rows([("22:00", 17, 49)])}
        lines = "\n".join(record_highlights(losing, 52.1))
        assert "nothing clears break-even" in lines
        assert "hunting" in record_summary(losing, 52.1)

    def test_the_panel_line_names_the_best_cell(self):
        from poa.reporting.ledger import record_summary

        note = record_summary(self._ledger(), 52.1)
        assert "14:00 UTC" in note and "63.8%" in note and "52.1%" in note

    def test_the_report_carries_it_ahead_of_the_tables(self):
        report = _report()
        report.ledger = self._ledger()
        text = build_report(report)
        assert text.index("WHAT THE RECORD SAYS") < text.index("BY EXPIRY")

    def test_the_view_model_shows_the_note_in_evidence(self):
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        vm.record_note = "Record at start: best cell 14:00 UTC at 64% over 47."
        assert vm.record_note in vm.render()["tuning"]


class TestTheLedgerEdges:
    """The remaining edges of the pooled record."""

    def _journal(self, tmp_path):
        return TestTheLedgerPoolsEverySession()._journal(tmp_path)

    def _filled(self, tmp_path):
        return TestTheLedgerPoolsEverySession()._filled(tmp_path)

    def test_an_empty_journal_writes_no_ledger_at_all(self, tmp_path):
        from poa.reporting.ledger import collect_ledger

        journal = self._journal(tmp_path)
        try:
            assert collect_ledger(journal, source="feed") is None
        finally:
            journal.close()
        assert "THE RECORD SO FAR" not in build_report(_report())

    def test_collect_carries_it_into_the_written_report(self, tmp_path):
        journal = self._filled(tmp_path)
        try:
            report = collect(journal, started=START, source="feed")
        finally:
            journal.close()
        text = build_report(report)
        assert "THE RECORD SO FAR" in text
        assert "28 settled calls, every session" in text
        assert "BY EXPIRY" in text and "BY HOUR (UTC)" in text
