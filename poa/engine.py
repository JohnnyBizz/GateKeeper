"""The live analysis loop.

Ties every layer together and runs the candle-by-candle cycle:

    capture -> validate -> analyse -> score -> gate -> duration -> track
    -> alert -> journal -> broadcast

The loop is designed to keep running. A failure in any single step is logged
and degrades to a WAIT state rather than stopping the application, because an
assistant that silently dies is worse than one that says it cannot see.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .alerts import AlertManager, AlertSettings
from .alerts.notifiers import Alert
from .chart_detection import ChartSource, build_source
from .chart_detection.base import Capture
from .chart_detection.quality import validate_series
from .config import CHART_TIMEFRAMES, TRADE_DURATIONS, Config
from .logging_setup import get_logger
from .models import DataQuality, Direction, Series, format_duration, utcnow
from .signals import GateSettings, Signal, SignalEngine, SignalRequest
from .signals.tracker import SignalTracker, TrackedChange, TrackerSettings
from .storage import Journal, ScreenshotStore

log = get_logger(__name__)


@dataclass
class EngineState:
    """The snapshot the dashboard renders."""

    running: bool = False
    signal: Signal | None = None
    change: TrackedChange | None = None
    capture_meta: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    last_update: str | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    candles: list[dict[str, Any]] = field(default_factory=list)
    heikin_ashi_candles: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "last_update": self.last_update,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
            "source": self.source,
            "capture": self.capture_meta,
            "signal": self.signal.to_dict() if self.signal else None,
            "change": (
                {
                    "material": self.change.material,
                    "kind": self.change.kind,
                    "description": self.change.description,
                }
                if self.change
                else None
            ),
            "candles": self.candles,
            "heikin_ashi": self.heikin_ashi_candles,
        }


class AnalysisEngine:
    """Owns the live loop and the state the dashboard reads."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.signal_engine = SignalEngine()
        self.tracker = SignalTracker(
            settings=TrackerSettings.from_config(config.section("signals"))
        )
        self.alerts = AlertManager(AlertSettings.from_config(config.section("alerts")))
        self.journal = Journal(config.resolve_path("storage.database"))
        self.screenshots = ScreenshotStore(
            config.resolve_path("storage.screenshot_dir"),
            retain=int(config.get("storage.retain_screenshots", 500)),
        )

        self.source: ChartSource = build_source(config)
        self.state = EngineState(source=self.source.describe())

        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._alert_subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Push alerts to the dashboard as they fire.
        from .alerts.notifiers import CallbackNotifier

        self.alerts.add_notifier(CallbackNotifier(self._on_alert))

    # -- runtime settings the UI can change -----------------------------

    @property
    def asset(self) -> str:
        return str(self.config.get("market.asset", "EUR/USD"))

    @property
    def chart_timeframe(self) -> int:
        return int(self.config.get("market.chart_timeframe", 60))

    @property
    def trade_duration(self) -> int:
        return int(self.config.get("market.trade_duration", 180))

    def gate_settings(self) -> GateSettings:
        return GateSettings.from_config(self.config.section("signals"))

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply UI setting changes and report what actually took effect."""
        applied: dict[str, Any] = {}
        with self._lock:
            if "asset" in changes and changes["asset"]:
                self.config.set("market.asset", str(changes["asset"]))
                applied["asset"] = self.config.get("market.asset")
            if "chart_timeframe" in changes:
                timeframe = int(changes["chart_timeframe"])
                if timeframe <= 0:
                    raise ValueError("chart_timeframe must be positive")
                self.config.set("market.chart_timeframe", timeframe)
                applied["chart_timeframe"] = timeframe
                # The source produces candles at a fixed timeframe, so changing
                # it means rebuilding the source rather than reinterpreting
                # candles we already have.
                self._rebuild_source()
            if "trade_duration" in changes:
                duration = int(changes["trade_duration"])
                if duration <= 0:
                    raise ValueError("trade_duration must be positive")
                self.config.set("market.trade_duration", duration)
                applied["trade_duration"] = duration
            if "min_confidence" in changes:
                value = float(changes["min_confidence"])
                self.config.set("signals.min_confidence", max(0.0, min(100.0, value)))
                applied["min_confidence"] = self.config.get("signals.min_confidence")
            if "min_duration_compatibility" in changes:
                value = float(changes["min_duration_compatibility"])
                self.config.set(
                    "signals.min_duration_compatibility", max(0.0, min(100.0, value))
                )
                applied["min_duration_compatibility"] = self.config.get(
                    "signals.min_duration_compatibility"
                )
            if "alerts_enabled" in changes:
                self.alerts.settings.enabled = bool(changes["alerts_enabled"])
                applied["alerts_enabled"] = self.alerts.settings.enabled
            if "alert_cooldown" in changes:
                self.alerts.settings.cooldown_seconds = float(changes["alert_cooldown"])
                applied["alert_cooldown"] = self.alerts.settings.cooldown_seconds
            if "poll_seconds" in changes:
                value = max(0.5, float(changes["poll_seconds"]))
                self.config.set("capture.poll_seconds", value)
                applied["poll_seconds"] = value

        if applied:
            # Settings changed the meaning of the current signal, so drop it
            # rather than leaving a stale evaluation on screen.
            self.tracker.reset()
            log.info("settings updated: %s", applied)
        return applied

    def _rebuild_source(self) -> None:
        try:
            self.source.stop()
        except Exception:  # pragma: no cover - best effort
            pass
        self.source = build_source(self.config)
        self.state.source = self.source.describe()

    # -- subscriptions ---------------------------------------------------

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def subscribe_alerts(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._alert_subscribers.append(callback)

    def unsubscribe_alerts(self, callback: Callable[[dict[str, Any]], None]) -> None:
        if callback in self._alert_subscribers:
            self._alert_subscribers.remove(callback)

    def _on_alert(self, alert: Alert) -> None:
        payload = {"type": "alert", "alert": alert.to_dict()}
        for callback in list(self._alert_subscribers):
            try:
                callback(payload)
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("alert subscriber failed: %s", exc)

    def _broadcast(self) -> None:
        payload = {"type": "state", "state": self.state.to_dict()}
        for callback in list(self._subscribers):
            try:
                callback(payload)
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("state subscriber failed: %s", exc)

    # -- the cycle --------------------------------------------------------

    def tick(self) -> EngineState:
        """Run one full capture-analyse-alert cycle."""
        try:
            capture = self.source.capture()
        except Exception as exc:
            return self._degrade(f"Chart capture failed: {exc}")

        series = capture.series
        quality = capture.quality

        if series is None or len(series) == 0:
            return self._degrade(
                "; ".join(quality.issues) or "No chart data was returned."
            )

        # The source may know the asset and timeframe better than the config
        # does (a CSV knows its own spacing; the screen source does not).
        asset = capture.asset or self.asset
        chart_timeframe = capture.timeframe_seconds or self.chart_timeframe

        request = SignalRequest(
            series=series,
            asset=asset,
            chart_timeframe=chart_timeframe,
            trade_duration=self.trade_duration,
            quality=quality,
            available_durations=TRADE_DURATIONS,
            higher_multiple=int(self.config.get("market.higher_timeframe_multiple", 5)),
            entry_multiple=int(self.config.get("market.entry_timeframe_multiple", 1)),
            settings=self.gate_settings(),
        )

        try:
            signal = self.signal_engine.evaluate(request)
        except Exception as exc:
            log.exception("signal evaluation failed")
            return self._degrade(f"Analysis failed: {exc}")

        change = self.tracker.update(signal)

        # Settle anything whose expiration has elapsed, using the live price.
        try:
            if signal.price is not None:
                self.journal.resolve_outcomes(signal.price)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("outcome resolution failed: %s", exc)

        # Only material changes are journalled and alerted, so the log stays
        # readable and the user is not pinged every poll.
        if change.material:
            screenshot_path = None
            if capture.screenshot_png:
                screenshot_path = self.screenshots.save(
                    capture.screenshot_png, signal.id, signal.timestamp
                )
            try:
                self.journal.record(signal, screenshot_path)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("journal write failed: %s", exc)

            for alert in self.alerts.evaluate(signal, change):
                try:
                    self.journal.record_alert(
                        alert.kind,
                        alert.title,
                        alert.body,
                        alert.confidence,
                        alert.signal_id,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    log.debug("alert journal write failed: %s", exc)

        with self._lock:
            self.state.signal = signal
            self.state.change = change
            self.state.capture_meta = {
                **capture.meta,
                "quality": quality.to_dict(),
                "asset": asset,
                "timeframe_seconds": chart_timeframe,
            }
            self.state.last_update = utcnow().isoformat()
            self.state.last_error = None
            self.state.consecutive_errors = 0
            self.state.candles = _candle_payload(series)
            self.state.heikin_ashi_candles = _heikin_ashi_payload(series)

        self._broadcast()
        return self.state

    def _degrade(self, message: str) -> EngineState:
        """Record a failure without letting the loop die or a signal persist."""
        log.warning("%s", message)
        with self._lock:
            self.state.consecutive_errors += 1
            self.state.last_error = message
            self.state.last_update = utcnow().isoformat()

            # An unreadable chart must not leave an old BUY/SELL on screen.
            quality = DataQuality(
                ok=False,
                confidence=0.0,
                candle_count=0,
                issues=[message],
                source=self.source.name,
            )
            request = SignalRequest(
                series=Series((), self.chart_timeframe, self.asset),
                asset=self.asset,
                chart_timeframe=self.chart_timeframe,
                trade_duration=self.trade_duration,
                quality=quality,
                settings=self.gate_settings(),
            )
            signal = self.signal_engine.evaluate(request)
            change = self.tracker.update(signal)
            self.state.signal = signal
            self.state.change = change
            self.state.candles = []
            self.state.heikin_ashi_candles = []

        if change.material:
            for alert in self.alerts.evaluate(signal, change):
                log.debug("degraded-state alert: %s", alert.kind)
        self._broadcast()
        return self.state

    # -- background thread -------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        try:
            self.source.start()
        except Exception as exc:
            log.error("chart source failed to start: %s", exc)
        self._thread = threading.Thread(target=self._run, name="poa-engine", daemon=True)
        self._thread.start()
        self.state.running = True
        log.info(
            "engine started — source=%s asset=%s chart=%s duration=%s",
            self.source.name,
            self.asset,
            format_duration(self.chart_timeframe),
            format_duration(self.trade_duration),
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        try:
            self.source.stop()
        except Exception:  # pragma: no cover - best effort
            pass
        self.state.running = False
        log.info("engine stopped")

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
                backoff = 1.0
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("engine tick crashed: %s", exc)
                # Back off so a persistent failure does not spin the CPU.
                backoff = min(backoff * 2, 30.0)
                self._stop.wait(backoff)
                continue

            poll = max(0.5, float(self.config.get("capture.poll_seconds", 2.0)))
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.1, poll - elapsed))

    # -- reads for the API -------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self.state.to_dict()

    def options(self) -> dict[str, Any]:
        return {
            "chart_timeframes": [
                {"seconds": s, "label": format_duration(s)} for s in CHART_TIMEFRAMES
            ],
            "trade_durations": [
                {"seconds": s, "label": format_duration(s)} for s in TRADE_DURATIONS
            ],
            "current": {
                "asset": self.asset,
                "chart_timeframe": self.chart_timeframe,
                "trade_duration": self.trade_duration,
                "min_confidence": self.config.get("signals.min_confidence"),
                "min_duration_compatibility": self.config.get(
                    "signals.min_duration_compatibility"
                ),
                "poll_seconds": self.config.get("capture.poll_seconds"),
                "alerts_enabled": self.alerts.settings.enabled,
                "alert_cooldown": self.alerts.settings.cooldown_seconds,
            },
            "source": self.source.describe(),
        }

    def close(self) -> None:
        self.stop()
        try:
            self.journal.close()
        except Exception:  # pragma: no cover - best effort
            pass


def _candle_payload(series: Series, limit: int = 180) -> list[dict[str, Any]]:
    return [c.to_dict() for c in series.tail(limit)]


def _heikin_ashi_payload(series: Series, limit: int = 180) -> list[dict[str, Any]]:
    from .analysis import heikin_ashi

    return [c.to_dict() for c in heikin_ashi(series).tail(limit)]
