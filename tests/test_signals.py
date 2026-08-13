"""Signal engine behaviour, and the safety invariants that matter most.

The central claims under test:

* WAIT is the default — a direction requires every gate to pass;
* bad data never produces a confident direction;
* chart timeframe and trade duration are independent variables;
* the assistant never claims certainty about an outcome.
"""

from __future__ import annotations

import json

import pytest

from conftest import (
    build_series,
    choppy_series,
    good_quality,
    pullback_trend,
    ranging_series,
    trending_series,
)
from poa.config import TRADE_DURATIONS
from poa.models import (
    Bias,
    DataQuality,
    Direction,
    Series,
    SetupQuality,
    SignalState,
    format_duration,
)
from poa.signals import (
    GateSettings,
    SignalEngine,
    SignalRequest,
    contains_banned_language,
    score_direction,
)
from poa.signals.duration import analyze_duration
from poa.analysis import build_multi_timeframe


def evaluate(
    series: Series,
    *,
    trade_duration: int = 180,
    chart_timeframe: int = 60,
    quality: DataQuality | None = None,
    settings: GateSettings | None = None,
):
    request = SignalRequest(
        series=series,
        asset="EUR/USD",
        chart_timeframe=chart_timeframe,
        trade_duration=trade_duration,
        quality=quality or good_quality(series),
        available_durations=TRADE_DURATIONS,
        settings=settings or GateSettings(),
    )
    return SignalEngine().evaluate(request)


class TestScoring:
    def test_component_weights_sum_to_one_hundred(self):
        from poa.signals.scoring import WEIGHTS

        assert sum(WEIGHTS.values()) == pytest.approx(100.0)

    def test_a_bullish_market_scores_call_above_put(self):
        mtf = build_multi_timeframe(pullback_trend(400, direction=1), 5, 1)
        call = score_direction(mtf, Direction.CALL)
        put = score_direction(mtf, Direction.PUT)
        assert call.total > put.total

    def test_a_bearish_market_scores_put_above_call(self):
        mtf = build_multi_timeframe(pullback_trend(400, direction=-1), 5, 1)
        assert score_direction(mtf, Direction.PUT).total > score_direction(
            mtf, Direction.CALL
        ).total

    def test_scores_stay_within_zero_and_one_hundred(self):
        for series in (
            pullback_trend(400, direction=1),
            choppy_series(400),
            ranging_series(400),
        ):
            mtf = build_multi_timeframe(series, 5, 1)
            for direction in (Direction.CALL, Direction.PUT):
                assert 0.0 <= score_direction(mtf, direction).total <= 100.0

    def test_a_wait_direction_produces_no_components(self):
        mtf = build_multi_timeframe(trending_series(200), 5, 1)
        assert score_direction(mtf, Direction.WAIT).components == []


class TestWaitIsTheDefault:
    def test_choppy_markets_do_not_produce_a_direction(self):
        signal = evaluate(choppy_series(400))
        assert signal.direction in (Direction.WAIT, Direction.NO_TRADE)

    def test_a_range_does_not_produce_a_direction(self):
        signal = evaluate(ranging_series(400))
        assert signal.direction in (Direction.WAIT, Direction.NO_TRADE)

    def test_a_wait_signal_explains_itself(self):
        signal = evaluate(choppy_series(400))
        assert signal.reason
        assert len(signal.reason) > 20

    def test_a_wait_signal_is_not_actionable(self):
        assert not evaluate(choppy_series(400)).actionable

    def test_raising_the_confidence_floor_suppresses_signals(self):
        series = pullback_trend(400, direction=1)
        permissive = evaluate(series, settings=GateSettings(min_confidence=60))
        strict = evaluate(series, settings=GateSettings(min_confidence=99))
        assert strict.direction is Direction.WAIT
        # The score itself is unchanged; only the decision differs.
        assert permissive.direction_confidence == pytest.approx(
            strict.direction_confidence
        )


