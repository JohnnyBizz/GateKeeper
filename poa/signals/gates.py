"""Minimum confirmation requirements.

The score ranks setups; the gates decide whether a setup is allowed to become a
directional signal at all. Every gate must pass. A high score with a failed gate
is still WAIT — that is the whole point of this module.

Gates are evaluated in order of severity so the dashboard can lead with the
most important reason a trade was declined.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from ..analysis import MultiTimeframeAnalysis
from ..models import Bias, DataQuality, Direction, LevelImportance, Regime
from .scoring import ScoreResult, direction_to_bias


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str
    blocking: bool = True  # False for advisory checks that only add context

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "blocking": self.blocking,
        }


@dataclass
class GateReport:
    results: list[GateResult]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results if r.blocking)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if r.blocking and not r.passed]

    @property
    def warnings(self) -> list[GateResult]:
        return [r for r in self.results if not r.blocking and not r.passed]

    @property
    def primary_failure(self) -> GateResult | None:
        failures = self.failures
        return failures[0] if failures else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "results": [r.to_dict() for r in self.results],
            "failures": [r.detail for r in self.failures],
            "warnings": [r.detail for r in self.warnings],
        }


@dataclass
class GateSettings:
    min_confidence: float = 75.0
    min_duration_compatibility: float = 65.0
    min_data_confidence: float = 70.0
    min_component_agreement: float = 0.55
    max_atr_percentile: float = 92.0
    resistance_proximity_atr: float = 0.75
    require_multi_timeframe_agreement: bool = True
    require_heikin_ashi_confirmation: bool = True

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> "GateSettings":
        defaults = cls()
        return cls(
            min_confidence=float(section.get("min_confidence", defaults.min_confidence)),
            min_duration_compatibility=float(
                section.get(
                    "min_duration_compatibility", defaults.min_duration_compatibility
                )
            ),
            min_data_confidence=float(
                section.get("min_data_confidence", defaults.min_data_confidence)
            ),
            min_component_agreement=float(
                section.get("min_component_agreement", defaults.min_component_agreement)
            ),
            max_atr_percentile=float(
                section.get("max_atr_percentile", defaults.max_atr_percentile)
            ),
            resistance_proximity_atr=float(
                section.get("resistance_proximity_atr", defaults.resistance_proximity_atr)
            ),
            require_multi_timeframe_agreement=bool(
                section.get(
                    "require_multi_timeframe_agreement",
                    defaults.require_multi_timeframe_agreement,
                )
            ),
            require_heikin_ashi_confirmation=bool(
                section.get(
                    "require_heikin_ashi_confirmation",
                    defaults.require_heikin_ashi_confirmation,
                )
            ),
        )


def evaluate_gates(
    mtf: MultiTimeframeAnalysis,
    direction: Direction,
    score: ScoreResult,
    quality: DataQuality,
    settings: GateSettings,
) -> GateReport:
    """Run every confirmation requirement for ``direction``."""
    wanted = direction_to_bias(direction)
    current = mtf.current
    entry = mtf.entry
    bullish = wanted is Bias.BULLISH
    results: list[GateResult] = []

    # 1. Data quality. Nothing else matters if we cannot read the chart.
    results.append(
        GateResult(
            "data_quality",
            quality.ok and quality.confidence >= settings.min_data_confidence,
            (
                f"Chart data confidence {quality.confidence:.0f}% "
                f"(minimum {settings.min_data_confidence:.0f}%)"
                + (f" — {'; '.join(quality.issues)}" if quality.issues else "")
            ),
        )
    )

    # 2. Regime must permit a directional trade at all.
    regime = current.regime.regime
    results.append(
        GateResult(
            "regime",
            regime.tradeable,
            (
                f"Market regime is {regime.label}"
                + ("" if regime.tradeable else " — not a tradeable regime")
            ),
        )
    )

    # 3. Market structure must agree with the direction.
    structure = current.structure
    structure_ok = structure.bias is wanted and structure.strength >= 0.35
    structure_detail = f"Market structure: {structure.label}"

    # A confirmed break of structure in our favour also satisfies this, since
    # that is how a new trend begins before the swing sequence catches up.
    wanted_break = "bullish" if bullish else "bearish"
    if not structure_ok and structure.break_of_structure == wanted_break:
        structure_ok = structure.strength >= 0.2

    # A trend that runs without pulling back never prints the swing lows (or
    # highs) the fractal detector needs, so structure has no opinion rather
    # than a negative one. Falling back to the regime here is what stops the
    # cleanest trends from being rejected for lack of a retracement — and the
    # regime itself already demands ADX, EMA alignment and price efficiency,
    # so this cannot rescue a choppy market.
    if not structure_ok and not structure.has_swing_history:
        trend_regimes = (
            Regime.STRONG_UPTREND,
            Regime.WEAK_UPTREND,
            Regime.STRONG_DOWNTREND,
            Regime.WEAK_DOWNTREND,
            Regime.BREAKOUT,
        )
        if current.regime.regime in trend_regimes and current.regime.bias is wanted:
            structure_ok = True
            structure_detail = (
                f"No confirmed swing sequence yet; direction taken from the "
                f"{current.regime.regime.label.lower()} regime"
            )

    results.append(GateResult("market_structure", structure_ok, structure_detail))

    # 4. Momentum must be pushing our way.
    momentum_ok = (
        current.momentum.bias is wanted and current.momentum.strength >= 0.25
    ) or (entry is not current and entry.momentum.bias is wanted and entry.momentum.strength >= 0.4)
    results.append(
        GateResult(
            "momentum",
            momentum_ok,
            f"Momentum is {current.momentum.label} {current.momentum.bias.value.lower()}",
        )
    )

    # 5. Heikin Ashi confirmation on the entry timeframe.
    if settings.require_heikin_ashi_confirmation:
        ha_ok = entry.heikin_ashi.confirms(wanted) or current.heikin_ashi.confirms(wanted)
        results.append(
            GateResult(
                "heikin_ashi",
                ha_ok,
                f"Heikin Ashi: {entry.heikin_ashi.pattern}",
            )
        )

    # 6. No major level sitting immediately in the way.
    levels = current.levels
    obstacle = levels.nearest_resistance if bullish else levels.nearest_support
    distance = levels.distance_in_atr(obstacle)
    if obstacle is None or distance is None:
        obstacle_ok = True
        obstacle_detail = (
            "No significant level immediately overhead"
            if bullish
            else "No significant level immediately below"
        )
    else:
        blocking_level = (
            obstacle.importance is LevelImportance.MAJOR
            and distance < settings.resistance_proximity_atr
        )
        obstacle_ok = not blocking_level
        side = "resistance" if bullish else "support"
        obstacle_detail = (
            f"{obstacle.importance.value.title()} {side} at {obstacle.price:.5f}, "
            f"{distance:.1f} ATR away"
        )
    results.append(GateResult("clear_path", obstacle_ok, obstacle_detail))

    # 7. Higher-timeframe alignment, unless a reversal is properly confirmed.
    if settings.require_multi_timeframe_agreement:
        if mtf.conflicts_with_higher(wanted):
            reversal_ok = mtf.reversal_confirmed(wanted)
            detail = (
                f"Higher timeframe ({mtf.higher.label}) is "
                f"{mtf.higher.trend_bias.value.lower()}"
                + (
                    " but a reversal is confirmed across timeframes"
                    if reversal_ok
                    else " — this would be a counter-trend entry without confirmed reversal"
                )
            )
            results.append(GateResult("higher_timeframe", reversal_ok, detail))
        else:
            results.append(
                GateResult(
                    "higher_timeframe",
                    True,
                    f"Higher timeframe ({mtf.higher.label}) does not oppose this direction",
                )
            )

    # 8. Volatility ceiling.
    atr_percentile = current.indicators.atr_percentile
    volatility_ok = (
        not np.isfinite(atr_percentile) or atr_percentile <= settings.max_atr_percentile
    )
    results.append(
        GateResult(
            "volatility_ceiling",
            volatility_ok,
            f"ATR percentile {atr_percentile:.0f} (ceiling {settings.max_atr_percentile:.0f})",
        )
    )

    # 9. Breadth of agreement across scoring components.
    results.append(
        GateResult(
            "component_agreement",
            score.agreement >= settings.min_component_agreement,
            (
                f"{score.agreement:.0%} of scoring weight favours this direction "
                f"(minimum {settings.min_component_agreement:.0%})"
            ),
        )
    )

    # 10. Overall confidence floor.
    results.append(
        GateResult(
            "confidence",
            score.total >= settings.min_confidence,
            f"Setup score {score.total:.0f}/100 (minimum {settings.min_confidence:.0f})",
        )
    )

    # -- advisory checks (context only, never blocking) ---------------------
    results.append(
        GateResult(
            "reversal_risk",
            current.regime.reversal_risk < 0.5,
            f"Reversal risk {current.regime.reversal_risk:.0%}",
            blocking=False,
        )
    )
    results.append(
        GateResult(
            "heikin_ashi_momentum",
            not current.heikin_ashi.momentum_weakening,
            "Heikin Ashi bodies are contracting within the current run",
            blocking=False,
        )
    )

    return GateReport(results=results)
