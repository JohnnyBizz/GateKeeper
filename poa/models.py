"""Core data types shared by every layer of the assistant.

Everything downstream (indicators, analysis, signals, storage) speaks in terms
of the types defined here, so this module deliberately has no dependencies
beyond the standard library and numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Sequence

import numpy as np


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class Direction(str, Enum):
    """The directional call the assistant is willing to make."""

    CALL = "CALL"
    PUT = "PUT"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"

    @property
    def emoji(self) -> str:
        return {
            Direction.CALL: "\U0001f7e2",
            Direction.PUT: "\U0001f534",
            Direction.WAIT: "\U0001f7e1",
            Direction.NO_TRADE: "\U0001f6d1",
        }[self]


class Bias(str, Enum):
    """A non-committal directional lean used by individual analysis modules."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

    def inverse(self) -> "Bias":
        if self is Bias.BULLISH:
            return Bias.BEARISH
        if self is Bias.BEARISH:
            return Bias.BULLISH
        return Bias.NEUTRAL


class Regime(str, Enum):
    STRONG_UPTREND = "STRONG_UPTREND"
    WEAK_UPTREND = "WEAK_UPTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"
    WEAK_DOWNTREND = "WEAK_DOWNTREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    POTENTIAL_REVERSAL = "POTENTIAL_REVERSAL"
    BREAKOUT = "BREAKOUT"
    UNCLEAR = "UNCLEAR"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()

    @property
    def bias(self) -> Bias:
        if self in (Regime.STRONG_UPTREND, Regime.WEAK_UPTREND):
            return Bias.BULLISH
        if self in (Regime.STRONG_DOWNTREND, Regime.WEAK_DOWNTREND):
            return Bias.BEARISH
        return Bias.NEUTRAL

    @property
    def tradeable(self) -> bool:
        """Regimes in which a directional signal may be considered at all."""
        return self not in (Regime.UNCLEAR, Regime.HIGH_VOLATILITY)


class SetupQuality(str, Enum):
    VERY_STRONG = "VERY_STRONG"
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    INSUFFICIENT = "INSUFFICIENT"

    @classmethod
    def from_score(cls, score: float) -> "SetupQuality":
        if score >= 90:
            return cls.VERY_STRONG
        if score >= 80:
            return cls.STRONG
        if score >= 70:
            return cls.MODERATE
        if score >= 60:
            return cls.WEAK
        return cls.INSUFFICIENT

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class LevelKind(str, Enum):
    SUPPORT = "SUPPORT"
    RESISTANCE = "RESISTANCE"


class LevelImportance(str, Enum):
    MINOR = "MINOR"
    IMPORTANT = "IMPORTANT"
    MAJOR = "MAJOR"

    @classmethod
    def from_strength(cls, strength: float) -> "LevelImportance":
        if strength >= 75:
            return cls.MAJOR
        if strength >= 50:
            return cls.IMPORTANT
        return cls.MINOR


class SignalState(str, Enum):
    """Lifecycle of an emitted signal, tracked candle by candle."""

    ACTIVE = "ACTIVE"
    WEAKENING = "WEAKENING"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class DurationFit(str, Enum):
    STRONG_MATCH = "STRONG_MATCH"
    GOOD_MATCH = "GOOD_MATCH"
    QUESTIONABLE = "QUESTIONABLE"
    POOR_MATCH = "POOR_MATCH"

    @classmethod
    def from_score(cls, score: float) -> "DurationFit":
        if score >= 85:
            return cls.STRONG_MATCH
        if score >= 70:
            return cls.GOOD_MATCH
        if score >= 55:
            return cls.QUESTIONABLE
        return cls.POOR_MATCH

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()

    @property
    def emoji(self) -> str:
        return {
            DurationFit.STRONG_MATCH: "\U0001f7e2",
            DurationFit.GOOD_MATCH: "\U0001f7e2",
            DurationFit.QUESTIONABLE: "\U0001f7e1",
            DurationFit.POOR_MATCH: "\U0001f534",
        }[self]


