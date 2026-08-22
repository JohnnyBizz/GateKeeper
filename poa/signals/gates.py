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


# Gates that may never be demoted. Data quality is not a claim about the
# market — it is a claim about whether the candles are readable at all, and no
# win rate can make an unreadable chart tradeable.
NEVER_ADVISORY = frozenset({"data_quality"})

# Gates the replay's own audit may never retire, on top of NEVER_ADVISORY.
# Each of these rests on trades that actually settled at the platform's own
# prices — evidence the replay does not have and cannot outvote. A replay
# walks overlapping windows of whatever trend it was handed, so it will
# happily report that the rule written *by the live record* blocked setups
# that would have paid; letting that retire the rule would be the record
# silencing itself. Retiring any of these takes a human reading FINDINGS.md,
# not a resample. (The overheat ceiling rests on the same live evidence but
# is enforced on the shown number in the signal engine, beside the shown
# floor — it is not a named gate, so there is nothing here for the audit to
# retire.)
NEVER_AUTO_RETIRED = NEVER_ADVISORY | frozenset(
    {"measured_edge", "regime_record"}
)


@dataclass
class GateSettings:
    #: Minimum *direction score*. An internal number, not the one on the
    #: panel — ``min_shown_confidence`` below is that one.
    min_confidence: float = 85.0
    #: Minimum for the number the panel actually shows.
    #:
    #: These were one setting, and that was a trap. Raising min_confidence to
    #: 85 gated the direction score at 85 and left the panel free to display
    #: 80.1 on a call that passed, because what it shows is
    #: ``direction x 0.6 + duration x 0.4`` capped at the weaker plus twelve —
    #: always the lower figure. Somebody told to trade at 85 would have been
    #: shown, and taken, calls in the high seventies.
    #:
    #: Off by default *here* and set to 85 in the shipped config. The
    #: distinction matters: this dataclass is what a caller constructing
    #: ``GateSettings()`` directly gets, and a gate that silences the engine
    #: unless argued out of it would make every such caller quietly agree to
    #: a product decision it never asked about. Zero disables it.
    min_shown_confidence: float = 0.0
    #: Refuse a setup the panel would *show* at or above this. Enforced in
    #: the signal engine beside ``min_shown_confidence``, on the same shown
    #: number, because that is the number the evidence was measured on: the
    #: session reports band the shown reading, and in all four live sessions
    #: the calls shown at ninety and above settled below the calls beneath
    #: them — 14%, 31%, 42%, 21%, against 54%, 48%, 44%, 65% just below.
    #: An earlier version capped the internal direction score instead — the
    #: wrong-number mistake this project had already made once with the
    #: floor — and within the hour a live session showed seven 90-plus
    #: calls sailing under it. Zero disables it, and zero is the default
    #: here for the same reason as the floor: the shipped config makes the
    #: product decision, a bare ``GateSettings()`` does not smuggle it in.
    overheat_ceiling: float = 0.0
    min_duration_compatibility: float = 65.0
    min_data_confidence: float = 70.0
    min_component_agreement: float = 0.55
    max_atr_percentile: float = 92.0
    resistance_proximity_atr: float = 0.75
    require_multi_timeframe_agreement: bool = True
    require_heikin_ashi_confirmation: bool = True
    # Refuse a setup when the measured record says setups like it lose more
    # often than this payout can carry. Only ever acts on a sample big enough
    # to mean something, so it is silent until one exists.
    require_measured_edge: bool = True
    # Refuse setups in the market conditions this chart is measurably least
    # often right in. Unlike a verdict on the score band this cannot silence
    # the tool — it rules out some conditions and leaves the rest open — so it
    # can act on replayed evidence rather than waiting for real trades.
    avoid_weak_regimes: bool = True
    # Gates demoted to advisory because the measured record says they block
    # setups that would have paid. A blocking gate is a claim — "setups failing
    # this are worse than setups passing it" — and a claim the chart disagrees
    # with is not prudence, it is a rule quietly costing money.
    advisory: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> "GateSettings":
        defaults = cls()
        return cls(
            min_confidence=float(section.get("min_confidence", defaults.min_confidence)),
            min_shown_confidence=float(
                section.get("min_shown_confidence", defaults.min_shown_confidence)
            ),
            overheat_ceiling=float(
                section.get("overheat_ceiling", defaults.overheat_ceiling)
            ),
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
            require_measured_edge=bool(
                section.get("require_measured_edge", defaults.require_measured_edge)
            ),
            avoid_weak_regimes=bool(
                section.get("avoid_weak_regimes", defaults.avoid_weak_regimes)
            ),
            advisory=frozenset(section.get("advisory_gates", ()) or ()),
        )


