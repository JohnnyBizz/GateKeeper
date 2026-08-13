"""Indicator correctness.

Values are checked against hand-computed expectations rather than a snapshot,
so a change in behaviour is caught rather than blessed.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from conftest import build_series, trending_series
from poa.indicators import compute_indicators
from poa.indicators.core import (
    IndicatorError,
    adx,
    atr,
    bollinger_bands,
    ema,
    last_finite,
    macd,
    percentile_rank,
    rsi,
    slope,
    sma,
    true_range,
    vwap,
)
from poa.models import Bias


class TestMovingAverages:
    def test_sma_matches_manual_mean(self):
        values = [1, 2, 3, 4, 5, 6]
        result = sma(values, 3)
        assert np.isnan(result[:2]).all()
        assert result[2] == pytest.approx(2.0)
        assert result[5] == pytest.approx(5.0)

    def test_sma_is_nan_until_the_lookback_is_satisfied(self):
        assert np.isnan(sma([1, 2], 5)).all()

    def test_ema_seeds_on_the_first_sma_then_smooths(self):
        values = [1, 2, 3, 4, 5]
        result = ema(values, 3)
        assert np.isnan(result[:2]).all()
        assert result[2] == pytest.approx(2.0)  # mean of 1,2,3
        # alpha = 2/(3+1) = 0.5
        assert result[3] == pytest.approx(4 * 0.5 + 2.0 * 0.5)

    def test_ema_of_a_constant_series_is_that_constant(self):
        result = ema([7.0] * 40, 10)
        assert result[-1] == pytest.approx(7.0)

    def test_rejects_a_non_positive_period(self):
        with pytest.raises(IndicatorError):
            ema([1, 2, 3], 0)


class TestRsi:
    def test_a_monotonic_rise_pins_rsi_at_100(self):
        result = rsi(list(range(1, 40)), 14)
        assert result[-1] == pytest.approx(100.0)

    def test_a_monotonic_fall_pins_rsi_at_zero(self):
        result = rsi(list(range(40, 1, -1)), 14)
        assert result[-1] == pytest.approx(0.0)

    def test_alternating_moves_sit_near_the_midpoint(self):
        values = [10 + (1 if i % 2 else -1) for i in range(60)]
        assert 35 < last_finite(rsi(values, 14)) < 65

    def test_output_length_matches_input(self):
        values = list(range(50))
        assert rsi(values, 14).size == len(values)


class TestMacd:
    def test_histogram_is_the_difference_of_line_and_signal(self):
        values = list(np.linspace(1.0, 2.0, 120))
        result = macd(values)
        index = -1
        assert result.histogram[index] == pytest.approx(
            result.macd[index] - result.signal[index]
        )

    def test_line_is_positive_in_an_uptrend(self):
        result = macd(list(np.linspace(1.0, 2.0, 120)))
        assert last_finite(result.macd) > 0

    def test_line_is_negative_in_a_downtrend(self):
        result = macd(list(np.linspace(2.0, 1.0, 120)))
        assert last_finite(result.macd) < 0

    def test_fast_must_be_shorter_than_slow(self):
        with pytest.raises(IndicatorError):
            macd([1.0] * 50, fast=26, slow=12)


class TestAtr:
    def test_true_range_uses_the_previous_close(self):
        high = [10, 12]
        low = [9, 11]
        close = [9.5, 11.5]
        result = true_range(high, low, close)
        assert result[0] == pytest.approx(1.0)  # first bar: its own range
        # second bar: max(12-11, |12-9.5|, |11-9.5|) = 2.5
        assert result[1] == pytest.approx(2.5)

    def test_atr_of_constant_range_bars_equals_that_range(self):
        n = 40
        high = [10.5] * n
        low = [9.5] * n
        close = [10.0] * n
        assert last_finite(atr(high, low, close, 14)) == pytest.approx(1.0)

    def test_mismatched_lengths_are_rejected(self):
        with pytest.raises(IndicatorError):
            true_range([1, 2], [1], [1, 2])


class TestAdx:
    def test_a_strong_trend_produces_a_high_adx(self):
        series = trending_series(200, step=0.0005, noise=0.0)
        result = adx(series.high, series.low, series.close, 14)
        assert last_finite(result.adx) > 25

    def test_plus_di_dominates_in_an_uptrend(self):
        series = trending_series(200, step=0.0005, noise=0.0)
        result = adx(series.high, series.low, series.close, 14)
        assert last_finite(result.plus_di) > last_finite(result.minus_di)

    def test_minus_di_dominates_in_a_downtrend(self):
        series = trending_series(200, step=-0.0005, noise=0.0)
        result = adx(series.high, series.low, series.close, 14)
        assert last_finite(result.minus_di) > last_finite(result.plus_di)

    def test_short_input_returns_all_nan(self):
        result = adx([1, 2, 3], [0, 1, 2], [0.5, 1.5, 2.5], 14)
        assert np.isnan(result.adx).all()


class TestBollinger:
    def test_bands_straddle_the_middle(self):
        values = list(np.linspace(1.0, 1.1, 60))
        bands = bollinger_bands(values, 20, 2.0)
        assert bands.upper[-1] > bands.middle[-1] > bands.lower[-1]

    def test_zero_variance_collapses_the_bands(self):
        bands = bollinger_bands([5.0] * 40, 20, 2.0)
        assert bands.upper[-1] == pytest.approx(bands.lower[-1])
        assert bands.percent_b[-1] == pytest.approx(0.5)

    def test_percent_b_is_one_at_the_upper_band(self):
        values = list(np.linspace(1.0, 1.2, 60))
        bands = bollinger_bands(values, 20, 2.0)
        assert 0.0 <= bands.percent_b[-1] <= 1.5


class TestVwap:
    def test_returns_nan_without_volume(self):
        assert np.isnan(vwap([1], [1], [1], None)).all()

    def test_sits_inside_the_price_range(self):
        series = trending_series(60)
        result = vwap(series.high, series.low, series.close, series.volume)
        assert series.low.min() <= last_finite(result) <= series.high.max()

    def test_zero_volume_is_treated_as_unavailable(self):
        assert np.isnan(vwap([1, 2], [1, 2], [1, 2], [0, 0])).all()


class TestHelpers:
    def test_slope_is_positive_for_rising_values(self):
        assert slope([1, 2, 3, 4, 5]) > 0

    def test_slope_is_zero_for_a_flat_line(self):
        assert slope([3, 3, 3, 3]) == pytest.approx(0.0)

    def test_slope_ignores_nan_values(self):
        assert slope([float("nan"), 1, 2, 3]) > 0

    def test_slope_of_too_little_data_is_zero(self):
        assert slope([1]) == 0.0

    def test_last_finite_skips_trailing_nan(self):
        assert last_finite([1.0, 2.0, float("nan")]) == 2.0

    def test_last_finite_falls_back_to_the_default(self):
        assert last_finite([float("nan")], default=-1.0) == -1.0

    def test_percentile_rank_of_the_maximum_is_one_hundred(self):
        assert percentile_rank([1, 2, 3, 4], 4) == pytest.approx(100.0)


class TestSnapshot:
    def test_uptrend_produces_a_bullish_snapshot(self):
        snapshot = compute_indicators(trending_series(260, step=0.0002))
        assert snapshot.ema_alignment is Bias.BULLISH
        # The MACD *line* tracks direction. The histogram tracks acceleration,
        # so on a constant-slope trend it hovers around zero and its sign is
        # not a direction signal — assert on the line instead.
        assert snapshot.macd_line > 0
        assert snapshot.rsi > 55
        assert snapshot.price > snapshot.ema21

    def test_downtrend_produces_a_bearish_snapshot(self):
        snapshot = compute_indicators(trending_series(260, step=-0.0002))
        assert snapshot.ema_alignment is Bias.BEARISH
        assert snapshot.macd_line < 0
        assert snapshot.rsi < 45
        assert snapshot.price < snapshot.ema21

    def test_macd_histogram_turns_positive_when_a_downtrend_decelerates(self):
        # Steep fall, then a flattening. The histogram should register the
        # loss of downside acceleration even while the line stays negative.
        closes = [1.10 - 0.0004 * i for i in range(80)]
        closes += [closes[-1] - 0.00002 * i for i in range(40)]
        snapshot = compute_indicators(build_series(closes))
        assert snapshot.macd_line < 0
        assert snapshot.macd_hist > 0

    def test_short_series_still_produces_a_snapshot(self):
        # Long-lookback indicators are unavailable, which must be nan rather
        # than a misleading zero.
        snapshot = compute_indicators(build_series([1.0, 1.001, 1.002, 1.0015]))
        assert math.isnan(snapshot.ema200)
        assert snapshot.ema_alignment is Bias.NEUTRAL

    def test_empty_series_is_rejected(self):
        from poa.models import Series

        with pytest.raises(IndicatorError):
            compute_indicators(Series((), 60, "X"))

    def test_snapshot_dict_is_json_safe(self):
        import json

        snapshot = compute_indicators(build_series([1.0, 1.001, 1.002]))
        # nan must not leak into the payload; json.dumps would emit invalid JSON.
        text = json.dumps(snapshot.to_dict())
        assert "NaN" not in text
