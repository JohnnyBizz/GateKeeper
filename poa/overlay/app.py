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
from typing import Any, Callable

from ..config import Config, load_config
from ..engine import AnalysisEngine
from ..logging_setup import get_logger, setup_logging
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


class OverlayApp:
    """The overlay application: engine + panel + the glue between them."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()
        setup_logging(
            level=str(self.config.get("logging.level", "INFO")),
            file=self.config.resolve_path("logging.file"),
        )

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
        )
        self.vm.scan.duration = float(self.config.get("overlay.scan_seconds", 2.4))

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
        self.vm.asset = captured_asset or self.engine.asset

        quality = meta.get("quality") or {}
        self.vm.data_confidence = quality.get("confidence")

        if self.vm.scan.scanning:
            # Hold the incoming signal back until the scan window completes, so
            # the reveal is the result of the scan rather than a mid-scan flip.
            self._pending_signal = signal
        else:
            self.vm.signal = signal

    def _refresh_session(self) -> None:
        # Scoped to the pair *and* the data source currently in use. A tally
        # that mixes a synthetic-feed session into a live one is not a record
        # of anything, and the user reads it as their real win rate.
        try:
            stats = self.engine.journal.statistics(
                asset=self.vm.asset or self.engine.asset,
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
        """
        self.vm.scan.begin()
        self._pending_signal = None
        self.engine.tracker.reset()

        if self._should_relocate():
            self.locate_chart()

        try:
            # Run one cycle immediately rather than waiting for the next poll.
            self.engine.tick()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("manual scan failed: %s", exc)
            self.vm.last_error = f"Scan failed: {exc}"

    def _should_relocate(self) -> bool:
        """Whether Scan should go looking for the chart before analysing.

        Only for the screen source, and only when the current region is either
        missing or plainly not working. Re-locating a region that is reading
        the chart well would throw away a good calibration for nothing.
        """
        if str(self.config.get("capture.source", "screen")) != "screen":
            return False
        from ..chart_detection.autodetect import Box

        if Box.from_dict(self.config.get("capture.region")) is None:
            return True
        confidence = self.vm.data_confidence
        return confidence is not None and confidence < RELOCATE_BELOW_CONFIDENCE

    def locate_chart(self) -> str:
        """Find the chart on screen and reconfigure from what is found."""
        from .autoscan import scan_screen

        result = scan_screen(self.config, exclude=self._own_windows())
        self._last_layout = result.layout
        if result.applied:
            self.engine._rebuild_source()
            self.vm.signal = None
            self.vm.asset = self.engine.asset
            self.vm.chart_timeframe = self.engine.chart_timeframe
            self._refresh_session()
            log.info("auto-scan: %s", result.message)
        else:
            self.vm.last_error = result.message
            log.warning("auto-scan found nothing: %s", result.message)
        return result.message

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
            # A different source means different candles entirely.
            self.engine._rebuild_source()
            self.vm.signal = None

        if changes:
            self.engine.update_settings(changes)

        self.vm.asset = self.engine.asset
        self.vm.chart_timeframe = self.engine.chart_timeframe
        self.vm.trade_duration = self.engine.trade_duration

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
            if self.vm.scan.poll():
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
            position=(
                int(self.config.get("overlay.x", 40)),
                int(self.config.get("overlay.y", 80)),
            ),
            opacity=float(self.config.get("overlay.opacity", 0.96)),
        )

        self.engine.subscribe(self._on_engine_state)
        self.engine.start()

        # Paint once immediately so the panel is never blank on open.
        self._apply_state()
        self.panel.refresh()
        self.panel.root.after(TICK_MS, self._tick)

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