def evaluate_gates(
    mtf: MultiTimeframeAnalysis,
    direction: Direction,
    score: ScoreResult,
    quality: DataQuality,
    settings: GateSettings,
    calibration: Any | None = None,
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
        if not mtf.higher_is_distinct:
            # There is no higher timeframe — only the current one standing in
            # for it, for want of history to aggregate. It cannot oppose the
            # direction, and it cannot confirm it either. A requirement met by
            # a view agreeing with itself is not a requirement.
            results.append(
                GateResult(
                    "higher_timeframe",
                    False,
                    "No higher timeframe yet — not enough history to build one, "
                    "so there is nothing to confirm against",
                )
            )
        elif mtf.conflicts_with_higher(wanted):
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

    # The measured record, last, because it is the only check that asks what
    # setups like this one have *actually* settled at rather than what they
    # look like. It overrules the rest when it has the sample to: a setup that
    # satisfies every structural requirement and has still lost more often than
    # this payout can carry is a setup whose requirements are not the point.
    if settings.require_measured_edge and calibration is not None:
        # Keyed on the *direction* score, which is what exists at gate time and
        # what the structural analysis actually produced. How well the expiry
        # fits is scored separately and gated separately; folding the two
        # together here would calibrate against a number that does not yet
        # exist and blur two different questions into one bucket.
        beats, detail = calibration.verdict(score.total, current.regime.regime.name)
        # Only a record of trades that were actually *placed* earns a veto.
        # A replay's opinion cannot have one: live setups land in the same
        # score band the replay is dominated by, so a band that measured badly
        # would silence the tool completely — and a silent tool takes no
        # trades, so no real record forms, so it stays silent. That loop closes
        # on itself and never reopens.
        vetoing = bool(getattr(calibration, "from_real_trades", False))
        if beats is False:
            # A replay saying this loses is worth reporting either way. It just
            # does not get to stop the trade unless real trades are behind it.
            results.append(GateResult("measured_edge", False, detail, blocking=vetoing))
        else:
            # No opinion is not a failure. Early in a session, on a new pair, or
            # after a settings change there is no record yet, and refusing to
            # signal until one exists would mean never building one.
            results.append(GateResult("measured_edge", True, detail, blocking=False))

        # And the conditions this chart is least often right in. A separate
        # question from the score band, and answerable from replayed evidence
        # where the band is not: ruling out one regime leaves the others open,
        # so the tool goes on trading and goes on learning. Ruling out a score
        # band silences it, and a silent tool never earns the record that
        # would reopen the question.
        weak = {}
        if settings.avoid_weak_regimes:
            try:
                weak = calibration.weak_regimes()
            except Exception:  # pragma: no cover - defensive
                weak = {}
        here = weak.get(current.regime.regime.name)
        if here is not None:
            average = calibration.overall_rate() or 0.0
            results.append(
                GateResult(
                    "regime_record",
                    False,
                    (
                        f"Setups in a {regime.label.lower()} market have been "
                        f"right {here.win_rate:.0f}% of the time here over "
                        f"{here.settled}, against {average:.0f}% across every "
                        "condition — the least reliable place to read this chart"
                    ),
                )
            )

    # Demotion last, in one place, so every gate above can be written as if
    # it blocks and none of them has to know it might not.
    if settings.advisory:
        results = [
            GateResult(r.name, r.passed, r.detail, blocking=False)
            if r.name in settings.advisory and r.name not in NEVER_ADVISORY
            else r
            for r in results
        ]

    return GateReport(results=results)
