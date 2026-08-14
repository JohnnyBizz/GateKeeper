"""Wires the analysis engine to the overlay panel.

The engine already runs its own background loop and pushes state to
subscribers, so the overlay subscribes to it exactly as the web dashboard does.
Tk is not thread-safe, so engine updates are handed to the UI thread through
``after()`` rather than touching widgets directly.

Both scan modes run together: the engine keeps monitoring candle by candle and
raising alerts, while the Scan button forces an immediate fresh evaluation.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable

from ..config import Config, data_root, load_config
from ..engine import AnalysisEngine
from ..logging_setup import get_logger, install_crash_handlers, setup_logging
from ..models import utcnow
from ..risk import SessionStats
from .viewmodel import OverlayViewModel, ScanState

log = get_logger(__name__)

# How often the UI thread drains the engine queue and repaints, in ms. Fast
# enough for the dot animation to look continuous without busy-waiting.
TICK_MS = 80

# Below this recognition confidence, Scan goes looking for the chart again
# rather than analysing whatever the current region happens to contain. Set
# under the 50 the quality check calls usable, so a working region is never
# thrown away, and above the 35 a badly-placed region typically scores.
RELOCATE_BELOW_CONFIDENCE = 45.0

# Shown in place of the pair when the source knows which instrument it reads
# and currently does not know. Naming one anyway is how the panel came to
# report a market the user was not looking at.
UNKNOWN_ASSET = "—"

# History needed before replaying the engine over it is worth doing, and how
# many new bars have to arrive before the answer is re-measured.
PROOF_MIN_BARS = 150
PROOF_RERUN_BARS = 60


class OverlayApp:
    """The overlay application: engine + panel + the glue between them."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()
        setup_logging(
            level=str(self.config.get("logging.level", "INFO")),
            file=self.config.resolve_path("logging.file"),
        )
        install_crash_handlers()
        log.info("settings and data directory: %s", data_root())

        # Point the OCR wrapper at a usable Tesseract before anything tries to
        # read the screen. Without this a packaged build silently cannot read
        # the pair name, the timeframe or the price axis.
        from ..chart_detection.tesseract_setup import configure as configure_ocr

        self.ocr_ready = configure_ocr()

        self.engine = AnalysisEngine(self.config)
        self.vm = OverlayViewModel(
            session=SessionStats(),
            payout=float(self.config.get("market.payout", 0.92)),
            balance=float(self.config.get("risk.balance", 1000.0)),
            risk_percent=float(self.config.get("risk.risk_percent", 2.0)),
            asset=self.engine.asset,
            chart_timeframe=self.engine.chart_timeframe,
            trade_duration=self.engine.trade_duration,
            source=str(self.config.get("capture.source", "screen")),
            session_manual=bool(self.config.get("overlay.session_manual", True)),
            risk_collapsed=bool(self.config.get("overlay.risk_collapsed", False)),
        )
        self.vm.scan.duration = float(self.config.get("overlay.scan_seconds", 2.4))

        # The id of the last setup counted, so one signal held across many
        # polls is one call rather than one per poll.
        self._counted_signal: str | None = None

        # Where this session's tally starts. The journal outlives the app, so
        # without a boundary the "session" win rate would be every trade ever
        # recorded in the file — and Reset would clear it only until the next
        # refresh read it all back.
        self._session_since = utcnow()

        # The engine publishes from its own thread; the UI thread drains this.
        self._updates: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=32)
        self._pending_signal = None
        self._last_layout = None
        self.panel = None

        # The replay that measures the engine against this chart's own history.
        # Keyed by what would change the answer, so it re-runs when the chart or
        # the expiry changes and not on every poll.
        self._proof_results: queue.Queue[Any] = queue.Queue()
        self._proof_busy = False
        self._proof_key: tuple[Any, ...] | None = None
        self._proof_bars = 0

        # Results from the chart-search worker, handed back to the UI thread.
        self._scan_results: queue.Queue[Any] = queue.Queue()
        self._scan_busy = False
        self._scan_done: Callable[[str], None] | None = None
        # Guards the move-and-rescan from looping when there is nowhere clear.
        self._moved_for_scan = False

    # -- engine plumbing ----------------------------------------------------

    def _on_engine_state(self, payload: dict[str, Any]) -> None:
        """Called on the engine thread. Must not touch Tk."""
        try:
            self._updates.put_nowait(payload)
        except queue.Full:
            # Dropping a frame is fine; the next one carries current state.
            pass

    def _drain(self) -> None:
        """Runs on the UI thread: fold queued engine state into the view model."""
        latest = None
        while True:
            try:
                payload = self._updates.get_nowait()
            except queue.Empty:
                break
            if payload.get("type") == "state":
                latest = payload

        if latest is not None:
            self._apply_state()

        # Refresh the session counters from settled journal outcomes.
        self._refresh_session()

    def _apply_state(self) -> None:
        state = self.engine.state
        signal = state.signal

        self.vm.connected = state.running
        self.vm.last_error = state.last_error
        self.vm.trade_duration = self.engine.trade_duration

        meta = state.capture_meta or {}
        # As with the asset: when the timeframe is being read from the screen
        # the capture is the truth, and config is only the last typed value.
        self.vm.chart_timeframe = (
            meta.get("timeframe_seconds") or self.engine.chart_timeframe
        )
        # The capture's asset wins: when the pair label is being read from the
        # screen it is the truth, and the config value is just the last name
        # the user typed.
        captured_asset = meta.get("asset")
        if captured_asset:
            self.vm.asset = captured_asset
        elif getattr(self.engine.source, "names_own_chart", False):
            # It would have named the chart if it knew which one is open, so
            # the honest answer is nothing, not the last pair it saw.
            self.vm.asset = UNKNOWN_ASSET
        else:
            self.vm.asset = self.engine.asset

        quality = meta.get("quality") or {}
        self.vm.data_confidence = quality.get("confidence")

        # Count a setup once, when it first qualifies — not once per poll for
        # as long as it stands, which would turn one call into dozens.
        if signal is not None and signal.actionable and signal.id != self._counted_signal:
            self._counted_signal = signal.id
            self.vm.calls_this_session += 1

        if self.vm.scan.scanning:
            # Hold the incoming signal back until the scan window completes, so
            # the reveal is the result of the scan rather than a mid-scan flip.
            self._pending_signal = signal
        else:
            self.vm.signal = signal

    # -- measuring the engine against the chart in front of you --------------

    def _maybe_measure(self) -> None:
        """Replay this chart's history through the engine, in the background.

        Everything else on the panel is an opinion about what price will do.
        This is the part that checks: the same engine, the same gates, the same
        expiry, walked bar by bar over the history the platform already sent,
        with no visibility of what came next. It answers "are these calls any
        good on this instrument" with a number instead of a claim.

        Re-run when the answer could have changed — a different chart, a
        different expiry, or enough new bars to move it — and not otherwise; it
        is seconds of work, and running it every poll would heat the room to no
        purpose.
        """
        if self._proof_busy:
            return
        series = self.engine.latest_series()
        if series is None or len(series) < PROOF_MIN_BARS:
            return

        key = (series.symbol, series.timeframe_seconds, self.engine.trade_duration)
        grown = len(series) - self._proof_bars >= PROOF_RERUN_BARS
        if key == self._proof_key and not grown:
            return

        self._proof_key = key
        self._proof_bars = len(series)
        self._proof_busy = True
        threading.Thread(
            target=self._proof_worker,
            args=(series, self.engine.trade_duration, self.vm.payout),
            daemon=True,
        ).start()

    def _proof_worker(self, series: Any, duration: int, payout: float) -> None:
        """Off the UI thread. Touches no Tk and no engine state."""
        from .proof import measure

        try:
            result = measure(
                series,
                trade_duration=duration,
                payout=payout,
                settings=self.engine.gate_settings(),
                higher_multiple=int(
                    self.config.get("market.higher_timeframe_multiple", 5)
                ),
                entry_multiple=int(self.config.get("market.entry_timeframe_multiple", 1)),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("replay failed")
            self._proof_results.put(exc)
            return
        self._proof_results.put(result)

    def _collect_proof(self) -> None:
        """UI thread: pick up whatever the replay found."""
        try:
            result = self._proof_results.get_nowait()
        except queue.Empty:
            return
        self._proof_busy = False
        if isinstance(result, Exception):
            log.warning("replay failed: %s", result)
            return
        log.info("replay: %s", result.summary())
        self.vm.proof = result
        # Hand the measured record back to the engine, keyed to the chart it
        # was measured on, so the next evaluation can be checked against what
        # setups like it actually settled at.
        if result.calibration is not None and self._proof_key is not None:
            asset, timeframe, _duration = self._proof_key
            self.engine.set_calibration(result.calibration, asset, timeframe)

    def _toggle_risk(self) -> None:
        """Fold the risk block away, and remember that across restarts."""
        self.vm.risk_collapsed = not self.vm.risk_collapsed
        self.config.set("overlay.risk_collapsed", self.vm.risk_collapsed)
        try:
            self.config.save()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("could not save the risk fold state: %s", exc)

    def _refresh_session(self) -> None:
        # The tally is the user's record of their own trading, and only they
        # know which trades they actually placed. Filling it from journalled
        # outcomes meant the app was writing into a column the user was also
        # keeping, and neither number ended up meaning anything.
        if self.vm.session_manual:
            return

        # Scoped to the pair *and* the data source currently in use. A tally
        # that mixes a synthetic-feed session into a live one is not a record
        # of anything, and the user reads it as their real win rate.
        try:
            asset = self.vm.asset
            if not asset or asset == UNKNOWN_ASSET:
                asset = self.engine.asset
            stats = self.engine.journal.statistics(
                asset=asset,
                source=getattr(self.engine.source, "name", None),
                since=self._session_since,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("session refresh failed: %s", exc)
            return
        self.vm.session.set_auto(int(stats.get("wins", 0)), int(stats.get("losses", 0)))

    # -- UI callbacks -------------------------------------------------------

    def _begin_scan(self) -> None:
        """Force a fresh evaluation and show the scanning state.

        A manual scan means "read the chart in front of you, from scratch" —
        the user may have switched pairs or timeframes on the platform since
        the last signal. So the tracker's memory (peak confidence, weakening
        state, expiry) is dropped first; the revealed verdict describes only
        what the scan saw, never what an earlier chart looked like.

        "From scratch" now includes finding the chart. Scan locates the plot,
        its price axis, the pair name and the timeframe badge by itself when it
        has no working region — pressing one button is the whole interaction.

        Searching the screen takes seconds: a full-desktop grab, then OCR on
        every text-shaped thing near the chart. That cannot happen on the UI
        thread. Tk would stop answering the window manager, Windows would paint
        the panel grey and title it "Not Responding", and the user would
        reasonably call that a crash. So the search runs on a worker and the
        result is applied back here.
        """
        if self._scan_busy:
            return  # a scan is already running; a second press is not a queue

        self.vm.scan.begin()
        self._pending_signal = None
        self.vm.last_error = None
        self._moved_for_scan = False
        self.engine.tracker.reset()

        relocate = self._should_relocate()
        # Tk objects may only be touched from this thread, so the panel's own
        # geometry is measured now rather than inside the worker.
        exclude = self._own_windows() if relocate else []

        if not relocate:
            # Nothing on screen to go looking for, but Scan still has to mean
            # "work out what I am looking at". When the feed has not been told
            # which chart is open, that is the thing to fix.
            self._resync_feed(only_if_lost=True)
            self._run_engine_cycle()
            return

        self._scan_busy = True
        threading.Thread(
            target=self._locate_worker, args=(exclude,), daemon=True
        ).start()

    def _locate_worker(self, exclude: list[Any]) -> None:
        """Off the UI thread: find the chart. Touches no Tk and no engine."""
        from .autoscan import AutoScanResult, scan_screen
        from ..chart_detection.autodetect import Layout

        try:
            result = scan_screen(self.config, exclude=exclude)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("chart search failed")
            result = AutoScanResult(
                layout=Layout(), applied=False, message=f"Chart search failed: {exc}"
            )
        self._scan_results.put(result)

    def _collect_scan_result(self) -> None:
        """UI thread: apply whatever the worker found, then analyse."""
        try:
            result = self._scan_results.get_nowait()
        except queue.Empty:
            return

        self._scan_busy = False
        self._last_layout = result.layout
        notify, self._scan_done = self._scan_done, None

        # The panel was covering the chart it just found. Telling the user to
        # move it is a chore we can do ourselves — it is our window, and we now
        # know exactly where the chart is. One retry only: if the second scan
        # still finds the panel in the way, the screen has no room and the
        # message stands.
        if result.layout.overlapped_by_app and not self._moved_for_scan:
            self._moved_for_scan = True
            if self._move_panel_clear_of(result.layout.chart):
                log.info("moved the panel off the chart; scanning again")
                self._scan_done = notify
                self._scan_busy = True
                threading.Thread(
                    target=self._locate_worker,
                    args=(self._own_windows(),),
                    daemon=True,
                ).start()
                return
        if result.applied:
            log.info("auto-scan: %s", result.message)
            try:
                self.engine._rebuild_source()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("could not switch to the detected region")
                self.vm.last_error = f"Could not use the detected chart area: {exc}"
                return
            self.vm.signal = None
            self.vm.asset = self.engine.asset
            self.vm.chart_timeframe = self.engine.chart_timeframe
            self.vm.source = str(self.config.get("capture.source", "screen"))
            self._refresh_session()
        else:
            self.vm.last_error = result.message
            log.warning("auto-scan found nothing: %s", result.message)

        self._run_engine_cycle()

        if notify is not None:
            try:
                notify(result.message)
            except Exception:  # pragma: no cover - the dialog may have closed
                log.debug("scan callback failed", exc_info=True)

    def _run_engine_cycle(self) -> None:
        """One immediate capture-analyse pass, instead of waiting for the poll."""
        try:
            self.engine.tick()
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("manual scan failed")
            self.vm.last_error = f"Scan failed: {exc}"

    def _should_relocate(self) -> bool:
        """Whether Scan should go looking for the chart before analysing.

        Reasons to go looking:

        * the app is not reading the screen at all. It ships pointed at demo
          data so a first run has something to show, and that default used to
          be a dead end — Scan would only search when the source was already
          ``screen``, so the one button that is supposed to set everything up
          refused to until the user had already set it up. Pressing Scan means
          "read the chart in front of me";
        * there is no chart region;
        * the region there is cannot read the chart;
        * the region works, but the pair and timeframe badges were never
          located — which is its own bug to have missed. A chart region can
          read candles perfectly and still leave the pair frozen on whatever
          was typed last, because the name is text somewhere else on screen.
          Waiting for the *candles* to fail would never fix that.

        The one thing that stops it is the user having *chosen* a source. Demo
        data and CSV replay are deliberate choices, and Scan does not overrule
        them — it only fills in a default nobody picked.
        """
        from ..chart_detection.autodetect import Box, ocr_available

        source = str(self.config.get("capture.source", "feed"))
        if source == "feed":
            # Nothing on screen is being read, so there is nothing to find.
            return False
        if source != "screen":
            return not bool(self.config.get("capture.source_chosen", False))

        if Box.from_dict(self.config.get("capture.region")) is None:
            return True
        confidence = self.vm.data_confidence
        if confidence is not None and confidence < RELOCATE_BELOW_CONFIDENCE:
            return True
        if ocr_available() and not self._labels_located():
            return True
        return False

    def _resync_feed(self, only_if_lost: bool = False) -> str | None:
        """Ask the feed source to re-read which chart the platform has open.

        Returns the message to show, or ``None`` when the current source is not
        one that can re-sync — screen capture and demo data have their own
        answers to "find the chart".

        ``only_if_lost`` restricts it to the case that actually needs it: no
        instrument identified, or no history behind the candles. Re-syncing
        reloads the platform's page, which is not something to do on every
        press of Scan when the feed is already reading the right chart.
        """
        source = getattr(self.engine, "source", None)
        resync = getattr(source, "resync", None)
        if not callable(resync):
            return None

        if only_if_lost:
            state = getattr(source, "describe", lambda: {})() or {}
            settled = bool(state.get("symbol")) and bool(state.get("history_loaded"))
            if settled:
                return None
        try:
            message = resync()
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("could not re-sync the feed")
            return f"Could not re-read the chart: {exc}"

        self.vm.signal = None
        log.info("feed re-sync requested")
        return message

    def _labels_located(self) -> bool:
        from ..chart_detection.autodetect import Box

        return all(
            Box.from_dict(self.config.get(key)) is not None
            for key in ("capture.asset_region", "capture.timeframe_region")
        )

    def locate_chart(self, on_done: Callable[[str], None] | None = None) -> None:
        """Find the chart on screen and reconfigure from what is found.

        Asynchronous, like Scan, and for the same reason: this is seconds of
        screen capture and OCR, and running it on the UI thread stops Tk
        answering the window manager. A settings dialog that hides itself and
        then freezes is indistinguishable from one that has crashed.
        """
        if self._scan_busy:
            if on_done is not None:
                on_done("Already looking for the chart…")
            return

        # Reading the feed means there is no region to find, and a button that
        # silently does nothing is worse than no button. Re-sync instead: that
        # is the equivalent action for a source with no pixels in it.
        message = self._resync_feed()
        if message is not None:
            if on_done is not None:
                on_done(message)
            return

        self._scan_done = on_done
        exclude = self._own_windows()
        self._scan_busy = True
        threading.Thread(
            target=self._locate_worker, args=(exclude,), daemon=True
        ).start()

    def _move_panel_clear_of(self, chart: Any) -> bool:
        """Shift the panel so it stops covering ``chart``. True if it moved.

        Prefers the widest empty strip beside the chart, and settles for the
        screen corner furthest from it when the chart fills the display.
        """
        if self.panel is None or chart is None:
            return False
        try:
            root = self.panel.root
            root.update_idletasks()
            screen_w = int(root.winfo_screenwidth())
            screen_h = int(root.winfo_screenheight())
            width = int(root.winfo_width())
            height = int(root.winfo_height())
        except Exception:  # pragma: no cover - window not realised
            return False

        margin = 8
        # Room to the left, right, above and below the chart.
        candidates = [
            (chart.left - width - margin, margin),
            (chart.left + chart.width + margin, margin),
            (margin, chart.top - height - margin),
            (margin, chart.top + chart.height + margin),
        ]
        for x, y in candidates:
            if 0 <= x <= screen_w - width and 0 <= y <= screen_h - height:
                return self._place_panel(x, y)

        # Nowhere clear: the bottom-right corner is at least out of the way of
        # the price axis, which is the part whose loss breaks calibration.
        return self._place_panel(
            max(0, screen_w - width - margin), max(0, screen_h - height - margin)
        )

    def _place_panel(self, x: int, y: int) -> bool:
        try:
            self.panel.root.geometry(f"+{int(x)}+{int(y)}")
            self.panel.root.update_idletasks()
        except Exception:  # pragma: no cover - defensive
            return False
        self.config.set("overlay.x", int(x))
        self.config.set("overlay.y", int(y))
        return True

    def _own_windows(self) -> list[Any]:
        """GateKeeper's own windows, so the detector never analyses itself.

        The panel is always on top and full of candle-coloured buttons. Left in
        the frame it is a plausible-looking chart sitting directly over the real
        one, and whichever it picked would be wrong.
        """
        from ..chart_detection.autodetect import Box

        boxes: list[Any] = []
        if self.panel is None:
            return boxes
        for window in (getattr(self.panel, "root", None),):
            if window is None:
                continue
            try:
                window.update_idletasks()
                box = Box(
                    int(window.winfo_rootx()),
                    int(window.winfo_rooty()),
                    int(window.winfo_width()),
                    int(window.winfo_height()),
                )
            except Exception:  # pragma: no cover - window not realised
                continue
            if box.area > 0:
                # A little margin: the window's drop shadow and border are not
                # inside winfo_width, and they are coloured too.
                boxes.append(Box(box.left - 8, box.top - 8, box.width + 16, box.height + 16))
        return boxes

    def _finish_scan(self) -> None:
        self.vm.signal = self._pending_signal or self.engine.state.signal
        self._pending_signal = None

    def _reset(self) -> None:
        """Start a new session: clear the tally and drop the tracked signal.

        The boundary moves too, otherwise the next journal refresh would read
        the cleared trades straight back in and Reset would appear to do
        nothing.
        """
        self._session_since = utcnow()
        self.vm.session.reset()
        self.vm.calls_this_session = 0
        self._counted_signal = None
        self.engine.tracker.reset()
        self.vm.scan.reset()

    def _adjust(self, wins: int, losses: int) -> None:
        self.vm.session.adjust(wins, losses)

    def _set_asset(self, asset: str) -> None:
        """The user renamed the pair after switching charts on the platform."""
        asset = asset.strip().upper()
        if not asset or asset == self.vm.asset:
            return
        try:
            self.engine.update_settings({"asset": asset})
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("asset change failed: %s", exc)
            return
        self.vm.asset = asset
        # The old signal described a different chart; blank until re-analysed.
        self.vm.signal = None
        self._refresh_session()

    def _pick_asset_label(self, prefix: str, done: Callable[[str], None]) -> None:
        """Offer the two optional label boxes: the pair name, then the timeframe.

        Neither can be inferred from candles — the pair is text, and a 1-minute
        and a 5-minute chart draw identical-looking bars. Both are offered
        rather than required; skipping costs only the automatic updates.
        """
        self._pick_label(
            key="capture.asset_region",
            title="Read the pair name automatically?",
            question=(
                "GateKeeper can read the pair's name (e.g. EUR/USD) from the "
                "screen, so it renames itself when you switch charts."
            ),
            hint="Drag a small box around the pair name only (e.g. EUR/USD).   Esc to skip.",
            success="Pair name will be read from the screen.",
            skipped="Pair name will need renaming by hand.",
            prefix=prefix,
            # Chain the timeframe step after this one, so the important chart
            # area is already saved even if the user abandons the extras.
            done=lambda message: self._pick_timeframe_label(message, done),
        )

    def _pick_timeframe_label(self, prefix: str, done: Callable[[str], None]) -> None:
        self._pick_label(
            key="capture.timeframe_region",
            title="Read the chart timeframe automatically?",
            question=(
                "GateKeeper can also read the timeframe badge (M1, M5, H1...). "
                "This one matters: every duration recommendation is measured in "
                "candles of that length, so a 5-minute chart read as 1-minute "
                "would suggest expirations five times too short."
            ),
            hint="Drag a small box around the timeframe badge only (e.g. M1).   Esc to skip.",
            success="Timeframe will be read from the screen.",
            skipped="Set the timeframe by hand in settings when you change it.",
            prefix=prefix,
            done=done,
        )

    def _pick_label(
        self,
        *,
        key: str,
        title: str,
        question: str,
        hint: str,
        success: str,
        skipped: str,
        prefix: str,
        done: Callable[[str], None],
    ) -> None:
        """Ask about, then select, one small OCR region."""
        from tkinter import messagebox

        from .region_picker import RegionPicker

        if not messagebox.askyesno(title, f"{prefix}\n\n{question}\n\nSelect it now?"):
            done(f"{prefix} {skipped}")
            return

        def after(selection) -> None:
            if selection is None:
                done(f"{prefix} Skipped.")
                return
            self.config.set(key, selection.region_dict())
            try:
                self.config.save()
            except OSError:  # pragma: no cover - filesystem dependent
                pass
            self.engine._rebuild_source()
            done(f"{prefix} {success}")

        try:
            RegionPicker(
                self.panel.root,
                monitor_index=int(self.config.get("capture.monitor", 1)),
                on_done=after,
                hint=hint,
                analyse=False,
                min_size=10,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("label picker failed: %s", exc)
            done(prefix)

    def _set_stake(self, stake: float | None) -> None:
        self.vm.stake_override = stake if (stake is None or stake > 0) else None

    def _set_balance(self, balance: float) -> None:
        if balance > 0:
            self.vm.balance = balance
            self.config.set("risk.balance", balance)

    # -- settings -----------------------------------------------------------

    def _open_settings(self) -> None:
        from .settings_dialog import SettingsDialog

        SettingsDialog(
            self.panel.root,
            self.config,
            on_apply=self._apply_settings,
            on_pick_region=self._pick_region,
            on_locate_chart=self.locate_chart,
        )

    def _apply_settings(self, changes: dict[str, Any]) -> None:
        """Apply settings from the dialog, then persist them."""
        source = changes.pop("source", None)
        payout = changes.pop("payout", None)
        balance = changes.pop("balance", None)

        if payout is not None:
            self.config.set("market.payout", payout)
            self.vm.payout = payout
        if balance is not None:
            self.config.set("risk.balance", balance)
            self.vm.balance = balance
        if source is not None and source != self.config.get("capture.source"):
            self.config.set("capture.source", source)
            # Picking one here is a decision, and Scan stops second-guessing it.
            # Without this flag it could not tell "the user wants demo data"
            # from "nobody has set this up yet".
            self.config.set("capture.source_chosen", True)
            # A different source means different candles entirely.
            self.engine._rebuild_source()
            self.vm.signal = None

        if changes:
            self.engine.update_settings(changes)

        self.vm.asset = self.engine.asset
        self.vm.chart_timeframe = self.engine.chart_timeframe
        self.vm.trade_duration = self.engine.trade_duration
        self.vm.source = str(self.config.get("capture.source", "screen"))

        try:
            saved = self.config.save()
            log.info("settings saved to %s", saved)
        except OSError as exc:
            # Not fatal: the change is live, it just will not survive a restart.
            log.warning("could not save settings: %s", exc)

    def _pick_region(self, done: Callable[[str], None]) -> None:
        """Run the chart-area picker, then the optional price calibration."""
        from .region_picker import CalibrationPicker, RegionPicker

        def after_calibration(selection) -> None:
            if selection is None:
                done("Selection cancelled.")
                return
            self.config.set("capture.region", selection.region_dict())
            self.config.set("capture.calibration", selection.calibration_dict())
            self.config.set("capture.source", "screen")
            try:
                self.config.save()
            except OSError as exc:  # pragma: no cover - filesystem dependent
                log.warning("could not save region: %s", exc)

            self.engine._rebuild_source()
            self.vm.signal = None

            message = (
                f"Found {selection.candles_found} candles at "
                f"{selection.confidence:.0f}% confidence."
            )
            if not selection.calibrated:
                message += " Price scale not calibrated — levels will be relative."
            if selection.candles_found < 30:
                message += (
                    " That is fewer than 30 — widen the box or zoom the chart out."
                )
            if selection.issues:
                message += " " + " ".join(selection.issues[:2])

            # Offer the optional pair-label box last, so the important step is
            # already saved even if the user skips this one.
            self._pick_asset_label(message, done)

        def after_region(selection) -> None:
            if selection is None:
                done("Selection cancelled.")
                return
            try:
                CalibrationPicker(
                    self.panel.root, selection, on_done=after_calibration
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("calibration failed: %s", exc)
                after_calibration(selection)

        try:
            RegionPicker(
                self.panel.root,
                monitor_index=int(self.config.get("capture.monitor", 1)),
                colors=self.config.get("capture.colors"),
                on_done=after_region,
            )
        except Exception as exc:
            log.warning("region picker failed: %s", exc)
            done(f"Could not open the picker: {exc}")

    # -- lifecycle ----------------------------------------------------------

    def _tick(self) -> None:
        """UI-thread heartbeat: drain, advance the scan, repaint."""
        try:
            self._drain()
            self._collect_scan_result()
            self._collect_proof()
            self._maybe_measure()
            # The scan state is held open while the search runs, not just for
            # the timer. Letting the timer end it early would drop the panel
            # out of SCANNING and paint the *previous* chart's verdict as if
            # it were the new one — the exact failure the blanking exists to
            # prevent.
            if not self._scan_busy and self.vm.scan.poll():
                self._finish_scan()
            if self.panel is not None:
                self.panel.refresh()
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("overlay tick failed: %s", exc)
        finally:
            if self.panel is not None:
                self.panel.root.after(TICK_MS, self._tick)

    def run(self) -> None:
        from .panel import OverlayPanel

        self.panel = OverlayPanel(
            self.vm,
            on_scan=self._begin_scan,
            on_reset=self._reset,
            on_adjust=self._adjust,
            on_asset=self._set_asset,
            on_stake=self._set_stake,
            on_balance=self._set_balance,
            on_settings=self._open_settings,
            on_close=self.shutdown,
            on_toggle_risk=self._toggle_risk,
            position=(
                int(self.config.get("overlay.x", 40)),
                int(self.config.get("overlay.y", 80)),
            ),
            opacity=float(self.config.get("overlay.opacity", 0.96)),
        )

        # Tk swallows callback exceptions by printing them to stderr, which a
        # windowed build does not have. Send them to the log instead, so a
        # button that stops working leaves a trace.
        self.panel.root.report_callback_exception = (
            lambda kind, value, tb: log.critical(
                "unhandled error in a UI callback", exc_info=(kind, value, tb)
            )
        )

        self.engine.subscribe(self._on_engine_state)
        self.engine.start()

        # Paint once immediately so the panel is never blank on open.
        self._apply_state()
        self.panel.refresh()
        self.panel.root.after(TICK_MS, self._tick)

        # Go and find the chart without being asked. Opening the app and being
        # shown demo data until you discover which button fixes it is not a
        # setup step, it is a dead end — and the panel would be scoring a
        # market that does not exist the whole time.
        if self._should_relocate():
            self.panel.root.after(400, self._begin_scan)

        try:
            self.panel.run()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        try:
            self.engine.unsubscribe(self._on_engine_state)
        except Exception:  # pragma: no cover - best effort
            pass
        self.engine.close()


def run(config_path: str | None = None) -> None:
    """Entry point for ``python overlay.py`` / ``python -m poa.overlay``."""
    config = load_config(config_path)
    log.info("=" * 60)
    log.info("GateKeeper — overlay")
    log.info("Source: %s | asset: %s", config.get("capture.source"), config.get("market.asset"))
    log.info("Analysis and alerts only. This tool never places a trade.")
    log.info("=" * 60)
    OverlayApp(config).run()
