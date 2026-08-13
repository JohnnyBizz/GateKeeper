"""Technical indicators.

Every function takes and returns numpy arrays of the same length. Values that
cannot be computed yet (because the lookback is not satisfied) are ``nan``
rather than zero, so downstream code can tell "not enough data" apart from
"the indicator says zero".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class IndicatorError(ValueError):
    """Raised when an indicator is asked for something impossible."""


def _as_array(values) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise IndicatorError("indicator input must be one dimensional")
    return arr


def _nan_prefix(length: int, count: int) -> np.ndarray:
    out = np.full(length, np.nan, dtype=np.float64)
    return out if count >= length else out


def sma(values, period: int) -> np.ndarray:
    """Simple moving average."""
    arr = _as_array(values)
    if period <= 0:
        raise IndicatorError("period must be positive")
    out = np.full(arr.size, np.nan, dtype=np.float64)
    if arr.size < period:
        return out
    cumsum = np.cumsum(np.insert(arr, 0, 0.0))
    window = (cumsum[period:] - cumsum[:-period]) / period
    out[period - 1 :] = window
    return out


def ema(values, period: int) -> np.ndarray:
    """Exponential moving average, seeded with the first SMA of ``period``."""
    arr = _as_array(values)
    if period <= 0:
        raise IndicatorError("period must be positive")
    out = np.full(arr.size, np.nan, dtype=np.float64)
    if arr.size < period:
        return out
    alpha = 2.0 / (period + 1.0)
    prev = float(np.mean(arr[:period]))
    out[period - 1] = prev
    for i in range(period, arr.size):
        prev = arr[i] * alpha + prev * (1.0 - alpha)
        out[i] = prev
    return out


def rma(values, period: int) -> np.ndarray:
    """Wilder's smoothing (used by RSI, ATR and ADX)."""
    arr = _as_array(values)
    if period <= 0:
        raise IndicatorError("period must be positive")
    out = np.full(arr.size, np.nan, dtype=np.float64)
    if arr.size < period:
        return out
    prev = float(np.mean(arr[:period]))
    out[period - 1] = prev
    for i in range(period, arr.size):
        prev = (prev * (period - 1) + arr[i]) / period
        out[i] = prev
    return out


