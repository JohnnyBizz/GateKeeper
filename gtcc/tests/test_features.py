"""The feature engine.

Indicators are easy to write and easy to get subtly wrong, and a subtle
error does not announce itself: the series looks plausible, the chart
looks plausible, and every threshold comparison downstream is off by a
little. So these tests check values against arithmetic done by hand,
not against the implementation's own output.

Alignment gets as much attention as the numbers. A series that starts a
bar early shifts every comparison after it.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from gtcc.domain.enums import Timeframe
from gtcc.domain.market_data import Bar
from gtcc.domain.money import D
from gtcc.features.indicators import (
    IndicatorError,
    adx,
    atr,
    bollinger,
    ema,
    historical_volatility,
    macd,
    obv,
    relative_volume,
    roc,
    rsi,
    sma,
    stochastic,
    true_range,
    volume_average,
    vwap,
)

START = None  # set in the fixture


def make_bars(closes, *, highs=None, lows=None, volumes=None, now=None, closed=True):
    """Build a bar series from closes, with sensible high/low defaults."""
    from datetime import datetime, timezone

    base = now or datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    out = []
    for index, close in enumerate(closes):
        close = D(close)
        high = D(highs[index]) if highs else close
        low = D(lows[index]) if lows else close
        volume = D(volumes[index]) if volumes else D(100)
        out.append(
            Bar(
                symbol="X",
                timeframe=Timeframe.M5,
                timestamp=base + timedelta(minutes=5 * index),
                open=close,
                high=max(high, close),
                low=min(low, close),
                close=close,
                volume=volume,
                closed=closed,
            )
        )
    return out


class TestAlignmentAndWarmUp:
    """A series must line up with its bars, bar for bar."""

    def test_every_series_has_one_value_per_bar(self):
        bars = make_bars(range(1, 61))

        for series in (
            sma(bars, 10), ema(bars, 10), rsi(bars, 14), atr(bars, 14),
            roc(bars, 12), obv(bars), vwap(bars), volume_average(bars, 20),
            relative_volume(bars, 20), adx(bars, 14),
            historical_volatility(bars, period=20),
            true_range(bars),
        ):
            assert len(series) == len(bars)

    def test_the_first_sma_value_lands_on_the_period_th_bar(self):
        bars = make_bars([1, 2, 3, 4, 5])

        values = sma(bars, 3)

        assert values[0] is None
        assert values[1] is None
        assert values[2] == D(2)  # (1+2+3)/3
        assert values[3] == D(3)
        assert values[4] == D(4)

    def test_warm_up_is_none_and_never_zero(self):
        """Zero is a meaningful RSI. Using it as 'no value' would make
        the start of every series look like a collapse."""
        bars = make_bars(range(1, 30))

        values = rsi(bars, 14)

        assert all(value is None for value in values[:14])
        assert values[14] is not None
        assert not any(value == 0 for value in values[:14])

    def test_too_little_history_gives_an_all_none_series(self):
        bars = make_bars([1, 2, 3])

        assert sma(bars, 10) == [None, None, None]
        assert ema(bars, 10) == [None, None, None]

    def test_an_unclosed_bar_is_refused(self):
        """A forming candle's close is information that does not exist."""
        bars = make_bars(range(1, 30), closed=False)

        with pytest.raises(IndicatorError, match="unclosed"):
            sma(bars, 10)

    def test_a_non_positive_period_is_refused(self):
        bars = make_bars(range(1, 30))

        with pytest.raises(IndicatorError, match="positive period"):
            sma(bars, 0)


class TestMovingAverages:
    def test_sma_of_a_flat_series_is_the_level(self):
        bars = make_bars([100] * 20)

        assert sma(bars, 10)[-1] == D(100)

    def test_ema_is_seeded_with_the_first_sma(self):
        """Seeding from the first close instead would make early values
        depend on one bar, and two implementations would disagree for
        hundreds of bars afterwards."""
        bars = make_bars([1, 2, 3, 4, 5, 6])

        values = ema(bars, 3)

        assert values[2] == D(2)  # SMA of 1,2,3

    def test_ema_matches_hand_arithmetic(self):
        bars = make_bars([1, 2, 3, 4, 5])
        # multiplier = 2/(3+1) = 0.5, seed = 2
        # bar 3: (4 - 2) * 0.5 + 2 = 3
        # bar 4: (5 - 3) * 0.5 + 3 = 4
        values = ema(bars, 3)

        assert values[3] == D(3)
        assert values[4] == D(4)

    def test_ema_reacts_faster_than_sma_to_a_jump(self):
        bars = make_bars([10] * 20 + [20])

        assert ema(bars, 10)[-1] > sma(bars, 10)[-1]