class TestDirectionalSignals:
    def test_a_clean_uptrend_can_produce_a_call(self):
        signal = evaluate(pullback_trend(400, direction=1))
        assert signal.direction is Direction.CALL
        assert signal.gates.passed
        assert signal.actionable

    def test_a_clean_downtrend_can_produce_a_put(self):
        signal = evaluate(pullback_trend(400, direction=-1))
        assert signal.direction is Direction.PUT
        assert signal.actionable

    def test_a_directional_signal_carries_an_expiry(self):
        signal = evaluate(pullback_trend(400, direction=1), trade_duration=300)
        assert signal.expires_at is not None
        elapsed = (signal.expires_at - signal.timestamp).total_seconds()
        assert elapsed == pytest.approx(300)

    def test_a_directional_signal_states_its_invalidation(self):
        signal = evaluate(pullback_trend(400, direction=1))
        assert "invalid" in signal.invalidation.lower()

    def test_every_gate_is_reported_even_when_all_pass(self):
        signal = evaluate(pullback_trend(400, direction=1))
        names = {gate.name for gate in signal.gates.results}
        assert {"data_quality", "regime", "market_structure", "momentum"} <= names


class TestDataQualityGate:
    def test_unusable_data_yields_wait_not_a_direction(self):
        series = pullback_trend(400, direction=1)
        bad = DataQuality(
            ok=False,
            confidence=10.0,
            candle_count=len(series),
            issues=["Chart is obstructed."],
            source="test",
        )
        signal = evaluate(series, quality=bad)
        assert signal.direction is Direction.WAIT
        assert "Insufficient chart data" in signal.reason

    def test_low_confidence_data_blocks_a_direction(self):
        series = pullback_trend(400, direction=1)
        marginal = DataQuality(
            ok=True, confidence=55.0, candle_count=len(series), source="test"
        )
        signal = evaluate(series, quality=marginal)
        assert signal.direction is Direction.WAIT

    def test_an_empty_series_never_produces_a_direction(self):
        empty = Series((), 60, "EUR/USD")
        quality = DataQuality(ok=False, confidence=0.0, candle_count=0, source="test")
        signal = evaluate(empty, quality=quality)
        assert signal.direction is Direction.WAIT
        assert signal.setup_quality is SetupQuality.INSUFFICIENT

    def test_bad_data_never_yields_high_confidence(self):
        series = pullback_trend(400, direction=1)
        bad = DataQuality(ok=False, confidence=15.0, candle_count=len(series), source="t")
        signal = evaluate(series, quality=bad)
        assert signal.overall_confidence <= 50.0


class TestTimeframeAndDurationAreSeparate:
    def test_the_same_chart_supports_different_durations(self):
        series = pullback_trend(400, direction=1)
        short = evaluate(series, chart_timeframe=60, trade_duration=60)
        long = evaluate(series, chart_timeframe=60, trade_duration=600)
        assert short.chart_timeframe == long.chart_timeframe == 60
        assert short.trade_duration != long.trade_duration
        # Direction is a property of the market; duration fit is not.
        assert short.direction_confidence == pytest.approx(long.direction_confidence)
        assert short.duration_confidence != long.duration_confidence

    def test_labels_render_both_variables_separately(self):
        signal = evaluate(pullback_trend(400, direction=1), chart_timeframe=60, trade_duration=180)
        assert signal.chart_timeframe_label == "1 MIN"
        assert signal.trade_duration_label == "3 MIN"

    def test_a_poorly_matched_duration_downgrades_to_wait(self):
        # A 30-second expiration on a 1-minute chart resolves inside a single
        # candle, which the chart cannot resolve.
        series = pullback_trend(400, direction=1)
        signal = evaluate(series, chart_timeframe=60, trade_duration=30)
        assert signal.duration.selected_score < 65
        assert signal.direction is Direction.WAIT
        # The WAIT must be attributed to the expiration, not to the direction.
        assert "expiration" in signal.reason.lower()
        assert signal.direction_confidence >= 75

    def test_the_direction_is_still_reported_when_duration_blocks_it(self):
        signal = evaluate(pullback_trend(400, direction=1), trade_duration=30)
        assert "CALL" in signal.headline
        assert signal.direction_confidence > 60


