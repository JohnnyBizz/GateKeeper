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
from .cdp import BrowserError, launch_browser, list_targets, pick_target
from .frames import decode_frame
from .protocol import (
    infer_period,
    parse_chart_request,
    parse_displayed_chart,
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

# How many charts keep their candles while another is being looked at.
# Enough to glance around a watchlist and come back; not so many that the
# buffers become a slow leak dressed as a feature.
MAX_REMEMBERED_CHARTS = 8

# How long the instrument we are following may go without a message before a
# lesser-ranked claim from another instrument is believed. This is the escape
# hatch for the case where the platform changed chart without saying so in a
# message we rank highly.
ASSET_STALE_SECONDS = 25.0

# How long to wait for the page to say what it is showing before asking it to
# reload. Attaching to a tab that loaded its chart minutes ago means the whole
# bootstrap — the symbol, the period, the history — has already been and gone.
BOOTSTRAP_GRACE_SECONDS = 8.0
REFRESH_COOLDOWN_SECONDS = 60.0
# Reloading a page that still says nothing is not going to start working on the
# fifth attempt, and a tab that reloads itself forever is its own bug. After
# this many the source says what it needs instead, and Scan can still ask again.
MAX_AUTO_REFRESHES = 2

# How the instrument was learned, most trustworthy last. A tick is never
# trusted: the socket streams several instruments at once, so the first symbol
# to arrive is arbitrary, and believing it is what put the wrong pair on the
# panel.
RANK_NONE = -1
RANK_TICK_HISTORY = 0  # updateHistoryNewFast — sent for a subscribed asset
RANK_HISTORY = 1  # loadHistoryPeriod(Fast) — history for the drawn chart
RANK_DECLARED = 2  # changeSymbol / saveCharts — the page naming its own chart


class FeedChartSource(ChartSource):
    """Candles built from the platform's own WebSocket messages."""

    name = "feed"
    vision_based = False
    names_own_chart = True

    def __init__(
        self,
        port: int = 9222,
        match: str = "pocketoption",
        min_candles: int = 60,
        max_candles: int = 500,
        auto_launch: bool = True,
        profile_dir: Any = None,
        refresh_chart: bool = True,
    ) -> None:
        if websockets is None:  # pragma: no cover - guaranteed by requirements
            raise ChartSourceError("Reading the feed needs the 'websockets' package.")
        self.port = int(port)
        self.match = match
        self.min_candles = int(min_candles)
        self.max_candles = int(max_candles)
        self.auto_launch = bool(auto_launch)
        self.profile_dir = profile_dir
        self.refresh_chart = bool(refresh_chart)
        self._launched = False

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._builder = CandleBuilder(period_seconds=60, max_candles=max_candles)
        # One builder per chart, kept so that looking at another pair and
        # coming back does not cost the history already gathered. Switching
        # charts to check something should be free; throwing away an hour
        # of candles for it is a reason not to look.
        self._charts: dict[tuple[str, int], CandleBuilder] = {}
        self._asset: str | None = None
        self._period: int | None = None
        self._asset_rank = RANK_NONE
        self._asset_seen = 0.0
        self._last_message = 0.0
        self._history_seen = False
        self._error: str | None = None
        self._connected = False
        # Instruments seen ticking while no chart has been identified. Only
        # used to explain the wait — never to pick one.
        self._streaming: set[str] = set()
        self._history_by_chart: dict[tuple[str, int], bool] = {}
        self._refresh_requested = False
        self._refreshed_at: float | None = None
        self._auto_refreshes = 0

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
        try:
            targets = list_targets(self.port)
        except BrowserError:
            # Nothing is listening. Start the browser rather than telling the
            # user to — the whole point is that opening GateKeeper is the only
            # thing they have to do.
            if not self.auto_launch:
                raise
            self._open_browser()
            targets = list_targets(self.port)

        target = pick_target(targets, self.match)
        if target is None:
            raise BrowserError(
                f"No {self.match} tab is open in the debugged browser. Sign in "
                "and open your chart in the window GateKeeper opened."
            )

        async with websockets.connect(
            target.websocket_url, max_size=32 * 1024 * 1024, ping_interval=None
        ) as connection:
            await connection.send(json.dumps({"id": 1, "method": "Network.enable"}))
            with self._lock:
                self._connected = True
                self._error = None
            log.info("reading the feed from %s", target.url)

            # A binary payload's name lives in the header frame before it, and
            # the two directions interleave, so each keeps its own pending name.
            pending: dict[str, str | None] = {"in": None, "out": None}
            attached_at = time.monotonic()
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(connection.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    await self._maybe_refresh(connection, attached_at)
                    continue

                try:
                    message = json.loads(raw)
                except ValueError:  # pragma: no cover - malformed
                    continue
                method = message.get("method")
                if method not in (
                    "Network.webSocketFrameReceived",
                    "Network.webSocketFrameSent",
                ):
                    continue

                # The page's own messages are the ones that say which chart is
                # open, and those travel outbound. Listening only to what the
                # server pushed is what left the panel guessing.
                direction = "in" if method.endswith("Received") else "out"
                response = (message.get("params") or {}).get("response") or {}
                frame = decode_frame(
                    str(response.get("payloadData", "")),
                    direction=direction,
                    opcode=int(response.get("opcode", 1)),
                )
                name = frame.event or pending[direction]
                pending[direction] = frame.announces
                if frame.payload is not None:
                    self._handle(name, frame.payload, direction)

                await self._maybe_refresh(connection, attached_at)

    async def _maybe_refresh(self, connection: Any, attached_at: float) -> None:
        """Reload the chart when the page has not told us what it is showing.

        Attaching to a tab whose chart loaded minutes ago means the messages
        that name the instrument and carry its history have already gone past.
        Rather than asking the user to switch timeframe to shake them loose —
        which is exactly the kind of chore this is supposed to remove — ask the
        page to load itself again, once.
        """
        with self._lock:
            now = time.monotonic()
            asked = self._refresh_requested
            if not asked:
                # A page that says nothing after two reloads is not going to
                # start on the third; something else is wrong, and saying so
                # beats a tab that reloads itself forever.
                if not self.refresh_chart:
                    return
                if self._asset is not None and self._history_seen:
                    return
                if now - attached_at <= BOOTSTRAP_GRACE_SECONDS:
                    return
                if self._auto_refreshes >= MAX_AUTO_REFRESHES:
                    return
                if (
                    self._refreshed_at is not None
                    and now - self._refreshed_at <= REFRESH_COOLDOWN_SECONDS
                ):
                    return
                self._auto_refreshes += 1
            self._refresh_requested = False
            self._refreshed_at = now

        log.info("asking the page to reload so the chart announces itself")
        await connection.send(json.dumps({"id": 2, "method": "Page.enable"}))
        await connection.send(
            json.dumps({"id": 3, "method": "Page.reload", "params": {}})
        )

    def _open_browser(self) -> None:
        """Start the debuggable browser once, and only once per session."""
        if self._launched:
            raise BrowserError(
                "The browser was started but is not answering. Close any "
                "GateKeeper browser windows and try again."
            )
        self._launched = True
        from ..config import data_root

        profile = self.profile_dir or (data_root() / "browser-profile")
        launch_browser(profile, port=self.port)

    # -- state --------------------------------------------------------------

    def _handle(
        self, event: str | None, payload: Any, direction: str = "in"
    ) -> None:
        with self._lock:
            self._last_message = time.monotonic()

            if event == "changeSymbol":
                change = parse_symbol_change(payload)
                if change is not None:
                    self._claim(change.asset, change.period_seconds, RANK_DECLARED)
                return

            if event == "saveCharts":
                asset, period = parse_displayed_chart(payload)
                if asset:
                    self._claim(asset, period, RANK_DECLARED)
                return

            if event in ("loadHistoryPeriod", "changeTimeFrame"):
                asset, period = parse_chart_request(payload)
                if asset:
                    self._claim(asset, period, RANK_HISTORY)
                return

            if event == "loadHistoryPeriodFast":
                asset, candles = parse_history_candles(payload)
                if not candles:
                    return
                # History for the pair the user just left can arrive after they
                # have switched away, so it only counts if it belongs to the
                # chart we are following.
                if asset and not self._claim(asset, infer_period(candles), RANK_HISTORY):
                    return
                if self._asset is None:
                    return
                self._builder.seed(candles)
                self._history_seen = True
                return

            if event == "updateHistoryNewFast":
                asset, period, ticks = parse_tick_history(payload)
                if asset and not self._claim(asset, period, RANK_TICK_HISTORY):
                    return
                if self._asset is None:
                    return
                self._builder.extend(ticks)
                return

            if event == "updateStream" or event is None:
                ticks = parse_ticks(payload)
                if ticks:
                    if self._asset is None:
                        # The socket carries several instruments at once. Which
                        # one ticks first says nothing about which chart is on
                        # screen, so wait to be told rather than pick.
                        self._streaming.update(tick.symbol for tick in ticks)
                        return
                    mine = [tick for tick in ticks if tick.symbol == self._asset]
                    if mine:
                        self._asset_seen = time.monotonic()
                    self._builder.extend(mine)
                    return

            # Last resort, and only while nothing is known: any message the
            # page itself sent that names an asset and a period is about the
            # chart it has open. The protocol is undocumented and can be
            # renamed under us; ending up with no chart at all is worse than
            # starting from a message we did not anticipate, and anything
            # better replaces it the moment it arrives.
            if direction == "out" and self._asset is None:
                asset, period = parse_chart_request(payload)
                if asset and period:
                    self._claim(asset, period, RANK_TICK_HISTORY)

    def _claim(self, asset: str, period: int | None, rank: int) -> bool:
        """Record what a message says the open chart is. Lock held.

        Returns whether the message belongs to the chart being followed, so its
        candles can be used. Claims are ranked: a message where the page names
        its own chart outranks history, which outranks a per-asset tick feed. A
        weaker claim never unseats a stronger one — that is what stops history
        for an instrument the user has just left from stealing the panel — but
        any claim wins once the instrument we are on has fallen silent.
        """
        now = time.monotonic()
        same = asset == self._asset
        if same:
            self._asset_seen = now

        if self._asset is None:
            self._rekey(asset, period or 60, rank)
            return True
        if same and (not period or period == self._period or rank < self._asset_rank):
            self._asset_rank = max(self._asset_rank, rank)
            return True

        silent = self._asset_seen == 0.0 or now - self._asset_seen > ASSET_STALE_SECONDS
        if rank >= self._asset_rank or silent:
            self._rekey(asset, period or self._period or 60, rank)
            return True
        return False

    def _rekey(self, asset: str, period: int, rank: int) -> None:
        """Follow a different chart from here. Lock held.

        A different instrument *or* a different timeframe is a different chart,
        so the candles never mix. But the one being left is remembered rather
        than discarded: glancing at another pair should not cost the history
        already gathered, or the cost becomes a reason not to look.
        """
        log.info("chart is %s at %ss", asset, period)
        key = (asset, int(period))
        if self._asset is not None and self._period is not None:
            self._charts[(self._asset, int(self._period))] = self._builder
            self._history_by_chart[(self._asset, int(self._period))] = self._history_seen

        self._asset = asset
        self._period = int(period)
        self._asset_rank = rank
        self._asset_seen = time.monotonic()
        self._streaming.clear()

        existing = self._charts.get(key)
        if existing is not None:
            self._builder = existing
            self._history_seen = self._history_by_chart.get(key, False)
            log.info("resuming %s at %ss with %d candles held",
                     asset, period, len(existing.settled))
            return

        self._history_seen = False
        self._builder = CandleBuilder(
            period_seconds=int(period), max_candles=self.max_candles, symbol=asset
        )
        self._forget_oldest_charts()

    def _forget_oldest_charts(self) -> None:
        """Bound what is remembered. Lock held.

        Remembering every chart ever opened is a slow leak dressed as a
        feature; a handful covers looking around and coming back.
        """
        while len(self._charts) > MAX_REMEMBERED_CHARTS:
            oldest = next(iter(self._charts))
            self._charts.pop(oldest, None)
            self._history_by_chart.pop(oldest, None)

    def resync(self) -> str:
        """Forget the chart and ask the page to announce it again.

        What Scan means when there is nothing on screen to look for.
        """
        with self._lock:
            self._asset = None
            self._period = None
            self._asset_rank = RANK_NONE
            self._asset_seen = 0.0
            self._history_seen = False
            self._streaming.clear()
            self._builder = CandleBuilder(
                period_seconds=60, max_candles=self.max_candles
            )
            self._refresh_requested = True
            self._auto_refreshes = 0
        return "Re-reading the chart from the platform…"

    # -- the engine's view --------------------------------------------------

    def capture(self) -> Capture:
        self.start()
        with self._lock:
            asset = self._asset
            period = self._period or 60
            error = self._error
            connected = self._connected
            history_seen = self._history_seen
            streaming = len(self._streaming)
            asked_twice = self._auto_refreshes >= MAX_AUTO_REFRESHES
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
            if not streaming:
                issues.append(
                    "Connected, but the platform has not sent any prices yet. "
                    "Open a chart in the browser window GateKeeper started."
                )
            elif asked_twice:
                issues.append(
                    "Prices are arriving, but the platform never said which "
                    "chart is open. Click a pair in the browser window, then "
                    "press Scan."
                )
            else:
                issues.append(
                    "Reading the platform, waiting for it to say which chart "
                    "is open — reloading the chart to ask."
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
                + ("" if history_seen else " Loading history from the platform…")
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
                "history_loaded": self._history_seen,
                "candles": len(self._builder.settled),
            }
