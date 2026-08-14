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

from ..models import Direction, SignalState, format_duration, format_price
from ..risk import RiskAssessment, SessionStats, assess_risk
from ..signals.engine import Signal

# How long the scanning state is held before the verdict is revealed. The
# analysis itself completes in well under this; the delay exists so the user
# sees the verdict was re-derived rather than silently swapped.
SCAN_DURATION_SECONDS = 2.4


class ScanState(str, Enum):
    IDLE = "IDLE"
    SCANNING = "SCANNING"
    REVEALED = "REVEALED"


# Colours the panel paints with, kept here so tests can assert on them.
COLORS = {
    "call": "#22c55e",
    "put": "#ef4444",
    "wait": "#eab308",
    "no_trade": "#dc2626",
    "neutral": "#64748b",
    "text": "#e2e8f0",
    "dim": "#94a3b8",
    "faint": "#64748b",
    "bg": "#0b0f16",
    "panel": "#121826",
    "raised": "#1b2333",
    "border": "#26304a",
    "accent": "#3b82f6",
}


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
                # One chart, one setup: the count is 0 or 1, and it is spelled
                # out because "NO TRADE" and "WAIT" both mean zero while
                # looking nothing alike, and because a number is what you can
                # glance at without reading a word.
                "take_now": 1 if signal.actionable else 0,
                "take_label": "1 trade" if signal.actionable else "0 trades",
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
            "scan": {
                "state": self.scan.state.value,
                "progress": round(self.scan.progress(), 3),
                "scanning": scanning,
            },
            "session": {
                **self.session.to_dict(self.payout),
                "calls": self.calls_this_session,
                "manual": self.session_manual,
            },
            "risk": {
                **risk.to_dict(),
                "stake_overridden": self.stake_override is not None,
                "collapsed": self.risk_collapsed,
            },
            "proof": self._proof(),
            "calibration": self._calibration(),
            "tuning": [
                adjustment.describe() for adjustment in self.tuning[-2:]
            ],
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
        if self.source == "synthetic":
            warnings.insert(
                0,
                "This is demo data, not your chart. Press Scan to find the "
                "chart on your screen.",
            )
        if self.signal.state is SignalState.WEAKENING:
            warnings.insert(0, "Setup is weakening — confidence has fallen since entry.")
        elif self.signal.state is SignalState.INVALIDATED:
            warnings.insert(0, "Setup invalidated — do not treat the last signal as live.")
        return warnings[:4]


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
