"""Alert routing, deduplication and cooldown.

The engine re-evaluates every poll, so without suppression the user would be
notified several times a minute about the same unchanged setup. The manager
enforces three rules:

* only *material* changes produce alerts (the tracker decides what is material);
* the same fingerprint is not repeated inside the cooldown window;
* alert kinds the user switched off are dropped before any channel sees them.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from ..logging_setup import get_logger
from ..models import Direction, Regime, SignalState, utcnow
from ..signals.engine import Signal
from ..signals.tracker import TrackedChange
from .notifiers import Alert, DesktopNotifier, LogNotifier, Notifier, SoundNotifier

log = get_logger(__name__)

ALERT_KINDS: tuple[str, ...] = (
    "BUY_SIGNAL",
    # A setup on one of the other charts being watched. Same bar, same gates —
    # a chart nobody is looking at is still a chart being read.
    "WATCHLIST",
    "SELL_SIGNAL",
    "SETUP_INVALIDATED",
    "SETUP_WEAKENING",
    "TREND_REVERSAL",
    "HIGH_VOLATILITY",
    "CONFLICTING_SIGNALS",
    "DATA_QUALITY",
)

_EMOJI = {
    "BUY_SIGNAL": "\U0001f7e2",
    "WATCHLIST": "\U0001f440",
    "SELL_SIGNAL": "\U0001f534",
    "SETUP_INVALIDATED": "\U0001f534",
    "SETUP_WEAKENING": "⚠️",
    "TREND_REVERSAL": "\U0001f504",
    "HIGH_VOLATILITY": "⚠️",
    "CONFLICTING_SIGNALS": "⚠️",
    "DATA_QUALITY": "⚠️",
}


# Alert kinds that postdate saved configuration files. Nothing exposes the
# notify list for editing, so a config written before one of these existed
# cannot have deliberately excluded it — it simply predates it. Silently
# leaving such a kind off would make a new feature look broken on every
# install that has ever saved its settings.
_ADDED_LATER = ("WATCHLIST",)


def _with_new_kinds(kinds: Iterable[Any]) -> tuple[str, ...]:
    chosen = [str(kind).upper() for kind in kinds]
    for kind in _ADDED_LATER:
        if kind not in chosen:
            chosen.append(kind)
    return tuple(chosen)


@dataclass
class AlertSettings:
    enabled: bool = True
    desktop_notifications: bool = True
    # On by default: the tool's whole job is telling the user something, and
    # it spent eight releases doing so silently on the platform most installs
    # run on. The default command needs nothing installed on any platform.
    sound: bool = True
    sound_command: str = ""
    cooldown_seconds: float = 120.0
    min_confidence: float = 75.0
    notify_on: tuple[str, ...] = ALERT_KINDS

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> "AlertSettings":
        defaults = cls()
        kinds = section.get("notify_on") or list(defaults.notify_on)
        return cls(
            enabled=bool(section.get("enabled", defaults.enabled)),
            desktop_notifications=bool(
                section.get("desktop_notifications", defaults.desktop_notifications)
            ),
            sound=bool(section.get("sound", defaults.sound)),
            sound_command=str(section.get("sound_command", defaults.sound_command)),
            cooldown_seconds=float(
                section.get("cooldown_seconds", defaults.cooldown_seconds)
            ),
            min_confidence=float(section.get("min_confidence", defaults.min_confidence)),
            notify_on=_with_new_kinds(kinds),
        )


# Alert kinds whose whole message is "act now". While the user's own
# loss-limit brake is tripped these stay unsent: a notification saying BUY
# beside a panel saying STAND DOWN is the tool losing an argument with its
# own limit, out loud. Everything informational — invalidations, reversals,
# data quality — still flows; those describe the market rather than prompt
# an entry.
ACT_NOW_KINDS = frozenset({"BUY_SIGNAL", "SELL_SIGNAL", "WATCHLIST"})


class AlertManager:
    """Decides what deserves an alert and sends it to every enabled channel."""

    def __init__(
        self,
        settings: AlertSettings | None = None,
        notifiers: Iterable[Notifier] | None = None,
    ) -> None:
        self.settings = settings or AlertSettings()
        #: While True, the act-now kinds are withheld. Set by the overlay
        #: from the risk brake; nothing in here decides when — this class
        #: only promises that a standing-down panel and a shouting
        #: notification cannot happen at the same time.
        self.stand_down = False
        self.notifiers: list[Notifier] = list(notifiers) if notifiers else []
        if not self.notifiers:
            self.notifiers.append(LogNotifier())
            if self.settings.desktop_notifications:
                desktop = DesktopNotifier()
                if desktop.available:
                    self.notifiers.append(desktop)
                else:
                    log.info(
                        "Desktop notifications are enabled but no notification "
                        "tool was found on this system."
                    )
            if self.settings.sound:
                sound = SoundNotifier(self.settings.sound_command)
                if sound.available:
                    self.notifiers.append(sound)

        self._last_sent: dict[str, datetime] = {}
        self.recent: deque[Alert] = deque(maxlen=100)

    def add_notifier(self, notifier: Notifier) -> None:
        self.notifiers.append(notifier)

    # ------------------------------------------------------------------

    def evaluate(
        self, signal: Signal, change: TrackedChange, now: datetime | None = None
    ) -> list[Alert]:
        """Build the alerts a change warrants, then dispatch them."""
        if not self.settings.enabled or not change.material:
            return []

        now = now or utcnow()
        alerts = self._build(signal, change)
        sent: list[Alert] = []
        for alert in alerts:
            if alert.kind not in self.settings.notify_on:
                continue
            if self.stand_down and alert.kind in ACT_NOW_KINDS:
                continue
            if alert.kind in ("BUY_SIGNAL", "SELL_SIGNAL") and (
                alert.confidence < self.settings.min_confidence
            ):
                continue
            fingerprint = f"{alert.kind}|{signal.asset}|{int(alert.confidence // 5) * 5}"
            last = self._last_sent.get(fingerprint)
            if last is not None and now - last < timedelta(
                seconds=self.settings.cooldown_seconds
            ):
                continue
            self._last_sent[fingerprint] = now
            alert.timestamp = now.isoformat()
            self._dispatch(alert)
            self.recent.append(alert)
            sent.append(alert)
        return sent

    def announce(
        self,
        kind: str,
        title: str,
        body: str,
        confidence: float = 0.0,
        asset: str = "",
        now: datetime | None = None,
    ) -> Alert | None:
        """Raise an alert that did not come from tracking one signal.

        A setup on a chart the user is not looking at has no tracked change to
        describe it — nothing was followed, because nothing was on screen. It
        still deserves the same noise, and the same restraint: the enabled
        switch, the notify list and the cooldown all apply exactly as they do
        to a signal on the open chart.

        The cooldown is per chart and per direction rather than one queue for
        the whole watchlist, so eight charts setting up at once do raise eight
        alerts. That is the point of reading eight charts; what it must not do
        is repeat one that is merely still standing, and the caller keys that
        off the transition into being tradeable.
        """
        if not self.settings.enabled:
            return None
        if kind.upper() not in self.settings.notify_on:
            return None
        if self.stand_down and kind.upper() in ACT_NOW_KINDS:
            return None

        moment = now or utcnow()
        fingerprint = f"{kind}|{asset}|{int(confidence // 5) * 5}"
        last = self._last_sent.get(fingerprint)
        if last is not None and moment - last < timedelta(
            seconds=self.settings.cooldown_seconds
        ):
            return None

        alert = Alert(
            kind=kind.upper(),
            title=title,
            body=body,
            emoji=_EMOJI.get(kind.upper(), "\U0001f440"),
            confidence=float(confidence),
            timestamp=moment.isoformat(),
        )
        self._last_sent[fingerprint] = moment
        self._dispatch(alert)
        self.recent.append(alert)
        return alert

    def _dispatch(self, alert: Alert) -> None:
        for notifier in self.notifiers:
            try:
                notifier.send(alert)
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("notifier %s failed: %s", notifier.name, exc)

    # ------------------------------------------------------------------

    def _build(self, signal: Signal, change: TrackedChange) -> list[Alert]:
        alerts: list[Alert] = []
        confidence = signal.overall_confidence

        if signal.state is SignalState.INVALIDATED:
            alerts.append(
                Alert(
                    kind="SETUP_INVALIDATED",
                    title="SETUP INVALIDATED",
                    body=change.description,
                    emoji=_EMOJI["SETUP_INVALIDATED"],
                    confidence=confidence,
                    signal_id=signal.id,
                    severity="warning",
                )
            )
        elif signal.state is SignalState.WEAKENING:
            alerts.append(
                Alert(
                    kind="SETUP_WEAKENING",
                    title="SETUP WEAKENING",
                    body=change.description,
                    emoji=_EMOJI["SETUP_WEAKENING"],
                    confidence=confidence,
                    signal_id=signal.id,
                    severity="warning",
                )
            )
        elif signal.direction is Direction.CALL and change.kind in ("new", "direction"):
            alerts.append(
                Alert(
                    kind="BUY_SIGNAL",
                    title=f"BUY SIGNAL — {signal.asset}",
                    body=(
                        f"CALL on {signal.asset} {signal.chart_timeframe_label} chart. "
                        f"Recommended duration {signal.duration.recommended_label if signal.duration else '--'}. "
                        f"Direction {signal.direction_confidence:.0f}/100, "
                        f"duration fit {signal.duration_confidence:.0f}/100."
                    ),
                    emoji=_EMOJI["BUY_SIGNAL"],
                    confidence=confidence,
                    signal_id=signal.id,
                    severity="critical",
                )
            )
        elif signal.direction is Direction.PUT and change.kind in ("new", "direction"):
            alerts.append(
                Alert(
                    kind="SELL_SIGNAL",
                    title=f"SELL SIGNAL — {signal.asset}",
                    body=(
                        f"PUT on {signal.asset} {signal.chart_timeframe_label} chart. "
                        f"Recommended duration {signal.duration.recommended_label if signal.duration else '--'}. "
                        f"Direction {signal.direction_confidence:.0f}/100, "
                        f"duration fit {signal.duration_confidence:.0f}/100."
                    ),
                    emoji=_EMOJI["SELL_SIGNAL"],
                    confidence=confidence,
                    signal_id=signal.id,
                    severity="critical",
                )
            )

        # Context alerts, independent of the direction decision.
        if signal.mtf is not None:
            regime = signal.mtf.current.regime
            if regime.regime is Regime.HIGH_VOLATILITY and change.kind == "state":
                alerts.append(
                    Alert(
                        kind="HIGH_VOLATILITY",
                        title="HIGH VOLATILITY",
                        body=(
                            "Volatility has spiked; analysis reliability is reduced "
                            "and standing aside is the safer choice."
                        ),
                        emoji=_EMOJI["HIGH_VOLATILITY"],
                        confidence=confidence,
                        signal_id=signal.id,
                        severity="warning",
                    )
                )
            elif regime.regime is Regime.POTENTIAL_REVERSAL and change.kind == "state":
                alerts.append(
                    Alert(
                        kind="TREND_REVERSAL",
                        title="TREND REVERSAL DETECTED",
                        body=(
                            f"Reversal cues are building on {signal.asset} "
                            f"({regime.reversal_risk:.0%} reversal risk)."
                        ),
                        emoji=_EMOJI["TREND_REVERSAL"],
                        confidence=confidence,
                        signal_id=signal.id,
                        severity="warning",
                    )
                )
            elif (
                signal.direction is Direction.WAIT
                and signal.score is not None
                and 0.35 <= signal.score.agreement < 0.55
                and change.kind in ("new", "direction", "state")
            ):
                alerts.append(
                    Alert(
                        kind="CONFLICTING_SIGNALS",
                        title="CONFLICTING SIGNALS",
                        body=signal.reason,
                        emoji=_EMOJI["CONFLICTING_SIGNALS"],
                        confidence=confidence,
                        signal_id=signal.id,
                        severity="info",
                    )
                )

        if not signal.quality.ok and change.kind in ("new", "direction", "state"):
            alerts.append(
                Alert(
                    kind="DATA_QUALITY",
                    title="CHART DATA UNRELIABLE",
                    body="; ".join(signal.quality.issues)
                    or "Chart data could not be validated.",
                    emoji=_EMOJI["DATA_QUALITY"],
                    confidence=signal.quality.confidence,
                    signal_id=signal.id,
                    severity="warning",
                )
            )

        return alerts

    def recent_alerts(self, limit: int = 25) -> list[dict[str, Any]]:
        return [a.to_dict() for a in list(self.recent)[-limit:][::-1]]
