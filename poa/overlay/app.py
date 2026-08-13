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
from ..risk import SessionStats
from .viewmodel import OverlayViewModel, ScanState

log = get_logger(__name__)

# How often the UI thread drains the engine queue and repaints, in ms. Fast
# enough for the dot animation to look continuous without busy-waiting.
TICK_MS = 80


class OverlayApp:
    """The overlay application: engine + panel + the glue between them."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()
        setup_logging(
            level=str(self.config.get("logging.level", "INFO")),
            file=self.config.resolve_path("logging.file"),
        )

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

        # The engine publishes from its own thread; the UI thread drains this.
        self._updates: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=32)
        self._pending_signal = None
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
        self.vm.chart_timeframe = self.engine.chart_timeframe
        self.vm.trade_duration = self.engine.trade_duration

        meta = state.capture_meta or {}
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
        try:
            stats = self.engine.journal.statistics(asset=self.engine.asset)
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
        """
        self.vm.scan.begin()
        self._pending_signal = None
        self.engine.tracker.reset()
        try:
            # Run one cycle immediately rather than waiting for the next poll.
            self.engine.tick()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("manual scan failed: %s", exc)
            self.vm.last_error = f"Scan failed: {exc}"

    def _finish_scan(self) -> None:
        self.vm.signal = self._pending_signal or self.engine.state.signal
        self._pending_signal = None

    def _reset(self) -> None:
        """Clear the session tally and drop the tracked signal."""
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
        """Optionally point GateKeeper at the platform's pair-name label.

        The candles say nothing about *which* instrument they belong to, so
        without this the name has to be typed whenever the user switches
        charts. It is offered rather than required: skipping it costs only the
        automatic rename.
        """
        from tkinter import messagebox

        from .region_picker import RegionPicker

        wants = messagebox.askyesno(
            "Read the pair name automatically?",
            f"{prefix}\n\nGateKeeper can also read the pair's name (e.g. "
            "EUR/USD) from the screen, so it renames itself when you switch "
            "charts.\n\nSelect the small label now?",
        )
        if not wants:
            done(prefix + " Pair name will need renaming by hand.")
            return

        def after_label(selection) -> None:
            if selection is None:
                done(prefix + " Pair-name box skipped.")
                return
            self.config.set("capture.asset_region", selection.region_dict())
            try:
                self.config.save()
            except OSError:  # pragma: no cover - filesystem dependent
                pass
            self.engine._rebuild_source()
            done(prefix + " Pair name will now be read from the screen.")

        try:
            RegionPicker(
                self.panel.root,
                monitor_index=int(self.config.get("capture.monitor", 1)),
                on_done=after_label,
                hint=(
                    "Now drag a small box around the pair name only "
                    "(e.g. EUR/USD).   Esc to skip."
                ),
                analyse=False,
                min_size=10,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("asset label picker failed: %s", exc)
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
