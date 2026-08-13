"""Indicator computation and the per-series snapshot used by the analysis layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..models import Bias, Series
from .core import (
    AdxResult,
    BollingerResult,
    IndicatorError,
    MacdResult,
    adx,
    atr,
    bollinger_bands,
    ema,
    last_finite,
    macd,
    percentile_rank,
    rma,
    rsi,
    slope,
    sma,
    true_range,
    vwap,
)

__all__ = [
    "AdxResult",
    "BollingerResult",
    "IndicatorError",
    "IndicatorSnapshot",
    "MacdResult",
    "adx",
    "atr",
    "bollinger_bands",
    "compute_indicators",
    "ema",
    "last_finite",
    "macd",
    "percentile_rank",
    "rma",
    "rsi",
    "slope",
    "sma",
    "true_range",
    "vwap",
]


def _safe(value: float) -> float | None:
    """JSON-safe scalar: nan/inf become None."""
    if value is None or not np.isfinite(value):
        return None
    return float(value)


@dataclass
class IndicatorSnapshot:
    """The indicator picture at the most recent closed candle.

    Arrays are kept alongside the scalars because the analysis layer needs the
    history (e.g. ATR percentiles, EMA slope over time), while the dashboard
    only ever consumes the scalars.
    """

    price: float
    ema9: float
    ema21: float
    ema50: float
    ema200: float
    rsi: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    macd_hist_prev: float
    atr: float
    atr_percent: float
    atr_percentile: float
    adx: float
    plus_di: float
    minus_di: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    bb_bandwidth: float
    bb_percent_b: float
    vwap: float
    volume_available: bool

    arrays: dict[str, np.ndarray] = field(default_factory=dict, repr=False)

    # -- derived reads ------------------------------------------------------

    @property
    def ema_alignment(self) -> Bias:
        """Bullish when the fast-to-slow EMA stack is stacked in order."""
        values = [self.ema9, self.ema21, self.ema50]
        if any(not np.isfinite(v) for v in values):
            return Bias.NEUTRAL
        if self.ema9 > self.ema21 > self.ema50 and self.price > self.ema21:
            return Bias.BULLISH
        if self.ema9 < self.ema21 < self.ema50 and self.price < self.ema21:
            return Bias.BEARISH
        return Bias.NEUTRAL

    @property
    def ema_alignment_strength(self) -> float:
        """0..1 measure of how cleanly the EMAs are stacked and separated."""
        if not np.isfinite(self.ema9) or not np.isfinite(self.ema21):
            return 0.0
        if not np.isfinite(self.atr) or self.atr <= 0:
            return 0.0
        gap = abs(self.ema9 - self.ema21) / self.atr
        stacked = self.ema_alignment is not Bias.NEUTRAL
        base = 0.5 if stacked else 0.0
        if np.isfinite(self.ema50) and np.isfinite(self.ema200):
            long_agrees = (
                (self.ema50 > self.ema200 and self.ema_alignment is Bias.BULLISH)
                or (self.ema50 < self.ema200 and self.ema_alignment is Bias.BEARISH)
            )
            if long_agrees:
                base += 0.2
        return float(min(1.0, base + min(gap, 1.0) * 0.3))

    @property
    def macd_bias(self) -> Bias:
        if not np.isfinite(self.macd_hist):
            return Bias.NEUTRAL
        if self.macd_hist > 0 and self.macd_line > self.macd_signal:
            return Bias.BULLISH
        if self.macd_hist < 0 and self.macd_line < self.macd_signal:
            return Bias.BEARISH
        return Bias.NEUTRAL

    @property
    def macd_expanding(self) -> bool:
        """Is the histogram growing in its current direction?"""
        if not (np.isfinite(self.macd_hist) and np.isfinite(self.macd_hist_prev)):
            return False
        return abs(self.macd_hist) > abs(self.macd_hist_prev)

    @property
    def rsi_bias(self) -> Bias:
        if not np.isfinite(self.rsi):
            return Bias.NEUTRAL
        if self.rsi >= 55:
            return Bias.BULLISH
        if self.rsi <= 45:
            return Bias.BEARISH
        return Bias.NEUTRAL

    @property
    def trend_strength(self) -> float:
        """ADX mapped onto 0..1; 25+ is conventionally a trending market."""
        if not np.isfinite(self.adx):
            return 0.0
        return float(min(1.0, max(0.0, (self.adx - 12.0) / 33.0)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": _safe(self.price),
            "ema9": _safe(self.ema9),
            "ema21": _safe(self.ema21),
            "ema50": _safe(self.ema50),
            "ema200": _safe(self.ema200),
            "rsi": _safe(self.rsi),
            "macd": _safe(self.macd_line),
            "macd_signal": _safe(self.macd_signal),
            "macd_hist": _safe(self.macd_hist),
            "atr": _safe(self.atr),
            "atr_percent": _safe(self.atr_percent),
            "atr_percentile": _safe(self.atr_percentile),
            "adx": _safe(self.adx),
            "plus_di": _safe(self.plus_di),
            "minus_di": _safe(self.minus_di),
            "bb_upper": _safe(self.bb_upper),
            "bb_middle": _safe(self.bb_middle),
            "bb_lower": _safe(self.bb_lower),
            "bb_bandwidth": _safe(self.bb_bandwidth),
            "bb_percent_b": _safe(self.bb_percent_b),
            "vwap": _safe(self.vwap),
            "volume_available": self.volume_available,
            "ema_alignment": self.ema_alignment.value,
            "macd_bias": self.macd_bias.value,
            "rsi_bias": self.rsi_bias.value,
            "trend_strength": round(self.trend_strength, 3),
        }


def compute_indicators(series: Series) -> IndicatorSnapshot:
    """Compute every indicator for ``series`` and snapshot the latest values.

    Short series are handled gracefully: indicators that cannot warm up return
    ``nan``, and the analysis layer treats those as "no opinion" rather than
    as a zero reading.
    """
    close = series.close
    high = series.high
    low = series.low
    volume = series.volume
    n = close.size

    if n == 0:
        raise IndicatorError("cannot compute indicators on an empty series")

    ema9 = ema(close, 9)
    ema21 = ema(close, 21)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    rsi_values = rsi(close, 14)
    macd_result = macd(close)
    atr_values = atr(high, low, close, 14)
    adx_result = adx(high, low, close, 14)
    bands = bollinger_bands(close, 20, 2.0)
    vwap_values = vwap(high, low, close, volume)

    price = float(close[-1])
    atr_now = last_finite(atr_values)
    atr_percent = (
        (atr_now / price * 100.0) if (np.isfinite(atr_now) and price) else float("nan")
    )
    atr_pct_rank = percentile_rank(atr_values, atr_now)

    hist = macd_result.histogram
    macd_hist_prev = float(hist[-2]) if hist.size >= 2 else float("nan")

    return IndicatorSnapshot(
        price=price,
        ema9=last_finite(ema9),
        ema21=last_finite(ema21),
        ema50=last_finite(ema50),
        ema200=last_finite(ema200),
        rsi=last_finite(rsi_values),
        macd_line=last_finite(macd_result.macd),
        macd_signal=last_finite(macd_result.signal),
        macd_hist=last_finite(hist),
        macd_hist_prev=macd_hist_prev,
        atr=atr_now,
        atr_percent=atr_percent,
        atr_percentile=atr_pct_rank,
        adx=last_finite(adx_result.adx),
        plus_di=last_finite(adx_result.plus_di),
        minus_di=last_finite(adx_result.minus_di),
        bb_upper=last_finite(bands.upper),
        bb_middle=last_finite(bands.middle),
        bb_lower=last_finite(bands.lower),
        bb_bandwidth=last_finite(bands.bandwidth),
        bb_percent_b=last_finite(bands.percent_b),
        vwap=last_finite(vwap_values),
        volume_available=volume is not None,
        arrays={
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "ema200": ema200,
            "rsi": rsi_values,
            "macd": macd_result.macd,
            "macd_signal": macd_result.signal,
            "macd_hist": hist,
            "atr": atr_values,
            "adx": adx_result.adx,
            "plus_di": adx_result.plus_di,
            "minus_di": adx_result.minus_di,
            "bb_upper": bands.upper,
            "bb_middle": bands.middle,
            "bb_lower": bands.lower,
            "bb_bandwidth": bands.bandwidth,
            "bb_percent_b": bands.percent_b,
            "vwap": vwap_values,
        },
    )
