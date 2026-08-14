"""Letting the record set the gates.

``min_confidence`` and ``min_duration_compatibility`` decide how selective the
assistant is, and both shipped as numbers somebody chose. That is the wrong way
round: the right gate is not a matter of taste, it is whatever the measured
record says was worth the most, and the record can work that out.

So it does. After each replay, the threshold tables answer "a gate here would
have taken N trades at X%", the best of those is picked by expected value, and
the live gates move toward it.

Four guards, because a gate that chases noise is worse than one that is merely
wrong:

* **Evidence.** Nothing moves without a recommendation the sample can carry —
  the same twenty-trade floor everything else here respects.
* **Distance.** Gates move a few points at a time. A gate that jumps to
  wherever the last replay pointed would swing on every fresh candle, and the
  user would watch the tool change its mind rather than settle.
* **Bounds.** Never so loose that the score stops meaning anything, never so
  tight that nothing can pass. Auto-tuning that can reach zero is a tool that
  eventually takes every trade.
* **Reasons.** Every move says what moved it and what the evidence was. A
  setting that changes silently is indistinguishable from a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

# How far a gate may travel in one adjustment. Small enough that a single
# unlucky replay cannot swing the tool's whole character.
MAX_STEP = 5.0

# Bounds. Below the floor the score is not discriminating between anything;
# above the ceiling almost nothing is ever signalled and the tool goes quiet
# in a way no measurement asked for.
FLOOR_CONFIDENCE = 55.0
CEILING_CONFIDENCE = 90.0
FLOOR_DURATION = 35.0
CEILING_DURATION = 85.0


@dataclass
class Adjustment:
    """One gate moved, and the evidence that moved it."""

    key: str
    was: float
    now: float
    reason: str

    @property
    def looser(self) -> bool:
        return self.now < self.was

    def describe(self) -> str:
        name = "Signal gate" if self.key.endswith("confidence") else "Expiry gate"
        return f"{name} {self.was:.0f} → {self.now:.0f} · {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "was": self.was,
            "now": self.now,
            "reason": self.reason,
            "looser": self.looser,
        }


def _step_toward(current: float, target: float, floor: float, ceiling: float) -> float:
    """Move ``current`` at most ``MAX_STEP`` toward ``target``, within bounds."""
    if target > current:
        moved = min(current + MAX_STEP, target)
    else:
        moved = max(current - MAX_STEP, target)
    return round(max(floor, min(ceiling, moved)), 1)


def tune(
    calibration: Any,
    min_confidence: float,
    min_duration_compatibility: float,
) -> list[Adjustment]:
    """Propose gate changes the record supports. Empty when it supports none."""
    if calibration is None:
        return []

    adjustments: list[Adjustment] = []
    provenance = getattr(calibration, "provenance", "the record")

    for key, current, recommend, floor, ceiling in (
        (
            "min_confidence",
            float(min_confidence),
            getattr(calibration, "recommended_threshold", lambda: None),
            FLOOR_CONFIDENCE,
            CEILING_CONFIDENCE,
        ),
        (
            "min_duration_compatibility",
            float(min_duration_compatibility),
            getattr(calibration, "recommended_duration_threshold", lambda: None),
            FLOOR_DURATION,
            CEILING_DURATION,
        ),
    ):
        best = recommend()
        if best is None:
            continue
        threshold, bucket = best
        target = float(threshold)
        moved = _step_toward(current, target, floor, ceiling)
        if abs(moved - current) < 0.5:
            continue  # already there, or the bounds refuse the move

        adjustments.append(
            Adjustment(
                key=key,
                was=round(current, 1),
                now=moved,
                reason=(
                    f"{threshold}+ measured {bucket.win_rate:.0f}% over "
                    f"{bucket.settled} in {provenance}"
                ),
            )
        )

    for adjustment in adjustments:
        log.info("auto-tuned %s", adjustment.describe())
    return adjustments
