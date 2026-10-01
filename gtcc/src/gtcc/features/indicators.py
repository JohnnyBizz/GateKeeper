"""Technical indicators — specification section 5.

Every function here takes a list of closed :class:`~gtcc.domain.market_data.Bar`
and returns a series aligned to it, with ``None`` wherever there was not
yet enough history to compute a value. That alignment is the important
part: a series that silently starts short, or pads with zeros, shifts
every downstream comparison by a few bars and the error is invisible.

Three rules hold throughout.

**Decimal, not float.** Prices are exact. An indicator that drifts in
the eleventh decimal place is harmless; one that drifts enough to flip
a threshold comparison is not, and there is no clean line between them.

**Warm-up is ``None``, never zero.** An RSI of 0 means something — a
collapse — and using it as "no value yet" would make the first bars of
every series look like a crash.

**No indicator authorises a trade.** These are inputs to the confluence
score and to strategies, both of which go through the risk engine. The
specification says this explicitly and it is worth repeating where the
numbers are produced.

Unclosed bars are refused. A strategy reading the close of a forming
candle is using information that did not exist yet, which is the
cheapest way to invent an edge that evaporates in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from gtcc.domain.market_data import Bar
from gtcc.domain.money import ONE, ZERO, D

Series = list[Decimal | None]


class IndicatorError(ValueError):
    """The inputs cannot produce a meaningful series."""


def _closes(bars: Sequence[Bar]) -> list[Decimal]:
    return [bar.close for bar in bars]


def _require(bars: Sequence[Bar], period: int, name: str) -> None:
    if period <= 0:
        raise IndicatorError(f"{name} needs a positive period, got {period}")
    if any(not bar.closed for bar in bars):
        raise IndicatorError(
            f"{name} was given an unclosed bar. A forming candle's close is "
            "information that does not exist yet."
        )


def _sqrt(value: Decimal) -> Decimal:
    """Decimal square root, guarded against the negative-zero case."""
    return value.sqrt() if value > ZERO else ZERO


# -- moving averages -------------------------------------------------------


def sma(bars: Sequence[Bar], period: int) -> Series:
    """Simple moving average of closes."""
    _require(bars, period, "sma")
    closes = _closes(bars)
    out: Series = [None] * len(closes)
    running = ZERO
    for index, close in enumerate(closes):
        running += close
        if index >= period:
            running -= closes[index - period]
        if index >= period - 1:
            out[index] = running / period
    return out


def ema(bars: Sequence[Bar], period: int) -> Series:
    """Exponential moving average, seeded with the first SMA.

    Seeding matters: starting from the first close instead makes the
    early values depend heavily on one bar, and two implementations
    that disagree about the seed disagree for hundreds of bars.
    """
    _require(bars, period, "ema")
    closes = _closes(bars)
    out: Series = [None] * len(closes)
    if len(closes) < period:
        return out

    multiplier = D(2) / D(period + 1)
    seed = sum(closes[:period], ZERO) / period
    out[period - 1] = seed
    previous = seed
    for index in range(period, len(closes)):
        previous = (closes[index] - previous) * multiplier + previous
        out[index] = previous
    return out


def vwap(bars: Sequence[Bar], *, anchor: int = 0) -> Series:
    """Volume-weighted average price from *anchor* onward.

    ``anchor=0`` is the session or series VWAP. An anchored VWAP sets
    the anchor to a specific bar — a swing low, a news release, the
    open — and measures from there.

    Bars before the anchor are ``None`` rather than carrying a value
    from a different anchor's accumulation.
    """
    _require(bars, 1, "vwap")
    if not 0 <= anchor < max(1, len(bars)):
        raise IndicatorError(f"anchor {anchor} is outside the series")

    out: Series = [None] * len(bars)
    volume_sum = ZERO
    price_volume = ZERO
    for index in range(anchor, len(bars)):
        bar = bars[index]
        typical = (bar.high + bar.low + bar.close) / D(3)
        volume_sum += bar.volume
        price_volume += typical * bar.volume
        # Zero-volume bars are real on illiquid symbols. Carrying the
        # previous value is right; dividing by zero is not.
        out[index] = price_volume / volume_sum if volume_sum > ZERO else None
    return out


# -- momentum ----------------------------------------------------------------


def rsi(bars: Sequence[Bar], period: int = 14) -> Series:
    """Wilder's relative strength index.

    Wilder's smoothing, not a simple average of gains and losses. The
    two differ by enough to move a 70 reading several points, and most
    published levels assume Wilder's.
    """
    _require(bars, period, "rsi")
    closes = _closes(bars)
    out: Series = [None] * len(closes)
    if len(closes) <= period:
        return out

    gains = ZERO
    losses = ZERO
    for index in range(1, period + 1):
        change = closes[index] - closes[index - 1]
        gains += max(change, ZERO)
        losses += max(-change, ZERO)
    average_gain = gains / period
    average_loss = losses / period
    out[period] = _rsi_value(average_gain, average_loss)

    for index in range(period + 1, len(closes)):
        change = closes[index] - closes[index - 1]
        average_gain = (average_gain * (period - 1) + max(change, ZERO)) / period
        average_loss = (average_loss * (period - 1) + max(-change, ZERO)) / period
        out[index] = _rsi_value(average_gain, average_loss)
    return out


def _rsi_value(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    if average_loss == ZERO:
        # Unbroken gains. 100 is the defined limit, not a missing value.
        return D(100)
    strength = average_gain / average_loss
    return D(100) - (D(100) / (ONE + strength))


@dataclass(frozen=True, slots=True)
class MACD:
    macd: Series
    signal: Series
    histogram: Series


def macd(
    bars: Sequence[Bar], *, fast: int = 12, slow: int = 26, signal: int = 9
) -> MACD:
    """Moving average convergence divergence.

    The signal line is an EMA *of the MACD line*, which only exists
    once the slow EMA does, so it is computed over the defined portion
    and realigned rather than over the padded series.
    """
    if fast >= slow:
        raise IndicatorError(f"fast period {fast} must be shorter than slow {slow}")
    _require(bars, fast, "macd")

    fast_line = ema(bars, fast)
    slow_line = ema(bars, slow)
    line: Series = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_line, slow_line)
    ]

    defined = [(index, value) for index, value in enumerate(line) if value is not None]
    signal_line: Series = [None] * len(line)
    histogram: Series = [None] * len(line)
    if len(defined) >= signal:
        multiplier = D(2) / D(signal + 1)
        seed = sum((value for _, value in defined[:signal]), ZERO) / signal
        position, _ = defined[signal - 1]
        signal_line[position] = seed
        previous = seed
        for index, value in defined[signal:]:
            previous = (value - previous) * multiplier + previous
            signal_line[index] = previous

    for index, value in enumerate(line):
        if value is not None and signal_line[index] is not None:
            histogram[index] = value - signal_line[index]

    return MACD(macd=line, signal=signal_line, histogram=histogram)


def roc(bars: Sequence[Bar], period: int = 12) -> Series:
    """Rate of change, as a percentage."""
    _require(bars, period, "roc")
    closes = _closes(bars)
    out: Series = [None] * len(closes)
    for index in range(period, len(closes)):
        earlier = closes[index - period]
        if earlier > ZERO:
            out[index] = (closes[index] - earlier) / earlier * D(100)
    return out


@dataclass(frozen=True, slots=True)
class Stochastic:
    k: Series
    d: Series


def stochastic(
    bars: Sequence[Bar], *, period: int = 14, smooth_k: int = 3, smooth_d: int = 3
) -> Stochastic:
    """Stochastic oscillator, slow by default.

    When the lookback's high equals its low the position within the
    range is undefined, not 50. A flat range is reported as ``None``
    rather than as a midpoint nobody measured.
    """
    _require(bars, period, "stochastic")
    out_k: Series = [None] * len(bars)
    for index in range(period - 1, len(bars)):
        window = bars[index - period + 1 : index + 1]
        highest = max(bar.high for bar in window)
        lowest = min(bar.low for bar in window)
        span = highest - lowest
        if span > ZERO:
            out_k[index] = (bars[index].close - lowest) / span * D(100)

    smoothed_k = _smooth(out_k, smooth_k)
    smoothed_d = _smooth(smoothed_k, smooth_d)
    return Stochastic(k=smoothed_k, d=smoothed_d)


def _smooth(series: Series, period: int) -> Series:
    """Simple moving average over the defined part of a series."""
    if period <= 1:
        return list(series)
    out: Series = [None] * len(series)
    window: list[Decimal] = []
    for index, value in enumerate(series):
        if value is None:
            window.clear()
            continue
        window.append(value)
        if len(window) > period:
            window.pop(0)
        if len(window) == period:
            out[index] = sum(window, ZERO) / period
    return out


# -- volatility ----------------------------------------------------------------


def true_range(bars: Sequence[Bar]) -> Series:
    """True range: the high-low span, extended by any overnight gap."""
    _require(bars, 1, "true_range")
    out: Series = [None] * len(bars)
    for index, bar in enumerate(bars):
        if index == 0:
            out[index] = bar.high - bar.low
            continue
        previous_close = bars[index - 1].close
        out[index] = max(
            bar.high - bar.low,
            abs(bar.high - previous_close),
            abs(bar.low - previous_close),
        )
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> Series:
    """Average true range, Wilder-smoothed."""
    _require(bars, period, "atr")
    ranges = true_range(bars)
    out: Series = [None] * len(bars)
    if len(bars) < period:
        return out

    values = [value for value in ranges[:period] if value is not None]
    previous = sum(values, ZERO) / period
    out[period - 1] = previous
    for index in range(period, len(bars)):
        current = ranges[index]
        assert current is not None
        previous = (previous * (period - 1) + current) / period
        out[index] = previous
    return out


@dataclass(frozen=True, slots=True)
class Bands:
    upper: Series
    middle: Series
    lower: Series

    def width(self) -> Series:
        """Band width as a fraction of the middle band."""
        return [
            ((u - low) / m) if (u is not None and low is not None and m and m > ZERO) else None
            for u, m, low in zip(self.upper, self.middle, self.lower)
        ]


def bollinger(
    bars: Sequence[Bar], *, period: int = 20, deviations: Decimal = D(2)
) -> Bands:
    """Bollinger bands around a simple moving average."""
    _require(bars, period, "bollinger")
    middle = sma(bars, period)
    closes = _closes(bars)
    upper: Series = [None] * len(bars)
    lower: Series = [None] * len(bars)

    for index in range(period - 1, len(bars)):
        mean = middle[index]
        assert mean is not None
        window = closes[index - period + 1 : index + 1]
        variance = sum(((value - mean) ** 2 for value in window), ZERO) / period
        spread = _sqrt(variance) * D(deviations)
        upper[index] = mean + spread
        lower[index] = mean - spread
    return Bands(upper=upper, middle=middle, lower=lower)


def historical_volatility(
    bars: Sequence[Bar], *, period: int = 20, periods_per_year: int = 252
) -> Series:
    """Annualised standard deviation of log returns.

    Uses the sample standard deviation, so the first value needs
    ``period + 1`` bars: *period* returns require one extra close.
    """
    _require(bars, period, "historical_volatility")
    import math

    closes = _closes(bars)
    returns: list[Decimal | None] = [None] * len(closes)
    for index in range(1, len(closes)):
        previous = closes[index - 1]
        if previous > ZERO and closes[index] > ZERO:
            returns[index] = D(math.log(float(closes[index] / previous)))

    out: Series = [None] * len(bars)
    scale = D(math.sqrt(periods_per_year))
    for index in range(period, len(bars)):
        window = [value for value in returns[index - period + 1 : index + 1] if value is not None]
        if len(window) < period:
            continue
        mean = sum(window, ZERO) / period
        variance = sum(((value - mean) ** 2 for value in window), ZERO) / (period - 1)
        out[index] = _sqrt(variance) * scale * D(100)
    return out


# -- trend strength ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DirectionalMovement:
    """Wilder's directional system.

    ``adx`` says how strongly a market is trending and deliberately not
    which way. ``plus_di`` and ``minus_di`` are where the direction
    lives. Keeping them together makes that hard to forget: reading a
    high ADX as bullish is a common and expensive mistake, and the
    only cure is having the directional lines to hand.
    """

    plus_di: Series
    minus_di: Series
    adx: Series


def directional_movement(bars: Sequence[Bar], period: int = 14) -> DirectionalMovement:
    """+DI, -DI and ADX, Wilder-smoothed."""
    _require(bars, period, "directional_movement")
    empty: Series = [None] * len(bars)
    if len(bars) < period * 2:
        return DirectionalMovement(plus_di=empty, minus_di=list(empty), adx=list(empty))

    plus_dm: list[Decimal] = [ZERO]
    minus_dm: list[Decimal] = [ZERO]
    for index in range(1, len(bars)):
        up = bars[index].high - bars[index - 1].high
        down = bars[index - 1].low - bars[index].low
        plus_dm.append(up if (up > down and up > ZERO) else ZERO)
        minus_dm.append(down if (down > up and down > ZERO) else ZERO)

    ranges = true_range(bars)
    smoothed_tr = sum((ranges[i] or ZERO) for i in range(1, period + 1))
    smoothed_plus = sum(plus_dm[1 : period + 1], ZERO)
    smoothed_minus = sum(minus_dm[1 : period + 1], ZERO)

    plus_series: Series = [None] * len(bars)
    minus_series: Series = [None] * len(bars)
    dx_values: list[tuple[int, Decimal]] = []

    for index in range(period, len(bars)):
        if index > period:
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + (ranges[index] or ZERO)
            smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_dm[index]
            smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm[index]
        if smoothed_tr <= ZERO:
            continue
        plus_di = smoothed_plus / smoothed_tr * D(100)
        minus_di = smoothed_minus / smoothed_tr * D(100)
        plus_series[index] = plus_di
        minus_series[index] = minus_di
        total = plus_di + minus_di
        if total > ZERO:
            dx_values.append((index, abs(plus_di - minus_di) / total * D(100)))

    adx_series: Series = [None] * len(bars)
    if len(dx_values) >= period:
        first_index = dx_values[period - 1][0]
        previous = sum((value for _, value in dx_values[:period]), ZERO) / period
        adx_series[first_index] = previous
        for index, value in dx_values[period:]:
            previous = (previous * (period - 1) + value) / period
            adx_series[index] = previous

    return DirectionalMovement(
        plus_di=plus_series, minus_di=minus_series, adx=adx_series
    )


def adx(bars: Sequence[Bar], period: int = 14) -> Series:
    """Average directional index: how strongly a market is trending.

    Direction-agnostic by construction. A high ADX says a trend is
    strong, not which way it points. Use :func:`directional_movement`
    when the direction matters.
    """
    return directional_movement(bars, period).adx


# -- volume -------------------------------------------------------------------------


def obv(bars: Sequence[Bar]) -> Series:
    """On-balance volume. Starts at zero on the first bar by convention."""
    _require(bars, 1, "obv")
    out: Series = [None] * len(bars)
    if not bars:
        return out
    running = ZERO
    out[0] = running
    for index in range(1, len(bars)):
        if bars[index].close > bars[index - 1].close:
            running += bars[index].volume
        elif bars[index].close < bars[index - 1].close:
            running -= bars[index].volume
        out[index] = running
    return out


def volume_average(bars: Sequence[Bar], period: int = 20) -> Series:
    _require(bars, period, "volume_average")
    out: Series = [None] * len(bars)
    running = ZERO
    for index, bar in enumerate(bars):
        running += bar.volume
        if index >= period:
            running -= bars[index - period].volume
        if index >= period - 1:
            out[index] = running / period
    return out


def relative_volume(bars: Sequence[Bar], period: int = 20) -> Series:
    """This bar's volume as a multiple of its recent average.

    The average deliberately excludes the current bar: comparing a bar
    against a window containing itself damps exactly the spike the
    measure exists to detect.
    """
    _require(bars, period, "relative_volume")
    out: Series = [None] * len(bars)
    for index in range(period, len(bars)):
        window = bars[index - period : index]
        average = sum((bar.volume for bar in window), ZERO) / period
        if average > ZERO:
            out[index] = bars[index].volume / average
    return out
