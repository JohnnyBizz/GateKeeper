"""Market data value objects.

Every object here carries the timestamp it describes *and* the moment it
reached us. The gap between the two is what the staleness check in
:mod:`gtcc.data.quality` reads, and a feed that cannot report both is a
feed we cannot trade on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from gtcc.domain.enums import Timeframe
from gtcc.domain.money import ZERO, D


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(moment: datetime, name: str) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware; naive timestamps cause silent drift")
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV candle. *timestamp* is the bar's OPEN time."""

    symbol: str
    timeframe: Timeframe
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = ZERO
    #: False while the candle is still forming. A backtest or a strategy
    #: that reads an unclosed bar's close is using information that did
    #: not exist yet — see the look-ahead guard in the backtest engine.
    closed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _require_utc(self.timestamp, "Bar.timestamp"))

    @property
    def close_time(self) -> datetime:
        from datetime import timedelta

        return self.timestamp + timedelta(seconds=self.timeframe.seconds)

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def is_coherent(self) -> bool:
        """Does this candle obey the definition of a candle?"""
        return (
            self.low <= self.open <= self.high
            and self.low <= self.close <= self.high
            and self.low <= self.high
            and self.low > ZERO
            and self.volume >= ZERO
        )


@dataclass(frozen=True, slots=True)
class Quote:
    """Top of book."""

    symbol: str
    timestamp: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal = ZERO
    ask_size: Decimal = ZERO
    received_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _require_utc(self.timestamp, "Quote.timestamp"))
        object.__setattr__(self, "received_at", _require_utc(self.received_at, "Quote.received_at"))

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / D(2)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> Decimal:
        mid = self.mid
        if mid <= ZERO:
            return ZERO
        return self.spread / mid * D(10000)

    @property
    def is_crossed(self) -> bool:
        """A bid above the ask means the book is broken or stitched from
        two venues. Never trade on it."""
        return self.bid > self.ask

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.timestamp).total_seconds()


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Depth snapshot. *bids* descend, *asks* ascend."""

    symbol: str
    timestamp: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    received_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _require_utc(self.timestamp, "OrderBook.timestamp"))

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    def depth_within(self, side_levels: tuple[BookLevel, ...], limit_price: Decimal) -> Decimal:
        """Total size available at or better than *limit_price*."""
        total = ZERO
        for level in side_levels:
            total += level.size
            if level.price == limit_price:
                break
        return total

    @property
    def imbalance(self) -> Decimal | None:
        """(bid size - ask size) / total, over the whole snapshot.

        None when either side is empty: an imbalance computed against
        nothing is not an imbalance.
        """
        bid_size = sum((lvl.size for lvl in self.bids), ZERO)
        ask_size = sum((lvl.size for lvl in self.asks), ZERO)
        if bid_size <= ZERO or ask_size <= ZERO:
            return None
        return (bid_size - ask_size) / (bid_size + ask_size)


@dataclass(frozen=True, slots=True)
class Trade:
    """A print on the tape."""

    symbol: str
    timestamp: datetime
    price: Decimal
    size: Decimal
    #: Which side was the aggressor, when the venue reports it. None
    #: means unknown — do not infer it from an uptick.
    aggressor: str | None = None
