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

    def test_half_an_hour_is_a_second_chart_and_not_a_minute_one(self):
        text = rec.coverage(1800.0)
        by_line = {line.split()[0]: line for line in text.splitlines()}

        assert "enough to measure" in by_line["5s"]
        assert "too few" in by_line["1m"]
        assert "too few" in by_line["5m"]

    def test_three_hours_reaches_the_minute_chart(self):
        by_line = {
            line.split()[0]: line for line in rec.coverage(3 * 3600.0).splitlines()
        }
        assert "enough to measure" in by_line["1m"]


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