def rsi(values, period: int = 14) -> np.ndarray:
    """Relative Strength Index (Wilder)."""
    arr = _as_array(values)
    out = np.full(arr.size, np.nan, dtype=np.float64)
    if arr.size <= period:
        return out
    delta = np.diff(arr)
    gains = np.clip(delta, 0.0, None)
    losses = np.clip(-delta, 0.0, None)
    avg_gain = rma(gains, period)
    avg_loss = rma(losses, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
        values_rsi = 100.0 - (100.0 / (1.0 + rs))
    values_rsi = np.where(np.isnan(avg_gain), np.nan, values_rsi)
    # diff shortened the array by one; realign to the original index.
    out[1:] = values_rsi
    return out


@dataclass
class MacdResult:
    macd: np.ndarray
    signal: np.ndarray
    histogram: np.ndarray


def macd(
    values, fast: int = 12, slow: int = 26, signal_period: int = 9
) -> MacdResult:
    """Moving Average Convergence Divergence."""
    arr = _as_array(values)
    if fast >= slow:
        raise IndicatorError("fast period must be shorter than slow period")
    fast_line = ema(arr, fast)
    slow_line = ema(arr, slow)
    macd_line = fast_line - slow_line

    # The signal line is an EMA of the MACD line, which only exists once the
    # slow EMA has warmed up. Compute it over the valid tail and pad back.
    valid = ~np.isnan(macd_line)
    signal_line = np.full(arr.size, np.nan, dtype=np.float64)
    if valid.any():
        start = int(np.argmax(valid))
        tail = macd_line[start:]
        signal_tail = ema(tail, signal_period)
        signal_line[start:] = signal_tail
    histogram = macd_line - signal_line
    return MacdResult(macd=macd_line, signal=signal_line, histogram=histogram)


def true_range(high, low, close) -> np.ndarray:
    """True range; the first element falls back to the bar's own range."""
    h = _as_array(high)
    l = _as_array(low)
    c = _as_array(close)
    if not (h.size == l.size == c.size):
        raise IndicatorError("high/low/close must be the same length")
    if h.size == 0:
        return np.array([], dtype=np.float64)
    prev_close = np.empty_like(c)
    prev_close[0] = c[0]
    prev_close[1:] = c[:-1]
    tr = np.maximum.reduce(
        [h - l, np.abs(h - prev_close), np.abs(l - prev_close)]
    )
    tr[0] = h[0] - l[0]
    return tr


def atr(high, low, close, period: int = 14) -> np.ndarray:
    """Average True Range."""
    return rma(true_range(high, low, close), period)


@dataclass
class AdxResult:
    adx: np.ndarray
    plus_di: np.ndarray
    minus_di: np.ndarray


def adx(high, low, close, period: int = 14) -> AdxResult:
    """Average Directional Index with its +DI / -DI components."""
    h = _as_array(high)
    l = _as_array(low)
    c = _as_array(close)
    n = h.size
    empty = np.full(n, np.nan, dtype=np.float64)
    if n < period * 2:
        return AdxResult(adx=empty, plus_di=empty.copy(), minus_di=empty.copy())

    up_move = np.zeros(n, dtype=np.float64)
    down_move = np.zeros(n, dtype=np.float64)
    up_move[1:] = h[1:] - h[:-1]
    down_move[1:] = l[:-1] - l[1:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = true_range(h, l, c)
    smoothed_tr = rma(tr, period)
    smoothed_plus = rma(plus_dm, period)
    smoothed_minus = rma(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        denominator = plus_di + minus_di
        dx = np.where(
            denominator == 0, 0.0, 100.0 * np.abs(plus_di - minus_di) / denominator
        )
    dx = np.where(np.isnan(plus_di) | np.isnan(minus_di), np.nan, dx)

    adx_values = np.full(n, np.nan, dtype=np.float64)
    valid = ~np.isnan(dx)
    if valid.sum() >= period:
        start = int(np.argmax(valid))
        smoothed_dx = rma(dx[start:], period)
        adx_values[start:] = smoothed_dx
    return AdxResult(adx=adx_values, plus_di=plus_di, minus_di=minus_di)


@dataclass
class BollingerResult:
    upper: np.ndarray
    middle: np.ndarray
    lower: np.ndarray
    bandwidth: np.ndarray
    percent_b: np.ndarray


def bollinger_bands(
    values, period: int = 20, deviations: float = 2.0
) -> BollingerResult:
    """Bollinger Bands with bandwidth and %B."""
    arr = _as_array(values)
    middle = sma(arr, period)
    std = np.full(arr.size, np.nan, dtype=np.float64)
    if arr.size >= period:
        windows = np.lib.stride_tricks.sliding_window_view(arr, period)
        std[period - 1 :] = windows.std(axis=1)
    upper = middle + deviations * std
    lower = middle - deviations * std
    with np.errstate(divide="ignore", invalid="ignore"):
        bandwidth = np.where(middle == 0, np.nan, (upper - lower) / middle)
        span = upper - lower
        percent_b = np.where(span == 0, 0.5, (arr - lower) / span)
    percent_b = np.where(np.isnan(middle), np.nan, percent_b)
    return BollingerResult(
        upper=upper,
        middle=middle,
        lower=lower,
        bandwidth=bandwidth,
        percent_b=percent_b,
    )


def vwap(high, low, close, volume) -> np.ndarray:
    """Cumulative volume weighted average price over the supplied window.

    Returns all-``nan`` when no usable volume is available, which is common on
    binary-option charts.
    """
    h = _as_array(high)
    l = _as_array(low)
    c = _as_array(close)
    if volume is None:
        return np.full(h.size, np.nan, dtype=np.float64)
    v = _as_array(volume)
    if v.size != h.size or not np.isfinite(v).all() or v.sum() <= 0:
        return np.full(h.size, np.nan, dtype=np.float64)
    typical = (h + l + c) / 3.0
    cumulative_pv = np.cumsum(typical * v)
    cumulative_v = np.cumsum(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(cumulative_v == 0, np.nan, cumulative_pv / cumulative_v)
    return out


def slope(values, lookback: int = 5) -> float:
    """Least-squares slope of the last ``lookback`` finite values.

    Returns 0.0 when there is not enough clean data to fit a line.
    """
    arr = _as_array(values)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return 0.0
    window = arr[-lookback:] if arr.size >= lookback else arr
    x = np.arange(window.size, dtype=np.float64)
    x_mean = x.mean()
    y_mean = window.mean()
    denominator = float(((x - x_mean) ** 2).sum())
    if denominator == 0:
        return 0.0
    return float(((x - x_mean) * (window - y_mean)).sum() / denominator)


def last_finite(values, default: float = float("nan")) -> float:
    """The most recent finite value in an indicator array."""
    arr = _as_array(values)
    finite = np.isfinite(arr)
    if not finite.any():
        return default
    return float(arr[np.nonzero(finite)[0][-1]])


def percentile_rank(values, value: float) -> float:
    """Where ``value`` sits within ``values``, as a 0-100 percentile."""
    arr = _as_array(values)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0 or not np.isfinite(value):
        return 50.0
    return float((arr <= value).sum() / arr.size * 100.0)
