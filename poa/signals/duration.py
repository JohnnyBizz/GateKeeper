"""Trade-duration (expiration) analysis.

The chart timeframe and the trade duration are separate variables and this
module is where that separation lives. Given the current market *speed*, it
answers:

* how far is price likely to travel within a candidate expiration?
* is that far enough to clear the noise floor and stay clear of it?
* does the setup survive that long, or does it burn out first?

There is no fixed rule such as "1-minute chart means 3-minute trade". The
recommendation is computed from ATR, momentum persistence, directional
efficiency, trend strength and distance to the nearest opposing level.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from ..analysis import MultiTimeframeAnalysis
from ..models import (
    Bias,
    Direction,
    DurationCandidate,
    DurationFit,
    format_duration,
)
from .scoring import direction_to_bias


@dataclass
class DurationAnalysis:
    """Fit of the user's chosen expiration, plus the engine's own preference."""

    selected_seconds: int
    selected_score: float
    recommended_seconds: int
    recommended_score: float
    candidates: list[DurationCandidate]
    horizon_seconds: float  # how long the setup is expected to remain valid
    expected_move_atr: float  # expected travel over the selected duration, in ATR
    reason: str
    notes: list[str]

    @property
    def selected_fit(self) -> DurationFit:
        return DurationFit.from_score(self.selected_score)

    @property
    def matches_recommendation(self) -> bool:
        return self.selected_seconds == self.recommended_seconds

    @property
    def selected_label(self) -> str:
        return format_duration(self.selected_seconds)

    @property
    def recommended_label(self) -> str:
        return format_duration(self.recommended_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_seconds": self.selected_seconds,
            "selected_label": self.selected_label,
            "selected_score": round(self.selected_score, 1),
            "selected_fit": self.selected_fit.value,
            "selected_fit_label": self.selected_fit.label,
            "selected_fit_emoji": self.selected_fit.emoji,
            "recommended_seconds": self.recommended_seconds,
            "recommended_label": self.recommended_label,
            "recommended_score": round(self.recommended_score, 1),
            "matches_recommendation": self.matches_recommendation,
            "horizon_seconds": round(self.horizon_seconds, 1),
            "expected_move_atr": round(self.expected_move_atr, 2),
            "candidates": [c.to_dict() for c in self.candidates],
            "reason": self.reason,
            "notes": list(self.notes),
        }


def _persistence(mtf: MultiTimeframeAnalysis, wanted: Bias) -> float:
    """How many candles the current push is likely to keep going, 0..1 scaled.

    Built from trend strength, directional efficiency, Heikin Ashi streak
    quality and how much of the higher timeframe agrees.
    """
    current = mtf.current
    score = 0.0
    score += 0.30 * current.indicators.trend_strength
    score += 0.25 * min(current.volatility.directional_efficiency * 2.0, 1.0)
    score += 0.20 * (current.heikin_ashi.strength if current.heikin_ashi.bias is wanted else 0.0)
    score += 0.15 * (current.structure.strength if current.structure.bias is wanted else 0.0)
    score += 0.10 * (1.0 if mtf.higher.trend_bias is wanted else 0.0)

    # A move that is already fading will not persist regardless of how strong
    # it looks in aggregate.
    if current.momentum.decelerating:
        score *= 0.7
    if current.heikin_ashi.momentum_weakening:
        score *= 0.8
    if current.volatility.choppy:
        score *= 0.6
    return float(max(0.0, min(1.0, score)))


def _expected_move(
    mtf: MultiTimeframeAnalysis, candles_ahead: float, persistence: float
) -> float:
    """Expected directional travel over ``candles_ahead`` candles, in ATR.

    Random walk scaling (sqrt of time) is the floor; persistence pushes the
    exponent toward linear as the trend gets cleaner. This is the key reason a
    strong trend justifies a longer expiration and chop does not.
    """
    if candles_ahead <= 0:
        return 0.0
    exponent = 0.5 + 0.5 * persistence  # 0.5 = random walk, 1.0 = pure trend
    efficiency = max(mtf.current.volatility.directional_efficiency, 0.05)
    return float(math.pow(candles_ahead, exponent) * efficiency)


def _noise_floor(mtf: MultiTimeframeAnalysis) -> float:
    """Minimum move, in ATR, that counts as more than noise.

    A binary option settles on where price is at expiry relative to entry, so a
    move smaller than a candle's own wiggle is a coin flip.
    """
    current = mtf.current
    if not np.isfinite(current.indicators.atr) or current.indicators.atr <= 0:
        return 1.0
    body_share = current.heikin_ashi.avg_body_ratio
    # Wicky, low-body markets need a bigger move to be meaningful.
    return float(0.6 + 0.5 * (1.0 - min(body_share, 1.0)))


def analyze_duration(
    mtf: MultiTimeframeAnalysis,
    direction: Direction,
    selected_seconds: int,
    available_durations: Sequence[int],
    chart_timeframe_seconds: int | None = None,
) -> DurationAnalysis:
    """Score every candidate expiration and pick the best fit."""
    current = mtf.current
    timeframe = int(chart_timeframe_seconds or current.timeframe_seconds or 60)
    wanted = direction_to_bias(direction)
    notes: list[str] = []

    durations = sorted({int(d) for d in available_durations if int(d) > 0})
    if selected_seconds not in durations:
        durations = sorted(set(durations) | {int(selected_seconds)})

    persistence = _persistence(mtf, wanted) if wanted is not Bias.NEUTRAL else 0.25
    noise_floor = _noise_floor(mtf)

    # How long the setup itself is expected to stay valid, in seconds. A clean
    # trend survives many candles; a fading push survives one or two.
    base_candles = 2.0 + 10.0 * persistence
    if current.momentum.accelerating:
        base_candles *= 1.2
    if current.regime.reversal_risk >= 0.5:
        base_candles *= 0.6
    horizon_seconds = base_candles * timeframe

    # Distance to the level that would stop the move, in candles' worth of ATR.
    obstacle = (
        current.levels.nearest_resistance
        if wanted is Bias.BULLISH
        else current.levels.nearest_support
    )
    obstacle_atr = current.levels.distance_in_atr(obstacle)

    candidates: list[DurationCandidate] = []
    for seconds in durations:
        candles_ahead = seconds / timeframe
        expected = _expected_move(mtf, candles_ahead, persistence)
        score, reason = _score_duration(
            seconds=seconds,
            candles_ahead=candles_ahead,
            expected_move_atr=expected,
            noise_floor=noise_floor,
            horizon_seconds=horizon_seconds,
            persistence=persistence,
            obstacle_atr=obstacle_atr,
            mtf=mtf,
            timeframe=timeframe,
        )
        candidates.append(
            DurationCandidate(seconds=seconds, score=round(score, 1), reason=reason)
        )

    best = max(candidates, key=lambda c: c.score)
    selected = next(c for c in candidates if c.seconds == int(selected_seconds))

    selected_candles = selected.seconds / timeframe
    expected_selected = _expected_move(mtf, selected_candles, persistence)

    if persistence >= 0.6:
        notes.append(
            "The move is persistent, which supports holding through several candles."
        )
    elif persistence <= 0.3:
        notes.append(
            "The move is not persistent; only short expirations stay inside the "
            "window where the current push is still valid."
        )
    if obstacle_atr is not None and obstacle_atr < 1.0:
        notes.append(
            "An opposing level sits close by, which caps how far the move can travel."
        )
    if current.volatility.regime == "low":
        notes.append("Compressed volatility means price may not travel far in any window.")

    reason = _explain(selected, best, expected_selected, noise_floor, horizon_seconds, timeframe)

    return DurationAnalysis(
        selected_seconds=int(selected_seconds),
        selected_score=selected.score,
        recommended_seconds=best.seconds,
        recommended_score=best.score,
        candidates=candidates,
        horizon_seconds=horizon_seconds,
        expected_move_atr=expected_selected,
        reason=reason,
        notes=notes,
    )


def _score_duration(
    *,
    seconds: int,
    candles_ahead: float,
    expected_move_atr: float,
    noise_floor: float,
    horizon_seconds: float,
    persistence: float,
    obstacle_atr: float | None,
    mtf: MultiTimeframeAnalysis,
    timeframe: int,
) -> tuple[float, str]:
    """Score one candidate expiration out of 100, with a one-line rationale."""
    reasons: list[str] = []

    # -- 1. Signal-to-noise: will the move clear the noise floor? (0-40) ----
    ratio = expected_move_atr / noise_floor if noise_floor > 0 else 0.0
    if ratio >= 1.0:
        snr_score = 40.0 * min(1.0, 0.6 + 0.4 * min((ratio - 1.0) / 1.5, 1.0))
        reasons.append("expected move clears the noise floor")
    else:
        snr_score = 40.0 * (ratio ** 1.5) * 0.6
        reasons.append("expected move is close to the noise floor")

    # -- 2. Does the setup survive the expiration? (0-30) -------------------
    if horizon_seconds <= 0:
        horizon_score = 0.0
    else:
        overshoot = seconds / horizon_seconds
        if overshoot <= 0.6:
            horizon_score = 30.0
            reasons.append("well within the setup's expected lifetime")
        elif overshoot <= 1.0:
            horizon_score = 30.0 - 8.0 * ((overshoot - 0.6) / 0.4)
            reasons.append("fits inside the setup's expected lifetime")
        else:
            decay = min((overshoot - 1.0), 2.0) / 2.0
            horizon_score = 22.0 * (1.0 - decay)
            reasons.append("extends past the point where the setup is expected to hold")

    # -- 3. Resolution: enough candles to be readable? (0-15) ---------------
    # Sub-candle expirations resolve on intra-candle noise the chart cannot
    # show us; very long ones drift away from what was analysed.
    if candles_ahead < 0.5:
        resolution_score = 3.0
        reasons.append("shorter than the chart can resolve")
    elif candles_ahead < 1.0:
        resolution_score = 8.0
        reasons.append("resolves inside a single candle")
    elif candles_ahead <= 8.0:
        resolution_score = 15.0
    elif candles_ahead <= 20.0:
        resolution_score = 11.0
    else:
        resolution_score = 5.0
        reasons.append("far beyond the analysed window")

    # -- 4. Room before the opposing level (0-15) ---------------------------
    if obstacle_atr is None:
        obstacle_score = 13.0
    elif expected_move_atr <= obstacle_atr:
        obstacle_score = 15.0
    else:
        overrun = (expected_move_atr - obstacle_atr) / max(expected_move_atr, 1e-9)
        obstacle_score = 15.0 * (1.0 - min(overrun, 1.0))
        reasons.append("the move would run into an opposing level before expiry")

    total = snr_score + horizon_score + resolution_score + obstacle_score

    # A market with no persistence should not score any duration highly.
    if persistence < 0.25:
        total *= 0.75

    return float(max(0.0, min(100.0, total))), "; ".join(reasons)


def _explain(
    selected: DurationCandidate,
    best: DurationCandidate,
    expected_move_atr: float,
    noise_floor: float,
    horizon_seconds: float,
    timeframe: int,
) -> str:
    """Plain-language explanation of the selected duration's fit."""
    horizon_candles = horizon_seconds / timeframe if timeframe else 0.0
    base = (
        f"Over {selected.label.lower()} the current market speed projects roughly "
        f"{expected_move_atr:.1f} ATR of travel against a {noise_floor:.1f} ATR noise "
        f"floor, while the setup is expected to stay valid for about "
        f"{horizon_candles:.0f} candles."
    )
    if selected.seconds == best.seconds:
        return base + " The selected duration is the best available match."
    direction_word = "longer" if best.seconds > selected.seconds else "shorter"
    return (
        base
        + f" A {direction_word} {best.label.lower()} expiration scores higher "
        f"({best.score:.0f} versus {selected.score:.0f}) because it lines up better "
        "with how far and how long this move is projected to run."
    )