# --------------------------------------------------------------------------
# Candles
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candle:
    """A single OHLC(V) bar.

    ``complete`` marks whether the bar has closed. The engine deliberately
    treats the forming bar differently: analysis is anchored on closed candles
    so that a signal does not flicker as the live bar wiggles.
    """

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    complete: bool = True

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"candle high {self.high} below low {self.low}")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"candle {name} is not a finite number: {value}")

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_ratio(self) -> float:
        """Body as a share of the full range; 0.0 for a zero-range candle."""
        if self.range <= 0:
            return 0.0
        return self.body / self.range

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Candle":
        ts = raw["timestamp"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return cls(
            timestamp=ts,
            open=float(raw["open"]),
            high=float(raw["high"]),
            low=float(raw["low"]),
            close=float(raw["close"]),
            volume=None if raw.get("volume") in (None, "") else float(raw["volume"]),
            complete=bool(raw.get("complete", True)),
        )


class Series:
    """An ordered, immutable-ish collection of candles for one timeframe.

    Column access is cached because the indicator layer asks for the same
    numpy arrays repeatedly within a single analysis pass.
    """

    __slots__ = ("candles", "timeframe_seconds", "symbol", "_cache")

    def __init__(
        self,
        candles: Sequence[Candle],
        timeframe_seconds: int,
        symbol: str = "UNKNOWN",
    ) -> None:
        self.candles: tuple[Candle, ...] = tuple(candles)
        self.timeframe_seconds = int(timeframe_seconds)
        self.symbol = symbol
        self._cache: dict[str, np.ndarray] = {}

    # -- container protocol -------------------------------------------------

    def __len__(self) -> int:
        return len(self.candles)

    def __iter__(self) -> Iterable[Candle]:
        return iter(self.candles)

    def __getitem__(self, item):
        if isinstance(item, slice):
            return Series(self.candles[item], self.timeframe_seconds, self.symbol)
        return self.candles[item]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Series(symbol={self.symbol!r}, tf={self.timeframe_seconds}s, "
            f"n={len(self.candles)})"
        )

    # -- columns ------------------------------------------------------------

    def _column(self, name: str) -> np.ndarray:
        cached = self._cache.get(name)
        if cached is None:
            cached = np.array(
                [getattr(c, name) for c in self.candles], dtype=np.float64
            )
            cached.flags.writeable = False
            self._cache[name] = cached
        return cached

    @property
    def open(self) -> np.ndarray:
        return self._column("open")

    @property
    def high(self) -> np.ndarray:
        return self._column("high")

    @property
    def low(self) -> np.ndarray:
        return self._column("low")

    @property
    def close(self) -> np.ndarray:
        return self._column("close")

    @property
    def volume(self) -> np.ndarray | None:
        if not self.candles or any(c.volume is None for c in self.candles):
            return None
        return self._column("volume")

    @property
    def timestamps(self) -> list[datetime]:
        return [c.timestamp for c in self.candles]

    # -- helpers ------------------------------------------------------------

    @property
    def last(self) -> Candle | None:
        return self.candles[-1] if self.candles else None

    @property
    def last_price(self) -> float | None:
        return self.candles[-1].close if self.candles else None

    def closed(self) -> "Series":
        """Drop a trailing incomplete candle, if present."""
        if self.candles and not self.candles[-1].complete:
            return Series(self.candles[:-1], self.timeframe_seconds, self.symbol)
        return self

    def tail(self, n: int) -> "Series":
        return Series(self.candles[-n:], self.timeframe_seconds, self.symbol)

    def append(self, candle: Candle) -> "Series":
        return Series(self.candles + (candle,), self.timeframe_seconds, self.symbol)

    def with_candles(self, candles: Sequence[Candle]) -> "Series":
        return Series(candles, self.timeframe_seconds, self.symbol)


# --------------------------------------------------------------------------
# Analysis result fragments
# --------------------------------------------------------------------------


@dataclass
class Level:
    """A horizontal support or resistance zone."""

    price: float
    kind: LevelKind
    strength: float
    touches: int
    last_touch_index: int
    zone_low: float
    zone_high: float

    @property
    def importance(self) -> LevelImportance:
        return LevelImportance.from_strength(self.strength)

    def distance_from(self, price: float) -> float:
        """Absolute distance to the nearest edge of the zone (0 inside it)."""
        if self.zone_low <= price <= self.zone_high:
            return 0.0
        if price < self.zone_low:
            return self.zone_low - price
        return price - self.zone_high

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["importance"] = self.importance.value
        return data


@dataclass
class ScoreComponent:
    """One weighted contribution to the overall setup score."""

    name: str
    weight: float
    score: float  # normalised 0..1 in favour of the evaluated direction
    detail: str = ""

    @property
    def points(self) -> float:
        return self.weight * self.score

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": round(self.weight, 2),
            "score": round(self.score, 3),
            "points": round(self.points, 2),
            "detail": self.detail,
        }


@dataclass
class DurationCandidate:
    """A candidate expiration together with how well it fits the setup."""

    seconds: int
    score: float
    reason: str = ""

    @property
    def label(self) -> str:
        return format_duration(self.seconds)

    @property
    def fit(self) -> DurationFit:
        return DurationFit.from_score(self.score)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seconds": self.seconds,
            "label": self.label,
            "score": round(self.score, 1),
            "fit": self.fit.value,
            "emoji": self.fit.emoji,
            "reason": self.reason,
        }


@dataclass
class DataQuality:
    """How much the engine trusts the candles it was handed."""

    ok: bool
    confidence: float  # 0..100
    candle_count: int
    issues: list[str] = field(default_factory=list)
    source: str = "unknown"
    timeframe_detected: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "confidence": round(self.confidence, 1),
            "candle_count": self.candle_count,
            "issues": list(self.issues),
            "source": self.source,
            "timeframe_detected": self.timeframe_detected,
        }


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def format_duration(seconds: int) -> str:
    """Render a duration the way a trading platform would label it."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} SEC"
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return f"{hours} HOUR" if hours == 1 else f"{hours} HOURS"
    if seconds % 60 == 0:
        return f"{seconds // 60} MIN"
    return f"{seconds // 60}M {seconds % 60}S"


def format_price(price: float | None, digits: int | None = None) -> str:
    if price is None:
        return "--"
    if digits is None:
        magnitude = abs(price)
        if magnitude >= 1000:
            digits = 2
        elif magnitude >= 10:
            digits = 3
        elif magnitude >= 1:
            digits = 5
        else:
            digits = 6
    return f"{price:.{digits}f}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
