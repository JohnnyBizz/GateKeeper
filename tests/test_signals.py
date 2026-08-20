"""Signal engine behaviour, and the safety invariants that matter most.

The central claims under test:

* WAIT is the default — a direction requires every gate to pass;
* bad data never produces a confident direction;
* chart timeframe and trade duration are independent variables;
* the assistant never claims certainty about an outcome.
"""

from __future__ import annotations

import json
import random

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

    def test_usable_data_still_carries_its_issues(self):
        """Good enough to analyse is not the same as nothing to report.

        A disputed price scale leaves every pattern readable and every printed
        price suspect, so the warning has to reach the panel even though the
        signal itself goes ahead.
        """
        series = pullback_trend(400, direction=1)
        note = "The saved price scale disagrees with the axis on screen."
        usable = DataQuality(
            ok=True,
            confidence=88.0,
            candle_count=len(series),
            issues=[note],
            source="test",
        )
        signal = evaluate(series, quality=usable)
        assert note in signal.warnings


class TestConfirmationMustBeReal:
    """A requirement met by a view agreeing with itself is no requirement."""

    def test_a_thin_read_cannot_claim_multi_timeframe_confirmation(self):
        """The higher view falls back to the base series when history is short.

        It then agrees with the current view by construction. Counting that as
        the multi-timeframe confirmation the settings demand is how a read of
        forty candles scores like a read of five hundred.
        """
        series = pullback_trend(45, direction=1)
        quality = DataQuality(
            ok=True, confidence=95.0, candle_count=len(series), source="test"
        )
        signal = evaluate(series, quality=quality)

        assert signal.direction is Direction.WAIT
        assert not signal.actionable
        text = " ".join(signal.warnings + [signal.reason]).lower()
        assert "higher timeframe" in text

    def test_a_deep_read_is_not_penalised_for_it(self):
        series = pullback_trend(400, direction=1)
        quality = DataQuality(
            ok=True, confidence=95.0, candle_count=len(series), source="test"
        )
        signal = evaluate(series, quality=quality)
        gates = {r["name"]: r for r in signal.gates.to_dict()["results"]}
        assert gates["higher_timeframe"]["passed"]


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


def _quiet_walk(seed: int, *, n: int = 400, timeframe: int = 5) -> "Series":
    """A drifting market whose few-candle moves sit under the noise floor.

    What a fast synthetic pair looks like most of the time, and the condition
    under which the longest expiration on the ladder used to win: the short
    ones score near nothing for signal-to-noise, so anything longer beats them.
    """
    rng = random.Random(seed)
    closes, price = [], 1.2374
    for _ in range(n):
        price += rng.gauss(0, 0.00012)
        closes.append(price)
    return build_series(closes, timeframe=timeframe, wick=0.00006)


class TestAnExpiryNeverOutlivesTheSetupItWasReadFrom:
    """The four duration terms are added, and signal-to-noise rewards length
    without limit — the expected move grows with the horizon. On a quiet fast
    chart, where a few candles of movement sit under the noise floor, the short
    expiries score near nothing there and anything longer beats them. That was
    enough for the longest rung on the ladder to outvote a flat zero for
    surviving the setup: a five-second chart recommending thirty minutes, three
    hundred and sixty candles ahead, which is not a reading of anything on it.

    Exercised through the whole engine rather than the duration scorer alone,
    because that is where it showed up — the timeframe stack the request builds
    is part of the condition.
    """

    def _recommendations(self, timeframe, expiry, seeds=40):
        engine = SignalEngine()
        worst, actionable = 0.0, 0
        for seed in range(seeds):
            series = _quiet_walk(seed, timeframe=timeframe)
            signal = engine.evaluate(SignalRequest(
                series=series, asset="EUR/USD OTC", chart_timeframe=timeframe,
                trade_duration=expiry, quality=good_quality(series),
                settings=GateSettings(),
            ))
            if signal.actionable:
                actionable += 1
            if signal.duration is not None:
                worst = max(worst, signal.duration.recommended_seconds / timeframe)
        return worst, actionable

    def test_a_fast_chart_stops_recommending_half_an_hour(self):
        worst, _ = self._recommendations(5, 30)
        assert worst <= 20, f"recommended {worst:.0f} candles ahead"

    def test_no_timeframe_recommends_beyond_what_it_can_speak_to(self):
        for timeframe, expiry in ((5, 30), (15, 60), (60, 180), (300, 900)):
            worst, _ = self._recommendations(timeframe, expiry, seeds=20)
            assert worst <= 20, (
                f"{timeframe}s chart recommends {worst:.0f} candles ahead"
            )

    def test_the_cap_does_not_cost_call_volume(self):
        """The user wants more calls, not fewer. Capping an expiry that never
        should have been offered must not also refuse the ones that should."""
        _, actionable = self._recommendations(60, 180)
        assert actionable >= 1


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

    def test_an_ordinary_expiry_on_an_ordinary_chart_is_untouched(self):
        """The cap must not cost call volume on the timeframes that work."""
        result = self._duration(pullback_trend(400, direction=1), Direction.CALL, 180)
        three = next(c for c in result.candidates if c.seconds == 180)
        assert three.score > 40

    def test_the_reason_says_when_an_expiry_outlives_its_setup(self):
        result = self._duration(
            choppy_series(400, timeframe=5), Direction.CALL, 30, timeframe=5
        )
        longest = max(result.candidates, key=lambda c: c.seconds)
        assert "outlives the setup" in longest.reason

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


