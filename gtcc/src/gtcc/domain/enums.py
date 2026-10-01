"""The vocabulary of the platform.

Every value here is persisted somewhere, so treat these as a wire format:
add members freely, rename them only with a migration.
"""

from __future__ import annotations

from enum import StrEnum


class Market(StrEnum):
    """Top-level market class. Used for exposure limits and routing."""

    CRYPTO = "CRYPTO"
    STOCKS = "STOCKS"
    FOREX = "FOREX"
    FUTURES = "FUTURES"
    OPTIONS = "OPTIONS"


class AssetClass(StrEnum):
    """How an instrument's maths works. Sizing dispatches on this."""

    EQUITY = "EQUITY"
    ETF = "ETF"
    CRYPTO_SPOT = "CRYPTO_SPOT"
    CRYPTO_PERP = "CRYPTO_PERP"
    CRYPTO_FUTURE = "CRYPTO_FUTURE"
    FOREX_SPOT = "FOREX_SPOT"
    FUTURE = "FUTURE"
    OPTION = "OPTION"


class TradingMode(StrEnum):
    """The three modes from the specification. PAPER is the default."""

    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Decision(StrEnum):
    """What the pipeline concluded. WAIT is a first-class answer."""

    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"
    REDUCE = "REDUCE"
    EXIT = "EXIT"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 for long exposure, -1 for short."""
        return 1 if self is Side.BUY else -1


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"


class TimeInForce(StrEnum):
    GTC = "GTC"
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    """The order lifecycle from the specification, section 18.

    An order is never FILLED because we submitted it — only because the
    broker said so. :mod:`gtcc.execution.oms` enforces the transitions.
    """

    REQUESTED = "REQUESTED"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES

    @property
    def is_open(self) -> bool:
        """True while the order can still consume buying power."""
        return self in (
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        )


_TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.CLOSED}
)


class DataQuality(StrEnum):
    """Specification section 35. INVALID data means no trade, full stop."""

    GOOD = "GOOD"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"

    @property
    def tradeable(self) -> bool:
        return self is not DataQuality.INVALID


class Regime(StrEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    BREAKOUT = "BREAKOUT"
    CHOPPY = "CHOPPY"
    EVENT_RISK = "EVENT_RISK"
    UNKNOWN = "UNKNOWN"


class RiskAction(StrEnum):
    """What the deterministic risk engine decided to do with a request."""

    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    REJECT = "REJECT"


class Timeframe(StrEnum):
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"

    @property
    def seconds(self) -> int:
        return _TIMEFRAME_SECONDS[self]

    @property
    def is_intraday(self) -> bool:
        return self.seconds < _TIMEFRAME_SECONDS[Timeframe.D1]


_TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M3: 180,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
    Timeframe.W1: 604800,
}
