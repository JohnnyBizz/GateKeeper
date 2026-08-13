"""Turning the numbers into a trader's explanation.

This module owns all user-facing wording for a signal. It is kept separate so
that the vocabulary rules live in one place: the assistant talks in terms of
probability and confirmation, and never claims certainty. The banned-phrase
check at the bottom is enforced by the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..analysis import MultiTimeframeAnalysis
from ..models import Bias, DataQuality, Direction, LevelImportance, format_price
from .duration import DurationAnalysis
from .gates import GateReport
from .scoring import ScoreResult

# Language the assistant must never use about a trade outcome.
BANNED_PHRASES: tuple[str, ...] = (
    "guaranteed",
    "guarantee",
    "100% accurate",
    "cannot lose",
    "can't lose",
    "sure thing",
    "risk free",
    "risk-free",
    "certain win",
    "always wins",
)


@dataclass
class Narrative:
    reason: str
    invalidation: str
    why: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_narrative(
    *,
    mtf: MultiTimeframeAnalysis,
    direction: Direction,
    candidate: Direction,
    score: ScoreResult,
    gates: GateReport,
    duration: DurationAnalysis,
    quality: DataQuality,
) -> Narrative:
    """Assemble the reason, the WHY? bullets and the invalidation condition."""
    current = mtf.current
    bullish = candidate is Direction.CALL
    side = "bullish" if bullish else "bearish"

    why: list[str] = [
        f"Higher timeframe ({mtf.higher.label}): "
        f"{mtf.higher.trend_bias.value.title()} at {mtf.higher.trend_strength:.0f}/100",
        f"Current timeframe ({current.label}): "
        f"{current.trend_bias.value.title()} at {current.trend_strength:.0f}/100",
        f"Entry timeframe ({mtf.entry.label}): momentum "
        f"{mtf.entry.momentum.bias.value.title()} at "
        f"{mtf.entry.momentum.strength * 100:.0f}/100",
        f"Market regime: {current.regime.regime.label}",
        f"Market structure: {current.structure.label}",
        f"Heikin Ashi: {current.heikin_ashi.pattern}",
        f"Candle pattern: {current.pattern.name}"
        + (
            f" ({current.pattern.bias.value.lower()})"
            if current.pattern.bias is not Bias.NEUTRAL
            else ""
        ),
        f"Momentum: {current.momentum.label.title()}"
        + (
            " and increasing"
            if current.momentum.accelerating
            else " and fading"
            if current.momentum.decelerating
            else ""
        ),
        f"Volatility: {current.volatility.regime} "
        f"(ATR percentile {current.volatility.atr_percentile:.0f})",
    ]

    support = current.levels.nearest_support
    resistance = current.levels.nearest_resistance
    if support is not None:
        distance = current.levels.distance_in_atr(support)
        why.append(
            f"Support: {format_price(support.price)} "
            f"({support.importance.value.lower()}, strength {support.strength:.0f}/100"
            + (f", {distance:.1f} ATR below" if distance is not None else "")
            + ")"
        )
    else:
        why.append("Support: no significant level identified below price")
    if resistance is not None:
        distance = current.levels.distance_in_atr(resistance)
        why.append(
            f"Resistance: {format_price(resistance.price)} "
            f"({resistance.importance.value.lower()}, strength {resistance.strength:.0f}/100"
            + (f", {distance:.1f} ATR above" if distance is not None else "")
            + ")"
        )
    else:
        why.append("Resistance: no significant level identified above price")

    why.append(
        f"Timeframe agreement: {mtf.agreement:.0%} "
        f"(consensus {mtf.consensus.value.title()})"
    )
    why.append(
        f"{duration.selected_label} duration: "
        f"{duration.selected_score:.0f}/100 — {duration.selected_fit.label}"
    )
    if not duration.matches_recommendation:
        why.append(
            f"Engine's preferred duration: {duration.recommended_label} "
            f"({duration.recommended_score:.0f}/100)"
        )

    # Component detail lines, strongest contributors first.
    for component in sorted(score.components, key=lambda c: -c.points):
        if component.detail:
            why.append(f"{component.name.replace('_', ' ').title()}: {component.detail}")

    # -- reason -------------------------------------------------------------
    quality_word = _quality_word(score.total)
    reason_parts = [
        f"This is a {quality_word} {side} setup, not a certainty.",
        f"Price is in a {current.structure.label} on the {current.label} chart "
        f"with {current.regime.regime.label.lower()} conditions.",
    ]
    if current.heikin_ashi.streak >= 2:
        reason_parts.append(
            f"Heikin Ashi shows {current.heikin_ashi.streak} consecutive "
            f"{current.heikin_ashi.bias.value.lower()} candles with "
            f"{current.heikin_ashi.body_trend} bodies."
        )
    if current.momentum.bias is not Bias.NEUTRAL:
        reason_parts.append(
            f"Momentum is {current.momentum.label} and "
            + (
                "increasing"
                if current.momentum.accelerating
                else "fading" if current.momentum.decelerating else "steady"
            )
            + "."
        )
    if mtf.consensus is not Bias.NEUTRAL:
        reason_parts.append(
            f"The higher timeframe agrees with the direction "
            f"({mtf.agreement:.0%} of the timeframe stack)."
            if mtf.consensus.value.lower() == side
            else f"The higher timeframe does not agree ({mtf.agreement:.0%} stack agreement)."
        )
    reason_parts.append(duration.reason)
    reason = " ".join(reason_parts)

    # -- invalidation -------------------------------------------------------
    invalidation = _invalidation(mtf, candidate, bullish)

    # -- warnings -----------------------------------------------------------
    warnings: list[str] = []
    if quality.confidence < 85:
        warnings.append(
            f"Chart recognition confidence is {quality.confidence:.0f}% — "
            "verify the readings against the chart itself."
        )
    if current.regime.reversal_risk >= 0.4:
        warnings.append(
            f"Reversal risk is {current.regime.reversal_risk:.0%}; the move may be late."
        )
    if current.heikin_ashi.momentum_weakening:
        warnings.append("Heikin Ashi bodies are contracting — the push is losing force.")
    # A named pattern pointing the other way is worth saying out loud, since it
    # is the thing a trader would notice first when looking at the same chart.
    wanted_bias = Bias.BULLISH if bullish else Bias.BEARISH
    if current.pattern.bias is not Bias.NEUTRAL and current.pattern.bias is not wanted_bias:
        if current.pattern.strength >= 0.5:
            warnings.append(
                f"The last candles form a {current.pattern.name}, which points "
                f"{current.pattern.bias.value.lower()} — against this direction."
            )
    elif current.pattern.name == "Doji" and current.pattern.strength >= 0.5:
        warnings.append(
            "The most recent candle is a Doji — neither side finished in control."
        )
    if current.volatility.regime in ("elevated", "extreme"):
        warnings.append(f"Volatility is {current.volatility.regime}; risk is elevated.")
    obstacle = resistance if bullish else support
    if obstacle is not None and obstacle.importance is LevelImportance.MAJOR:
        distance = current.levels.distance_in_atr(obstacle)
        if distance is not None and distance < 1.5:
            warnings.append(
                f"A major level sits {distance:.1f} ATR away — trades into major "
                "levels need extra confirmation."
            )

    return Narrative(
        reason=_sanitise(reason),
        invalidation=_sanitise(invalidation),
        why=[_sanitise(w) for w in why],
        warnings=[_sanitise(w) for w in warnings],
    )


def _quality_word(score: float) -> str:
    if score >= 90:
        return "high-probability"
    if score >= 80:
        return "high-probability"
    if score >= 70:
        return "moderate-confidence"
    if score >= 60:
        return "low-confidence"
    return "weak"


def _invalidation(
    mtf: MultiTimeframeAnalysis, candidate: Direction, bullish: bool
) -> str:
    """The concrete conditions that would kill this setup."""
    current = mtf.current
    clauses: list[str] = []

    protective = current.levels.nearest_support if bullish else current.levels.nearest_resistance
    if protective is not None:
        word = "below" if bullish else "above"
        clauses.append(
            f"price closes {word} {format_price(protective.price)}"
        )

    last_swing = None
    swings = [s for s in current.structure.swings if s.kind == ("low" if bullish else "high")]
    if swings:
        last_swing = swings[-1]
        word = "below" if bullish else "above"
        clauses.append(
            f"the last swing {'low' if bullish else 'high'} at "
            f"{format_price(last_swing.price)} is broken {word}"
        )

    opposite = "bearish" if bullish else "bullish"
    clauses.append(f"Heikin Ashi flips {opposite} with an expanding body")
    clauses.append(f"{opposite} momentum develops on the entry timeframe")

    return (
        "This setup becomes invalid if "
        + ", or if ".join(clauses)
        + ". Recalculate before entering if the market moves in the meantime."
    )


def _sanitise(text: str) -> str:
    """Strip any wording that would over-promise an outcome.

    This is a backstop, not the primary control — the phrasing above is written
    to avoid these words in the first place — but it guarantees the invariant
    holds even if a future edit is careless.
    """
    lowered = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            # Replace defensively rather than silently shipping the claim.
            start = lowered.index(phrase)
            text = text[:start] + "high-probability" + text[start + len(phrase) :]
            lowered = text.lower()
    return text


def contains_banned_language(text: str) -> bool:
    """Used by the test suite to assert the vocabulary rules hold."""
    lowered = text.lower()
    return any(phrase in lowered for phrase in BANNED_PHRASES)