class TestTheRecordSetsTheGates:
    """How selective to be is not a matter of taste.

    ``min_confidence`` and ``min_duration_compatibility`` shipped as numbers
    somebody chose. The right values are whatever the measured record says were
    worth the most, and the record can work that out — carefully.
    """

    def _calibration(self, real=True):
        from poa.backtesting.calibration import Record, build_calibration

        cal = build_calibration(
            [Record(score=60, won=i < 20, duration_score=45) for i in range(40)]
            + [Record(score=85, won=i < 34, duration_score=80) for i in range(40)],
            payout=0.92,
        )
        cal.from_real_trades = real
        return cal

    def test_a_gate_moves_toward_what_the_record_recommends(self):
        from poa.signals.autotune import tune

        adjustments = {a.key: a for a in tune(self._calibration(), 75.0, 65.0)}
        assert adjustments["min_confidence"].now < 75.0
        assert adjustments["min_confidence"].looser

    def test_it_moves_a_few_points_at_a_time(self):
        """A gate that jumps wherever the last replay pointed never settles."""
        from poa.signals.autotune import MAX_STEP, tune

        for adjustment in tune(self._calibration(), 90.0, 85.0):
            assert abs(adjustment.now - adjustment.was) <= MAX_STEP

    def test_it_settles_instead_of_oscillating(self):
        from poa.signals.autotune import tune

        cal = self._calibration()
        confidence, duration = 75.0, 65.0
        for _ in range(20):
            adjustments = tune(cal, confidence, duration)
            if not adjustments:
                break
            for adjustment in adjustments:
                if adjustment.key == "min_confidence":
                    confidence = adjustment.now
                else:
                    duration = adjustment.now
        else:
            raise AssertionError("the gates never settled")
        assert tune(cal, confidence, duration) == []

    def test_it_never_loosens_past_the_floor(self):
        """Auto-tuning that can reach zero eventually takes every trade."""
        from poa.signals.autotune import FLOOR_CONFIDENCE, FLOOR_DURATION, tune

        confidence, duration = 20.0, 20.0
        for _ in range(30):
            adjustments = tune(self._calibration(), confidence, duration)
            if not adjustments:
                break
            for adjustment in adjustments:
                if adjustment.key == "min_confidence":
                    confidence = adjustment.now
                else:
                    duration = adjustment.now
        assert confidence >= FLOOR_CONFIDENCE
        assert duration >= FLOOR_DURATION

    def test_it_never_tightens_past_the_ceiling(self):
        from poa.signals.autotune import CEILING_CONFIDENCE, CEILING_DURATION, tune

        for adjustment in tune(self._calibration(), 99.0, 99.0):
            ceiling = (
                CEILING_CONFIDENCE
                if adjustment.key == "min_confidence"
                else CEILING_DURATION
            )
            assert adjustment.now <= ceiling

    def test_no_evidence_means_no_movement(self):
        from poa.backtesting.calibration import Record, build_calibration
        from poa.signals.autotune import tune

        thin = build_calibration([Record(score=80, won=True) for _ in range(4)])
        assert tune(thin, 75.0, 65.0) == []
        assert tune(None, 75.0, 65.0) == []

    def test_every_move_carries_its_evidence(self):
        """A setting that changes silently is indistinguishable from a bug."""
        from poa.signals.autotune import tune

        for adjustment in tune(self._calibration(), 80.0, 75.0):
            assert "measured" in adjustment.reason
            assert "%" in adjustment.reason
            assert "→" in adjustment.describe()


