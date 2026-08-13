"""Support and resistance detection.

Swing points are clustered into zones (a level is never a single price — it is
a band roughly the width of recent volatility). Each zone gets a strength score
built from how many times it was touched, how recently, and how much of a
reaction it produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..indicators.core import atr, last_finite
from ..models import Level, LevelKind, Series
from .structure import Swing, find_swings


@dataclass
class LevelsReading:
    levels: list[Level]
    nearest_support: Level | None
    nearest_resistance: Level | None
    price: float
    atr: float
    in_range: bool  # price is boxed between two significant levels

    def distance_in_atr(self, level: Level | None) -> float | None:
        if level is None or not np.isfinite(self.atr) or self.atr <= 0:
            return None
        return level.distance_from(self.price) / self.atr

    def to_dict(self) -> dict[str, Any]:
        return {
            "levels": [lv.to_dict() for lv in self.levels],
            "nearest_support": (
                self.nearest_support.to_dict() if self.nearest_support else None
            ),
            "nearest_resistance": (
                self.nearest_resistance.to_dict() if self.nearest_resistance else None
            ),
            "support_distance_atr": _round(self.distance_in_atr(self.nearest_support)),
            "resistance_distance_atr": _round(
                self.distance_in_atr(self.nearest_resistance)
            ),
            "in_range": self.in_range,
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _cluster(
    swings: list[Swing], tolerance: float
) -> list[tuple[float, list[Swing]]]:
    """Group swings whose prices sit within ``tolerance`` of each other."""
    if not swings:
        return []
    ordered = sorted(swings, key=lambda s: s.price)
    clusters: list[list[Swing]] = [[ordered[0]]]
    for swing in ordered[1:]:
        current = clusters[-1]
        centre = float(np.mean([s.price for s in current]))
        if abs(swing.price - centre) <= tolerance:
            current.append(swing)
        else:
            clusters.append([swing])
    return [(float(np.mean([s.price for s in c])), c) for c in clusters]


def detect_levels(
    series: Series,
    max_levels: int = 8,
    swing_left: int = 2,
    swing_right: int = 2,
) -> LevelsReading:
    """Find support and resistance zones around the current price."""
    n = len(series)
    price = float(series.close[-1]) if n else float("nan")
    atr_now = last_finite(atr(series.high, series.low, series.close, 14))
    if not np.isfinite(atr_now) or atr_now <= 0:
        # Fall back to an average bar range so the clustering still has a scale.
        spans = series.high - series.low
        atr_now = float(np.mean(spans)) if spans.size else 0.0

    if n < 10 or atr_now <= 0:
        return LevelsReading(
            levels=[],
            nearest_support=None,
            nearest_resistance=None,
            price=price,
            atr=atr_now,
            in_range=False,
        )

    swings = find_swings(series, swing_left, swing_right)
    tolerance = atr_now * 0.6

    # Historical extremes are levels in their own right even without a
    # confirmed fractal, so seed the pool with them.
    seeded: list[Swing] = list(swings)
    seeded.append(Swing(index=int(np.argmax(series.high)), price=float(series.high.max()), kind="high"))
    seeded.append(Swing(index=int(np.argmin(series.low)), price=float(series.low.min()), kind="low"))

    levels: list[Level] = []
    for centre, members in _cluster(seeded, tolerance):
        touches = len(members)
        last_index = max(m.index for m in members)
        recency = last_index / max(n - 1, 1)  # 0 = oldest, 1 = most recent

        # Reaction size: how far price travelled away from the level after each
        # touch. A level that produced real movement matters more than one price
        # drifted through.
        reactions: list[float] = []
        for member in members:
            end = min(member.index + 6, n)
            if end - member.index < 2:
                continue
            if member.kind == "high":
                excursion = centre - float(series.low[member.index : end].min())
            else:
                excursion = float(series.high[member.index : end].max()) - centre
            reactions.append(max(0.0, excursion) / atr_now)
        reaction_score = float(np.mean(reactions)) if reactions else 0.0

        strength = (
            min(touches, 5) / 5.0 * 45.0
            + recency * 25.0
            + min(reaction_score / 3.0, 1.0) * 30.0
        )

        kind = LevelKind.RESISTANCE if centre >= price else LevelKind.SUPPORT
        half_width = tolerance / 2.0
        levels.append(
            Level(
                price=round(float(centre), 8),
                kind=kind,
                strength=round(float(min(100.0, strength)), 1),
                touches=touches,
                last_touch_index=last_index,
                zone_low=round(float(centre - half_width), 8),
                zone_high=round(float(centre + half_width), 8),
            )
        )

    # Merge zones that ended up overlapping after clustering.
    levels = _merge_overlaps(levels)
    levels.sort(key=lambda lv: (-lv.strength, lv.distance_from(price)))
    levels = levels[:max_levels]

    supports = [lv for lv in levels if lv.kind is LevelKind.SUPPORT]
    resistances = [lv for lv in levels if lv.kind is LevelKind.RESISTANCE]
    nearest_support = min(supports, key=lambda lv: lv.distance_from(price), default=None)
    nearest_resistance = min(
        resistances, key=lambda lv: lv.distance_from(price), default=None
    )

    in_range = bool(
        nearest_support is not None
        and nearest_resistance is not None
        and nearest_support.strength >= 50
        and nearest_resistance.strength >= 50
        and (nearest_resistance.price - nearest_support.price) < atr_now * 6
    )

    return LevelsReading(
        levels=levels,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        price=price,
        atr=atr_now,
        in_range=in_range,
    )


def _merge_overlaps(levels: list[Level]) -> list[Level]:
    """Fold together zones whose bands overlap, keeping the stronger read."""
    if not levels:
        return []
    ordered = sorted(levels, key=lambda lv: lv.price)
    merged: list[Level] = [ordered[0]]
    for level in ordered[1:]:
        previous = merged[-1]
        if level.zone_low <= previous.zone_high and level.kind is previous.kind:
            merged[-1] = Level(
                price=(previous.price + level.price) / 2.0,
                kind=previous.kind,
                strength=max(previous.strength, level.strength),
                touches=previous.touches + level.touches,
                last_touch_index=max(previous.last_touch_index, level.last_touch_index),
                zone_low=min(previous.zone_low, level.zone_low),
                zone_high=max(previous.zone_high, level.zone_high),
            )
        else:
            merged.append(level)
    return merged
