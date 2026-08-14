"""Turning the platform's tick stream into candles.

The feed sends ticks, not candles::

    [["EURUSD_otc", 1786662847.12, 1.16233]]

— symbol, unix time, price — arriving several times a second under the
``updateStream`` event. That is strictly more information than the chart shows:
candles are just ticks grouped into buckets, so any timeframe can be built from
them, and the numbers are exact rather than measured off a picture.

The one thing ticks cannot provide is the past. A stream that started a minute
ago holds a minute of history, and the analysis wants sixty candles, so the
platform's own history message is still needed to backfill. Until then this
reports honestly how much it has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable

from ..models import Candle, Series

# EURUSD_otc, AUDCAD_otc, EURUSD, BTCUSD_otc — a six-letter pair, sometimes
# with a suffix marking the platform's synthetic out-of-hours instruments.
_SYMBOL = re.compile(r"^([A-Z]{6,8})(?:[_-](otc|OTC))?$")


def display_symbol(raw: str) -> str:
    """``EURUSD_otc`` becomes ``EUR/USD OTC``.

    Reading the pair from the feed rather than from the screen removes the
    whole class of mistake where a browser tab or a menu item is read as an
    instrument: this string comes from the platform itself.
    """
    text = str(raw).strip()
    match = _SYMBOL.match(text)
    if not match:
        return text.upper()

    body, otc = match.group(1).upper(), match.group(2)
    suffix = " OTC" if otc else ""
    if len(body) == 6:
        return f"{body[:3]}/{body[3:]}{suffix}"
    return f"{body}{suffix}"


@dataclass(frozen=True)
class Tick:
    """One price at one moment."""

    symbol: str
    timestamp: float
    price: float

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)


def parse_ticks(payload: Any) -> list[Tick]:
    """Pull ticks out of a decoded ``updateStream`` payload.

    Accepts the bare array the binary attachment carries, and also the wrapped
    ``["updateStream", [...]]`` form, since which one arrives depends on
    whether the frame used a binary attachment or not.
    """
    if isinstance(payload, list) and payload and isinstance(payload[0], str):
        payload = payload[1] if len(payload) > 1 else []

    if not isinstance(payload, list):
        return []

    # A single tick may arrive unwrapped rather than inside a list of ticks.
    rows = payload
    if rows and not isinstance(rows[0], (list, tuple)):
        rows = [rows]

    ticks: list[Tick] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        symbol, when, price = row[0], row[1], row[2]
        if not isinstance(symbol, str):
            continue
        try:
            ticks.append(Tick(symbol, float(when), float(price)))
        except (TypeError, ValueError):
            continue
    return ticks


@dataclass
class CandleBuilder:
    """Groups ticks into OHLC candles of a fixed period.

    Deliberately keeps the in-progress candle separate from the settled ones.
    The newest bar on a chart is always half-formed, and treating it as
    finished is how a pattern gets called on a candle that has not closed.
    """

    period_seconds: int = 60
    max_candles: int = 500
    symbol: str = ""
    _closed: list[Candle] = field(default_factory=list, repr=False)
    _open: Candle | None = field(default=None, repr=False)

    def _bucket(self, timestamp: float) -> datetime:
        period = max(1, int(self.period_seconds))
        start = int(timestamp) - (int(timestamp) % period)
        return datetime.fromtimestamp(start, tz=timezone.utc)

    def add(self, tick: Tick) -> None:
        if self.symbol and tick.symbol != self.symbol:
            return
        if not self.symbol:
            self.symbol = tick.symbol

        start = self._bucket(tick.timestamp)
        if self._open is None:
            self._open = self._forming(start, tick.price, tick.price, tick.price)
            return

        if start > self._open.timestamp:
            # The bucket has rolled over, so the bar that was forming is now a
            # closed bar and may be marked as one.
            self._closed.append(replace(self._open, complete=True))
            if len(self._closed) > self.max_candles:
                del self._closed[: len(self._closed) - self.max_candles]
            self._open = self._forming(start, tick.price, tick.price, tick.price)
            return
        if start < self._open.timestamp:
            return  # a tick from before the current bucket; the past is settled

        current = self._open
        self._open = self._forming(
            current.timestamp,
            current.open,
            max(current.high, tick.price),
            min(current.low, tick.price),
            tick.price,
        )

    @staticmethod
    def _forming(start, open_, high, low, close=None) -> Candle:
        """The in-progress bar, marked as such.

        Calling it complete is a lie the analysis believes: a bar ten seconds
        into its minute has a tiny body and reads as a doji or a pin bar, and
        forty seconds later it is a full-bodied candle pointing the other way.
        """
        return Candle(
            start,
            open_,
            high,
            low,
            close if close is not None else open_,
            complete=False,
        )

    def extend(self, ticks: Iterable[Tick]) -> None:
        for tick in ticks:
            self.add(tick)

    def seed(self, candles: Iterable[Candle]) -> None:
        """Backfill settled candles from the platform's history message.

        The platform's history usually ends with the bar that is still running,
        and the tick stream is building that same bar. Taking both would put two
        candles with one timestamp into the series — a duplicate bar the
        indicators would happily average over. The forming bar has one owner
        here, and it is the tick stream.
        """
        known = {candle.timestamp for candle in self._closed}
        # The current wall-clock bucket is unfinished whether or not a tick has
        # opened it yet, so history for it is dropped either way — otherwise a
        # backfill that lands before the first tick seeds a "closed" bar that
        # the stream then builds again.
        cutoff = self._bucket(datetime.now(tz=timezone.utc).timestamp())
        if self._open is not None:
            cutoff = min(cutoff, self._open.timestamp)
        for candle in candles:
            if candle.timestamp in known:
                continue
            if candle.timestamp >= cutoff:
                continue
            self._closed.append(replace(candle, complete=True))
            known.add(candle.timestamp)
        self._closed.sort(key=lambda candle: candle.timestamp)
        del self._closed[: max(0, len(self._closed) - self.max_candles)]

    @property
    def settled(self) -> list[Candle]:
        return list(self._closed)

    @property
    def forming(self) -> Candle | None:
        return self._open

    def series(self, include_forming: bool = True) -> Series:
        # One timestamp, one candle. Two bars sharing a bucket would be
        # averaged over silently by every indicator downstream, so the invariant
        # is enforced here rather than trusted.
        candles = list(self._closed)
        if include_forming and self._open is not None:
            if candles and self._open.timestamp <= candles[-1].timestamp:
                candles = [c for c in candles if c.timestamp < self._open.timestamp]
            candles.append(self._open)
        return Series(
            candles=tuple(candles),
            timeframe_seconds=int(self.period_seconds),
            symbol=display_symbol(self.symbol) if self.symbol else "UNKNOWN",
        )
