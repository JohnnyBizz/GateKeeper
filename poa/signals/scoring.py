"""The weighted scoring engine.

Each component returns a 0..1 score *in favour of the direction being
evaluated*, multiplied by a fixed weight. The weights sum to 100, so the total
is directly the 0-100 setup score.

The score alone never authorises a trade — see ``gates.py``. Its job is to rank
setups that have already passed the structural requirements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..analysis import MultiTimeframeAnalysis, TimeframeAnalysis
from ..models import Bias, Direction, LevelImportance, ScoreComponent

# Component weights. These sum to 100.
WEIGHTS: dict[str, float] = {
    "trend": 20.0,
    "structure": 15.0,
    "heikin_ashi": 15.0,
    "momentum": 10.0,
    "ema_alignment": 10.0,
    "support_resistance": 10.0,
    "rsi": 5.0,
    "macd": 5.0,
    "volatility": 5.0,
    "multi_timeframe": 5.0,
}


def direction_to_bias(direction: Direction) -> Bias:
    if direction is Direction.CALL:
        return Bias.BULLISH
    if direction is Direction.PUT:
        return Bias.BEARISH
    return Bias.NEUTRAL


@dataclass
class ScoreResult:
    direction: Direction
    components: list[ScoreComponent]

    @property
    def total(self) -> float:
        return round(sum(c.points for c in self.components), 1)

    @property
    def agreement(self) -> float:
        """Share of the total weight that actively favours the direction.

        Distinct from the score: a setup can reach 70 by having a few very
        strong components while most are indifferent. Agreement catches that.
        """
        total_weight = sum(c.weight for c in self.components)
        if total_weight <= 0:
            return 0.0
        favouring = sum(c.weight for c in self.components if c.score >= 0.5)
        return round(favouring / total_weight, 3)

    def component(self, name: str) -> ScoreComponent | None:
        for c in self.components:
            if c.name == name:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction.value,
            "total": self.total,
            "agreement": self.agreement,
            "components": [c.to_dict() for c in self.components],
        }


def _bias_score(actual: Bias, wanted: Bias, strength: float) -> float:
    """Map a module's (bias, strength) onto a 0..1 score for ``wanted``.

    Neutral sits at 0.5-ish scaled down, and an opposing read scores below 0.5
    so it actively drags the total down rather than merely not helping.
    """
    strength = float(max(0.0, min(1.0, strength)))
    if actual is wanted:
        return 0.5 + 0.5 * strength
    if actual is Bias.NEUTRAL:
        return 0.4 * (1.0 - strength * 0.5)
    return max(0.0, 0.25 * (1.0 - strength))


def score_direction(
    mtf: MultiTimeframeAnalysis,
    direction: Direction,
    resistance_proximity_atr: float = 0.75,
) -> ScoreResult:
    """Score how well the evidence supports ``direction``."""
    wanted = direction_to_bias(direction)
    if wanted is Bias.NEUTRAL:
        return ScoreResult(direction=direction, components=[])

    current = mtf.current
    higher = mtf.higher
    entry = mtf.entry
    bullish = wanted is Bias.BULLISH

    components: list[ScoreComponent] = [
        _score_trend(current, higher, wanted),
        _score_structure(current, wanted),
        _score_heikin_ashi(current, entry, wanted),
        _score_momentum(current, entry, wanted),
        _score_ema(current, wanted),
        _score_levels(current, wanted, bullish, resistance_proximity_atr),
        _score_rsi(current, wanted),
        _score_macd(current, wanted),
        _score_volatility(current),
        _score_mtf(mtf, wanted),
    ]
    return ScoreResult(direction=direction, components=components)


# --------------------------------------------------------------------------
# Individual components
# --------------------------------------------------------------------------


def _score_trend(
    current: TimeframeAnalysis, higher: TimeframeAnalysis, wanted: Bias
) -> ScoreComponent:
    current_score = _bias_score(
        current.trend_bias, wanted, current.trend_strength / 100.0
    )
    higher_score = _bias_score(higher.trend_bias, wanted, higher.trend_strength / 100.0)
    score = current_score * 0.6 + higher_score * 0.4
    detail = (
        f"{current.label} trend {current.trend_bias.value.lower()} "
        f"({current.trend_strength:.0f}/100); "
        f"{higher.label} trend {higher.trend_bias.value.lower()} "
        f"({higher.trend_strength:.0f}/100)"
    )
    return ScoreComponent("trend", WEIGHTS["trend"], score, detail)


def _score_structure(current: TimeframeAnalysis, wanted: Bias) -> ScoreComponent:
    structure = current.structure
    score = _bias_score(structure.bias, wanted, structure.strength)
    wanted_break = "bullish" if wanted is Bias.BULLISH else "bearish"
    if structure.break_of_structure == wanted_break:
        score = min(1.0, score + 0.1)
    elif structure.break_of_structure != "none":
        score = max(0.0, score - 0.15)
    if structure.consolidating:
        score *= 0.75
    return ScoreComponent(
        "structure", WEIGHTS["structure"], score, structure.label
    )


def _score_heikin_ashi(
    current: TimeframeAnalysis, entry: TimeframeAnalysis, wanted: Bias
) -> ScoreComponent:
    ha = current.heikin_ashi
    score = _bias_score(ha.bias, wanted, ha.strength)
    if ha.momentum_weakening and ha.bias is wanted:
        score *= 0.7
    if entry is not current:
        entry_score = _bias_score(entry.heikin_ashi.bias, wanted, entry.heikin_ashi.strength)
        score = score * 0.7 + entry_score * 0.3
    detail = f"{ha.pattern}, streak {ha.streak}, bodies {ha.body_trend}"
    return ScoreComponent("heikin_ashi", WEIGHTS["heikin_ashi"], score, detail)


def _score_momentum(
    current: TimeframeAnalysis, entry: TimeframeAnalysis, wanted: Bias
) -> ScoreComponent:
    momentum = current.momentum
    score = _bias_score(momentum.bias, wanted, momentum.strength)
    if momentum.bias is wanted:
        if momentum.accelerating:
            score = min(1.0, score + 0.1)
        elif momentum.decelerating:
            score *= 0.8
    if entry is not current and entry.momentum.bias is not wanted:
        score *= 0.85
    detail = f"{momentum.label} {momentum.bias.value.lower()}"
    if momentum.accelerating:
        detail += ", increasing"
    elif momentum.decelerating:
        detail += ", fading"
    return ScoreComponent("momentum", WEIGHTS["momentum"], score, detail)


def _score_ema(current: TimeframeAnalysis, wanted: Bias) -> ScoreComponent:
    indicators = current.indicators
    score = _bias_score(
        indicators.ema_alignment, wanted, indicators.ema_alignment_strength
    )
    price = indicators.price
    if np.isfinite(indicators.ema21):
        above = price > indicators.ema21
        if (wanted is Bias.BULLISH) == above:
            score = min(1.0, score + 0.05)
        else:
            score = max(0.0, score - 0.1)
    detail = f"EMA stack {indicators.ema_alignment.value.lower()}"
    return ScoreComponent("ema_alignment", WEIGHTS["ema_alignment"], score, detail)


def _score_levels(
    current: TimeframeAnalysis,
    wanted: Bias,
    bullish: bool,
    proximity_atr: float,
) -> ScoreComponent:
    """Reward room to run; penalise trading straight into a strong level."""
    levels = current.levels
    obstacle = levels.nearest_resistance if bullish else levels.nearest_support
    behind = levels.nearest_support if bullish else levels.nearest_resistance

    obstacle_distance = levels.distance_in_atr(obstacle)
    behind_distance = levels.distance_in_atr(behind)

    if obstacle is None or obstacle_distance is None:
        score = 0.75
        detail = "no significant level immediately overhead" if bullish else "no significant level immediately below"
    else:
        # Room to run: 0 at the level, saturating around 3 ATR away.
        room = min(obstacle_distance / 3.0, 1.0)
        score = 0.25 + 0.6 * room
        if obstacle.importance is LevelImportance.MAJOR and obstacle_distance < proximity_atr:
            score *= 0.5
        elif obstacle.importance is LevelImportance.IMPORTANT and obstacle_distance < proximity_atr / 2:
            score *= 0.7
        side = "resistance" if bullish else "support"
        detail = (
            f"{obstacle.importance.value.lower()} {side} {obstacle.price:.5f} "
            f"at {obstacle_distance:.1f} ATR (strength {obstacle.strength:.0f})"
        )

    # A strong level at our back is protection and improves the setup.
    if behind is not None and behind_distance is not None and behind_distance < 1.5:
        if behind.strength >= 60:
            score = min(1.0, score + 0.15)
            support_word = "support" if bullish else "resistance"
            detail += f"; {support_word} {behind.price:.5f} holding behind price"

    return ScoreComponent(
        "support_resistance", WEIGHTS["support_resistance"], score, detail
    )


def _score_rsi(current: TimeframeAnalysis, wanted: Bias) -> ScoreComponent:
    rsi_value = current.indicators.rsi
    if not np.isfinite(rsi_value):
        return ScoreComponent("rsi", WEIGHTS["rsi"], 0.4, "RSI unavailable")

    if wanted is Bias.BULLISH:
        # Supportive above 50; but stretched readings are a warning, not a
        # green light, so the score falls away above 75.
        if rsi_value >= 75:
            score = 0.35
            detail = f"RSI {rsi_value:.0f} — overbought, chase risk"
        elif rsi_value >= 55:
            score = 0.9
            detail = f"RSI {rsi_value:.0f} — supportive"
        elif rsi_value >= 48:
            score = 0.6
            detail = f"RSI {rsi_value:.0f} — neutral"
        elif rsi_value >= 35:
            score = 0.3
            detail = f"RSI {rsi_value:.0f} — leaning bearish"
        else:
            score = 0.45
            detail = f"RSI {rsi_value:.0f} — oversold, possible bounce"
    else:
        if rsi_value <= 25:
            score = 0.35
            detail = f"RSI {rsi_value:.0f} — oversold, chase risk"
        elif rsi_value <= 45:
            score = 0.9
            detail = f"RSI {rsi_value:.0f} — supportive"
        elif rsi_value <= 52:
            score = 0.6
            detail = f"RSI {rsi_value:.0f} — neutral"
        elif rsi_value <= 65:
            score = 0.3
            detail = f"RSI {rsi_value:.0f} — leaning bullish"
        else:
            score = 0.45
            detail = f"RSI {rsi_value:.0f} — overbought, possible fade"

    return ScoreComponent("rsi", WEIGHTS["rsi"], score, detail)


def _score_macd(current: TimeframeAnalysis, wanted: Bias) -> ScoreComponent:
    indicators = current.indicators
    if not np.isfinite(indicators.macd_hist):
        return ScoreComponent("macd", WEIGHTS["macd"], 0.4, "MACD unavailable")
    score = _bias_score(indicators.macd_bias, wanted, 0.7)
    if indicators.macd_bias is wanted and indicators.macd_expanding:
        score = min(1.0, score + 0.15)
    detail = f"MACD {indicators.macd_bias.value.lower()}"
    if indicators.macd_expanding:
        detail += ", histogram expanding"
    else:
        detail += ", histogram flat or contracting"
    return ScoreComponent("macd", WEIGHTS["macd"], score, detail)


def _score_volatility(current: TimeframeAnalysis) -> ScoreComponent:
    """Volatility is direction-agnostic: it scores *tradeability*."""
    volatility = current.volatility
    if volatility.regime == "extreme":
        score = 0.2
        detail = "extreme volatility — execution risk is elevated"
    elif volatility.regime == "low":
        score = 0.45
        detail = "compressed volatility — moves may not travel far"
    elif volatility.regime == "elevated":
        score = 0.7
        detail = "elevated but workable volatility"
    else:
        score = 0.85
        detail = "volatility in its normal range"
    if volatility.choppy:
        score *= 0.5
        detail += "; price action is choppy"
    return ScoreComponent("volatility", WEIGHTS["volatility"], score, detail)


def _score_mtf(mtf: MultiTimeframeAnalysis, wanted: Bias) -> ScoreComponent:
    agreement = mtf.agreement
    consensus = mtf.consensus
    if consensus is wanted:
        score = min(1.0, 0.5 + agreement * 0.5)
        detail = f"all timeframes lean {wanted.value.lower()} (agreement {agreement:.0%})"
    elif consensus is Bias.NEUTRAL:
        score = 0.35
        detail = f"timeframes are split (agreement {agreement:.0%})"
    else:
        score = 0.1
        detail = f"timeframes lean {consensus.value.lower()}, against this direction"
    return ScoreComponent("multi_timeframe", WEIGHTS["multi_timeframe"], score, detail)
