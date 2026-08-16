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
    parse_payouts,
    parse_settled_trade,
    parse_workspace_charts,
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

# Candle lengths built straight from the tick stream, below whatever the open
# chart is on. Everything *above* the chart's timeframe can be aggregated from
# candles already held, but nothing can be aggregated downwards — five-second
# candles cannot be recovered from one-minute ones, and a chart open at M1
# therefore had no way to see them at all.
#
# The ticks for them are already arriving several times a second, so this
# costs nothing on the wire. It costs a builder per pair per length, which is
# why they are capped and kept shallow: sixty candles is what the analysis
# reads, and a few hundred is ample for the replay behind it.
FAST_PERIODS: tuple[int, ...] = (5, 10, 15, 30)
MAX_FAST_CHARTS = 24
FAST_MAX_CANDLES = 600

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
        # A watched chart the user has chosen to read instead of the open one.
        # Nothing is clicked on the platform to honour it — the stream already
        # carries every instrument, so this only picks which one is read.
        self._focus: tuple[str, int] | None = None
        # Charts that have actually been open, as against ones that merely
        # ticked past. Kept because the two are not worth the same when the
        # watchlist is full and something has to be dropped.
        self._visited: set[tuple[str, int]] = set()
        # The instruments the page says it keeps charts for — the tabs along
        # the top of the platform. Empty until it says so.
        self._workspace: set[str] = set()
        # Sub-minute candles, built from the same ticks. Separate from
        # _charts because these are derived rather than followed: no history
        # ever arrives for them, and they are never what the platform means by
        # "the open chart".
        self._fast: dict[tuple[str, int], CandleBuilder] = {}
        # What each instrument pays, straight from the platform. Typed in by
        # hand this goes stale silently and in the flattering direction.
        self._payouts: dict[str, float] = {}
        # Trades the user actually placed, as the broker settled them. The
        # best evidence there is: a real fill on a real account, and not a
        # replay's opinion of where price would have been.
        self._settled: list[dict[str, Any]] = []

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

            if event == "updateAssets":
                found = parse_payouts(payload)
                if found:
                    self._payouts.update(found)
                return

            if event == "successcloseOrder":
                for trade in parse_settled_trade(payload):
                    self._settled.append(trade)
                    log.info(
                        "settled %s %s at %s%%: %s",
                        trade["asset"],
                        trade["direction"],
                        int((trade.get("payout") or 0) * 100),
                        "win" if trade["won"] else "loss",
                    )
                # Bounded: this is a hand-off queue, not a second journal.
                if len(self._settled) > 200:
                    del self._settled[:-200]
                return

            if event == "saveCharts":
                # The page's own record of a chart it keeps. One message
                # describes one chart — chartId, and that chart's settings —
                # so the workspace is the union of them, not the last one
                # seen. Replacing it collapsed the watchlist to whichever
                # chart the platform happened to save most recently, which is
                # a worse answer than not filtering at all.
                self._workspace |= parse_workspace_charts(payload)
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
                    # Every instrument the socket carries gets its own builder,
                    # at the timeframe the open chart is on. The stream arrives
                    # regardless; reading one and dropping the rest threw away
                    # a watchlist that costs nothing to keep.
                    period = int(self._period or 60)
                    keep = self._watchable()
                    for tick in ticks:
                        if keep is not None and tick.symbol not in keep:
                            continue
                        self._add_fast(tick, period)
                        if tick.symbol == self._asset:
                            self._asset_seen = time.monotonic()
                            self._builder.add(tick)
                            continue
                        key = (tick.symbol, period)
                        builder = self._charts.get(key)
                        if builder is None:
                            if len(self._charts) >= MAX_REMEMBERED_CHARTS:
                                continue
                            builder = CandleBuilder(
                                period_seconds=period,
                                max_candles=self.max_candles,
                                symbol=tick.symbol,
                            )
                            self._charts[key] = builder
                        builder.add(tick)
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

    def _add_fast(self, tick: Any, period: int) -> None:
        """Build the candle lengths below the open chart's. Lock held.

        Only below it: anything at or above can be aggregated from candles
        already held, and building it twice would be two answers to one
        question that could drift apart.
        """
        for length in FAST_PERIODS:
            if length >= period:
                break
            key = (tick.symbol, length)
            builder = self._fast.get(key)
            if builder is None:
                if len(self._fast) >= MAX_FAST_CHARTS:
                    continue
                builder = CandleBuilder(
                    period_seconds=length,
                    max_candles=FAST_MAX_CANDLES,
                    symbol=tick.symbol,
                )
                self._fast[key] = builder
            builder.add(tick)

    def _watchable(self) -> set[str] | None:
        """The instruments worth building candles for. Lock held.

        None means "anything on the stream" — the answer before the page has
        said what it keeps. Once it has, the watchlist is the user's own chart
        tabs rather than whichever dozen instruments tick loudest, which is
        what they were asking for.

        The open chart must be among them, or the nest was not what it looked
        like and the safe reading is to stop filtering.
        """
        if not self._workspace:
            return None
        if self._asset is not None and self._asset not in self._workspace:
            return None
        # Charts already being kept stay kept, so a pair that was on the list
        # when it was built does not lose its candles the moment the user
        # closes that tab on the platform.
        return self._workspace | {asset for asset, _period in self._charts}

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
        # Opening a chart on the platform is the clearest statement of what the
        # user wants read, so it ends any tab they had pinned here. Last action
        # wins, whichever window it happened in.
        if rank >= RANK_DECLARED:
            self._focus = None
        if self._asset is not None and self._period is not None:
            leaving = (self._asset, int(self._period))
            self._charts[leaving] = self._builder
            self._history_by_chart[leaving] = self._history_seen
            self._visited.add(leaving)

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

        A chart the user has actually opened is worth more than one that
        merely appeared on the stream — the platform ticks far more
        instruments than anyone trades, and without this the eight slots fill
        with whatever happened to arrive first and push out the pairs the user
        works on. Charts they have been on are given up last.
        """
        while len(self._charts) > MAX_REMEMBERED_CHARTS:
            spare = [
                key
                for key in self._charts
                if key not in self._visited and key[0] not in self._workspace
            ]
            if not spare:
                spare = [key for key in self._charts if key not in self._visited]
            oldest = spare[0] if spare else next(iter(self._charts))
            self._charts.pop(oldest, None)
            self._history_by_chart.pop(oldest, None)
            self._visited.discard(oldest)

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
            self._focus = None
        return "Re-reading the chart from the platform…"

    def focus(self, asset: str | None) -> bool:
        """Read one of the other watched charts instead of the open one.

        Every instrument on the watchlist is already being built from the same
        stream, so switching between them changes nothing on the platform:
        nothing is clicked, no chart is opened, no order is placed. It only
        picks which of the charts already in hand the panel reads.

        Passing the chart that is actually open on the platform — or None —
        goes back to following it.
        """
        wanted = str(asset or "").strip()
        with self._lock:
            open_key = (
                (self._asset, int(self._period or 60))
                if self._asset is not None
                else None
            )
            if not wanted:
                changed = self._focus is not None
                self._focus = None
                return changed

            match = self._resolve(wanted, open_key)
            if match is None:
                log.info("no watched chart called %s", wanted)
                return False
            if match == open_key:
                changed = self._focus is not None
                self._focus = None
                return changed
            if match == self._focus:
                return False
            log.info("reading %s from the watchlist", match[0])
            self._focus = match
            return True

    def _resolve(
        self, wanted: str, open_key: tuple[str, int] | None
    ) -> tuple[str, int] | None:
        """Find a watched chart by name. Lock held.

        The name may arrive in either form — the raw ``EURUSD_otc`` off the
        wire or the ``EUR/USD OTC`` the panel shows — so both are compared.
        """
        target = wanted.upper().replace(" ", "")
        candidates = list(self._charts)
        if open_key is not None:
            candidates.insert(0, open_key)
        for key in candidates:
            symbol = key[0]
            for name in (symbol, display_symbol(symbol)):
                if name.upper().replace(" ", "") == target:
                    return key
        return None

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

            # A chart picked off the watchlist is read in place of the open
            # one. It is built from the same stream, so it is as current; what
            # it will not have is the platform's history, which only arrives
            # for the chart actually open.
            focused = False
            if self._focus is not None and self._focus != (asset, period):
                builder = self._charts.get(self._focus)
                if builder is None:
                    # It aged out of what is remembered; stop pretending.
                    self._focus = None
                else:
                    asset, period = self._focus[0], self._focus[1]
                    series = builder.series()
                    focused = True

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
            if focused:
                # The platform only sends history for the chart it has open, so
                # a watched one fills from live ticks alone. Say that, rather
                # than promise a backfill that is not coming.
                tail = (
                    " Watched from the live stream — open it on the platform "
                    "to load its history."
                )
            else:
                tail = "" if history_seen else " Loading history from the platform…"
            issues.append(
                f"{count} candles so far; {self.min_candles} are needed for a "
                "full read." + tail
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
                    "history_loaded": history_seen and not focused,
                    "silent_for": round(silent_for, 1) if silent_for else None,
                    "candles": count,
                    "focused": focused,
                }
            },
        )

    def payout_for(self, asset: str) -> float | None:
        """What the platform says this instrument pays, or None."""
        with self._lock:
            if not self._payouts:
                return None
            wanted = str(asset).upper().replace(" ", "").replace("/", "")
            for symbol, payout in self._payouts.items():
                if symbol.upper().replace("_", "").replace("/", "") == wanted:
                    return payout
                if display_symbol(symbol).upper().replace(" ", "").replace("/", "") == wanted:
                    return payout
        return None

    def take_settled(self) -> list[dict[str, Any]]:
        """Hand over the real trades seen since this was last called."""
        with self._lock:
            out, self._settled = list(self._settled), []
        return out

    def watched(self) -> list[tuple[str, int, Series]]:
        """Every chart being followed, the active one first.

        The socket delivers all of them whether or not they are being looked
        at, so a watchlist is free — the only question is whether anything
        bothers to keep it.

        Names come out in the same display form as :meth:`read`, so a chart on
        this list and the same chart once it is opened are one chart to
        everything downstream: the panel's active tab, and the record of what
        has measured where.
        """
        with self._lock:
            out: list[tuple[str, int, Series]] = []
            if self._asset is not None:
                out.append(
                    (
                        display_symbol(self._asset),
                        int(self._period or 60),
                        self._builder.series(),
                    )
                )
            for (asset, period), builder in self._charts.items():
                if asset == self._asset and period == (self._period or 60):
                    continue
                out.append((display_symbol(asset), period, builder.series()))
            for (asset, period), builder in self._fast.items():
                out.append((display_symbol(asset), period, builder.series()))
        return out

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
                "watching": len(self._charts) + (1 if self._asset else 0),
                "focused": display_symbol(self._focus[0]) if self._focus else None,
            }