class TestDurationEngine:
    def _duration(self, series, direction, selected, timeframe=60):
        mtf = build_multi_timeframe(series, 5, 1)
        return analyze_duration(
            mtf, direction, selected, TRADE_DURATIONS, chart_timeframe_seconds=timeframe
        )

    def test_every_available_duration_is_scored(self):
        result = self._duration(pullback_trend(400, direction=1), Direction.CALL, 180)
        assert len(result.candidates) == len(TRADE_DURATIONS)
        for candidate in result.candidates:
            assert 0.0 <= candidate.score <= 100.0

    def test_the_recommendation_is_the_highest_scoring_candidate(self):
        result = self._duration(pullback_trend(400, direction=1), Direction.CALL, 180)
        best = max(result.candidates, key=lambda c: c.score)
        assert result.recommended_seconds == best.seconds

    def test_a_selected_duration_outside_the_ladder_is_still_scored(self):
        result = self._duration(pullback_trend(400, direction=1), Direction.CALL, 77)
        assert result.selected_seconds == 77
        assert any(c.seconds == 77 for c in result.candidates)

    def test_sub_candle_durations_score_poorly(self):
        result = self._duration(pullback_trend(400, direction=1), Direction.CALL, 30)
        thirty = next(c for c in result.candidates if c.seconds == 30)
        three_minutes = next(c for c in result.candidates if c.seconds == 180)
        assert thirty.score < three_minutes.score

    def test_a_persistent_trend_prefers_a_longer_duration_than_chop(self):
        trend = self._duration(pullback_trend(400, direction=1), Direction.CALL, 180)
        chop = self._duration(choppy_series(400), Direction.CALL, 180)
        assert trend.recommended_seconds >= chop.recommended_seconds

    def test_the_recommendation_is_not_a_fixed_multiple_of_the_timeframe(self):
        # The same chart timeframe must be able to yield different preferred
        # expirations depending on how the market is actually moving.
        fast = self._duration(pullback_trend(400, direction=1, impulse=0.005), Direction.CALL, 180)
        slow = self._duration(ranging_series(400), Direction.CALL, 180)
        assert (fast.recommended_seconds, fast.recommended_score) != (
            slow.recommended_seconds,
            slow.recommended_score,
        )

    def test_the_reason_mentions_the_selected_duration(self):
        result = self._duration(pullback_trend(400, direction=1), Direction.CALL, 180)
        assert format_duration(180).lower() in result.reason.lower()

    def test_fit_labels_track_the_score(self):
        from poa.models import DurationFit

        assert DurationFit.from_score(90) is DurationFit.STRONG_MATCH
        assert DurationFit.from_score(75) is DurationFit.GOOD_MATCH
        assert DurationFit.from_score(60) is DurationFit.QUESTIONABLE
        assert DurationFit.from_score(20) is DurationFit.POOR_MATCH


class TestSetupQuality:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (95, SetupQuality.VERY_STRONG),
            (85, SetupQuality.STRONG),
            (75, SetupQuality.MODERATE),
            (65, SetupQuality.WEAK),
            (40, SetupQuality.INSUFFICIENT),
        ],
    )
    def test_bands_match_the_specification(self, score, expected):
        assert SetupQuality.from_score(score) is expected


class TestLanguageSafety:
    """The assistant must never claim a trade is certain."""

    def _all_text(self, signal) -> str:
        parts = [signal.reason, signal.invalidation, signal.headline]
        parts.extend(signal.why)
        parts.extend(signal.warnings)
        if signal.duration:
            parts.append(signal.duration.reason)
            parts.extend(signal.duration.notes)
        return " ".join(p for p in parts if p)

    @pytest.mark.parametrize(
        "series_factory",
        [
            lambda: pullback_trend(400, direction=1),
            lambda: pullback_trend(400, direction=-1),
            lambda: choppy_series(400),
            lambda: ranging_series(400),
        ],
    )
    def test_no_signal_ever_promises_an_outcome(self, series_factory):
        signal = evaluate(series_factory())
        assert not contains_banned_language(self._all_text(signal))

    def test_the_sanitiser_strips_a_certainty_claim(self):
        from poa.signals.narrative import _sanitise

        assert not contains_banned_language(_sanitise("This is a guaranteed win"))

    def test_probabilistic_wording_is_used_for_strong_setups(self):
        signal = evaluate(pullback_trend(400, direction=1))
        assert "probability" in signal.reason.lower() or "confidence" in signal.reason.lower()


class TestSerialisation:
    def test_a_signal_serialises_to_valid_json(self):
        for series in (pullback_trend(400, direction=1), choppy_series(400)):
            payload = json.dumps(evaluate(series).to_dict())
            # nan and inf are not valid JSON and would break the dashboard.
            assert "NaN" not in payload and "Infinity" not in payload

    def test_the_payload_exposes_direction_and_duration_separately(self):
        data = evaluate(pullback_trend(400, direction=1)).to_dict()
        assert "direction_confidence" in data
        assert "duration_confidence" in data
        assert data["duration"]["recommended_seconds"] > 0

    def test_the_payload_includes_every_scoring_component(self):
        from poa.signals.scoring import WEIGHTS

        data = evaluate(pullback_trend(400, direction=1)).to_dict()
        names = {c["name"] for c in data["score"]["components"]}
        assert names == set(WEIGHTS)
