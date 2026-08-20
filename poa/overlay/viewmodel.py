"""The overlay's view model and scan state machine.

Deliberately free of any GUI import. Everything the panel draws is produced
here as plain data, which means the whole of the overlay's behaviour — the scan
cycle, the blanking, the colours, the disclaimers — is testable without a
display. The Tk layer is a thin renderer over this.

The scan cycle mirrors what a trader needs to see:

    IDLE ──scan()──▶ SCANNING (verdict blanked) ──▶ REVEALED (new verdict)

Blanking the previous verdict during the scan is not decoration. It stops the
old answer being read as the new one, which is exactly the failure mode a
persistent BUY on screen invites.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..models import Bias, Direction, SignalState, format_duration, format_price
from ..risk import RiskAssessment, SessionStats, assess_risk
from ..signals.engine import Signal

# How long the scanning state is held before the verdict is revealed. The
# analysis itself completes in well under this; the delay exists so the user
# sees the verdict was re-derived rather than silently swapped.
#
# It was 2.4 seconds, which is most of a five-second bar and a twelfth of a
# thirty-second trade spent watching an animation whose only job is to say
# "this is a new answer". Long enough to read as a re-read, short enough that
# pressing Scan is not itself a cost.
SCAN_DURATION_SECONDS = 0.6


class ScanState(str, Enum):
    IDLE = "IDLE"
    SCANNING = "SCANNING"
    REVEALED = "REVEALED"


# Colours the panel paints with, kept here so tests can assert on them.
# One palette, and every surface in the panel comes from it. The greys step
# evenly from the window background up to the raised tiles so that depth reads
# as depth rather than as three unrelated shades of navy, and the text tones
# are spaced far enough apart that "important", "supporting" and "aside" are
# distinguishable at a glance instead of on inspection.
COLORS = {
    "call": "#22c55e",
    "put": "#f43f5e",
    "wait": "#f59e0b",
    "no_trade": "#e11d48",
    "neutral": "#64748b",
    "text": "#eef2f8",
    "dim": "#9aa8bd",
    "faint": "#5f6e85",
    "bg": "#080b12",
    "panel": "#0f1725",
    "raised": "#16202f",
    # The hairline between surfaces. Named for what it is rather than for the
    # widget option it used to feed, now that surfaces are drawn rather than
    # outlined; ``border`` stays as the old name for anything still asking.
    "line": "#243044",
    "border": "#243044",
    "accent": "#6aa8ff",
}

# The chart on screen plus the eight the feed keeps behind it.
MAX_WATCHED = 9


def direction_color(direction: Direction | str) -> str:
    key = direction.value if isinstance(direction, Direction) else str(direction)
    return {
        "CALL": COLORS["call"],
        "PUT": COLORS["put"],
        "WAIT": COLORS["wait"],
        "NO_TRADE": COLORS["no_trade"],
    }.get(key, COLORS["neutral"])


def score_color(score: float | None) -> str:
    if score is None:
        return COLORS["neutral"]
    if score >= 80:
        return COLORS["call"]
    if score >= 65:
        return COLORS["accent"]
    if score >= 50:
        return COLORS["wait"]
    return COLORS["put"]


def strength_badge(score: float | None) -> tuple[str, str]:
    """(label, colour) for the HIGH / MEDIUM / LOW chip beside the score."""
    if score is None:
        return "--", COLORS["neutral"]
    if score >= 80:
        return "HIGH", COLORS["call"]
    if score >= 65:
        return "MEDIUM", COLORS["wait"]
    if score >= 50:
        return "LOW", COLORS["put"]
    return "VERY LOW", COLORS["put"]


@dataclass
class ScanController:
    """Tracks the scan cycle. Time is injected so tests need no sleeping."""

    state: ScanState = ScanState.IDLE
    started_at: float | None = None
    duration: float = SCAN_DURATION_SECONDS
    _clock: Any = field(default=time.monotonic, repr=False)

    def begin(self) -> None:
        self.state = ScanState.SCANNING
        self.started_at = self._clock()

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return self._clock() - self.started_at

    def progress(self) -> float:
        """0..1 through the scanning window."""
        if self.state is not ScanState.SCANNING or self.duration <= 0:
            return 1.0
        return max(0.0, min(1.0, self.elapsed() / self.duration))

    def poll(self) -> bool:
        """Advance the state machine. Returns True when the scan just finished."""
        if self.state is ScanState.SCANNING and self.elapsed() >= self.duration:
            self.state = ScanState.REVEALED
            return True
        return False

    @property
    def scanning(self) -> bool:
        return self.state is ScanState.SCANNING

    def reset(self) -> None:
        self.state = ScanState.IDLE
        self.started_at = None


@dataclass
class RecordingState:
    """A capture of the live feed, as the panel needs to see it.

    Written by the recording thread and read by the UI thread. Every field is
    a number or a string, assigned whole, so there is nothing here that can be
    read half-updated — which is what lets the two threads share it without a
    lock around every repaint.
    """

    active: bool = False
    elapsed: float = 0.0
    total: float = 0.0
    frames: int = 0
    #: A finished bundle the user has not opened yet. Cleared once they have,
    #: so the button goes back to offering another recording — a session is
    #: worth more as two captures of different markets than as one, and a
    #: button that permanently reads "TAP TO OPEN" can never take the second.
    bundle: str = ""
    #: The last file produced, kept after the button has gone back to idle.
    last_bundle: str = ""
    #: Why it could not be taken, if it could not.
    error: str = ""
    #: A line for the panel: what it is doing, or what it produced.
    message: str = ""
    #: When the run began, on the monotonic clock. The recording thread only
    #: reports progress as frames arrive, so a socket that has gone quiet
    #: would leave the countdown frozen at half an hour — which reads as a
    #: hung app during exactly the wait it exists to make bearable. The clock
    #: is the truth about how long is left; the frame count is not.
    started_at: float | None = None
    _clock: Any = field(default=time.monotonic, repr=False)

    def since_start(self) -> float:
        """How long this has been running, by the clock rather than by luck."""
        if self.active and self.started_at is not None:
            return max(0.0, self._clock() - self.started_at)
        return self.elapsed

    def progress(self) -> float:
        """0..1 through the recording."""
        if not self.total:
            return 0.0
        return max(0.0, min(1.0, self.since_start() / self.total))

    def remaining(self) -> float:
        return max(0.0, self.total - self.since_start())

    def to_dict(self) -> dict[str, Any]:
        if self.active:
            left = self.remaining()
            label = (
                f"RECORDING — {left / 60:.0f} MIN LEFT"
                if left >= 60
                else f"RECORDING — {left:.0f}s LEFT"
            )
            colour = COLORS["put"]
        elif self.error:
            label = "RECORD FAILED — TAP TO RETRY"
            colour = COLORS["wait"]
        elif self.bundle:
            label = "RECORDING SAVED — TAP TO OPEN"
            colour = COLORS["call"]
        else:
            label = "RECORD 30 MIN FOR ANALYSIS"
            colour = COLORS["accent"]
        return {
            "active": self.active,
            "progress": round(self.progress(), 3),
            "frames": self.frames,
            "elapsed": round(self.since_start(), 1),
            "remaining": round(self.remaining(), 1),
            "label": label,
            "color": colour,
            "bundle": self.bundle,
            "last_bundle": self.last_bundle,
            "error": self.error,
            "message": self.message,
        }


@dataclass
class OverlayViewModel:
    """Everything the panel renders, as plain data."""

    signal: Signal | None = None
    session: SessionStats = field(default_factory=SessionStats)
    payout: float = 0.92
    balance: float = 1000.0
    risk_percent: float = 2.0
    # When set, the user typed a stake directly; it beats the percentage.
    stake_override: float | None = None
    scan: ScanController = field(default_factory=ScanController)
    asset: str = "EUR/USD"
    chart_timeframe: int = 60
    trade_duration: int = 180
    connected: bool = False
    last_error: str | None = None
    data_confidence: float | None = None
    # Which feed the candles came from. Shown in the header, because a panel
    # reading LIVE over invented data is the most misleading thing this app
    # could do — every score and every pattern below it would be about a market
    # that does not exist.
    source: str = "screen"
    # How many setups have passed every gate since the session began. The
    # headline answers "what now"; this answers "how often", which is what
    # tells you whether the panel is quiet because the market is.
    calls_this_session: int = 0
    # When true the win/loss tally belongs to the user alone, and settled
    # journal outcomes never touch it.
    session_manual: bool = True
    # The risk block folds away. It is the tallest part of the panel and the
    # part that changes least once a stake is set.
    risk_collapsed: bool = False
    # The measured record of this engine on this chart's own history: the one
    # number here that is a fact rather than an opinion. None until the replay
    # has run. Typed loosely to keep the GUI-free layer free of the
    # backtester too.
    proof: Any | None = None
    # Gate changes the measured record made for itself, most recent last.
    # Shown because a setting that changes silently is indistinguishable from
    # a bug, and the user is entitled to see the tool adjusting itself.
    tuning: list[Any] = field(default_factory=list)
    # Rules the chart has shown to be wrong, and which no longer block.
    retired: list[str] = field(default_factory=list)
    # Every chart the feed is carrying, with its own verdict. The socket
    # delivers them whether or not they are being looked at.
    watchlist: list[Any] = field(default_factory=list)
    # The brakes. Zero disables either one.
    max_losses_in_a_row: int = 0
    max_daily_loss_percent: float = 0.0
    # Everything below the decision, folded away by default. The panel had
    # grown to hold every true thing at once, which is a different job from
    # showing the one thing being decided.
    details_collapsed: bool = True
    # The candles behind the verdict. The panel scored a market it never
    # showed: a number saying 82 and an arrow saying up are a claim, and the
    # shape of the last half hour is what lets anyone judge whether the claim
    # is plausible. Typed loosely to keep this layer free of the models too.
    recent: Any = None
    # A recording of the live feed, running or just finished. Held as plain
    # numbers rather than as a thread handle, so the panel can be drawn from
    # it and the whole of it tested without a browser.
    recording: RecordingState = field(default_factory=RecordingState)
    # Injected so the countdown can be tested without waiting for a minute.
    _now: Any = field(default=time.time, repr=False)

    # ------------------------------------------------------------------

    @property
    def risk(self) -> RiskAssessment:
        return assess_risk(
            self.balance,
            self.risk_percent,
            self.payout,
            observed_win_rate=self.session.win_rate,
            stake_override=self.stake_override,
            observed_sample=self.session.total,
            session=self.session,
            max_losses_in_a_row=self.max_losses_in_a_row,
            max_daily_loss_percent=self.max_daily_loss_percent,
        )

    def render(self) -> dict[str, Any]:
        """Build the full render payload for the panel."""
        scanning = self.scan.scanning
        signal = self.signal

        # While scanning, the verdict is deliberately withheld.
        if scanning or signal is None:
            verdict = {
                "direction": "--",
                "direction_label": "SCANNING" if scanning else "WAITING FOR DATA",
                "color": COLORS["neutral"],
                "arrow": "",
                "score": None,
                "score_display": "--",
                "score_color": COLORS["neutral"],
                "badge": "--",
                "badge_color": COLORS["neutral"],
                "pattern": "" if scanning else "--",
                "duration_score": None,
                "duration_display": "--",
                "recommended": "--",
                "state": "SCANNING" if scanning else "IDLE",
                "actionable": False,
                "blanked": True,
                "take_now": 0,
                "take_label": "--" if scanning else "0 trades",
            }
        else:
            score = signal.direction_confidence
            badge, badge_color = strength_badge(signal.overall_confidence)
            pattern = (
                signal.mtf.current.pattern.name if signal.mtf is not None else "--"
            )
            verdict = {
                "direction": signal.direction.value,
                "direction_label": _direction_label(signal.direction),
                "color": direction_color(signal.direction),
                "arrow": _arrow(signal.direction),
                "score": round(signal.overall_confidence, 0),
                "score_display": f"{signal.overall_confidence:.0f} / 100",
                "score_color": score_color(signal.overall_confidence),
                "badge": badge,
                "badge_color": badge_color,
                "pattern": pattern,
                "duration_score": (
                    round(signal.duration_confidence, 0) if signal.duration else None
                ),
                "duration_display": (
                    f"{signal.duration_confidence:.0f} / 100" if signal.duration else "--"
                ),
                "recommended": (
                    signal.duration.recommended_label if signal.duration else "--"
                ),
                "state": signal.state.value,
                "actionable": signal.actionable,
                "blanked": False,
                # Every chart being watched, not only the one on screen. The
                # count used to be 0 or 1 because one chart yields at most one
                # setup — but eight charts are being read at the same bar, and
                # counting only the open one reported a tenth of what was
                # actually available while the rest sat unmentioned in a tab.
                **self._take_now(signal),
            }

        risk = self.risk

        return {
            "header": {
                "asset": self.asset,
                "connected": self.connected,
                "status": self._status_text(),
                "status_color": self._status_color(),
            },
            "tiles": {
                "pair": self.asset,
                "payout": f"{self.payout * 100:.0f}%",
                "time": format_duration(self.trade_duration),
                "chart": format_duration(self.chart_timeframe),
            },
            "verdict": verdict,
            "trend": self._trend(scanning),
            "scan": {
                "state": self.scan.state.value,
                "progress": round(self.scan.progress(), 3),
                "scanning": scanning,
            },
            "session": {
                **self.session.to_dict(self.payout),
                "calls": self.calls_this_session,
                "manual": self.session_manual,
                "taught": self._taught(),
            },
            "risk": {
                **risk.to_dict(),
                "stake_overridden": self.stake_override is not None,
                "collapsed": self.risk_collapsed,
            },
            "entry": self._entry(scanning, signal),
            "recording": self.recording.to_dict(),
            "chart": self._chart(scanning),
            "details": self._details(),
            "watchlist": self._watchlist(),
            "lesson": self._lesson(),
            "proof": self._proof(),
            "calibration": self._calibration(),
            "tuning": [
                adjustment.describe() for adjustment in self.tuning[-2:]
            ]
            + list(self.retired[-2:]),
            "price": format_price(signal.price) if signal else "--",
            "reason": self._reason(),
            "warnings": self._warnings(),
            "disclaimer": "Analysis only — not a trading recommendation.",
        }

    # ------------------------------------------------------------------

    def _status_text(self) -> str:
        if self.last_error:
            return "ERROR"
        if not self.connected:
            return "OFFLINE"
        # Ahead of the data-confidence check: a perfect read of the demo feed
        # is still the demo feed, and "LOW DATA" would understate that.
        if self.source == "synthetic":
            return "DEMO DATA"
        if self.source == "csv":
            return "REPLAY"
        if self.source == "feed":
            return "LIVE FEED"
        if self.data_confidence is not None and self.data_confidence < 70:
            return f"LOW DATA {self.data_confidence:.0f}%"
        return "LIVE"

    def _status_color(self) -> str:
        if self.last_error or not self.connected:
            return COLORS["put"]
        if self.source not in ("screen", "feed"):
            return COLORS["wait"]
        if self.data_confidence is not None and self.data_confidence < 70:
            return COLORS["wait"]
        return COLORS["call"]

    def _calibration(self) -> dict[str, Any]:
        """What setups scoring like the live one have actually settled at.

        This is the difference between the panel saying "78" and the panel
        saying "setups scoring 70-80 here settled at 56% over 41 trades". The
        first is a number the engine made up from weights somebody chose; the
        second is a fact about this instrument. Only the second is worth
        anything when deciding whether to put money on it.
        """
        blank = {"text": "", "color": COLORS["faint"], "ready": False}
        calibration = getattr(self.proof, "calibration", None)
        if calibration is None or self.signal is None or self.scan.scanning:
            return blank

        # The direction score, which is what the record is keyed on.
        band = calibration.measured_rate(self.signal.direction_confidence)
        if band is None:
            recommended = calibration.recommended_threshold()
            if recommended is None:
                # A record big enough to judge, and no gate setting in it that
                # cleared the rate this payout needs. That is a finding, and
                # the useful thing to say — the answer is a different chart,
                # not a looser gate. Loosening until something fires would be
                # manufacturing calls the record says lose money.
                if getattr(calibration, "total", 0) >= calibration.min_sample:
                    return {
                        "text": (
                            f"No gate setting measured above break-even here "
                            f"across {calibration.total} setups — try another "
                            "pair or expiry."
                        ),
                        "color": COLORS["put"],
                        "ready": True,
                        "beats": False,
                    }
                return blank
            threshold, bucket = recommended
            return {
                "text": (
                    f"Gate at {threshold}+ measured {bucket.win_rate:.0f}% "
                    f"over {bucket.settled} here"
                ),
                "color": COLORS["dim"],
                "ready": True,
            }

        breakeven = calibration.breakeven
        beats = band.beats(breakeven)
        source = (
            "your settled trades"
            if getattr(calibration, "from_real_trades", False)
            else "replayed history"
        )
        return {
            "text": (
                f"Direction {band.label} settled at {band.win_rate:.0f}% over "
                f"{band.settled} ({source}) — break-even {breakeven:.0f}%"
            ),
            "color": COLORS["call"] if beats else COLORS["put"],
            "ready": True,
            "beats": beats,
        }

    def _entry(self, scanning: bool, signal: Signal | None) -> dict[str, Any]:
        """When to act, which is a different question from whether to.

        The panel could say BUY at 82 and still leave the one thing unanswered
        that decides whether the trade is any good: *now, or not yet?* The
        analysis reads candles that have closed, so the verdict on screen
        belongs to the bar currently forming — and it is re-derived the moment
        that bar ends. Knowing the verdict without knowing how much of that bar
        is left is knowing half of it, and it is the half that separates being
        on time from chasing.

        Candles sit on wall-clock boundaries, so the time remaining is
        arithmetic rather than something that has to be tracked: a one-minute
        candle closes on the minute, wherever the app happened to start.
        """
        period = max(1, int(self.chart_timeframe))
        remaining = period - (int(self._now()) % period)
        elapsed = 1.0 - (remaining / period)
        clock = f"{remaining // 60}:{remaining % 60:02d}"

        blank = {
            "text": "—",
            "detail": "",
            "clock": "",
            "seconds": remaining,
            "progress": elapsed,
            "urgent": False,
            "ready": False,
        }
        if scanning or signal is None:
            return blank

        if signal.actionable:
            # The last stretch of a bar is when a setup confirmed on it is
            # about to be re-read — which is a reason to hurry or to skip, but
            # either way a reason to know.
            urgent = remaining <= max(3, period // 10)
            return {
                "text": "TAKE IT NOW",
                "detail": (
                    f"{format_duration(self.trade_duration)} expiry — this "
                    f"candle closes in {remaining}s and the read changes with it"
                    if urgent
                    else f"{format_duration(self.trade_duration)} expiry — "
                    f"{remaining}s left on this candle"
                ),
                "clock": clock,
                "seconds": remaining,
                "progress": elapsed,
                "urgent": urgent,
                "ready": True,
            }

        return {
            # Reads as one phrase with the clock beside it: "NEXT CANDLE IN
            # 0:23". The old wording repeated what the clock already said and
            # left it nowhere to sit.
            "text": "NEXT CANDLE IN",
            "detail": "No setup here yet — every chart is re-read as its bar closes.",
            "clock": clock,
            "seconds": remaining,
            "progress": elapsed,
            "urgent": False,
            "ready": False,
        }

    def _chart(self, scanning: bool) -> dict[str, Any]:
        """The candles behind the verdict, ready to be drawn.

        Reduced to plain numbers here rather than handed over as a Series, so
        the drawing layer needs to know nothing about the models and this stays
        testable without one.

        The move over the window comes with them: a chart that has risen
        through the session and a chart that has fallen to the same price look
        alike at a glance and mean opposite things.

        Not blanked during a scan, unlike the verdict. The blanking rule exists
        so a stale *answer* cannot be read as a fresh one, and these are not an
        answer — they are the observation everything else is derived from, and
        they belong to whichever chart the panel is naming either way. Dropping
        them for the two seconds a scan takes only made the window jump.
        """
        blank: dict[str, Any] = {
            "closes": [], "bars": [], "change": None,
            "change_label": "", "rising": None, "ready": False,
        }
        series = self.recent
        candles = list(getattr(series, "candles", ()) or ())
        if len(candles) < 2:
            return blank

        closes = [float(candle.close) for candle in candles]
        first, last = closes[0], closes[-1]
        change = ((last - first) / first * 100.0) if first else 0.0
        return {
            "closes": closes,
            "bars": [
                (float(c.open), float(c.high), float(c.low), float(c.close))
                for c in candles
            ],
            "change": change,
            "change_label": f"{change:+.3f}%",
            "rising": change >= 0,
            "ready": True,
        }

    def _details(self) -> dict[str, Any]:
        """The evidence block, and the one line of it that stays out.

        Everything under the decision — the measured record, what the losing
        calls had in common, what the record changed about the gates — is true
        and worth having, and all of it at once is why the panel stopped being
        glanceable. Folded away by default, with the single number that says
        whether any of it is good news left on the header.
        """
        summary = "not measured yet"
        proof = self.proof
        if proof is not None:
            edge = getattr(proof, "edge", None)
            if edge is not None and getattr(proof, "meaningful", False):
                summary = f"{edge:+.0f} pts vs break-even"
            elif getattr(proof, "settled", 0):
                summary = f"{int(proof.settled)} measured so far"
        return {"collapsed": self.details_collapsed, "summary": summary}

    def _take_now(self, signal: Signal) -> dict[str, Any]:
        """How many setups are live right now, across everything being read.

        The bar is not lowered to get this number up. Eight charts arrive on
        the same socket and are evaluated against the same gates at the same
        moment; the count was simply ignoring seven of them. One chart yields
        a handful of setups in a day, which is a rate that makes the tool feel
        broken — and the fix is more charts, not weaker standards.

        The open chart is counted from its own live signal rather than from
        the watchlist sweep, because that one is re-read every couple of
        seconds while the sweep runs every fifteen.
        """
        here = 1 if signal.actionable else 0
        # A chart is a pair *and* a timeframe. The same pair at five minutes
        # is a different read from the one on screen at one minute, and a
        # tradeable setup there is a trade that is genuinely available.
        elsewhere = [
            row
            for row in self.watchlist
            if row.get("actionable")
            and (row.get("asset"), int(row.get("timeframe") or 0))
            != (self.asset, self.chart_timeframe)
        ]
        total = here + len(elsewhere)
        label = f"{total} trade{'' if total == 1 else 's'}"
        if elsewhere:
            # Name them: a count with nowhere to go is a count nobody can act
            # on, and the whole point is that the pair finds the user.
            names = ", ".join(
                str(row.get("asset", "")).replace(" OTC", "") for row in elsewhere[:3]
            )
            label = f"{total} — {names}" if not here else f"{total} — here, {names}"
        return {"take_now": total, "take_label": label}

    def _taught(self) -> str:
        """What the WIN/LOSS buttons have taught the record so far.

        Without this the buttons move a counter and appear to do nothing else,
        and the one loop that makes the tool better is invisible for exactly as
        long as it takes to fill — which is when the encouragement to keep
        filling it would be worth something.
        """
        calibration = getattr(self.proof, "calibration", None)
        if calibration is None:
            return ""
        settled = int(getattr(calibration, "real_available", 0) or 0)
        if settled <= 0:
            return ""
        if getattr(calibration, "from_real_trades", False):
            return f"Learning from your {settled} settled trades on this chart."
        needed = int(getattr(calibration, "min_sample", 20)) - settled
        return (
            f"{settled} of your trades recorded — {needed} more and they "
            "outrank the replay."
        )

    def _trend(self, scanning: bool) -> dict[str, Any]:
        """Which way the market is going — separately from whether to trade it.

        The verdict box answers "act or not", and most of the time the answer
        is not, which leaves the other question unanswered: which way is this
        thing moving? It was in the analysis all along — three timeframes each
        with a lean — and only ever surfaced as prose in the risk block, where
        you had to read a paragraph to find out the tool was leaning bearish.

        Three arrows, one per view, and a word for where they come out. When
        they point the same way that is a trend; when they do not, that is
        worth seeing too, and is usually why the verdict is WAIT.
        """
        blank = {
            "label": "--",
            "arrow": "",
            "color": COLORS["neutral"],
            "views": [],
            "strength": None,
            "agreement": None,
            "detail": "",
            "blanked": True,
        }
        signal = self.signal
        if scanning or signal is None or signal.mtf is None:
            return blank

        mtf = signal.mtf
        # Only the views that are really distinct. When there is not enough
        # history to aggregate a higher timeframe, or the entry timeframe is
        # the chart itself, the stack is one chart counted twice — and a copy
        # of a view agreeing with the view it copies is not agreement. Showing
        # it as separate arrows, or letting it vote twice for the word beside
        # them, is the same double-count the confirmation gate already refuses.
        views = [
            ("HIGH", mtf.higher, 0.40, mtf.higher_is_distinct),
            ("NOW", mtf.current, 0.35, True),
            ("ENTRY", mtf.entry, 0.25, mtf.entry_is_distinct),
        ]
        distinct = [(name, view, weight) for name, view, weight, ok in views if ok]
        rendered = [
            {
                "name": name,
                "arrow": _bias_arrow(view.trend_bias),
                "color": _bias_color(view.trend_bias),
                "bias": view.trend_bias.value,
            }
            for name, view, _weight in distinct
        ]

        # The word is computed from the same views the arrows show, so the two
        # can never contradict each other on screen.
        total = sum(weight for _n, _v, weight in distinct) or 1.0
        bull = sum(w for _n, v, w in distinct if v.trend_bias is Bias.BULLISH) / total
        bear = sum(w for _n, v, w in distinct if v.trend_bias is Bias.BEARISH) / total
        if abs(bull - bear) < 0.15:
            consensus = Bias.NEUTRAL
        else:
            consensus = Bias.BULLISH if bull > bear else Bias.BEARISH

        strength = round(mtf.current.trend_strength, 0)
        agreement = round(max(bull, bear) * 100, 0)
        if consensus is Bias.NEUTRAL:
            detail = "the timeframes disagree" if agreement < 60 else "no clear direction"
        else:
            detail = f"{agreement:.0f}% of the timeframes agree"

        return {
            "label": _bias_label(consensus),
            "arrow": _bias_arrow(consensus),
            "color": _bias_color(consensus),
            "views": rendered,
            "strength": strength,
            "agreement": agreement,
            "detail": detail,
            "blanked": False,
        }

    def _watchlist(self) -> list[dict[str, Any]]:
        """Every watched chart, ready to be shown as tabs.

        A chart is a pair *and* a timeframe: the same candles that say nothing
        at one minute can be a clean structure at five, so both appear, and
        both are named. Bounded, so a long session cannot grow the row past
        the panel.

        Ranking decides which rows survive the cut, not where they sit. With
        several timeframes per pair there are more charts than places, so the
        ones worth acting on have to be the ones kept — but the survivors are
        then laid out by name, because these are click targets and a tab that
        moves between the reach and the press opens something nobody asked
        for. Colour does the ranking on screen; colour can change without
        anything moving.
        """
        # A row containing only the chart already on screen says nothing the
        # rest of the panel is not already saying at greater length. Judged
        # here rather than upstream, because a lone setup elsewhere is still
        # worth counting and still worth an alert — it is only the *tabs* that
        # have nothing to show.
        if len(self.watchlist) < 2 and not any(
            (row.get("asset"), int(row.get("timeframe") or 0))
            != (self.asset, self.chart_timeframe)
            for row in self.watchlist
        ):
            return []

        kept = sorted(
            self.watchlist,
            key=lambda row: (not row.get("actionable"), -float(row.get("score") or 0)),
        )[:MAX_WATCHED]
        rows = []
        for row in sorted(
            kept, key=lambda r: (str(r.get("asset", "")), int(r.get("timeframe") or 0))
        ):
            direction = str(row.get("direction", "WAIT"))
            asset = str(row.get("asset", ""))
            timeframe = int(row.get("timeframe") or 0)
            expiry = int(row.get("expiry") or 0)
            short = format_duration(timeframe).replace(" ", "") if timeframe else ""
            actionable = bool(row.get("actionable"))
            # A setup found on another chart was scored against the expiry that
            # suits *that* chart, which is usually not the one the platform is
            # set to. Taking it at the wrong expiry is a different trade from
            # the one that passed — and the panel used to keep that to itself,
            # so a green tab invited exactly that mistake.
            mismatched = bool(actionable and expiry and expiry != self.trade_duration)
            rows.append(
                {
                    "asset": asset,
                    "timeframe": timeframe,
                    "expiry": expiry,
                    "label": f"{asset.replace(' OTC', '')} {short}".strip(),
                    "needs": _compact_duration(expiry) if mismatched else "",
                    "mismatched": mismatched,
                    "score": row.get("score"),
                    "direction": direction,
                    "actionable": actionable,
                    "color": (
                        direction_color(direction) if actionable else COLORS["faint"]
                    ),
                    "active": (
                        asset == self.asset and timeframe == self.chart_timeframe
                    ),
                }
            )
        return rows

    def _lesson(self) -> str:
        """What the losing calls on this chart had in common.

        "The setup failed" is useless. Which of the ten scored components
        argued for the losing side is answerable, and is the only form of
        learning from a loss that survives contact with the next one.
        """
        attribution = getattr(self.proof, "attribution", None)
        if attribution is None or self.scan.scanning:
            return ""
        if attribution.winners + attribution.losers < 10:
            return ""
        return attribution.headline()

    def _proof(self) -> dict[str, Any]:
        """The measured record, and how much weight the panel should give it.

        Colour is deliberately withheld until the sample is big enough to mean
        something. A green 100% over three trades is the most misleading thing
        this panel could paint.
        """
        if self.proof is None:
            return {"text": "Measuring this chart…", "color": COLORS["faint"], "ready": False}

        edge = getattr(self.proof, "edge", None)
        meaningful = bool(getattr(self.proof, "meaningful", False))
        if not meaningful or edge is None:
            color = COLORS["faint"]
        elif edge >= 0:
            color = COLORS["call"]
        else:
            color = COLORS["put"]
        return {
            "text": self.proof.summary(),
            "color": color,
            "ready": True,
            "meaningful": meaningful,
            "edge": edge,
        }

    def _reason(self) -> str:
        if self.scan.scanning:
            return "Re-reading the chart…"
        if self.last_error:
            return self.last_error
        if self.signal is None:
            return "Waiting for chart data."
        return self.signal.reason

    def _warnings(self) -> list[str]:
        if self.scan.scanning or self.signal is None:
            return []
        warnings = list(self.signal.warnings)
        # The reason line above already carries the engine's explanation, and
        # when a signal is refused for want of data the engine builds that
        # line *out of these very issues* — so the panel said "16 candles so
        # far; 60 are needed for a full read" as prose and then said it again
        # underneath with a warning triangle in front of it. Twice is not
        # twice as clear, and the second copy pushes anything genuinely new
        # off the bottom: only four warnings are ever shown.
        reason = self._reason()
        warnings = [
            line for line in warnings if line and str(line).strip() not in reason
        ]
        if self.source == "synthetic":
            warnings.insert(
                0,
                "This is demo data, not your chart. Press Scan to find the "
                "chart on your screen.",
            )
        # A call that needs a different expiry is a call the user cannot take
        # as they are set up. Said before anything else, because acting on it
        # unchanged is not "close enough" — it is a trade the engine never
        # scored, at an expiry it may well have refused.
        elsewhere = [
            row for row in self._watchlist() if row.get("mismatched")
        ]
        if elsewhere:
            wanted = sorted({int(row["expiry"]) for row in elsewhere})
            names = ", ".join(format_duration(seconds) for seconds in wanted[:2])
            warnings.insert(
                0,
                f"{len(elsewhere)} setup{'' if len(elsewhere) == 1 else 's'} "
                f"elsewhere need a {names} expiry — you are set to "
                f"{format_duration(self.trade_duration)}. Change it first, or "
                "it is a different trade.",
            )
        if self.signal.state is SignalState.WEAKENING:
            warnings.insert(0, "Setup is weakening — confidence has fallen since entry.")
        elif self.signal.state is SignalState.INVALIDATED:
            warnings.insert(0, "Setup invalidated — do not treat the last signal as live.")
        return warnings[:4]


def _compact_duration(seconds: int) -> str:
    """``600`` becomes ``10M`` — short enough to sit on a watchlist tab."""
    seconds = int(seconds)
    if seconds <= 0:
        return ""
    if seconds < 60:
        return f"{seconds}S"
    if seconds % 60:
        return f"{seconds // 60}M{seconds % 60}S"
    return f"{seconds // 60}M"


def _direction_label(direction: Direction) -> str:
    return {
        Direction.CALL: "BUY",
        Direction.PUT: "SELL",
        Direction.WAIT: "WAIT",
        Direction.NO_TRADE: "NO TRADE",
    }[direction]


def _arrow(direction: Direction) -> str:
    return {
        Direction.CALL: "▲",
        Direction.PUT: "▼",
        Direction.WAIT: "●",
        Direction.NO_TRADE: "✕",
    }[direction]


def _bias_arrow(bias: Bias) -> str:
    """Which way, at a glance. A flat bar for sideways, not a blank."""
    return {Bias.BULLISH: "▲", Bias.BEARISH: "▼"}.get(bias, "▬")


def _bias_label(bias: Bias) -> str:
    """What the market is doing, not what to do about it.

    Deliberately not BUY/SELL — those belong to the verdict box, and a trend
    word that reads like an instruction is how "the market is rising" turns
    into "buy", which is the whole thing this tool is built not to do.
    """
    return {
        Bias.BULLISH: "RISING",
        Bias.BEARISH: "FALLING",
    }.get(bias, "SIDEWAYS")


def _bias_color(bias: Bias) -> str:
    return {
        Bias.BULLISH: COLORS["call"],
        Bias.BEARISH: COLORS["put"],
    }.get(bias, COLORS["wait"])
