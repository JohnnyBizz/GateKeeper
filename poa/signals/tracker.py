"""Signal lifecycle tracking.

Once a direction is emitted, the dashboard must not keep showing it as though
nothing has changed. The tracker compares each new evaluation against the
signal that is currently live and decides whether it is still ACTIVE, has
started WEAKENING, has been INVALIDATED, or has EXPIRED.

It also decides whether an update is *material* — the engine re-evaluates every
poll, but the user should only be interrupted when something actually changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..models import Direction, SignalState, utcnow
from .engine import Signal


@dataclass
class TrackerSettings:
    weakening_drop: float = 15.0
    invalidation_drop: float = 25.0
    cooldown_seconds: float = 60.0

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> "TrackerSettings":
        defaults = cls()
        return cls(
            weakening_drop=float(section.get("weakening_drop", defaults.weakening_drop)),
            invalidation_drop=float(
                section.get("invalidation_drop", defaults.invalidation_drop)
            ),
            cooldown_seconds=float(
                section.get("cooldown_seconds", defaults.cooldown_seconds)
            ),
        )


@dataclass
class TrackedChange:
    """What changed between the previous evaluation and this one."""

    material: bool
    kind: str  # "new" | "state" | "direction" | "confidence" | "duration" | "none"
    description: str
    previous_state: SignalState | None = None
    new_state: SignalState | None = None


@dataclass
class SignalTracker:
    """Holds the currently live signal and applies the lifecycle rules."""

    settings: TrackerSettings = field(default_factory=TrackerSettings)
    current: Signal | None = None
    last_emitted_at: datetime | None = None
    history: list[Signal] = field(default_factory=list)
    max_history: int = 200

    def update(self, signal: Signal, now: datetime | None = None) -> TrackedChange:
        """Fold a fresh evaluation into the tracked state."""
        now = now or utcnow()
        previous = self.current

        if previous is None:
            self.current = signal
            self._record(signal)
            return TrackedChange(
                material=True,
                kind="new",
                description=f"First analysis: {signal.direction.value}",
                new_state=signal.state,
            )

        # --- expiry --------------------------------------------------------
        if (
            previous.actionable
            and previous.expires_at is not None
            and now >= previous.expires_at
        ):
            previous.state = SignalState.EXPIRED
            signal.state = SignalState.ACTIVE
            self.current = signal
            self._record(signal)
            return TrackedChange(
                material=True,
                kind="state",
                description=(
                    f"The {previous.trade_duration_label.lower()} window on the previous "
                    f"{previous.direction.value} signal has elapsed."
                ),
                previous_state=SignalState.ACTIVE,
                new_state=SignalState.EXPIRED,
            )

        # --- direction flip -------------------------------------------------
        if signal.direction is not previous.direction:
            if previous.actionable and signal.direction in (
                Direction.WAIT,
                Direction.NO_TRADE,
            ):
                signal.state = SignalState.INVALIDATED
                description = (
                    f"{previous.direction.value} setup invalidated: {signal.reason}"
                )
                kind = "state"
            else:
                signal.state = SignalState.ACTIVE
                description = (
                    f"Signal changed from {previous.direction.value} to "
                    f"{signal.direction.value}."
                )
                kind = "direction"
            signal.peak_confidence = signal.overall_confidence
            self.current = signal
            self._record(signal)
            return TrackedChange(
                material=True,
                kind=kind,
                description=description,
                previous_state=previous.state,
                new_state=signal.state,
            )

        # --- same direction: track confidence decay -------------------------
        signal.peak_confidence = max(previous.peak_confidence, signal.overall_confidence)
        signal.expires_at = previous.expires_at or signal.expires_at
        drop = signal.peak_confidence - signal.overall_confidence

        if previous.actionable:
            if drop >= self.settings.invalidation_drop:
                signal.state = SignalState.INVALIDATED
                self.current = signal
                self._record(signal)
                return TrackedChange(
                    material=True,
                    kind="state",
                    description=(
                        f"Confidence fell from {signal.peak_confidence:.0f} to "
                        f"{signal.overall_confidence:.0f}. The setup no longer holds."
                    ),
                    previous_state=previous.state,
                    new_state=SignalState.INVALIDATED,
                )
            if drop >= self.settings.weakening_drop:
                signal.state = SignalState.WEAKENING
                changed = previous.state is not SignalState.WEAKENING
                self.current = signal
                if changed:
                    self._record(signal)
                return TrackedChange(
                    material=changed,
                    kind="state" if changed else "confidence",
                    description=(
                        f"Setup weakening: confidence has fallen from "
                        f"{signal.peak_confidence:.0f} to {signal.overall_confidence:.0f}."
                    ),
                    previous_state=previous.state,
                    new_state=SignalState.WEAKENING,
                )
            signal.state = SignalState.ACTIVE
        else:
            signal.state = SignalState.ACTIVE

        # --- material-change detection for non-actionable updates -----------
        confidence_delta = abs(signal.overall_confidence - previous.overall_confidence)
        duration_changed = (
            signal.duration is not None
            and previous.duration is not None
            and signal.duration.recommended_seconds != previous.duration.recommended_seconds
        )
        regime_changed = _regime_of(signal) != _regime_of(previous)

        self.current = signal

        if regime_changed:
            self._record(signal)
            return TrackedChange(
                material=True,
                kind="state",
                description=f"Market regime changed to {_regime_of(signal)}.",
                previous_state=previous.state,
                new_state=signal.state,
            )
        if duration_changed:
            return TrackedChange(
                material=True,
                kind="duration",
                description=(
                    f"Preferred duration changed to "
                    f"{signal.duration.recommended_label}."
                ),
                previous_state=previous.state,
                new_state=signal.state,
            )
        if confidence_delta >= 10:
            return TrackedChange(
                material=True,
                kind="confidence",
                description=(
                    f"Confidence moved from {previous.overall_confidence:.0f} to "
                    f"{signal.overall_confidence:.0f}."
                ),
                previous_state=previous.state,
                new_state=signal.state,
            )

        return TrackedChange(
            material=False,
            kind="none",
            description="No material change.",
            previous_state=previous.state,
            new_state=signal.state,
        )

    def within_cooldown(self, now: datetime | None = None) -> bool:
        """Has a signal been emitted too recently to emit another?"""
        if self.last_emitted_at is None:
            return False
        now = now or utcnow()
        return now - self.last_emitted_at < timedelta(
            seconds=self.settings.cooldown_seconds
        )

    def mark_emitted(self, now: datetime | None = None) -> None:
        self.last_emitted_at = now or utcnow()

    def _record(self, signal: Signal) -> None:
        self.history.append(signal)
        if len(self.history) > self.max_history:
            del self.history[: len(self.history) - self.max_history]

    def reset(self) -> None:
        self.current = None
        self.last_emitted_at = None
        self.history.clear()


def _regime_of(signal: Signal) -> str:
    if signal.mtf is None:
        return "unknown"
    return signal.mtf.current.regime.regime.value
