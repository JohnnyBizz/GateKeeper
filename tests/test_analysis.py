"""Heikin Ashi, market structure, levels, volatility, regime and resampling."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import (
    START,
    build_series,
    choppy_series,
    ranging_series,
    trending_series,
)
from poa.analysis import (
    analyze_heikin_ashi,
    analyze_momentum,
    analyze_structure,
    analyze_timeframe,
    analyze_volatility,
    build_multi_timeframe,
    detect_levels,
    find_swings,
    heikin_ashi,
    resample,
)
from poa.indicators import compute_indicators
from poa.models import Bias, Candle, LevelKind, Regime, Series


def zigzag(
    direction: int = 1,
    legs: int = 10,
    leg_length: int = 5,
    advance: float = 0.0012,
    swing: float = 0.0020,
    start: float = 1.05,
) -> Series:
    """A staircase that genuinely alternates: rally, pull back, rally higher.

    Each leg walks price up (or down) then retraces part of it, so the fractal
    detector sees unambiguous alternating swing highs and swing lows.
    """
    closes: list[float] = [start]
    price = start
    for leg in range(legs):
        target = price + direction * swing
        for i in range(leg_length):
            closes.append(price + (target - price) * (i + 1) / leg_length)
        price = target
        pullback = price - direction * (swing - advance)
        for i in range(leg_length):
            closes.append(price + (pullback - price) * (i + 1) / leg_length)
        price = pullback
    return build_series(closes, wick=0.00005)


class TestHeikinAshiConversion:
    def test_close_is_the_average_of_ohlc(self):
        series = build_series([1.0, 1.001, 1.002])
        ha = heikin_ashi(series)
        first = series[0]
        expected = (first.open + first.high + first.low + first.close) / 4
        assert ha[0].close == pytest.approx(expected)

    def test_open_is_the_midpoint_of_the_previous_ha_candle(self):
        series = build_series([1.0, 1.001, 1.002, 1.003])
        ha = heikin_ashi(series)
        assert ha[2].open == pytest.approx((ha[1].open + ha[1].close) / 2)

    def test_length_is_preserved(self):
        series = trending_series(50)
        assert len(heikin_ashi(series)) == len(series)

    def test_empty_series_round_trips(self):
        empty = Series((), 60, "X")
        assert len(heikin_ashi(empty)) == 0

    def test_ha_candles_are_internally_consistent(self):
        ha = heikin_ashi(trending_series(60))
        for candle in ha:
            assert candle.high >= max(candle.open, candle.close)
            assert candle.low <= min(candle.open, candle.close)


class TestHeikinAshiReading:
    def test_a_steady_uptrend_reads_bullish_with_a_streak(self):
        reading = analyze_heikin_ashi(trending_series(60, step=0.0003))
        assert reading.bias is Bias.BULLISH
        assert reading.streak >= 3
        assert reading.strength > 0.4

    def test_a_steady_downtrend_reads_bearish(self):
        reading = analyze_heikin_ashi(trending_series(60, step=-0.0003))
        assert reading.bias is Bias.BEARISH
        assert reading.streak >= 3

    def test_a_single_candle_is_never_read_in_isolation(self):
        # Three bars is the documented minimum; below that the reader must
        # decline rather than guess from one candle.
        reading = analyze_heikin_ashi(build_series([1.0, 1.001]))
        assert reading.bias is Bias.NEUTRAL
        assert "Not enough" in reading.notes[0]

    def test_contracting_bodies_flag_weakening_momentum(self):
        # A strong push that decays to a crawl.
        closes = [1.0 + 0.0006 * i for i in range(10)]
        closes += [closes[-1] + 0.00002 * i for i in range(6)]
        reading = analyze_heikin_ashi(build_series(closes))
        assert reading.bias is Bias.BULLISH
        assert reading.momentum_weakening
        assert reading.body_trend == "contracting"

    def test_expanding_bodies_are_reported(self):
        closes = [1.0]
        for i in range(14):
            closes.append(closes[-1] + 0.00005 * (i + 1))
        reading = analyze_heikin_ashi(build_series(closes))
        assert reading.body_trend == "expanding"

    def test_confirms_requires_the_matching_direction(self):
        reading = analyze_heikin_ashi(trending_series(60, step=0.0003))
        assert reading.confirms(Bias.BULLISH)
        assert not reading.confirms(Bias.BEARISH)
        assert not reading.confirms(Bias.NEUTRAL)

    def test_weakening_run_is_described_as_such(self):
        closes = [1.0 + 0.0006 * i for i in range(10)]
        closes += [closes[-1] + 0.00001 * i for i in range(6)]
        reading = analyze_heikin_ashi(build_series(closes))
        # The flag is what the engine consumes; the wording just has to convey
        # that the run is no longer healthy.
        assert reading.momentum_weakening
        assert any(
            word in reading.pattern for word in ("losing momentum", "indecisive")
        )


class TestStructure:
    def test_swings_are_found_at_local_extremes(self):
        closes = [1.0, 1.002, 1.004, 1.002, 1.0, 1.002, 1.005]
        swings = find_swings(build_series(closes), left=2, right=2)
        assert any(s.kind == "high" for s in swings)

    def test_no_swings_in_a_series_shorter_than_the_window(self):
        assert find_swings(build_series([1.0, 1.001]), 2, 2) == []

    def test_uptrend_prints_higher_highs_and_higher_lows(self):
        reading = analyze_structure(zigzag(direction=1))
        assert reading.bias is Bias.BULLISH
        assert reading.higher_highs and reading.higher_lows
        assert "higher highs" in reading.label

    def test_downtrend_prints_lower_highs_and_lower_lows(self):
        reading = analyze_structure(zigzag(direction=-1))
        assert reading.bias is Bias.BEARISH
        assert reading.lower_highs and reading.lower_lows

    def test_insufficient_swings_produce_a_neutral_reading(self):
        reading = analyze_structure(build_series([1.0, 1.001, 1.002, 1.003]))
        assert reading.bias is Bias.NEUTRAL
        assert reading.strength == 0.0

    def test_a_range_does_not_read_as_a_trend(self):
        reading = analyze_structure(ranging_series(200))
        assert reading.strength < 0.7


class TestLevels:
    def test_levels_are_split_around_the_current_price(self):
        reading = detect_levels(ranging_series(220))
        price = reading.price
        for level in reading.levels:
            if level.kind is LevelKind.SUPPORT:
                assert level.price <= price + 1e-9
            else:
                assert level.price >= price - 1e-9

    def test_strength_is_bounded(self):
        for level in detect_levels(ranging_series(220)).levels:
            assert 0.0 <= level.strength <= 100.0

    def test_a_repeatedly_tested_level_scores_higher_than_a_one_off(self):
        # Price bounces off 1.0800 four times, and touches 1.0700 once.
        closes = [1.07]
        for _ in range(4):
            closes.extend([1.075, 1.0800, 1.0790, 1.076, 1.0785])
        closes.extend([1.0800, 1.0795, 1.078])
        reading = detect_levels(build_series(closes))
        assert reading.levels
        strongest = max(reading.levels, key=lambda lv: lv.strength)
        assert strongest.zone_low <= 1.0800 <= strongest.zone_high

    def test_distance_is_zero_inside_the_zone(self):
        reading = detect_levels(ranging_series(220))
        for level in reading.levels:
            midpoint = (level.zone_low + level.zone_high) / 2
            assert level.distance_from(midpoint) == 0.0

    def test_short_series_yields_no_levels(self):
        reading = detect_levels(build_series([1.0, 1.001, 1.002]))
        assert reading.levels == []
        assert reading.nearest_support is None


class TestVolatility:
    def test_a_clean_trend_is_directionally_efficient(self):
        series = trending_series(120, step=0.0003, noise=0.0)
        reading = analyze_volatility(series, compute_indicators(series))
        assert reading.directional_efficiency > 0.7
        assert not reading.choppy

    def test_chop_is_flagged_as_inefficient(self):
        series = choppy_series(120)
        reading = analyze_volatility(series, compute_indicators(series))
        assert reading.directional_efficiency < 0.3
        assert reading.choppy

    def test_consistency_is_high_when_every_candle_agrees(self):
        series = trending_series(120, step=0.0003, noise=0.0)
        reading = analyze_volatility(series, compute_indicators(series))
        assert reading.consistency > 0.9

    def test_too_short_a_series_is_reported_as_unknown(self):
        series = build_series([1.0, 1.001])
        reading = analyze_volatility(series, compute_indicators(series))
        assert reading.regime == "unknown"
        assert reading.choppy


class TestRegime:
    def test_a_clean_uptrend_classifies_as_an_uptrend(self):
        analysis = analyze_timeframe(trending_series(300, step=0.00025))
        assert analysis.regime.regime in (Regime.STRONG_UPTREND, Regime.WEAK_UPTREND)
        assert analysis.regime.bias is Bias.BULLISH

    def test_a_clean_downtrend_classifies_as_a_downtrend(self):
        analysis = analyze_timeframe(trending_series(300, step=-0.00025))
        assert analysis.regime.regime in (Regime.STRONG_DOWNTREND, Regime.WEAK_DOWNTREND)

    def test_chop_never_classifies_as_a_tradeable_trend(self):
        analysis = analyze_timeframe(choppy_series(300))
        assert analysis.regime.regime not in (
            Regime.STRONG_UPTREND,
            Regime.STRONG_DOWNTREND,
        )

    def test_unclear_and_high_volatility_are_not_tradeable(self):
        assert not Regime.UNCLEAR.tradeable
        assert not Regime.HIGH_VOLATILITY.tradeable
        assert Regime.STRONG_UPTREND.tradeable


class TestMomentum:
    @staticmethod
    def _walk(steps: list[float], start: float = 1.0) -> list[float]:
        closes = [start]
        for step in steps:
            closes.append(closes[-1] + step)
        return closes

    def test_accelerating_move_is_detected(self):
        # A slow drift that breaks into a sharp run over the last few bars.
        closes = self._walk([0.00001] * 25 + [0.0008] * 3)
        series = build_series(closes)
        reading = analyze_momentum(series, compute_indicators(series))
        assert reading.bias is Bias.BULLISH
        assert reading.accelerating
        assert not reading.decelerating

    def test_decelerating_move_is_detected(self):
        # A strong run that stalls.
        closes = self._walk([0.0006] * 25 + [0.000005] * 3)
        series = build_series(closes)
        reading = analyze_momentum(series, compute_indicators(series))
        assert reading.bias is Bias.BULLISH
        assert reading.decelerating
        assert not reading.accelerating

    def test_a_flat_market_reads_neutral(self):
        series = build_series([1.0] * 40)
        reading = analyze_momentum(series, compute_indicators(series))
        assert reading.bias is Bias.NEUTRAL
        assert reading.label == "flat"


class TestResample:
    def test_aggregation_reduces_the_candle_count(self):
        series = trending_series(120, timeframe=60)
        higher = resample(series, 300)
        assert higher.timeframe_seconds == 300
        assert len(higher) <= len(series) // 5 + 1

    def test_aggregated_ohlc_matches_the_group(self):
        series = trending_series(60, timeframe=60)
        higher = resample(series, 300)
        first = higher[0]
        members = [c for c in series if c.timestamp < first.timestamp + timedelta(seconds=300)]
        assert first.open == pytest.approx(members[0].open)
        assert first.high == pytest.approx(max(c.high for c in members))
        assert first.low == pytest.approx(min(c.low for c in members))
        assert first.close == pytest.approx(members[-1].close)

    def test_downsampling_is_refused(self):
        series = trending_series(60, timeframe=300)
        assert resample(series, 60) is series

    def test_non_multiple_targets_are_refused(self):
        series = trending_series(60, timeframe=60)
        assert resample(series, 90) is series

    def test_buckets_are_anchored_to_wall_clock_boundaries(self):
        series = trending_series(120, timeframe=60)
        higher = resample(series, 300)
        for candle in higher:
            assert int(candle.timestamp.timestamp()) % 300 == 0

    def test_volume_is_summed_when_present(self):
        series = trending_series(60, timeframe=60)
        higher = resample(series, 300)
        assert higher[0].volume is not None
        assert higher[0].volume >= series[0].volume


class TestMultiTimeframe:
    def test_three_views_are_produced(self):
        mtf = build_multi_timeframe(trending_series(400, step=0.0002), 5, 1)
        assert mtf.higher.timeframe_seconds >= mtf.current.timeframe_seconds
        assert mtf.entry.timeframe_seconds <= mtf.current.timeframe_seconds

    def test_a_consistent_trend_produces_high_agreement(self):
        mtf = build_multi_timeframe(trending_series(500, step=0.0003), 5, 1)
        assert mtf.consensus is Bias.BULLISH
        assert mtf.agreement > 0.5

    def test_conflict_with_the_higher_timeframe_is_detected(self):
        mtf = build_multi_timeframe(trending_series(500, step=0.0003), 5, 1)
        # The stack is bullish, so a bearish entry conflicts with it.
        assert mtf.conflicts_with_higher(Bias.BEARISH)
        assert not mtf.conflicts_with_higher(Bias.BULLISH)

    def test_reversal_is_not_confirmed_inside_a_healthy_trend(self):
        mtf = build_multi_timeframe(trending_series(500, step=0.0003), 5, 1)
        assert not mtf.reversal_confirmed(Bias.BEARISH)

    def test_falls_back_when_aggregation_leaves_too_few_candles(self):
        # 80 base candles aggregated 5:1 leaves 16, below the usable minimum.
        mtf = build_multi_timeframe(trending_series(80, step=0.0002), 5, 1)
        assert len(mtf.higher.series) >= 30 or mtf.higher is mtf.current

    def test_a_stand_in_higher_timeframe_says_so(self):
        """Agreement with a copy of the current view is not agreement.

        40 candles cannot aggregate into any usable higher view, so ``higher``
        becomes the base series again — and anything reading that as
        independent confirmation is reading one chart twice.
        """
        thin = build_multi_timeframe(trending_series(40, step=0.0002), 5, 1)
        assert thin.higher is thin.current
        assert thin.higher_is_distinct is False

        deep = build_multi_timeframe(trending_series(500, step=0.0003), 5, 1)
        assert deep.higher is not deep.current
        assert deep.higher_is_distinct is True


class TestHowFarTheMoveHasAlreadyGone:
    """The readings that do not say which way.

    Seventy of the score's hundred points measure direction of travel, five
    ways, and agree with each other by construction. These are unsigned on
    purpose: a market stretched far above its centre and one stretched far
    below score the same. That is the only reason adding them could change an
    answer rather than restate one.
    """

    def _series(self, closes, timeframe=5):
        from datetime import datetime, timedelta, timezone

        from poa.models import Candle, Series

        start = datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc)
        candles = [
            Candle(
                timestamp=start + timedelta(seconds=timeframe * i),
                open=c, high=c + 0.02, low=c - 0.02, close=c, volume=1.0,
            )
            for i, c in enumerate(closes)
        ]
        return Series(candles, timeframe, "TEST")

    def test_a_run_up_and_the_same_run_down_read_alike(self):
        """Unsigned is the whole point. If these could tell up from down they
        would be a sixth copy of the trend."""
        from poa.analysis import analyze_extension

        up = analyze_extension(self._series([100 + i * 0.1 for i in range(40)]))
        down = analyze_extension(self._series([100 - i * 0.1 for i in range(40)]))
        assert round(up.stretch, 2) == round(down.stretch, 2)
        assert up.run_length == down.run_length
        assert up.signed_stretch > 0 and down.signed_stretch < 0

    def test_a_market_going_nowhere_is_not_stretched(self):
        from poa.analysis import analyze_extension

        flat = [100.0 + (0.01 if i % 2 else -0.01) for i in range(40)]
        assert analyze_extension(self._series(flat)).stretch < 1.0

    def test_a_long_climb_is_stretched(self):
        from poa.analysis import analyze_extension

        reading = analyze_extension(self._series([100 + i * 0.1 for i in range(40)]))
        assert reading.stretch > 1.0
        assert reading.run_length >= 5

    def test_the_run_is_the_one_ending_now(self):
        """Twenty up then three down is a run of three, not twenty-three."""
        from poa.analysis import analyze_extension

        closes = [100 + i * 0.1 for i in range(20)] + [101.8, 101.7, 101.6]
        assert analyze_extension(self._series(closes)).run_length == 3

    def test_a_price_repeated_does_not_break_the_run(self):
        """A bar closing exactly where the last one did has gone neither way,
        and counting it as a change would end a run that never turned."""
        from poa.analysis import analyze_extension

        closes = [100 + i * 0.1 for i in range(20)] + [101.9, 102.0, 102.1]
        assert analyze_extension(self._series(closes)).run_length == 21

    def test_a_series_too_short_says_so_rather_than_raising(self):
        """An exception in the analysis path stops a panel that could still
        show a price."""
        from poa.analysis import analyze_extension

        reading = analyze_extension(self._series([100.0, 100.1, 100.2]))
        assert reading.stretch == 0.0
        assert "not enough history" in " ".join(reading.notes)

    def test_it_survives_a_series_that_never_moves(self):
        from poa.analysis import analyze_extension

        reading = analyze_extension(self._series([100.0] * 40))
        assert reading.run_length == 0
        assert reading.stretch == 0.0

    def test_nothing_here_is_wired_into_the_score(self):
        """It has not earned a weight against real outcomes yet, and
        FINDINGS.md records that extension did not predict on the recordings."""
        from poa.signals.scoring import WEIGHTS

        for name in WEIGHTS:
            assert "stretch" not in name and "extension" not in name


class TestTheHorizonMeasurement:
    def test_a_market_that_always_rises_reads_as_always_rising(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from horizon import measure

        series = TestHowFarTheMoveHasAlreadyGone()._series(
            [100 + i * 0.05 for i in range(200)]
        )
        counts = measure(series, bars=6)
        total = sum(t for t, _ in counts.values())
        ups = sum(u for _, u in counts.values())
        assert total > 0 and ups == total

    def test_the_buckets_are_fixed_in_advance_and_tile(self):
        """A bucket edge chosen after seeing the outcomes finds an edge in
        noise, which is the mistake the whole file exists to prevent."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from horizon import BUCKETS

        for (_, high, _), (low, _, _) in zip(BUCKETS, BUCKETS[1:]):
            assert low == high, "the buckets must tile without a gap or overlap"

    def test_a_tie_decides_nothing_either_way(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from horizon import measure

        series = TestHowFarTheMoveHasAlreadyGone()._series([100.0] * 200)
        assert sum(t for t, _ in measure(series, bars=6).values()) == 0
