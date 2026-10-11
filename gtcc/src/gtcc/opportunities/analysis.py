"""Everything computed once per symbol, before any setup looks at it.

Every setup reads from the same `Analysis`, which is built only from closed
bars handed in by the caller. Nothing here fetches data, so nothing here
can read ahead: the slice the caller passes is the whole of what exists.

Indicators are computed over the last `ANALYSIS_WINDOW` bars rather than
the whole history. That keeps a backtest — which rebuilds this at every bar
— from doing quadratic work, and it means the live finder and the
backtester see identical numbers, because both use the same window.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from gtcc.domain.enums import Timeframe
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, Quote
from gtcc.domain.money import ZERO
from gtcc.features import indicators
from gtcc.features.indicators import IndicatorError, Series
from gtcc.features.regime import RegimeClassifier, RegimeReading
from gtcc.structure.engine import (
    StructureEngine,
    StructureKind,
    StructureReport,
)

#: Bars of history the indicators are computed over.
ANALYSIS_WINDOW = 250

#: The timeframe a top-down read checks above the entry timeframe. Roughly
#: Elder's factor of five between screens, snapped to what venues offer.
HIGHER: dict[Timeframe, Timeframe] = {
    Timeframe.M1: Timeframe.M5,
    Timeframe.M3: Timeframe.M15,
    Timeframe.M5: Timeframe.M30,
    Timeframe.M15: Timeframe.H1,
    Timeframe.M30: Timeframe.H4,
    Timeframe.H1: Timeframe.H4,
    Timeframe.H4: Timeframe.D1,
    Timeframe.D1: Timeframe.W1,
    Timeframe.W1: Timeframe.W1,
}


def _last(series: Series, back: int = 0) -> Decimal | None:
    index = len(series) - 1 - back
    if index < 0:
        return None
    return series[index]


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except IndicatorError:
        return None


@dataclass(frozen=True, slots=True)
class Analysis:
    symbol: str
    timeframe: Timeframe
    bars: tuple[Bar, ...]
    instrument: InstrumentSpec
    structure: StructureReport
    regime: RegimeReading
    quote: Quote | None = None
    higher_timeframe: Timeframe | None = None
    higher_bars: tuple[Bar, ...] = ()
    higher_structure: StructureReport | None = None
    atr: Decimal | None = None
    ema_fast: Series = ()
    ema_slow: Series = ()
    rsi: Series = ()
    macd_histogram: Series = ()
    plus_di: Decimal | None = None
    minus_di: Decimal | None = None
    adx: Decimal | None = None
    relative_volume: Decimal | None = None
    higher_ema_fast: Decimal | None = None
    higher_ema_slow: Decimal | None = None

    @property
    def last(self) -> Bar:
        return self.bars[-1]

    def swing_prices(self, kind: StructureKind) -> list[tuple[int, Decimal]]:
        return [
            (detection.index, detection.price)
            for detection in self.structure.of(kind)
        ]

    @property
    def has_volume(self) -> bool:
        return any(bar.volume > ZERO for bar in self.bars[-30:])


def analyse(
    bars: Sequence[Bar],
    instrument: InstrumentSpec,
    timeframe: Timeframe,
    *,
    quote: Quote | None = None,
    higher_bars: Sequence[Bar] = (),
    higher_timeframe: Timeframe | None = None,
    structure: StructureEngine | None = None,
    classifier: RegimeClassifier | None = None,
) -> Analysis:
    structure = structure or StructureEngine()
    classifier = classifier or RegimeClassifier()

    closed = tuple(bar for bar in bars if bar.closed)[-ANALYSIS_WINDOW:]
    higher = tuple(bar for bar in higher_bars if bar.closed)[-ANALYSIS_WINDOW:]

    report = structure.analyse(closed)
    regime = classifier.classify(closed, structure=report)
    higher_report = structure.analyse(higher) if higher else None

    atr_series = _safe(indicators.atr, closed, 14) or []
    fast = _safe(indicators.ema, closed, 20) or []
    slow = _safe(indicators.ema, closed, 50) or []
    rsi = _safe(indicators.rsi, closed, 14) or []
    macd = _safe(indicators.macd, closed)
    dm = _safe(indicators.directional_movement, closed, 14)
    rel_vol = _safe(indicators.relative_volume, closed, 20) or []
    higher_fast = _safe(indicators.ema, higher, 20) if higher else None
    higher_slow = _safe(indicators.ema, higher, 50) if higher else None

    return Analysis(
        symbol=instrument.symbol,
        timeframe=timeframe,
        bars=closed,
        instrument=instrument,
        structure=report,
        regime=regime,
        quote=quote,
        higher_timeframe=higher_timeframe if higher else None,
        higher_bars=higher,
        higher_structure=higher_report,
        atr=_last(atr_series),
        ema_fast=tuple(fast),
        ema_slow=tuple(slow),
        rsi=tuple(rsi),
        macd_histogram=tuple(macd.histogram) if macd else (),
        plus_di=_last(dm.plus_di) if dm else None,
        minus_di=_last(dm.minus_di) if dm else None,
        adx=_last(dm.adx) if dm else None,
        relative_volume=_last(rel_vol),
        higher_ema_fast=_last(higher_fast) if higher_fast else None,
        higher_ema_slow=_last(higher_slow) if higher_slow else None,
    )
