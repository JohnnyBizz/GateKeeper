"""Recording the live feed from inside the app.

The recorder used to be a second executable, and Windows refused to download
it — "RecordFeed.exe, couldn't download, virus detected" — which is a refusal
nobody can click past, because no file ever arrives. So the steps live in a
module both the app and the command-line tool call, and these are the tests
for that module and for the button that drives it.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from poa.feed.cdp import BrowserError
from poa.feed.frames import Summary, decode_frame
from poa.feed.recorder import Capture
from poa.feed import session_recording as rec


def _frames(count: int) -> list:
    """A handful of decoded frames, of the shape the platform really sends."""
    return [
        decode_frame(f'42["updateStream",[["EURUSD_otc",{1786663900 + i}.0,1.16]]]')
        for i in range(count)
    ]


def _fake_platform(frames: list, sockets: list[str] | None = None):
    """Stand in for the DevTools connection, handing over frames as it goes."""

    def record_platform(port, seconds=60.0, needle="", max_frames=0, on_frame=None):
        summary = Summary()
        kept = []
        for frame in frames:
            if on_frame is None:
                kept.append(frame)
            else:
                on_frame(frame)
            summary.add(frame)
        return Capture(frames=kept, summary=summary, sockets=list(sockets or ["wss://x"]))

    return record_platform


class TestARecordingBecomesOneSendableFile:
    """The point of a recording is a file that can be attached to a message."""

    def test_a_long_run_leaves_exactly_one_bundle(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rec, "record_platform", _fake_platform(_frames(12)))
        result = rec.record_session(9222, 1800.0, tmp_path)

        assert result.error is None
        assert result.frames == 12
        assert result.bundle is not None and result.bundle.exists()
        assert result.bundle.name.startswith("gatekeeper-recording-")
        assert result.bundle.suffix == ".zip"

    def test_the_bundle_carries_the_summary_and_the_candles(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(rec, "record_platform", _fake_platform(_frames(4)))
        # Candles the replay could not have produced from four ticks, so what
        # lands in the zip is unambiguously what was handed to it.
        folder = tmp_path / "candles"
        folder.mkdir()
        made = []
        for name in ("EURUSD-otc-60s.csv", "GBPUSD-otc-5s.csv"):
            path = folder / name
            path.write_text("timestamp,open,high,low,close\n", encoding="utf-8")
            made.append(path)

        summary = tmp_path / "feed-summary.txt"
        summary.write_text("what came through", encoding="utf-8")
        bundle = rec.bundle(tmp_path, summary, made)

        assert bundle is not None
        with zipfile.ZipFile(bundle) as archive:
            names = sorted(archive.namelist())
        assert names == [
            "candles/EURUSD-otc-60s.csv",
            "candles/GBPUSD-otc-5s.csv",
            "feed-summary.txt",
        ]

    def test_the_frames_are_streamed_to_disk_rather_than_held(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(rec, "record_platform", _fake_platform(_frames(5)))
        result = rec.record_session(9222, 1800.0, tmp_path)

        lines = result.sample.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5
        assert json.loads(lines[0])["event"] == "updateStream"
        assert result.sample_bytes > 0

    def test_a_short_run_answers_the_protocol_and_stops_there(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(rec, "record_platform", _fake_platform(_frames(3)))
        result = rec.record_session(9222, 60.0, tmp_path)

        # A minute is a protocol sample. Turning three ticks into candles and
        # zipping them would dress up something nobody can measure anything on.
        assert result.bundle is None
        assert result.summary is not None and result.summary.exists()
        assert "frames captured" in result.report

    def test_progress_is_reported_while_it_runs(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rec, "record_platform", _fake_platform(_frames(6)))
        seen: list[tuple[float, float, int]] = []
        rec.record_session(
            9222, 1800.0, tmp_path,
            on_progress=lambda e, l, f: seen.append((e, l, f)),
            progress_every=0.0,
        )
        # Half an hour of silence is indistinguishable from a hung app.
        assert len(seen) >= 6
        assert [frames for _, _, frames in seen][-1] == 6

    def test_no_debuggable_browser_is_said_plainly_and_claims_nothing(
        self, monkeypatch, tmp_path
    ):
        def refuse(*args, **kwargs):
            raise BrowserError("No tab matching 'pocketoption' is open.")

        monkeypatch.setattr(rec, "record_platform", refuse)
        result = rec.record_session(9222, 1800.0, tmp_path)

        assert result.error is not None and "pocketoption" in result.error
        assert result.bundle is None
        assert result.frames == 0

    def test_stopping_early_keeps_what_was_captured(self, monkeypatch, tmp_path):
        def interrupted(port, seconds=60.0, needle="", max_frames=0, on_frame=None):
            for frame in _frames(4):
                on_frame(frame)
            raise KeyboardInterrupt

        monkeypatch.setattr(rec, "record_platform", interrupted)
        result = rec.record_session(9222, 1800.0, tmp_path)

        assert result.interrupted
        assert result.frames == 4
        # Four ticks is not a market, but it is still a file, and throwing it
        # away because the run was cut short would lose the only copy.
        assert result.bundle is not None and result.bundle.exists()


class TestWhatALengthWillProduce:
    """Said before the wait rather than after it."""

    def _rows(self, seconds):
        """The table rows, without the caveat that follows them."""
        return {
            line.split()[0]: line
            for line in rec.coverage(seconds).splitlines()
            if line.strip() and line.strip()[0].isdigit()
        }

    def test_half_an_hour_is_a_second_chart_and_not_a_minute_one(self):
        by_line = self._rows(1800.0)

        assert "enough to measure" in by_line["5s"]
        assert "too few" in by_line["1m"]
        assert "too few" in by_line["5m"]

    def test_three_hours_reaches_the_minute_chart(self):
        assert "enough to measure" in self._rows(3 * 3600.0)["1m"]


class TestTheRecordButton:
    """The app-side of it: one button, on a thread of its own."""

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

    def test_it_records_and_says_where_the_file_went(self, monkeypatch, tmp_path):
        landed = tmp_path / "gatekeeper-recording-now.zip"
        landed.write_bytes(b"PK")

        def fake_session(port, seconds, storage, **kwargs):
            kwargs["on_progress"](5.0, seconds - 5.0, 120)
            return rec.Recording(frames=120, bundle=landed, candles=[Path("a.csv")])

        monkeypatch.setattr(rec, "record_session", fake_session)
        app = self._app(tmp_path, monkeypatch)
        try:
            app._start_recording(minutes=30)
            app._recording_thread.join(timeout=5)

            state = app.vm.recording
            assert not state.active
            assert state.bundle == str(landed)
            assert state.frames == 120
            assert "gatekeeper-recording-now.zip" in state.message
        finally:
            app.shutdown()

    def test_a_second_press_opens_the_file_rather_than_recording_again(
        self, monkeypatch, tmp_path
    ):
        app = self._app(tmp_path, monkeypatch)
        try:
            opened: list[Path] = []
            started: list[int] = []
            monkeypatch.setattr(app, "_reveal", lambda path: opened.append(path))
            monkeypatch.setattr(
                app, "_start_recording", lambda *a, **k: started.append(1)
            )
            app.vm.recording.bundle = str(tmp_path / "storage" / "r.zip")

            app._toggle_recording()

            # Half an hour is a long time to lose to a mis-click on the button
            # that just told you it had finished.
            assert started == []
            assert opened == [tmp_path / "storage"]
        finally:
            app.shutdown()

    def test_a_running_capture_is_not_restarted_by_pressing_it_again(
        self, monkeypatch, tmp_path
    ):
        app = self._app(tmp_path, monkeypatch)
        try:
            started: list[int] = []
            monkeypatch.setattr(
                app, "_start_recording", lambda *a, **k: started.append(1)
            )
            app.vm.recording.active = True

            app._toggle_recording()

            assert started == []
        finally:
            app.shutdown()

    def test_a_browser_that_is_not_there_is_reported_not_swallowed(
        self, monkeypatch, tmp_path
    ):
        def fake_session(port, seconds, storage, **kwargs):
            return rec.Recording(error="No tab matching 'pocketoption' is open.")

        monkeypatch.setattr(rec, "record_session", fake_session)
        app = self._app(tmp_path, monkeypatch)
        try:
            app._start_recording(minutes=30)
            app._recording_thread.join(timeout=5)

            state = app.vm.recording
            assert not state.active
            assert not state.bundle
            assert "pocketoption" in state.error
            assert app.vm.render()["recording"]["label"] == "RECORD FAILED — TAP TO RETRY"
        finally:
            app.shutdown()


class TestWhatTheButtonSays:
    """It is one control with four things to say, and it has to say them."""

    def _state(self, **kwargs):
        from poa.overlay.viewmodel import RecordingState

        return RecordingState(**kwargs).to_dict()

    def test_idle_offers_the_recording(self):
        assert self._state()["label"] == "RECORD 30 MIN FOR ANALYSIS"

    def test_running_counts_down_rather_than_up(self):
        state = self._state(active=True, elapsed=300.0, total=1800.0, frames=9)
        assert state["label"] == "RECORDING — 25 MIN LEFT"
        assert state["progress"] == pytest.approx(1 / 6, abs=0.01)

    def test_the_last_minute_counts_in_seconds(self):
        state = self._state(active=True, elapsed=1770.0, total=1800.0)
        assert state["label"] == "RECORDING — 30s LEFT"

    def test_a_finished_one_offers_to_open_it(self):
        assert self._state(bundle="/x/y.zip")["label"] == "RECORDING SAVED — TAP TO OPEN"

    def test_progress_never_leaves_its_bounds(self):
        assert self._state(active=True, elapsed=9999.0, total=60.0)["progress"] == 1.0
        assert self._state(active=True, elapsed=-5.0, total=60.0)["progress"] == 0.0
        assert self._state(active=True, elapsed=10.0, total=0.0)["progress"] == 0.0


class TestTheCountdownRunsOnTheClock:
    """A quiet socket must not read as a hung app.

    Progress is reported as frames arrive, so a market that goes still — or a
    DevTools connection that drops and is being retried — used to leave the
    panel showing "30 MIN LEFT" for the whole half hour. The one wait this
    button exists to make bearable is exactly the one it stopped counting.
    """

    def _state(self, elapsed_by_clock: float, **kwargs):
        from poa.overlay.viewmodel import RecordingState

        state = RecordingState(**kwargs)
        state.started_at = 1000.0
        state._clock = lambda: 1000.0 + elapsed_by_clock
        return state

    def test_time_passes_even_when_no_frame_arrives(self):
        state = self._state(600.0, active=True, total=1800.0, elapsed=0.0, frames=0)

        assert state.remaining() == pytest.approx(1200.0)
        assert state.to_dict()["label"] == "RECORDING — 20 MIN LEFT"

    def test_a_finished_run_reports_what_it_took_not_what_the_clock_says(self):
        # started_at is cleared when the run ends, so a bundle sitting on the
        # panel for an hour does not go on counting.
        state = self._state(9999.0, active=False, total=1800.0, elapsed=1800.0)
        state.started_at = None

        assert state.to_dict()["elapsed"] == 1800.0


class TestASessionCanTakeMoreThanOneRecording:
    """Two captures of different markets beat one long capture of the same one.

    The button says "TAP TO OPEN" once a bundle is waiting. If that were where
    it stayed, the first recording of a session would also be the last: every
    later press would reopen the same folder.
    """

    def _app(self, tmp_path, monkeypatch):
        return TestTheRecordButton()._app(tmp_path, monkeypatch)

    def test_opening_the_file_hands_the_button_back(self, monkeypatch, tmp_path):
        app = self._app(tmp_path, monkeypatch)
        try:
            monkeypatch.setattr(app, "_reveal", lambda path: None)
            saved = str(tmp_path / "storage" / "gatekeeper-recording-a.zip")
            app.vm.recording.bundle = saved

            app._toggle_recording()

            state = app.vm.render()["recording"]
            assert state["label"] == "RECORD 30 MIN FOR ANALYSIS"
            # Still says what the last one was, so it can be found again.
            assert "gatekeeper-recording-a.zip" in state["message"]
            assert state["last_bundle"] == saved
        finally:
            app.shutdown()

    def test_and_the_next_press_records_again(self, monkeypatch, tmp_path):
        app = self._app(tmp_path, monkeypatch)
        try:
            started: list[int] = []
            monkeypatch.setattr(app, "_reveal", lambda path: None)
            monkeypatch.setattr(
                app, "_start_recording", lambda *a, **k: started.append(1)
            )
            app.vm.recording.bundle = str(tmp_path / "storage" / "r.zip")

            app._toggle_recording()   # opens it
            app._toggle_recording()   # records again

            assert started == [1]
        finally:
            app.shutdown()


class TestARecordingSaysWhatItCouldNotCapture:
    """Thirty minutes of ticks is not thirty minutes of every chart.

    The first real recording came back with 5s, 10s, 15s and 30s charts and
    nothing above them — because the platform never sent its candle history,
    and half an hour of live M1 is thirty bars where sixty are needed. The
    file that came out could not answer anything about the timeframe actually
    being traded, and said so nowhere.
    """

    def _result(self, timeframes, candles=("a.csv",)):
        return rec.Recording(
            frames=100,
            candles=[Path(name) for name in candles],
            timeframes=list(timeframes),
        )

    def test_sub_minute_only_says_so_and_says_what_to_do(self):
        note = self._result([5, 10, 15, 30]).shortfall()

        assert "Sub-minute charts only" in note
        assert "cannot measure the timeframe you trade" in note
        # No cause asserted. The obvious one was wrong, and printing a guess
        # as a finding sent the user to change something that was never it.
        assert "never sent" not in note

    def test_a_recording_that_reached_a_minute_says_nothing(self):
        assert self._result([5, 15, 60, 300]).shortfall() == ""

    def test_a_run_that_captured_nothing_at_all_says_nothing_here(self):
        """No frames is a connection failure, and ``error`` already names it.

        Explaining the shape of a recording that never happened would bury
        the reason it did not under advice about timeframes.
        """
        assert rec.Recording(frames=0, candles=[]).shortfall() == ""

    def test_whether_the_history_arrived_is_recorded(self, monkeypatch, tmp_path):
        frames = _frames(3) + [
            decode_frame('42["loadHistoryPeriodFast",{"asset":"EURUSD_otc"}]')
        ]
        monkeypatch.setattr(rec, "record_platform", _fake_platform(frames))
        assert rec.record_session(9222, 1800.0, tmp_path).history_seen

        monkeypatch.setattr(rec, "record_platform", _fake_platform(_frames(3)))
        assert not rec.record_session(9222, 1800.0, tmp_path).history_seen

    def test_the_panel_repeats_it_when_the_recording_ends(
        self, monkeypatch, tmp_path
    ):
        landed = tmp_path / "gatekeeper-recording-now.zip"
        landed.write_bytes(b"PK")

        def fake_session(port, seconds, storage, **kwargs):
            return rec.Recording(
                frames=9, bundle=landed, candles=[Path("a.csv")], timeframes=[5, 30]
            )

        monkeypatch.setattr(rec, "record_session", fake_session)
        app = TestTheRecordButton()._app(tmp_path, monkeypatch)
        try:
            app._start_recording(minutes=30)
            app._recording_thread.join(timeout=5)
            assert "Sub-minute charts only" in app.vm.recording.message
        finally:
            app.shutdown()

    def test_what_has_to_happen_is_said_before_the_wait(self, monkeypatch, tmp_path):
        """Learning it afterwards costs another half hour."""
        from poa.overlay import app as app_module

        app = TestTheRecordButton()._app(tmp_path, monkeypatch)
        try:
            # The worker would overwrite the opening message within a tick,
            # and the opening message is the thing under test.
            class _Idle:
                def __init__(self, *args, **kwargs):
                    pass

                def start(self):
                    pass

            monkeypatch.setattr(app_module.threading, "Thread", _Idle)
            app._start_recording(minutes=30)

            message = app.vm.render()["recording"]["message"]
            assert "Leave the chart open on the timeframe you trade" in message
            assert "no trade is placed" in message
        finally:
            app.shutdown()


class TestTheSummaryHidesNoEvent:
    """A protocol summary that drops the rare events drops the point.

    Ticks arrive fourteen thousand times and say nothing new; the history
    block arrives twice. Ranking by frequency and cutting at twenty-five put
    the cut in the middle of a run of four-count events, so "did the history
    ever arrive" could not be answered from the file written to answer it.
    """

    def _summary(self, names):
        from poa.feed.frames import Frame, Summary

        summary = Summary()
        for name, count in names.items():
            for _ in range(count):
                summary.add(
                    Frame(direction="in", opcode=1, kind="json",
                          event=name, payload={"x": 1})
                )
        return summary

    def test_a_rare_event_is_still_listed(self):
        names = {f"chatter{n}": 100 for n in range(30)}
        names["loadHistoryPeriodFast"] = 2
        text = self._summary(names).render()

        assert "loadHistoryPeriodFast" in text

    def test_and_the_reader_is_told_which_lack_an_example(self):
        names = {f"chatter{n}": 100 for n in range(30)}
        names["loadHistoryPeriodFast"] = 2
        text = self._summary(names).render(max_events=25)

        assert "6 rarer events listed above without one" in text

    def test_a_short_list_reads_as_it_always_did(self):
        text = self._summary({"updateStream": 9, "ping": 2}).render()

        assert "One example of each" in text
        assert "rarer event" not in text


class TestAnAttachmentKeepsTheNameAnnouncedForIt:
    """The platform names its big messages in the frame *before* the payload.

    The live listener carried that name forward. The offline replay did not,
    and read each frame alone — so every announced message replayed as an
    anonymous payload matching no handler. The one that mattered is
    loadHistoryPeriodFast: the platform's own candle history, and the only
    source of any chart at a minute or longer.

    The symptom was a recording that came back holding 5s, 10s, 15s and 30s
    charts and nothing above them, which read as the platform never having
    sent its history. It had sent it — a hundred and fifty M1 candles — and
    the replay dropped them. Every measurement taken offline was therefore
    taken on tick-derived sub-minute charts alone.
    """

    HISTORY_START = 1787163540

    def _recording(self, tmp_path, *, announced: bool):
        """A capture that changes symbol and is then sent M1 history."""
        candles = [
            {
                "symbol_id": 1,
                "time": self.HISTORY_START + 60 * n,
                "open": 1.2350 + n * 0.0001,
                "close": 1.2351 + n * 0.0001,
                "high": 1.2353 + n * 0.0001,
                "low": 1.2349 + n * 0.0001,
                "volume": 90,
            }
            for n in range(80)
        ]
        frames = [
            {"direction": "out", "opcode": 1, "kind": "socket.io",
             "event": "changeSymbol", "announces": None,
             "payload": ["changeSymbol", {"asset": "EURUSD_otc", "period": 60}]},
        ]
        if announced:
            # How the platform really sends it: a header, then the payload.
            frames += [
                {"direction": "in", "opcode": 1, "kind": "socket.io",
                 "event": "loadHistoryPeriodFast",
                 "announces": "loadHistoryPeriodFast", "payload": None},
                {"direction": "in", "opcode": 2, "kind": "json",
                 "event": None, "announces": None,
                 "payload": {"asset": "EURUSD_otc", "data": candles}},
            ]
        else:
            frames.append(
                {"direction": "in", "opcode": 1, "kind": "json",
                 "event": "loadHistoryPeriodFast", "announces": None,
                 "payload": {"asset": "EURUSD_otc", "data": candles}}
            )
        path = tmp_path / "feed.jsonl"
        path.write_text(
            "\n".join(json.dumps(f) for f in frames) + "\n", encoding="utf-8"
        )
        return path

    def _minute_charts(self, path):
        from poa.feed.replay import charts_from_recording

        return [
            (asset, tf, series)
            for asset, tf, series in charts_from_recording(path, min_candles=20)
            if tf >= 60
        ]

    def test_the_history_survives_arriving_as_an_attachment(self, tmp_path):
        charts = self._minute_charts(self._recording(tmp_path, announced=True))

        assert charts, "the platform's candle history was dropped"
        asset, timeframe, series = charts[0]
        assert timeframe == 60
        assert len(series) >= 60

    def test_and_still_survives_arriving_named(self, tmp_path):
        """Not every message is split. The unsplit form must keep working."""
        charts = self._minute_charts(self._recording(tmp_path, announced=False))

        assert charts
        assert charts[0][1] == 60

    def test_the_two_directions_do_not_steal_each_other_s_names(self):
        """They interleave on one socket, so one pending name would cross."""
        from poa.feed.frames import AttachmentNamer

        namer = AttachmentNamer()
        assert namer.name_for("loadHistoryPeriodFast", "loadHistoryPeriodFast",
                              "in") == "loadHistoryPeriodFast"
        # An outbound frame lands between the header and its payload.
        assert namer.name_for("saveCharts", None, "out") == "saveCharts"
        # The inbound attachment still knows what it belongs to.
        assert namer.name_for(None, None, "in") == "loadHistoryPeriodFast"

    def test_a_name_is_carried_once_and_not_forever(self):
        from poa.feed.frames import AttachmentNamer

        namer = AttachmentNamer()
        namer.name_for("updateStream", "updateStream", "in")
        assert namer.name_for(None, None, "in") == "updateStream"
        # The next anonymous payload is not another updateStream.
        assert namer.name_for(None, None, "in") is None


class TestTheCoverageTableIsAFloorAndSaysSo:
    """It counts live ticks only, and a real run beat it by fifteen times.

    A thirty-minute recording with a 1 MIN chart open produced 193 M1 candles
    over 4.6 hours, because the open chart also receives the platform's own
    history. The table promises 30. Understating is the safe direction to be
    wrong in — somebody records for longer than they needed to — but only if
    the reader is told which direction the error runs in, otherwise the table
    reads as "half an hour cannot measure M1" and that is simply false.
    """

    def test_the_table_says_which_way_it_is_wrong(self):
        text = rec.coverage(1800.0)

        assert "From live ticks alone" in text
        assert "history" in text
        assert "do far better than this" in text

    def test_the_numbers_themselves_are_unchanged(self):
        """The floor is still the floor; only the caveat is new."""
        by_line = {
            line.split()[0]: line
            for line in rec.coverage(1800.0).splitlines()
            if line.strip() and line.strip()[0].isdigit()
        }
        assert "360 candles" in by_line["5s"]
        assert "30 candles" in by_line["1m"]


class TestARecordingThatNamesNoChartSaysSo:
    """The worst outcome: half an hour spent, a file produced, nothing in it.

    A real capture came back with 15,800 frames and zero candles. Prices arrive
    for the whole market whether or not anything says which chart is being
    followed, so the run looked busy the entire time and built nothing. The
    page announces its chart when it loads, and the chart had been open for
    hours before anybody pressed RECORD, so those messages were long past.

    The live source has always asked the page to reload when it has not been
    told what it is showing. The recorder did not.
    """

    def _naming(self, name):
        return decode_frame(f'42["{name}",{{"asset":"EURUSD_otc","period":60}}]')

    def test_the_capture_reports_whether_a_chart_was_named(self, monkeypatch, tmp_path):
        anonymous = _frames(4)
        monkeypatch.setattr(rec, "record_platform", _fake_platform(anonymous))
        assert not rec.record_session(9222, 1800.0, tmp_path).named_a_chart

    def test_a_run_with_no_charts_explains_itself(self):
        result = rec.Recording(frames=15800, candles=[], named_a_chart=False)
        note = result.shortfall()

        assert "never said which chart" in note
        assert "15,800" in note
        assert "Record again" in note

    def test_a_named_run_with_too_little_data_says_that_instead(self):
        """A different failure, and it needs a different answer."""
        result = rec.Recording(frames=900, candles=[], named_a_chart=True)
        note = result.shortfall()

        assert "never said which chart" not in note
        assert "longer recording" in note

    def test_the_panel_treats_an_empty_bundle_as_a_failure(
        self, monkeypatch, tmp_path
    ):
        """Offering it as "SAVED - TAP TO OPEN" is how an empty file gets sent."""
        landed = tmp_path / "gatekeeper-recording-empty.zip"
        landed.write_bytes(b"PK")

        def fake_session(port, seconds, storage, **kwargs):
            return rec.Recording(frames=15800, bundle=landed, candles=[],
                                 named_a_chart=False)

        monkeypatch.setattr(rec, "record_session", fake_session)
        app = TestTheRecordButton()._app(tmp_path, monkeypatch)
        try:
            app._start_recording(minutes=30)
            app._recording_thread.join(timeout=5)

            state = app.vm.render()["recording"]
            assert state["label"] == "RECORD FAILED — TAP TO RETRY"
            assert not state["bundle"]
            assert "never said which chart" in state["message"]
        finally:
            app.shutdown()

    def test_a_real_recording_still_reads_as_success(self, monkeypatch, tmp_path):
        landed = tmp_path / "gatekeeper-recording-good.zip"
        landed.write_bytes(b"PK")

        def fake_session(port, seconds, storage, **kwargs):
            return rec.Recording(frames=9000, bundle=landed,
                                 candles=[Path("EUR-USD-OTC-60s.csv")],
                                 timeframes=[60], named_a_chart=True)

        monkeypatch.setattr(rec, "record_session", fake_session)
        app = TestTheRecordButton()._app(tmp_path, monkeypatch)
        try:
            app._start_recording(minutes=30)
            app._recording_thread.join(timeout=5)

            state = app.vm.render()["recording"]
            assert state["label"] == "RECORDING SAVED — TAP TO OPEN"
            assert state["bundle"] == str(landed)
        finally:
            app.shutdown()

    def test_the_naming_events_are_the_ones_that_identify_a_chart(self):
        from poa.feed.recorder import NAMING_EVENTS

        # Every message the source uses to claim an asset has to be here, or
        # the recorder reloads a page that had in fact already told it.
        for event in ("changeSymbol", "saveCharts", "loadHistoryPeriodFast"):
            assert event in NAMING_EVENTS
        # A price tick is not one of them: it names an instrument but says
        # nothing about which chart is on screen, and there are thousands.
        assert "updateStream" not in NAMING_EVENTS
