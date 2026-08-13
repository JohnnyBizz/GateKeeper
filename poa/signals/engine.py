"""The signal engine.

Order of operations, and it matters:

1. validate the data — bad data means WAIT, never a confident signal;
2. build the multi-timeframe picture;
3. score *both* directions independently and take the better one;
4. run the confirmation gates on that direction;
5. analyse the trade duration separately from the direction;
6. combine the two into one of four states.

A direction is only emitted when the gates pass. Duration compatibility is
evaluated on its own axis, so "right direction, wrong expiration" is reported
as exactly that rather than being hidden inside a single number.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..analysis import MultiTimeframeAnalysis, build_multi_timeframe
from ..config import TRADE_DURATIONS
from ..models import (
    Bias,
    DataQuality,
    Direction,
    Series,
    SetupQuality,
    SignalState,
    format_duration,
    format_price,
    utcnow,
)
from .duration import DurationAnalysis, analyze_duration
from .gates import GateReport, GateSettings, evaluate_gates
from .narrative import build_narrative
from .scoring import ScoreResult, score_direction


@dataclass
class SignalRequest:
    """Everything the engine needs for one evaluation."""

    series: Series
    asset: str
    chart_timeframe: int
    trade_duration: int
    quality: DataQuality
    available_durations: Sequence[int] = TRADE_DURATIONS
    higher_multiple: int = 5
    entry_multiple: int = 1
    settings: GateSettings = field(default_factory=GateSettings)


@dataclass
class Signal:
    """A complete evaluation, ready for the dashboard, journal and alerts."""

    id: str
    timestamp: datetime
    asset: str
    chart_timeframe: int
    trade_duration: int
    price: float | None

    direction: Direction
    direction_confidence: float
    setup_quality: SetupQuality
    state: SignalState

    score: ScoreResult | None
    gates: GateReport | None
    duration: DurationAnalysis | None
    mtf: MultiTimeframeAnalysis | None
    quality: DataQuality

    headline: str
    reason: str
    invalidation: str
    why: list[str]
    warnings: list[str]

    # Set by the tracker as the signal is followed candle by candle.
    peak_confidence: float = 0.0
    expires_at: datetime | None = None

    # ---------------------------------------------------------------------

    @property
    def actionable(self) -> bool:
        """Is this a signal the user could act on right now?"""
        return self.direction in (Direction.CALL, Direction.PUT) and (
            self.state in (SignalState.ACTIVE, SignalState.WEAKENING)
        )

    @property
    def duration_confidence(self) -> float:
        return self.duration.selected_score if self.duration else 0.0

    @property
    def overall_confidence(self) -> float:
        """Direction and duration combined, weighted toward direction.

        Deliberately not a simple average: a good direction with a badly matched
        expiration is not a good trade, so the weaker of the two drags harder.
        """
        if self.direction in (Direction.WAIT, Direction.NO_TRADE):
            return round(self.direction_confidence, 1)
        combined = self.direction_confidence * 0.6 + self.duration_confidence * 0.4
        weakest = min(self.direction_confidence, self.duration_confidence)
        return round(min(combined, weakest + 12.0), 1)

    @property
    def chart_timeframe_label(self) -> str:
        return format_duration(self.chart_timeframe)

    @property
    def trade_duration_label(self) -> str:
        return format_duration(self.trade_duration)

    def to_dict(self, include_mtf: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "asset": self.asset,
            "chart_timeframe": self.chart_timeframe,
            "chart_timeframe_label": self.chart_timeframe_label,
            "trade_duration": self.trade_duration,
            "trade_duration_label": self.trade_duration_label,
            "price": self.price,
            "price_display": format_price(self.price),
            "direction": self.direction.value,
            "direction_emoji": self.direction.emoji,
            "direction_confidence": round(self.direction_confidence, 1),
            "duration_confidence": round(self.duration_confidence, 1),
            "overall_confidence": self.overall_confidence,
            "setup_quality": self.setup_quality.value,
            "setup_quality_label": self.setup_quality.label,
            "state": self.state.value,
            "actionable": self.actionable,
            "headline": self.headline,
            "reason": self.reason,
            "invalidation": self.invalidation,
            "why": list(self.why),
            "warnings": list(self.warnings),
            "peak_confidence": round(self.peak_confidence, 1),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "quality": self.quality.to_dict(),
            "score": self.score.to_dict() if self.score else None,
            "gates": self.gates.to_dict() if self.gates else None,
            "duration": self.duration.to_dict() if self.duration else None,
        }
        if include_mtf and self.mtf is not None:
            data["timeframes"] = self.mtf.to_dict()
        return data

    def fingerprint(self) -> str:
        """Identity used to suppress duplicate alerts for the same setup."""
        bucket = int(self.overall_confidence // 5) * 5
        return f"{self.asset}|{self.direction.value}|{self.trade_duration}|{bucket}|{self.state.value}"


class SignalEngine:
    """Stateless evaluator. Lifecycle tracking lives in ``tracker.SignalTracker``."""

    def evaluate(self, request: SignalRequest) -> Signal:
        quality = request.quality
        now = utcnow()
        price = request.series.last_price if len(request.series) else None

        # --- 1. Data validation -------------------------------------------
        if not quality.ok:
            return self._wait_signal(
                request,
                now,
                price,
                confidence=min(quality.confidence, 50.0),
                headline="INSUFFICIENT CHART DATA",
                reason=(
                    "Insufficient chart data. "
                    + ("; ".join(quality.issues) if quality.issues else "")
                ).strip(),
                warnings=list(quality.issues),
                direction=Direction.WAIT,
            )

        # --- 2. Multi-timeframe picture -----------------------------------
        try:
            mtf = build_multi_timeframe(
                request.series,
                higher_multiple=request.higher_multiple,
                entry_multiple=request.entry_multiple,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return self._wait_signal(
                request,
                now,
                price,
                confidence=0.0,
                headline="ANALYSIS ERROR",
                reason=f"Analysis could not be completed: {exc}",
                warnings=[str(exc)],
                direction=Direction.WAIT,
            )

        # --- 3. Score both directions -------------------------------------
        call_score = score_direction(
            mtf, Direction.CALL, request.settings.resistance_proximity_atr
        )
        put_score = score_direction(
            mtf, Direction.PUT, request.settings.resistance_proximity_atr
        )
        best_score = call_score if call_score.total >= put_score.total else put_score
        candidate = best_score.direction

        # --- 4. Gates ------------------------------------------------------
        gates = evaluate_gates(mtf, candidate, best_score, quality, request.settings)

        # --- 5. Duration ---------------------------------------------------
        duration = analyze_duration(
            mtf,
            candidate,
            selected_seconds=request.trade_duration,
            available_durations=request.available_durations,
            chart_timeframe_seconds=request.chart_timeframe,
        )

        # --- 6. Combine into a state ---------------------------------------
        regime = mtf.current.regime
        direction_confidence = best_score.total
        warnings = [w.detail for w in gates.warnings]

        if not regime.regime.tradeable and regime.regime.value == "HIGH_VOLATILITY":
            # Distinct from an ordinary WAIT: conditions are actively hostile.
            direction = Direction.NO_TRADE
            headline = "NO TRADE"
            reason = (
                "Volatility and market structure are too unstable for reliable "
                "analysis. Standing aside is the correct action here."
            )
        elif gates.passed:
            direction = candidate
            headline = f"{candidate.value} SETUP"
            reason = ""  # filled in by the narrative builder below
        else:
            direction = Direction.WAIT
            primary = gates.primary_failure
            headline = "WAIT"
            reason = _wait_reason(gates, candidate, mtf)

        setup_quality = SetupQuality.from_score(direction_confidence)

        narrative = build_narrative(
            mtf=mtf,
            direction=direction,
            candidate=candidate,
            score=best_score,
            gates=gates,
            duration=duration,
            quality=quality,
        )
        if direction in (Direction.CALL, Direction.PUT):
            reason = narrative.reason

        signal = Signal(
            id=uuid.uuid4().hex[:12],
            timestamp=now,
            asset=request.asset,
            chart_timeframe=request.chart_timeframe,
            trade_duration=request.trade_duration,
            price=price,
            direction=direction,
            direction_confidence=direction_confidence,
            setup_quality=setup_quality,
            state=SignalState.ACTIVE,
            score=best_score,
            gates=gates,
            duration=duration,
            mtf=mtf,
            quality=quality,
            headline=headline,
            reason=reason,
            invalidation=narrative.invalidation,
            why=narrative.why,
            warnings=warnings + narrative.warnings,
        )
        signal.peak_confidence = signal.overall_confidence

        # A confirmed direction whose expiration is a poor match is downgraded
        # to WAIT: the user asked for direction *and* duration to be confirmed.
        if signal.direction in (Direction.CALL, Direction.PUT):
            min_duration_score = getattr(
                request.settings, "min_duration_compatibility", 65.0
            )
            if duration.selected_score < min_duration_score:
                signal.headline = f"{candidate.value} DIRECTION — DURATION QUESTIONABLE"
                signal.direction = Direction.WAIT
                signal.reason = (
                    f"Directional bias is {candidate.value.lower()} at "
                    f"{direction_confidence:.0f}/100, but the selected "
                    f"{duration.selected_label.lower()} expiration scores only "
                    f"{duration.selected_score:.0f}/100 for this setup. "
                    f"{duration.reason}"
                )
                signal.warnings.append(
                    f"Selected duration is a {duration.selected_fit.label.lower()}; "
                    f"the engine prefers {duration.recommended_label.lower()}."
                )

        if signal.direction in (Direction.CALL, Direction.PUT):
            signal.expires_at = now + _timedelta(request.trade_duration)

        return signal

    # ------------------------------------------------------------------

    def _wait_signal(
        self,
        request: SignalRequest,
        now: datetime,
        price: float | None,
        *,
        confidence: float,
        headline: str,
        reason: str,
        warnings: list[str],
        direction: Direction,
    ) -> Signal:
        return Signal(
            id=uuid.uuid4().hex[:12],
            timestamp=now,
            asset=request.asset,
            chart_timeframe=request.chart_timeframe,
            trade_duration=request.trade_duration,
            price=price,
            direction=direction,
            direction_confidence=confidence,
            setup_quality=SetupQuality.INSUFFICIENT,
            state=SignalState.ACTIVE,
            score=None,
            gates=None,
            duration=None,
            mtf=None,
            quality=request.quality,
            headline=headline,
            reason=reason,
            invalidation="No signal is active, so there is nothing to invalidate.",
            why=[reason] if reason else [],
            warnings=warnings,
            peak_confidence=confidence,
        )


def _timedelta(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=int(seconds))


def _wait_reason(
    gates: GateReport, candidate: Direction, mtf: MultiTimeframeAnalysis
) -> str:
    """Explain a WAIT in the trader's own terms, leading with the real blocker."""
    failures = gates.failures
    if not failures:  # pragma: no cover - defensive
        return "Confirmation is insufficient for a directional signal."

    lead = failures[0]
    side = "bullish" if candidate is Direction.CALL else "bearish"

    explanations = {
        "data_quality": "Chart data is not reliable enough to analyse.",
        "regime": (
            f"Market structure is unclear and confirmation is insufficient "
            f"({mtf.current.regime.regime.label.lower()})."
        ),
        "market_structure": (
            f"The {side} case is not supported by market structure "
            f"({mtf.current.structure.label})."
        ),
        "momentum": f"Momentum is not confirming the {side} case.",
        "heikin_ashi": (
            f"Heikin Ashi has not confirmed the {side} case "
            f"({mtf.entry.heikin_ashi.pattern})."
        ),
        "clear_path": (
            f"Price is approaching a major level that blocks the {side} case."
        ),
        "higher_timeframe": (
            f"The higher timeframe opposes a {side} entry and no reversal is confirmed."
        ),
        "volatility_ceiling": "Volatility is too high for a dependable read.",
        "component_agreement": "The evidence is split rather than pointing one way.",
        "confidence": "Overall confidence is below the configured minimum.",
    }
    primary = explanations.get(lead.name, lead.detail)

    if len(failures) > 1:
        secondary = ", ".join(
            explanations.get(f.name, f.name).rstrip(".").lower() for f in failures[1:3]
        )
        return f"{primary} Also: {secondary}. Wait for confirmation."
    return f"{primary} Wait for confirmation."
