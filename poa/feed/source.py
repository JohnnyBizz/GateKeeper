"""A chart source that reads the platform's socket instead of the screen.

Same interface as the screen source, and none of its failure modes. There is no
region to select, no axis to read, no colour profile to match and no OCR: the
symbol, the timeframe, and every price come from the platform's own messages.

The listener runs on its own thread because the engine polls synchronously and
the socket pushes continuously. Everything the two share sits behind one lock,
and ``capture()`` only ever takes a snapshot.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from ..chart_detection.base import Capture, ChartSource, ChartSourceError
from ..logging_setup import get_logger
from ..models import DataQuality, Series
from .cdp import BrowserError, list_targets, pick_target
from .frames import decode_frame
from .protocol import (
    infer_period,
    parse_history_candles,
    parse_symbol_change,
    parse_tick_history,
)
from .ticks import CandleBuilder, display_symbol, parse_ticks

log = get_logger(__name__)

try:  # pragma: no cover - optional
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]

# A feed that has gone quiet for this long is not a slow market — the socket
# has dropped, the tab has closed, or the platform has stopped streaming.
STALE_AFTER_SECONDS = 20.0


class FeedChartSource(ChartSource):
    """Candles built from the platform's own WebSocket messages."""

    name = "feed"
    vision_based = False

    def __init__(
        self,
        port: int = 9222,
        match: str = "pocketoption",
        min_candles: int = 60,
        max_candles: int = 500,
    ) -> None:
        if websockets is None:  # pragma: no cover - guaranteed by requirements
            raise ChartSourceError("Reading the feed needs the 'websockets' package.")
        self.port = int(port)
        self.match = match
        self.min_candles = int(min_candles)
        self.max_candles = int(max_candles)

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._builder = CandleBuilder(period_seconds=60, max_candles=max_candles)
        self._asset: str | None = None
        self._period: int | None = None
        self._last_message = 0.0
        self._history_seen = False
        self._error: str | None = None
        self._connected = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="gatekeeper-feed", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        self._thread = None

    # -- the listener -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                asyncio.run(self._listen())
            except Exception as exc:  # pragma: no cover - connection churn
                with self._lock:
                    self._error = str(exc)
                    self._connected = False
                log.warning("feed listener stopped: %s", exc)
            if not self._stop.is_set():
                time.sleep(2.0)  # the tab may be reloading; try again shortly

    async def _listen(self) -> None:
        targets = list_targets(self.port)
        target = pick_target(targets, self.match)
        if target is None:
            raise BrowserError(
                f"No {self.match} tab is open in the debugged browser."
            )

        async with websockets.connect(
            target.websocket_url, max_size=32 * 1024 * 1024, ping_interval=None
        ) as connection:
            await connection.send(json.dumps({"id": 1, "method": "Network.enable"}))
            with self._lock:
                self._connected = True
                self._error = None
            log.info("reading the feed from %s", target.url)

            pending_name: str | None = None
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(connection.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                try:
                    message = json.loads(raw)
                except ValueError:  # pragma: no cover - malformed
                    continue
                if message.get("method") != "Network.webSocketFrameReceived":
                    continue

                response = (message.get("params") or {}).get("response") or {}
                frame = decode_frame(
                    str(response.get("payloadData", "")),
                    direction="in",
                    opcode=int(response.get("opcode", 1)),
                )
                # A binary payload's name lives in the header frame before it.
                name = frame.event or pending_name
                pending_name = frame.announces
                if frame.payload is not None:
                    self._handle(name, frame.payload)

    # -- state --------------------------------------------------------------

    def _handle(self, event: str | None, payload: Any) -> None:
        with self._lock:
            self._last_message = time.monotonic()

            change = parse_symbol_change(payload) if event == "changeSymbol" else None
            if change is not None:
                if change.asset != self._asset or change.period_seconds != self._period:
                    log.info(
                        "chart switched to %s at %ss",
                        change.asset, change.period_seconds,
                    )
                    self._asset = change.asset
                    self._period = change.period_seconds
                    # A different instrument or timeframe is a different chart.
                    # Carrying candles across would splice two of them together.
                    self._builder = CandleBuilder(
                        period_seconds=change.period_seconds,
                        max_candles=self.max_candles,
                        symbol=change.asset,
                    )
                    self._history_seen = False
                return

            if event == "loadHistoryPeriodFast":
                asset, candles = parse_history_candles(payload)
                if not candles:
                    return
                # History for the pair the user just left can arrive after they
                # have switched away.
                if asset and self._asset and asset != self._asset:
                    return
                if self._asset is None and asset:
                    self._adopt(asset, infer_period(candles) or 60)
                self._builder.seed(candles)
                self._history_seen = True
                return

            if event == "updateHistoryNewFast":
                asset, period, ticks = parse_tick_history(payload)
                if asset and self._asset and asset != self._asset:
                    return
                if self._asset is None and asset:
                    self._adopt(asset, period or 60)
                self._builder.extend(ticks)
                return

            if event == "updateStream" or event is None:
                ticks = parse_ticks(payload)
                if not ticks:
                    return
                if self._asset is None:
                    self._adopt(ticks[0].symbol, 60)
                self._builder.extend(
                    tick for tick in ticks if tick.symbol == self._asset
                )

    def _adopt(self, asset: str, period: int) -> None:
        """Called with the lock held."""
        self._asset = asset
        self._period = int(period)
        self._builder = CandleBuilder(
            period_seconds=int(period), max_candles=self.max_candles, symbol=asset
        )

    # -- the engine's view --------------------------------------------------

    def capture(self) -> Capture:
        self.start()
        with self._lock:
            asset = self._asset
            period = self._period or 60
            error = self._error
            connected = self._connected
            history_seen = self._history_seen
            series = self._builder.series() if asset else None
            silent_for = (
                time.monotonic() - self._last_message if self._last_message else None
            )

        issues: list[str] = []
        confidence = 100.0

        if error:
            issues.append(f"Not reading the feed: {error}")
            confidence = 0.0
        elif not connected:
            issues.append("Connecting to the browser…")
            confidence = 0.0
        elif asset is None:
            issues.append(
                "Connected, but the platform has not sent any prices yet. Open "
                "a chart in the browser window GateKeeper started."
            )
            confidence = 0.0
        elif silent_for is not None and silent_for > STALE_AFTER_SECONDS:
            issues.append(
                f"No prices for {silent_for:.0f}s — the chart may have been "
                "closed, or the connection dropped."
            )
            confidence = 25.0

        count = len(series) if series else 0
        if series is not None and count < self.min_candles:
            # Ticks alone accumulate in real time, so a fresh connection with no
            # history behind it needs minutes before there is enough to read.
            shortfall = 1.0 - count / max(self.min_candles, 1)
            confidence = min(confidence, 100.0 - 60.0 * shortfall)
            issues.append(
                f"{count} candles so far; {self.min_candles} are needed for a "
                "full read."
                + ("" if history_seen else " Switch timeframe once to load history.")
            )

        quality = DataQuality(
            ok=confidence >= 50.0 and count >= 25,
            confidence=max(0.0, min(100.0, confidence)),
            candle_count=count,
            issues=issues,
            source=self.name,
            timeframe_detected=self._period is not None,
        )

        return Capture(
            series=series,
            quality=quality,
            asset=display_symbol(asset) if asset else None,
            timeframe_seconds=period,
            meta={
                "feed": {
                    "asset": asset,
                    "period": period,
                    "connected": connected,
                    "history_loaded": history_seen,
                    "silent_for": round(silent_for, 1) if silent_for else None,
                    "candles": count,
                }
            },
        )

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "vision_based": False,
                "symbol": display_symbol(self._asset) if self._asset else None,
                "timeframe_seconds": self._period,
                "connected": self._connected,
            }