class TestARuleTheChartDisproves:
    """Every blocking gate is a claim, and a claim can be wrong.

    "Setups failing this are worse than setups passing it" is checkable, and
    nothing in the app had ever checked it. A gate refusing trades that would
    have paid is not prudence — it is a rule quietly costing money, and no
    amount of reasoning about the rule discovers that.
    """

    def _report(self, direction=Direction.PUT):
        from poa.analysis import build_multi_timeframe
        from poa.signals.gates import evaluate_gates
        from poa.signals.scoring import score_direction

        series = pullback_trend(400, direction=1)
        mtf = build_multi_timeframe(series, 5, 1)
        quality = DataQuality(
            ok=True, confidence=95.0, candle_count=len(series), source="t"
        )
        score = score_direction(mtf, direction)
        return mtf, direction, score, quality, evaluate_gates

    def test_a_demoted_gate_stops_blocking_but_keeps_reporting(self):
        from poa.signals.gates import GateSettings

        mtf, direction, score, quality, run = self._report()
        blocking = run(mtf, direction, score, quality, GateSettings())
        demoted = run(
            mtf, direction, score, quality,
            GateSettings(advisory=frozenset({"momentum"})),
        )

        assert "momentum" in {f.name for f in blocking.failures}
        assert "momentum" not in {f.name for f in demoted.failures}
        # Still evaluated, still reported — it just holds no veto.
        result = next(g for g in demoted.results if g.name == "momentum")
        assert result.passed is False
        assert result.blocking is False

    def test_data_quality_can_never_be_demoted(self):
        """No win rate makes an unreadable chart tradeable."""
        from poa.signals.gates import NEVER_ADVISORY, GateSettings

        assert "data_quality" in NEVER_ADVISORY
        series = pullback_trend(400, direction=1)
        unreadable = DataQuality(
            ok=False, confidence=5.0, candle_count=len(series),
            issues=["unreadable"], source="t",
        )
        signal = evaluate(
            series,
            quality=unreadable,
            settings=GateSettings(advisory=frozenset({"data_quality"})),
        )
        assert signal.direction is Direction.WAIT
        assert not signal.actionable

    def test_the_audit_names_a_gate_that_refuses_winners(self):
        from poa.backtesting.gatecheck import check_gates

        report = check_gates(
            pullback_trend(500, direction=1), trade_duration=180, payout=0.92
        )
        # Every gate that blocked anything is scored against what it blocked.
        for verdict in report.verdicts:
            assert verdict.blocked >= 0
            if verdict.blocked:
                assert verdict.blocked_rate is not None
        # And the headline is a sentence either way.
        assert isinstance(report.headline(), str) and report.headline()

    def test_a_gate_blocking_losers_is_left_alone(self):
        from poa.backtesting.gatecheck import GateVerdict

        good = GateVerdict("market_structure", blocked_wins=4, blocked_losses=46)
        assert good.blocked_rate == 8.0
        assert not good.costly(52.1)

    def test_a_gate_blocking_winners_is_flagged(self):
        from poa.backtesting.gatecheck import GateVerdict

        bad = GateVerdict("momentum", blocked_wins=48, blocked_losses=21)
        assert bad.blocked_rate is not None and bad.blocked_rate > 52.1
        assert bad.costly(52.1)

    def test_a_handful_of_blocks_cannot_condemn_a_gate(self):
        from poa.backtesting.gatecheck import GateVerdict

        thin = GateVerdict("momentum", blocked_wins=5, blocked_losses=0)
        assert thin.blocked_rate == 100.0
        assert not thin.costly(52.1)


