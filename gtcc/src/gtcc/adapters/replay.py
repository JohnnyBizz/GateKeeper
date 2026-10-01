"""Replay adapter: serves recorded market data from CSV.

This exists so the platform can be developed, demonstrated and tested
without a paid feed, and so a backtest can be reproduced exactly from a
file somebody can inspect.

What it will not do is invent a quote. A bar's close is not a bid and
not an ask, and a spread derived from one is a number nobody observed.
If the file carries ``bid`` and ``ask`` columns this adapter serves
quotes; if it carries only OHLCV, :meth:`get_quote` raises
:class:`~gtcc.adapters.errors.FeatureUnavailable` and the caller must
either find a real quote source or stay out of the market.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from gtcc.adapters.base import AdapterHealth, Capability, MarketDataAdapter
from gtcc.adapters.errors import FeatureUnavailable
from gtcc.domain.enums import Timeframe
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, Quote, utcnow
from gtcc.domain.money import D

_REQUIRED_COLUMNS = {"timestamp", "open", "high", "low", "close"}


def _parse_timestamp(raw: str) -> datetime:
    text = raw.strip()
    if text.isdigit():
        value = int(text)
        # Milliseconds if it is too large to be seconds this century.
        if value > 10_000_000_000:
            value //= 1000
        return datetime.fromtimestamp(value, tz=timezone.utc)
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass
class ReplayAdapter(MarketDataAdapter):
    """Reads one CSV per (symbol, timeframe) from a directory.

    Expected filename: ``<SYMBOL>_<timeframe>.csv``, e.g. ``BTCUSDT_5m.csv``.
    Columns: timestamp, open, high, low, close, and optionally volume,
    bid, ask, bid_size, ask_size.
    """

    directory: Path
    instruments: dict[str, InstrumentSpec] = field(default_factory=dict)
    #: Replay position. Bars at or before this moment are visible; later
    #: ones are not, which is what prevents a backtest from reading the
    #: future through this adapter.
    cursor: datetime | None = None

    name: str = "replay"
    capabilities: frozenset[Capability] = frozenset(
        {Capability.BARS, Capability.HISTORICAL}
    )

    _cache: dict[tuple[str, Timeframe], list[Bar]] = field(default_factory=dict, init=False)
    _quotes: dict[str, list[Quote]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)

    # -- instruments -----------------------------------------------------------

    def register_instrument(self, spec: InstrumentSpec) -> None:
        self.instruments[spec.symbol] = spec

    def get_instrument(self, symbol: str) -> InstrumentSpec:
        try:
            return self.instruments[symbol]
        except KeyError:
            raise FeatureUnavailable(
                self.name,
                "instrument specification",
                f"{symbol} has no registered contract spec; register one before sizing",
            ) from None

    def list_symbols(self) -> list[str]:
        return sorted({path.stem.split("_")[0] for path in self.directory.glob("*.csv")})

    # -- data ---------------------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        limit: int = 500,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        bars = self._load(symbol, timeframe)
        horizon = min(filter(None, [end, self.cursor])) if (end or self.cursor) else None
        if horizon is not None:
            cut = bisect_right([bar.timestamp for bar in bars], horizon)
            bars = bars[:cut]
        if start is not None:
            bars = [bar for bar in bars if bar.timestamp >= start]
        return bars[-limit:] if limit else bars

    def get_quote(self, symbol: str) -> Quote:
        quotes = self._quotes.get(symbol)
        if not quotes:
            raise FeatureUnavailable(
                self.name,
                Capability.QUOTES,
                f"the recording for {symbol} has no bid/ask columns. A bar close "
                "is not a quote, and a spread inferred from one would be invented.",
            )
        horizon = self.cursor
        if horizon is None:
            return quotes[-1]
        index = bisect_right([quote.timestamp for quote in quotes], horizon)
        if index == 0:
            raise FeatureUnavailable(
                self.name, Capability.QUOTES, f"no quote for {symbol} at or before {horizon}"
            )
        return quotes[index - 1]

    def health(self) -> AdapterHealth:
        if not self.directory.exists():
            return AdapterHealth.down(f"recording directory {self.directory} does not exist")
        return AdapterHealth.ok(f"replaying from {self.directory}")

    def advance_to(self, moment: datetime) -> None:
        """Move the replay cursor. Bars after it stay invisible."""
        self.cursor = moment

    # -- loading -------------------------------------------------------------------

    def _load(self, symbol: str, timeframe: Timeframe) -> list[Bar]:
        key = (symbol, timeframe)
        if key in self._cache:
            return self._cache[key]

        path = self.directory / f"{symbol}_{timeframe.value}.csv"
        if not path.exists():
            raise FeatureUnavailable(
                self.name, "bars", f"no recording at {path}"
            )

        bars: list[Bar] = []
        quotes: list[Quote] = []
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = _REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"{path} is missing column(s): {sorted(missing)}")
            has_quotes = {"bid", "ask"} <= set(reader.fieldnames or [])
            for row in reader:
                timestamp = _parse_timestamp(row["timestamp"])
                bars.append(
                    Bar(
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=timestamp,
                        open=D(row["open"]),
                        high=D(row["high"]),
                        low=D(row["low"]),
                        close=D(row["close"]),
                        volume=D(row.get("volume") or 0),
                    )
                )
                if has_quotes and row.get("bid") and row.get("ask"):
                    quotes.append(
                        Quote(
                            symbol=symbol,
                            timestamp=timestamp,
                            bid=D(row["bid"]),
                            ask=D(row["ask"]),
                            bid_size=D(row.get("bid_size") or 0),
                            ask_size=D(row.get("ask_size") or 0),
                            received_at=timestamp,
                        )
                    )

        bars.sort(key=lambda bar: bar.timestamp)
        self._cache[key] = bars
        if quotes:
            quotes.sort(key=lambda quote: quote.timestamp)
            self._quotes[symbol] = quotes
            self.capabilities = self.capabilities | {Capability.QUOTES}
        return bars
