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
from pathlib import Path
from typing import Any, Callable

from ..config import Config, data_root, load_config
from ..engine import AnalysisEngine
from ..logging_setup import get_logger, install_crash_handlers, setup_logging
from ..models import format_duration, utcnow
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

# The most gates that may ever be demoted to advisory. A tool able to
# retire all its own rules eventually has none left, and the point is to
# correct a wrong rule rather than to dismantle the strategy.
MAX_ADVISORY_GATES = 2

# How many charts keep their measurement while another is being looked at.
MAX_REMEMBERED_CHARTS = 8

# How often every watched chart is re-read. Paced by the shortest candle
# being watched, because a sweep slower than the candles is a sweep that
# misses most of what closes: at fifteen seconds a five-second chart is
# read once every three bars, and two setups in three are over before
# anything looks. Charts whose newest candle has not moved are skipped, so
# a fast sweep costs no more than the charts that actually changed.
WATCH_SWEEP_SECONDS = 15.0
MIN_WATCH_SWEEP_SECONDS = 4.0

# How much better the recommended expiry has to score before the tool
# switches to it. Small preferences would have the panel changing expiry
# every poll, which is its own kind of unusable.
EXPIRY_SWITCH_MARGIN = 10.0


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
            max_losses_in_a_row=int(self.config.get("risk.max_losses_in_a_row", 0)),
            max_daily_loss_percent=float(
                self.config.get("risk.max_daily_loss_percent", 0.0)
            ),
        )
        self.vm.scan.duration = float(self.config.get("overlay.scan_seconds", 2.4))

        # Whether the last evaluation was actionable, so a setup that stands
        # for many polls is counted once rather than once per poll.
        self._was_actionable = False
        # Which chart's payout is currently loaded.
        self._payout_asset: str | None = None

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
        # Timestamp of the newest candle at the last measurement.
        self._proof_at: Any | None = None
        # What each chart last measured, so switching away and back is free.
        self._measured: dict[tuple[Any, ...], tuple[Any, int, Any]] = {}

        # Every chart looked at this session, for the report written at the end.
        self._charts_seen: set[str] = set()
        # Watched charts already announced, so a setup that stands for several
        # sweeps is called once rather than every fifteen seconds.
        self._announced: set[tuple[str, int]] = set()
        # The newest candle each chart had when it was last read, and what it
        # said. A one-minute chart changes once a minute; re-deriving the same
        # verdict from the same candles in between is work that buys nothing,
        # and skipping it is what makes sweeping often enough for five-second
        # charts affordable at all.
        self._read_at: dict[tuple[str, int], Any] = {}
        self._read_was: dict[tuple[str, int], dict[str, Any]] = {}

        # Verdicts for every chart the socket carries, not just the open one.
        self._watch_results: queue.Queue[Any] = queue.Queue()
        self._watch_busy = False
        self._watch_at: Any | None = None

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

        # The payout is per-asset and the platform changes it through the
        # session. Carrying one pair's number onto another silently moves the
        # break-even bar that every measurement here is judged against.
        self._load_payout_for(self.vm.asset)

        # Every chart looked at, named for the report written at the end.
        if self.vm.asset and self.vm.asset != UNKNOWN_ASSET:
            self._charts_seen.add(
                f"{self.vm.asset} {format_duration(self.vm.chart_timeframe)}"
            )

        # Count a setup once, when it first qualifies. Every evaluation mints
        # a fresh signal id, so keying on that counted the same standing setup
        # again on every poll — one call became eighty-eight. What makes a call
        # distinct is the *transition* into being actionable, not the identity
        # of the object reporting it.
        actionable = bool(signal is not None and signal.actionable)
        if actionable and not self._was_actionable:
            self.vm.calls_this_session += 1
        self._was_actionable = actionable

        self._follow_recommended_expiry(signal)

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

        # A measurement belongs to the chart it was measured on. After a switch
        # the new chart has too little history to replay for a while, and
        # leaving the old numbers up means the panel reports one instrument's
        # record over another's candles — the same lie as showing the wrong
        # pair, one line further down.
        if series is not None and self._proof_key is not None:
            key = (series.symbol, series.timeframe_seconds, self.engine.trade_duration)
            if key != self._proof_key:
                # Remember what this chart measured, so coming back to it is
                # free. Looking at another pair should cost nothing, or the
                # cost becomes a reason not to look.
                if self._proof_key is not None and self.vm.proof is not None:
                    self._measured[self._proof_key] = (
                        self.vm.proof, self._proof_bars, self._proof_at
                    )
                    while len(self._measured) > MAX_REMEMBERED_CHARTS:
                        self._measured.pop(next(iter(self._measured)), None)

                remembered = self._measured.get(key)
                self.vm.tuning = []
                self.vm.retired = []
                if remembered is not None:
                    self.vm.proof, self._proof_bars, self._proof_at = remembered
                    self._proof_key = key
                    if getattr(self.vm.proof, "calibration", None) is not None:
                        self.engine.set_calibration(
                            self.vm.proof.calibration, key[0], key[1]
                        )
                else:
                    self.vm.proof = None
                    self._proof_key = None
                    self._proof_bars = 0
                    self._proof_at = None

        if series is None or len(series) < PROOF_MIN_BARS:
            return

        key = (series.symbol, series.timeframe_seconds, self.engine.trade_duration)
        # Re-measure when the chart has moved on, judged by the newest candle's
        # timestamp rather than by how many are held. The buffer is a rolling
        # window: once it is full the count stops rising, and a count-based
        # test then reads "nothing new has happened" forever — freezing the
        # measurement at whatever the first full buffer happened to contain,
        # for the rest of the session.
        newest = series.candles[-1].timestamp if len(series) else None
        advanced = False
        if newest is not None and self._proof_at is not None:
            elapsed = (newest - self._proof_at).total_seconds()
            advanced = elapsed >= PROOF_RERUN_BARS * max(series.timeframe_seconds, 1)
        elif newest is not None:
            advanced = True

        if key == self._proof_key and not advanced:
            return

        self._proof_key = key
        self._proof_bars = len(series)
        self._proof_at = newest
        # Real settled trades for this exact chart and expiry. They outrank the
        # replay and are read here, on the UI thread, because the journal is
        # this thread's to talk to.
        try:
            real = self.engine.journal.calibration_records(
                asset=series.symbol,
                source=getattr(self.engine.source, "name", None),
                chart_timeframe=series.timeframe_seconds,
                trade_duration=self.engine.trade_duration,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("could not read the settled record: %s", exc)
            real = []

        self._proof_busy = True
        threading.Thread(
            target=self._proof_worker,
            args=(series, self.engine.trade_duration, self.vm.payout, real),
            daemon=True,
        ).start()

    def _proof_worker(
        self, series: Any, duration: int, payout: float, real: Any = None
    ) -> None:
        """Off the UI thread. Touches no Tk and no engine state."""
        from .proof import measure

        try:
            result = measure(
                series,
                trade_duration=duration,
                payout=payout,
                real_records=real,
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
            self._auto_tune(result.calibration)
        if result.gate_report is not None:
            self._retire_costly_gates(result.gate_report)

    def _retire_costly_gates(self, report: Any) -> None:
        """Stop enforcing a rule the chart has shown to be wrong.

        Every blocking gate is a claim: setups failing it are worse than setups
        passing it. The audit checks that claim against what the blocked setups
        actually settled at, and a gate refusing trades that would have paid is
        not prudence — it is a rule quietly costing money, and no amount of
        reasoning about the rule can discover that.

        Demotion is to *advisory*, never removal: the gate still reports, it
        just stops holding a veto. And it is bounded — a tool that can retire
        all its own rules eventually has none.
        """
        if not bool(self.config.get("signals.auto_tune", True)):
            return
        costly = report.costly()
        if not costly:
            return

        current = set(self.config.get("signals.advisory_gates", []) or [])
        room = MAX_ADVISORY_GATES - len(current)
        if room <= 0:
            return

        added = []
        for verdict in costly[:room]:
            if verdict.name in current:
                continue
            current.add(verdict.name)
            added.append(
                f"{verdict.name.replace('_', ' ')} no longer blocks · it "
                f"refused {verdict.blocked} setups that settled at "
                f"{verdict.blocked_rate:.0f}%"
            )
        if not added:
            return

        self.config.set("signals.advisory_gates", sorted(current))
        try:
            self.config.save()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("could not save the gate demotions: %s", exc)
        for line in added:
            log.info("retired a gate: %s", line)
        self.vm.retired = added
        self._run_engine_cycle()

    def _auto_tune(self, calibration: Any) -> None:
        """Let the measured record set the gates, rather than a typed number.

        How selective to be is not a matter of taste — it is whatever the
        record says was worth the most, and the record can work that out. The
        guards live in the tuner; all that happens here is applying what it
        proposes and remembering it, so the next session starts where this one
        finished rather than back at a default nobody chose.
        """
        if not bool(self.config.get("signals.auto_tune", True)):
            return
        from ..signals.autotune import tune

        try:
            adjustments = tune(
                calibration,
                float(self.config.get("signals.min_confidence", 75)),
                float(self.config.get("signals.min_duration_compatibility", 65)),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("auto-tune failed")
            self.vm.last_error = f"Could not tune the gates: {exc}"
            return
        if not adjustments:
            return

        for adjustment in adjustments:
            self.config.set(f"signals.{adjustment.key}", adjustment.now)
        try:
            self.config.save()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("could not save tuned gates: %s", exc)

        self.vm.tuning = adjustments
        # The next evaluation should use them rather than waiting for a restart.
        self._run_engine_cycle()

    def _follow_recommended_expiry(self, signal: Any) -> None:
        """Evaluate at the expiry the analysis wants, not the one left in a box.

        The duration gate was rejecting sound setups for a reason that had
        nothing to do with the market: the expiry in the settings did not match
        the one the move needed. That is not a market saying no, it is a
        stale number saying no, and it is a class of mistake the user should
        not be able to make. The tool follows its own recommendation and states
        the expiry to set, so the instruction is complete rather than
        conditional on the reader noticing a mismatch.

        Only when the recommendation is clearly better, so a two-point
        preference cannot set the panel oscillating between expiries.
        """
        if not bool(self.config.get("signals.follow_recommended_expiry", True)):
            return
        duration = getattr(signal, "duration", None)
        if duration is None:
            return
        wanted = int(getattr(duration, "recommended_seconds", 0) or 0)
        if wanted <= 0 or wanted == self.engine.trade_duration:
            return
        gain = float(getattr(duration, "recommended_score", 0.0)) - float(
            getattr(duration, "selected_score", 0.0)
        )
        if gain < EXPIRY_SWITCH_MARGIN:
            return
        try:
            self.engine.update_settings({"trade_duration": wanted})
            self.config.save()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("could not follow the recommended expiry: %s", exc)
            return
        self.vm.trade_duration = wanted
        log.info("expiry follows the analysis: %ss", wanted)

    def _load_payout_for(self, asset: str) -> None:
        """Use the payout remembered for this chart, if one was set."""
        if not asset or asset == UNKNOWN_ASSET:
            return
        if asset == self._payout_asset:
            return
        self._payout_asset = asset
        remembered = (self.config.get("market.payouts") or {}).get(asset)
        if remembered:
            self.vm.payout = float(remembered)
            self.config.set("market.payout", float(remembered))

    def set_payout(self, percent: float) -> None:
        """The user read the payout off the platform. Remember it per chart."""
        value = float(percent) / 100.0 if percent > 1.5 else float(percent)
        if not 0.1 <= value <= 1.5:
            return
        self.vm.payout = value
        self.config.set("market.payout", value)
        asset = self.vm.asset
        if asset and asset != UNKNOWN_ASSET:
            payouts = dict(self.config.get("market.payouts") or {})
            payouts[asset] = value
            self.config.set("market.payouts", payouts)
        try:
            self.config.save()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("could not save the payout: %s", exc)
        # Break-even moved, so every measured verdict has to be re-derived.
        self._proof_key = None
        self._proof_at = None
        self.vm.proof = None

    # -- the watchlist ------------------------------------------------------

    def _sweep_watchlist(self) -> None:
        """Read every chart the socket is carrying, not only the open one.

        The stream delivers all of them anyway. Reading one and discarding the
        rest meant a setup on another pair went unseen until the user happened
        to look — which is the job they were hoping to hand over.
        """
        if self._watch_busy:
            return
        watched = getattr(self.engine.source, "watched", None)
        if not callable(watched):
            return
        now = utcnow()
        if self._watch_at is not None:
            if (now - self._watch_at).total_seconds() < self._sweep_interval():
                return
        self._watch_at = now

        try:
            charts = watched()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("could not list the watched charts: %s", exc)
            return
        if not charts:
            # Between charts — a reload, a re-read, or the moment before the
            # platform says which one is open. watched() reports nothing at
            # all during those, and wiping the row on that blink threw the
            # tabs away for something that had nothing to do with them.
            return

        # A chart that has been measured is judged by its own record, here as
        # much as when it is open. Read on this thread — the cache belongs to
        # it. Only the charts the feed really holds have a record; a timeframe
        # derived by aggregation is a chart nothing has measured yet, and the
        # lookup returning nothing for it is the right answer.
        duration = self.engine.trade_duration
        measured = {}
        for asset, timeframe, _series in charts:
            remembered = self._measured.get((asset, timeframe, duration))
            if remembered is not None:
                measured[(asset, timeframe)] = getattr(
                    remembered[0], "calibration", None
                )

        self._watch_busy = True
        threading.Thread(
            target=self._watch_worker, args=(charts, measured), daemon=True
        ).start()

    def _sweep_interval(self) -> float:
        """How long to wait before reading everything again.

        Paced by the shortest candle on the watchlist. Waiting fifteen seconds
        between reads of a five-second chart means two setups in three are
        finished before anything looks at them.
        """
        periods = [
            int(row.get("timeframe") or 0)
            for row in self.vm.watchlist
            if int(row.get("timeframe") or 0) > 0
        ]
        if not periods:
            return WATCH_SWEEP_SECONDS
        return max(MIN_WATCH_SWEEP_SECONDS, min(float(min(periods)), WATCH_SWEEP_SECONDS))

    def _with_other_timeframes(self, charts: Any) -> Any:
        """Read each chart at the platform's other timeframes as well.

        A setup is a statement about a timeframe as much as about a pair: the
        same candles that say nothing at one minute can be a clean structure
        at five, and reading only the timeframe the chart happens to be open
        on throws that away. The platform offers S5 through M30, and every one
        of them is derivable from candles already in hand.

        Only upwards, and only on whole multiples — that is what aggregation
        can honestly do. A one-minute chart yields M2, M3, M5, M10, M15 and
        M30 immediately, from real history rather than from ticks gathered
        over the next several hours. Going the other way would mean inventing
        candles, so a chart open at M1 simply does not offer the second
        timeframes; opening one on the platform is what makes those available.
        """
        from ..analysis.resample import resample
        from ..config import SCAN_TIMEFRAMES

        if not bool(self.config.get("market.scan_timeframes", True)):
            return charts

        minimum = int(self.config.get("market.min_candles", 60))
        out = list(charts)
        seen = {(asset, timeframe) for asset, timeframe, _s in charts}

        for asset, timeframe, series in list(charts):
            if series is None or timeframe <= 0:
                continue
            for target in SCAN_TIMEFRAMES:
                if target <= timeframe or target % timeframe or (asset, target) in seen:
                    continue
                # Enough aggregated bars to read, or there is nothing to say.
                if len(series) // (target // timeframe) < minimum:
                    continue
                try:
                    out.append((asset, target, resample(series, target)))
                except Exception as exc:  # pragma: no cover - defensive
                    log.debug("could not resample %s to %ss: %s", asset, target, exc)
                    continue
                seen.add((asset, target))
        return out

    def _watch_worker(self, charts: Any, measured: Any = None) -> None:
        """Off the UI thread. Evaluates only; records and alerts nothing.

        Always answers, even on the way out of a failure: the busy flag is
        cleared by the reply, so a worker that died quietly would leave the
        watchlist frozen for the rest of the session with nothing to show for
        it.
        """
        measured = measured or {}
        rows = []
        try:
            # Aggregating eight deep charts up through six timeframes is a few
            # hundred milliseconds. On the UI thread that is a visible stutter
            # every fifteen seconds, so it happens here.
            here = (self.vm.asset, int(self.vm.chart_timeframe))
            for asset, timeframe, series in self._with_other_timeframes(charts):
                key = (asset, timeframe)

                # Nothing has closed on this chart since it was last read, so
                # the answer cannot have changed. Re-use it rather than
                # recomputing an identical verdict.
                newest = series.candles[-1].timestamp if len(series) else None
                if newest is not None and self._read_at.get(key) == newest:
                    previous = self._read_was.get(key)
                    if previous is not None:
                        rows.append(previous)
                        continue

                calibration = measured.get((asset, timeframe))
                signal = self.engine.evaluate_series(
                    series, asset, timeframe, calibration
                )
                if signal is None:
                    continue

                # An expiry has to suit the chart it is taken against. The
                # user's setting belongs to the chart they are looking at; on
                # a fifteen-minute chart a three-minute expiry is not a poor
                # fit, it is the wrong question — and asking it of every other
                # timeframe made all of them fail the fit gate, which would
                # have made this whole feature produce nothing.
                #
                # So elsewhere, the expiry is the one the engine picks for
                # that chart, and the row carries it so the user knows what to
                # set before taking it.
                expiry = int(self.engine.trade_duration)
                if (asset, timeframe) != here and signal.duration is not None:
                    wanted = int(signal.duration.recommended_seconds)
                    if wanted and wanted != expiry:
                        expiry = wanted
                        signal = (
                            self.engine.evaluate_series(
                                series, asset, timeframe, calibration,
                                trade_duration=wanted,
                            )
                            or signal
                        )

                row = {
                    "asset": asset,
                    "timeframe": timeframe,
                    "expiry": expiry,
                    "direction": signal.direction.value,
                    "score": round(signal.direction_confidence, 0),
                    "actionable": bool(signal.actionable),
                    "candles": len(series),
                }
                rows.append(row)
                if newest is not None:
                    self._read_at[key] = newest
                    self._read_was[key] = row
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("reading the watchlist failed: %s", exc)
        finally:
            self._watch_results.put(rows)

    def _collect_watchlist(self) -> None:
        try:
            rows = self._watch_results.get_nowait()
        except queue.Empty:
            return
        self._watch_busy = False
        # Fixed order, by name. Sorting by score would put the best setup first,
        # but these are click targets: a tab that moves between the reach and
        # the press opens a pair the user did not ask for. Colour marks the ones
        # worth looking at, and colour can change without anything moving.
        rows.sort(key=lambda r: str(r["asset"]))
        self.vm.watchlist = rows
        self._announce_watchlist(rows)

    def _announce_watchlist(self, rows: Any) -> None:
        """Say something when a chart nobody is looking at has a setup.

        A tab turning green only helps somebody already watching the tab row.
        The reason for reading eight charts is that the pair worth trading
        finds the user rather than the other way round, and that needs a
        noise, once, on the transition into being tradeable — not on every
        sweep while it stays that way.
        """
        here = (self.vm.asset, int(self.vm.chart_timeframe))
        live = {
            (str(row["asset"]), int(row.get("timeframe") or 0))
            for row in rows
            if row.get("actionable")
            and (str(row["asset"]), int(row.get("timeframe") or 0)) != here
        }
        fresh = live - self._announced
        # Forget the ones that have gone, so the same pair setting up again
        # later is worth announcing again.
        self._announced = live

        for row in rows:
            asset = str(row.get("asset", ""))
            timeframe = int(row.get("timeframe") or 0)
            if (asset, timeframe) not in fresh:
                continue
            direction = str(row.get("direction", ""))
            where = f"{asset} {format_duration(timeframe)}" if timeframe else asset
            expiry = int(row.get("expiry") or 0)
            # The expiry belongs in the alert, not just the panel: it is the
            # one thing the user has to change on the platform before the
            # setup being described is the trade they would place.
            take = f" Set a {format_duration(expiry)} expiry." if expiry else ""
            self.engine.emit_alert(
                kind="watchlist",
                title=f"{where} — {direction}",
                body=(
                    f"{direction} setup on {where} at "
                    f"{float(row.get('score') or 0):.0f}/100.{take}"
                ),
                confidence=float(row.get("score") or 0.0),
            )

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
        self._was_actionable = False
        self.engine.tracker.reset()
        self.vm.scan.reset()

    def _adjust(self, wins: int, losses: int) -> None:
        """A trade the user took, and how it went.

        The tally moves either way. A trade *added* also gets filed, because
        the record only ever learned from calls the tool made — and the verdict
        is WAIT most of the time, so the trades that carry the information it
        is missing are precisely the ones it never saw: what actually happens
        at scores it currently refuses.
        """
        self.vm.session.adjust(wins, losses)
        if wins > 0 or losses > 0:
            # In order, so a losing *run* can be told from four losses spread
            # across a good afternoon. Only additions: taking a win back is
            # fixing a miscount, not reporting a trade that happened.
            self.vm.session.record(won=wins > 0)
            self._file_manual_outcome(won=wins > 0)

    def _file_manual_outcome(self, won: bool) -> None:
        """Teach the record from a trade the user placed themselves.

        Filed against the direction the *score* was computed for, not the
        verdict — the verdict is usually WAIT, which is not a direction, while
        the score always describes one specific case being argued for or
        against. That is also the number the calibration is keyed on, so the
        pair stays coherent: this score, on this side, settled this way.

        Which assumes the trade went the way the panel was leaning. That is
        what the MARKET line is now showing at the moment the button is
        pressed, so it is a visible assumption rather than a hidden one — and
        a trade taken against it is a trade this cannot learn from correctly.
        """
        signal = self.vm.signal
        score = getattr(signal, "score", None) if signal is not None else None
        direction = getattr(score, "direction", None)
        name = getattr(direction, "value", None)
        if name not in ("CALL", "PUT"):
            # Nothing scored — during a scan, or before the first read. The
            # tally still moves; there is simply nothing to attribute.
            log.debug("no scored direction to file this outcome against")
            return

        regime = ""
        mtf = getattr(signal, "mtf", None)
        if mtf is not None:
            regime = str(getattr(mtf.current.regime.regime, "name", "") or "")

        try:
            self.engine.journal.record_manual(
                asset=self.vm.asset,
                chart_timeframe=self.vm.chart_timeframe,
                trade_duration=self.engine.trade_duration,
                direction=name,
                direction_confidence=float(signal.direction_confidence),
                duration_confidence=float(signal.duration_confidence),
                won=won,
                market_regime=regime,
                source=getattr(self.engine.source, "name", None),
                price=getattr(signal, "price", None),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("could not file the manual outcome: %s", exc)
            return

        log.info(
            "filed a manual %s on %s at %.0f",
            "win" if won else "loss",
            name,
            float(signal.direction_confidence),
        )
        # The record has changed, so what it recommends may have too.
        self._proof_at = None

    def _set_asset(self, asset: str) -> None:
        """A pair was picked — from a watchlist tab, or typed into the box.

        On the feed this reads a chart already in hand rather than renaming the
        one being read: every watched instrument arrives on the same stream.
        Nothing is clicked on the platform to do it, and no order is placed.
        Where the source cannot name its own chart the old behaviour stands and
        the typed name is a label.
        """
        asset = asset.strip()
        if not asset:
            return
        pick = getattr(self.engine.source, "focus", None)
        if callable(pick):
            try:
                if not pick(asset):
                    return
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("could not read %s: %s", asset, exc)
                return
            # The next poll names the chart; blank the old verdict rather than
            # show one pair's call under another pair's name.
            self.vm.signal = None
            self._refresh_session()
            return

        asset = asset.upper()
        if asset == self.vm.asset:
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
            self._collect_watchlist()
            self._sweep_watchlist()
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
            on_payout=self.set_payout,
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

    def write_session_report(self) -> Any:
        """Write down what was decided this session, and where it went.

        The panel is a live instrument — it shows what is true at the moment
        you look and forgets. Judging whether the thing is any good needs the
        whole session laid out at once, in a file that outlives the process
        and can be read by somebody who was not sitting in front of it.
        """
        from ..reporting import collect, write_report

        try:
            report = collect(
                self.engine.journal,
                started=self._session_since,
                source=getattr(self.engine.source, "name", None),
                stake=float(self.vm.risk.stake or 0.0),
                payout=float(self.vm.payout),
                charts=sorted(self._charts_seen),
                tuning=self.vm.tuning,
                retired=self.vm.retired,
                measurement=(
                    self.vm.proof.summary() if self.vm.proof is not None else ""
                ),
                # The one line wanted, not a whole repaint. Rendering the
                # entire panel to fetch a string would let any unrelated
                # drawing problem take the report down with it, at the one
                # moment there is no next frame to recover on.
                lesson=self.vm._lesson(),
            )
            return write_report(report, self._report_dir())
        except Exception as exc:  # pragma: no cover - never block the exit
            log.warning("could not write the session report: %s", exc)
            return None

    def _report_dir(self) -> Any:
        configured = self.config.get("storage.report_dir", "storage/reports")
        path = Path(str(configured))
        return path if path.is_absolute() else data_root() / path

    def shutdown(self) -> None:
        # Before the engine closes, while the journal is still open.
        self.write_session_report()
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