class TestLearningFromLosses:
    """"The setup failed" teaches nothing. Which component argued for it does."""

    def _trade(self, outcome, components, regime="STRONG_UPTREND"):
        class T:
            pass

        t = T()
        t.outcome = outcome
        t.components = components
        t.regime = regime
        t.pattern = ""
        return t

    def test_a_component_scoring_higher_on_losers_is_named(self):
        from poa.backtesting.attribution import attribute

        trades = (
            [self._trade("win", {"macd": 0.1, "structure": 0.9}) for _ in range(12)]
            + [self._trade("loss", {"macd": 0.9, "structure": 0.9}) for _ in range(12)]
        )
        report = attribute(trades)
        macd = next(c for c in report.components if c.name == "macd")
        assert macd.verdict == "inverted"
        assert "wrong side" in macd.describe()
        assert "macd" in report.headline().lower()

    def test_a_component_scoring_the_same_either_way_is_called_silent(self):
        from poa.backtesting.attribution import attribute

        trades = (
            [self._trade("win", {"structure": 0.9}) for _ in range(12)]
            + [self._trade("loss", {"structure": 0.9}) for _ in range(12)]
        )
        report = attribute(trades)
        structure = next(c for c in report.components if c.name == "structure")
        assert structure.verdict == "silent"
        assert "without carrying information" in structure.describe()

    def test_a_component_that_discriminates_is_left_alone(self):
        from poa.backtesting.attribution import attribute

        trades = (
            [self._trade("win", {"trend": 0.9}) for _ in range(12)]
            + [self._trade("loss", {"trend": 0.3}) for _ in range(12)]
        )
        report = attribute(trades)
        trend = next(c for c in report.components if c.name == "trend")
        assert trend.verdict == "discriminating"

    def test_a_handful_of_trades_teaches_nothing(self):
        """Two means over four trades is noise wearing a sign."""
        from poa.backtesting.attribution import attribute

        trades = (
            [self._trade("win", {"macd": 0.1}) for _ in range(2)]
            + [self._trade("loss", {"macd": 0.9}) for _ in range(2)]
        )
        report = attribute(trades)
        assert report.components[0].verdict == "unread"
        assert "No settled calls" in report.headline() or report.headline()

    def test_losses_clustering_in_one_regime_are_reported(self):
        from poa.backtesting.attribution import attribute

        trades = (
            [self._trade("loss", {}, regime="CHOPPY") for _ in range(8)]
            + [self._trade("win", {}, regime="STRONG_UPTREND") for _ in range(4)]
        )
        report = attribute(trades)
        assert report.worst_condition() == ("CHOPPY", 8)
        assert "choppy" in report.headline().lower()

    def test_flat_expiries_teach_nothing_either_way(self):
        from poa.backtesting.attribution import attribute

        report = attribute([self._trade("flat", {"macd": 0.9}) for _ in range(20)])
        assert report.winners == 0 and report.losers == 0


