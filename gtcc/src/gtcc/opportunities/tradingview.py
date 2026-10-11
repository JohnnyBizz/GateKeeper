"""A link to the exact chart, so a pick is one click from being checked.

GateKeeper never reads TradingView. TradingView's own policy licenses its
market data for display to a person and prohibits machine-driven use of
it, and a scraper would be both a terms violation and worse data than the
numbers a broker serves directly. So the analysis runs on the broker's own
feed, and this module only builds the URL that opens the same instrument
on TradingView for a human to look at.

That is the right division anyway. The machine does the tedious part —
checking every symbol for every setup, every bar — and the person does the
part a person is better at: looking at the chart and deciding.
"""

from __future__ import annotations

from urllib.parse import quote

from gtcc.domain.enums import Timeframe

#: TradingView's interval codes. Minutes as numbers, then D and W.
INTERVAL: dict[Timeframe, str] = {
    Timeframe.M1: "1",
    Timeframe.M3: "3",
    Timeframe.M5: "5",
    Timeframe.M15: "15",
    Timeframe.M30: "30",
    Timeframe.H1: "60",
    Timeframe.H4: "240",
    Timeframe.D1: "D",
    Timeframe.W1: "W",
}

#: How each data venue's symbols appear on TradingView. OANDA's API names a
#: pair EUR_USD; TradingView lists the same instrument as OANDA:EURUSD.
#: Charting the SAME venue's prices matters: two brokers' EUR/USD differ by
#: their spreads and their candle boundaries, so a level that looks clean on
#: one may not exist on the other.
_PREFIX: dict[str, str] = {
    "oanda": "OANDA",
}


def tradingview_symbol(symbol: str, venue: str) -> str:
    """The symbol as TradingView names it.

    For a venue this module does not know, the bare symbol is returned and
    TradingView will pick its own default exchange — which may not be the
    one the analysis used. The page says so beside the link.
    """
    prefix = _PREFIX.get(venue.lower())
    if prefix is None:
        return symbol
    return f"{prefix}:{symbol.replace('_', '').replace('/', '')}"


def chart_url(symbol: str, timeframe: Timeframe, venue: str) -> str:
    return (
        "https://www.tradingview.com/chart/?symbol="
        + quote(tradingview_symbol(symbol, venue), safe="")
        + "&interval="
        + INTERVAL[timeframe]
    )


def is_exact(venue: str) -> bool:
    """Whether the link opens the same venue's prices the analysis used."""
    return venue.lower() in _PREFIX