class TestVwap:
    def test_vwap_weights_by_volume(self):
        """Two bars, the second with nine times the volume: the result
        must sit near the second, not midway."""
        bars = make_bars([10, 20], volumes=[1, 9])

        values = vwap(bars)

        assert values[0] == D(10)
        assert values[1] == D(19)  # (10*1 + 20*9) / 10

    def test_an_anchor_restarts_the_accumulation(self):
        bars = make_bars([10, 10, 100, 100], volumes=[1, 1, 1, 1])

        unanchored = vwap(bars)
        anchored = vwap(bars, anchor=2)

        assert anchored[0] is None
        assert anchored[1] is None
        assert anchored[3] == D(100)
        assert unanchored[3] == D("55")

    def test_zero_volume_does_not_divide_by_zero(self):
        bars = make_bars([10, 20], volumes=[0, 0])

        assert vwap(bars) == [None, None]

    def test_an_anchor_outside_the_series_is_refused(self):
        with pytest.raises(IndicatorError, match="outside the series"):
            vwap(make_bars([1, 2, 3]), anchor=9)


class TestRsi:
    def test_unbroken_gains_give_one_hundred(self):
        """The defined limit, not a missing value."""
        bars = make_bars(range(1, 40))

        assert rsi(bars, 14)[-1] == D(100)

    def test_unbroken_losses_give_zero(self):
        bars = make_bars(range(40, 1, -1))

        assert rsi(bars, 14)[-1] == D(0)

    def test_a_flat_market_gives_the_neutral_reading(self):
        """No gains and no losses: both averages are zero, and the
        convention is 100 rather than a division by zero."""
        bars = make_bars([50] * 40)

        assert rsi(bars, 14)[-1] == D(100)

    def test_rsi_stays_inside_its_bounds(self):
        import random

        random.seed(7)
        closes = [100]
        for _ in range(200):
            closes.append(max(1, closes[-1] + random.uniform(-3, 3)))
        bars = make_bars(closes)

        for value in rsi(bars, 14):
            if value is not None:
                assert D(0) <= value <= D(100)

    def test_it_matches_an_independently_written_reference(self):
        """Cross-checked against a second implementation written from
        the definition in plain floats.

        A test that only pins the implementation's own output would
        pass just as happily if the smoothing were wrong. Two
        independent routes to the same number is the useful check.
        """
        closes = [44, 44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                  45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]

        def reference(values, period=14):
            gains = losses = 0.0
            for i in range(1, period + 1):
                change = values[i] - values[i - 1]
                gains += max(change, 0.0)
                losses += max(-change, 0.0)
            average_gain, average_loss = gains / period, losses / period
            out = [100.0 if average_loss == 0 else 100 - 100 / (1 + average_gain / average_loss)]
            for i in range(period + 1, len(values)):
                change = values[i] - values[i - 1]
                average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
                average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period
                out.append(
                    100.0 if average_loss == 0
                    else 100 - 100 / (1 + average_gain / average_loss)
                )
            return out

        ours = [v for v in rsi(make_bars(closes), 14) if v is not None]
        theirs = reference([float(c) for c in closes])

        assert len(ours) == len(theirs)
        for mine, other in zip(ours, theirs):
            assert abs(float(mine) - other) < 1e-9

    def test_wilder_smoothing_decays_rather_than_dropping_values(self):
        """A simple rolling average forgets a bar completely once it
        leaves the window. Wilder's keeps a decaying trace of it, so a
        shock still shows well after fourteen bars."""
        calm = [100.0] * 40
        shocked = [100.0] * 20 + [80.0] + [100.0] * 19

        calm_rsi = rsi(make_bars(calm), 14)[-1]
        shocked_rsi = rsi(make_bars(shocked), 14)[-1]

        assert shocked_rsi < calm_rsi


class TestMacd:
    def test_the_line_is_the_difference_of_the_two_emas(self):
        bars = make_bars(range(1, 80))

        result = macd(bars, fast=12, slow=26, signal=9)
        fast_line = ema(bars, 12)
        slow_line = ema(bars, 26)

        assert result.macd[-1] == fast_line[-1] - slow_line[-1]

    def test_the_signal_is_defined_only_after_the_line_is(self):
        bars = make_bars(range(1, 80))

        result = macd(bars)

        first_line = next(i for i, v in enumerate(result.macd) if v is not None)
        first_signal = next(i for i, v in enumerate(result.signal) if v is not None)
        assert first_signal == first_line + 8  # nine values of the line

    def test_the_histogram_is_line_minus_signal(self):
        bars = make_bars(range(1, 80))

        result = macd(bars)

        assert result.histogram[-1] == result.macd[-1] - result.signal[-1]

    def test_a_fast_period_at_or_above_the_slow_one_is_refused(self):
        with pytest.raises(IndicatorError, match="shorter than"):
            macd(make_bars(range(1, 80)), fast=26, slow=12)


