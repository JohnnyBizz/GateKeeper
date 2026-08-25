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
    # What setups like this one have actually settled at, from replaying this
    # chart's own history. Absent during the replay itself — the record cannot
    # be an input to the trades that build it — and absent until one has run.
    calibration: Any | None = None
    # When this pair's most recent call by this tool settled as a loss, if
    # the caller checked. Feeds the loss cooldown; None means no recent loss
    # or nobody asked, and either way the cooldown abstains.
    last_loss_at: datetime | None = None
    # The platform's live payout for this chart as a fraction (0.92), or None
    # when the source does not know it. Feeds the payout floor, which never
    # blocks on an unknown payout.
    payout: float | None = None


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
        gates = evaluate_gates(
            mtf,
            candidate,
            best_score,
            quality,
            request.settings,
            calibration=request.calibration,
        )

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
        # Advisory gate failures (reversal risk, HA contraction) are already
        # phrased better by the narrative builder; repeating both fills the
        # panel with near-duplicates, so only the narrative's wording ships.
        #
        # Data-quality issues are different: the data can be good enough to
        # analyse and still carry something the user has to know — a price scale
        # that disagrees with the axis leaves every pattern valid and every
        # printed price wrong. Those ride along with the signal.
        warnings: list[str] = list(quality.issues)

        # The measured record is the exception to the rule above. It is a fact
        # about how setups like this one have settled, not a reading of the
        # candles, so the narrative builder has nothing to say about it — and
        # when it is advisory rather than blocking, this is the only place it
        # would ever be seen.
        warnings.extend(
            result.detail for result in gates.warnings if result.name == "measured_edge"
        )

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

        # And the number on the panel has to clear its own minimum, because
        # that is the number anybody acts on. Gating only the direction score
        # let a call pass at 86 and display 80.1 — the user reads the panel,
        # not the internals, so a "minimum 85" that shows 80 is not a strict
        # setting, it is a wrong one.
        if signal.direction in (Direction.CALL, Direction.PUT):
            floor = getattr(request.settings, "min_shown_confidence", 0.0)
            shown = signal.overall_confidence
            if floor and shown < floor:
                signal.headline = f"{candidate.value} SETUP — BELOW YOUR MINIMUM"
                signal.direction = Direction.WAIT
                signal.reason = (
                    f"Reads {int(shown)}/100 overall, under the {floor:.0f} you "
                    f"set. Direction scores {direction_confidence:.0f} and the "
                    f"{duration.selected_label.lower()} expiration scores "
                    f"{duration.selected_score:.0f}; the combined reading is "
                    "what this is judged on."
                )

        # The ceiling is on the same number as the floor, for the same
        # reason — and because it was *measured* on that number: the score
        # bands in every session report band the shown reading. In all four
        # live sessions the calls shown at ninety or above settled below the
        # calls just beneath them (14%, 31%, 42%, 21% across four). An
        # earlier version capped the internal direction score instead — the
        # wrong-number mistake this project had already made once with the
        # floor — and within the hour a session showed seven 90-plus calls
        # sailing under it, direction in the eighties, duration lifting the
        # shown number over the top.
        if signal.direction in (Direction.CALL, Direction.PUT):
            ceiling = getattr(request.settings, "overheat_ceiling", 0.0)
            shown = signal.overall_confidence
            if ceiling and shown >= ceiling:
                signal.headline = f"{candidate.value} SETUP — OVERHEATED"
                signal.direction = Direction.WAIT
                signal.reason = (
                    f"Reads {int(shown)}/100, at or above the {ceiling:.0f} "
                    "ceiling. In every live session measured, calls shown "
                    "this high settled below the band beneath them (14%, "
                    "31%, 42%, 21% across four sessions). The score reads "
                    "highest when every component finally agrees, and that "
                    "is the most stretched moment of the move it is reading "
                    "— not the safest."
                )

        # A pair that has just cost a trade is the pair most likely to be
        # read again out of pure momentum. Measured before shipping, over
        # the three sessions with row-level records: standing down for
        # three minutes after a losing call removed five losses and one
        # win from the session that had the chase pattern (50.0% → 60.0%)
        # and changed nothing on the others. The cascade variant — stand
        # down everywhere after clustered losses — was measured too, cost
        # winners, and did not ship.
        if signal.direction in (Direction.CALL, Direction.PUT):
            cooldown = float(
                getattr(request.settings, "loss_cooldown_minutes", 0.0)
            )
            lost_at = request.last_loss_at
            if cooldown > 0 and lost_at is not None:
                age = (now - lost_at).total_seconds() / 60.0
                if 0 <= age < cooldown:
                    remaining = max(1, round(cooldown - age))
                    signal.headline = f"{candidate.value} SETUP — COOLING OFF"
                    signal.direction = Direction.WAIT
                    signal.reason = (
                        f"This pair's last call lost "
                        f"{max(0, round(age))} minute(s) ago, so it is "
                        f"stood down for {remaining} more. Re-reading the "
                        "pair that just cost a trade is how one loss "
                        "becomes three; on the measured sessions this "
                        "pause removed five losses for every win it cost."
                    )

        # Some charts are unwinnable before the first click. At a 72%
        # payout the break-even is 58.1%, and nothing measured on this
        # project has cleared that bar; at 92% it is 52.1%. The floor is
        # arithmetic, not a reading of the candles — which is why it can
        # ship without a replication count.
        if signal.direction in (Direction.CALL, Direction.PUT):
            floor_payout = float(getattr(request.settings, "min_payout", 0.0))
            payout = request.payout
            if floor_payout > 0 and payout is not None and payout < floor_payout:
                breakeven = 100.0 / (1.0 + payout)
                signal.headline = f"{candidate.value} SETUP — PAYOUT TOO LOW"
                signal.direction = Direction.WAIT
                signal.reason = (
                    f"This chart pays {payout * 100:.0f}%, under your "
                    f"{floor_payout * 100:.0f}% floor. At that payout a "
                    f"trade must win {breakeven:.1f}% of the time just to "
                    "break even, and no configuration measured here has "
                    "cleared that bar. The same setup on a chart paying "
                    "90%+ is a different game."
                )

        # The reversal, last of all. It flips only a call that survived
        # every gate and demotion above, because those rules were measured
        # on the READ and refuse the moments where the read itself is
        # unsafe — overheated, unpaid, unfit. Promoted 2026-08-25 after
        # the pre-registered test: the mirror (this exact rulebook,
        # opposite side) settled its out-of-sample paper record above
        # break-even at the interval's lower bound and won the
        # head-to-head against the very calls it reverses. Everything
        # else on the signal still describes the read — the score, the
        # narrative, the invalidation — and the first sentence of the
        # reason says the call is its reverse, so the panel never shows a
        # side without saying where it came from.
        if (
            bool(getattr(request.settings, "invert_calls", False))
            and signal.direction in (Direction.CALL, Direction.PUT)
        ):
            read_side = signal.direction
            signal.direction = read_side.opposite
            signal.headline = f"{signal.direction.value} — REVERSED READ"
            signal.reason = (
                f"The read argues {read_side.value} at "
                f"{direction_confidence:.0f}/100; the call is "
                f"{signal.direction.value} because the measured record "
                "runs against reads like this one: pooled below a coin "
                "flip on their own side, above break-even reversed "
                "(the mirror experiment, promoted 2026-08-25). "
                + signal.reason
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
