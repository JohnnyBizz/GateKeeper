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
from typing import Any

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
        self.vm.asset = self.engine.asset
        self.vm.chart_timeframe = self.engine.chart_timeframe
        self.vm.trade_duration = self.engine.trade_duration

        quality = (state.capture_meta or {}).get("quality") or {}
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
        """Force a fresh evaluation and show the scanning state."""
        self.vm.scan.begin()
        self._pending_signal = None
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
    log.info("Technical Analysis Assistant — overlay")
    log.info("Source: %s | asset: %s", config.get("capture.source"), config.get("market.asset"))
    log.info("Analysis and alerts only. This tool never places a trade.")
    log.info("=" * 60)
    OverlayApp(config).run()