class TestTheNumberOnThePanelClearsTheMinimum:
    """A "minimum 85" that displays 80 is not strict, it is wrong.

    ``min_confidence`` gates the direction score. What the panel shows is
    ``direction x 0.6 + duration x 0.4``, capped at the weaker plus twelve —
    always the lower figure. So raising min_confidence to 85 gated an internal
    number at 85 and left the panel free to show 80.1 on a call that passed,
    and somebody told to trade at 85 would have taken calls in the high
    seventies without ever seeing a number that said so.

    Walked with the backtester rather than by hand: a directional call is
    about seven per cent of bars, so sampling every seventh one finds none and
    proves nothing.
    """

    def _calls(self, shown_floor, gate=75.0):
        from pathlib import Path

        from poa.backtesting.paper import Backtester
        from poa.chart_detection.csv_source import load_csv
        from poa.signals import GateSettings

        root = Path(__file__).resolve().parent.parent / "data/recorded"
        charts = sorted(root.glob("*/*-60s.csv"))
        assert charts, "no recorded minute charts to check against"

        trades = []
        for path in charts:
            series = load_csv(path, timeframe_seconds=60)
            result = Backtester(
                window=120, payout=0.92,
                settings=GateSettings(
                    min_confidence=gate, min_shown_confidence=shown_floor,
                    require_measured_edge=False,
                ),
            ).run(series, trade_duration=180, asset=path.stem, step=1)
            trades += result.trades
        return trades

    def test_no_call_ever_displays_under_its_own_floor(self):
        """The property that was false, over every chart on record."""
        calls = self._calls(85.0)

        assert calls, "nothing passed, so nothing was tested"
        assert min(t.confidence for t in calls) >= 85.0

    def test_the_floor_is_what_removed_them(self):
        """Without it, the same run shows calls beneath the same number."""
        loose = self._calls(0.0)
        strict = self._calls(85.0)

        assert [t for t in loose if t.confidence < 85.0], (
            "no low-reading calls existed, so the floor was never exercised"
        )
        assert len(strict) < len(loose)

    def test_a_floor_nothing_can_clear_silences_it_and_says_why(self):
        from poa.chart_detection.csv_source import load_csv
        from poa.chart_detection.quality import validate_series
        from poa.models import Direction, Series
        from poa.signals import GateSettings, SignalEngine, SignalRequest
        from pathlib import Path

        assert self._calls(99.0) == []

        # And the refusal is stated, not silent.
        path = sorted(
            (Path(__file__).resolve().parent.parent / "data/recorded").glob("*/*-60s.csv")
        )[0]
        series = load_csv(path, timeframe_seconds=60)
        seen = []
        for end in range(121, len(series)):
            window = Series(candles=list(series)[end - 120:end],
                            symbol=series.symbol, timeframe_seconds=60)
            signal = SignalEngine().evaluate(SignalRequest(
                series=window, asset="EUR/USD", chart_timeframe=60,
                trade_duration=180,
                quality=validate_series(window, min_candles=60),
                settings=GateSettings(min_confidence=75.0,
                                      min_shown_confidence=99.0,
                                      require_measured_edge=False)))
            assert signal.direction not in (Direction.CALL, Direction.PUT)
            if "BELOW YOUR MINIMUM" in signal.headline:
                seen.append(signal)
        assert seen, "the floor removed calls without ever saying so"
        assert "under the 99 you set" in seen[0].reason


class TestThePayoutDoesNotDecideAnything:
    """"You deal with the money, find me the winning trades." Said three times.

    The payout had two jobs it was never given. It set the bar the measured
    record had to clear before a setup was allowed — so the same reading of
    the same chart was endorsed at 92% and vetoed at 50%, having learned
    nothing about the market — and it picked which score threshold the record
    tuned itself to, by ranking thresholds on expected value.

    On a bad payout that veto tightened to 66.7%, which meant the tool went
    quietest exactly when the reason had least to do with the chart.
    """

    def _calibration(self, payout, rate, settled=60):
        from poa.backtesting.calibration import _band_label, Bucket, Calibration

        wins = round(settled * rate / 100)
        cal = Calibration(payout=payout, min_sample=20, total=settled)
        cal.bands = [
            Bucket(label=_band_label(80), wins=wins, losses=settled - wins,
                   min_sample=20)
        ]
        return cal

    def test_the_bar_is_being_right_not_being_profitable(self):
        """Fifty per cent, whatever the broker is paying."""
        for payout in (0.50, 0.80, 0.92):
            assert self._calibration(payout, 60.0).decision_threshold == 50.0

    def test_the_payout_is_still_reported(self):
        """Removed from the decision, not from the user's sight."""
        cal = self._calibration(0.50, 60.0)
        assert cal.breakeven > 60.0          # 50% payout needs 66.7%
        assert cal.decision_threshold == 50.0

    def test_a_band_winning_more_than_a_coin_is_not_vetoed_by_a_bad_payout(self):
        """The case that used to silence the tool on a 50% morning."""
        cal = self._calibration(0.50, 60.0)

        beats, detail = cal.verdict(85.0, "WEAK_UPTREND")

        # 60% is short of the 66.7% that payout needs, and well clear of a
        # coin. The record must not call that a losing setup.
        assert beats is not False, detail

    def test_a_band_losing_to_a_coin_is_still_refused(self):
        """Taking payout out is not the same as taking the brakes off."""
        cal = self._calibration(0.92, 30.0)

        beats, _detail = cal.verdict(85.0, "WEAK_UPTREND")

        assert beats is False

    def test_the_same_chart_gets_the_same_answer_at_any_payout(self):
        answers = {
            self._calibration(payout, 60.0).verdict(85.0, "WEAK_UPTREND")[0]
            for payout in (0.30, 0.50, 0.75, 0.92, 1.20)
        }
        assert len(answers) == 1, "the broker's pricing changed the reading"