class TestVolatility:
    def test_true_range_includes_the_gap(self):
        """A bar that opens above the previous close has a true range
        larger than its own high-low span."""
        bars = make_bars([10, 20], highs=[10, 21], lows=[9, 19])

        values = true_range(bars)

        assert values[0] == D(1)
        assert values[1] == D(11)  # 21 - 10, the gap, not 21 - 19

    def test_atr_of_a_constant_range_is_that_range(self):
        bars = make_bars([10] * 30, highs=[12] * 30, lows=[8] * 30)

        assert atr(bars, 14)[-1] == D(4)

    def test_atr_is_never_negative(self):
        import random

        random.seed(11)
        closes = [100 + random.uniform(-5, 5) for _ in range(100)]
        bars = make_bars(
            closes,
            highs=[c + 2 for c in closes],
            lows=[c - 2 for c in closes],
        )

        for value in atr(bars, 14):
            if value is not None:
                assert value >= 0

    def test_bollinger_bands_are_ordered_and_centred(self):
        import random

        random.seed(3)
        closes = [100 + random.uniform(-5, 5) for _ in range(60)]
        bars = make_bars(closes)

        bands = bollinger(bars, period=20)

        for upper, middle, lower in zip(bands.upper, bands.middle, bands.lower):
            if upper is None:
                continue
            assert lower < middle < upper
            # Symmetric about the mean, to within Decimal's working
            # precision. The two subtractions round differently in the
            # last few of 28 significant digits, which is noise far
            # below any price increment.
            asymmetry = abs((upper - middle) - (middle - lower))
            assert asymmetry / (upper - lower) < D("1e-20")

    def test_a_flat_series_gives_zero_width_bands(self):
        bars = make_bars([100] * 40)

        bands = bollinger(bars, period=20)

        assert bands.upper[-1] == bands.middle[-1] == bands.lower[-1] == D(100)
        assert bands.width()[-1] == 0

    def test_historical_volatility_is_zero_for_a_flat_series(self):
        bars = make_bars([100] * 40)

        assert historical_volatility(bars, period=20)[-1] == 0

    def test_historical_volatility_rises_with_dispersion(self):
        calm = make_bars([100 + (1 if i % 2 else -1) for i in range(60)])
        wild = make_bars([100 + (15 if i % 2 else -15) for i in range(60)])

        assert (
            historical_volatility(wild, period=20)[-1]
            > historical_volatility(calm, period=20)[-1]
        )


class TestStochastic:
    def test_a_close_at_the_high_of_the_range_is_one_hundred(self):
        closes = [10] * 13 + [20]
        bars = make_bars(closes, highs=[20] * 14, lows=[10] * 14)

        result = stochastic(bars, period=14, smooth_k=1, smooth_d=1)

        assert result.k[13] == D(100)

    def test_a_flat_range_is_undefined_rather_than_fifty(self):
        """Nobody measured a midpoint; there is no range to be mid of."""
        bars = make_bars([10] * 20, highs=[10] * 20, lows=[10] * 20)

        result = stochastic(bars, period=14, smooth_k=1, smooth_d=1)

        assert all(value is None for value in result.k)

    def test_smoothing_shifts_the_first_defined_value(self):
        bars = make_bars(range(1, 60))

        fast = stochastic(bars, period=14, smooth_k=1, smooth_d=1)
        slow = stochastic(bars, period=14, smooth_k=3, smooth_d=3)

        first_fast = next(i for i, v in enumerate(fast.k) if v is not None)
        first_slow = next(i for i, v in enumerate(slow.k) if v is not None)
        assert first_slow == first_fast + 2


class TestTrendStrength:
    def test_adx_is_high_in_a_strong_trend(self):
        closes = list(range(1, 120))
        bars = make_bars(closes, highs=[c + 1 for c in closes], lows=[c - 1 for c in closes])

        assert adx(bars, 14)[-1] > D(40)

    def test_adx_is_low_in_a_chop(self):
        closes = [100 + (2 if i % 2 else -2) for i in range(120)]
        bars = make_bars(closes, highs=[c + 1 for c in closes], lows=[c - 1 for c in closes])

        assert adx(bars, 14)[-1] < D(30)

    def test_adx_does_not_say_which_way(self):
        """Direction-agnostic by construction. Reading a high ADX as
        bullish is a common and expensive mistake."""
        up = list(range(1, 120))
        down = list(range(120, 1, -1))
        rising = make_bars(up, highs=[c + 1 for c in up], lows=[c - 1 for c in up])
        falling = make_bars(down, highs=[c + 1 for c in down], lows=[c - 1 for c in down])

        assert adx(rising, 14)[-1] > D(40)
        assert adx(falling, 14)[-1] > D(40)


class TestVolume:
    def test_obv_adds_on_up_bars_and_subtracts_on_down_bars(self):
        bars = make_bars([10, 11, 10, 12], volumes=[100, 50, 30, 20])

        values = obv(bars)

        assert values == [D(0), D(50), D(20), D(40)]

    def test_obv_ignores_unchanged_closes(self):
        bars = make_bars([10, 10, 10], volumes=[100, 100, 100])

        assert obv(bars)[-1] == D(0)

    def test_relative_volume_excludes_the_current_bar(self):
        """Comparing a bar against a window containing itself damps the
        very spike the measure exists to detect."""
        bars = make_bars([10] * 21, volumes=[100] * 20 + [300])

        assert relative_volume(bars, 20)[-1] == D(3)

    def test_volume_average_is_a_plain_mean(self):
        bars = make_bars([10] * 5, volumes=[10, 20, 30, 40, 50])

        assert volume_average(bars, 5)[-1] == D(30)
