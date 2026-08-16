"""Pocket Option's socket messages, as observed from a live capture.

Everything here was read off a recording rather than a specification, so each
parser is written to fail quietly on a shape it does not recognise: a platform
deploy that renames a field should stop the feed, not invent candles from
whatever arrives instead.

The four messages that matter:

``loadHistoryPeriodFast``
    Settled candles. ``{"asset": "AUDUSD_otc", "data": [{"time":…, "open":…,
    "close":…, "high":…, "low":…, "volume":…}, …]}``. This is the backfill —
    real OHLC with volume, which the screen never gave us.

``updateStream``
    Live ticks, ``[[symbol, unix_time, price]]``, several a second. These build
    the candle currently forming.

``updateHistoryNewFast``
    Recent ticks for one asset, ``{"asset":…, "period":…, "history": [[time,
    price], …]}``. Fills the gap between the last settled candle and now.

``changeSymbol``
    ``{"asset": "AUDUSD_otc", "period": 60}`` — what the user just switched to.
    Authoritative for both the instrument and the timeframe, which are the two
    things pixels could never tell us reliably.

``saveCharts`` / ``loadHistoryPeriod``
    Sent *by* the page rather than to it. The socket carries ticks for many
    instruments at once, so the only trustworthy answer to "which chart is on
    screen" comes from the messages the page itself writes — these two name it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..logging_setup import get_logger
from ..models import Candle
from .ticks import Tick, display_symbol

log = get_logger(__name__)


def _unwrap(payload: Any, name: str) -> Any:
    """Socket.IO events arrive as ``[name, body]``; binary ones as just body."""
    if isinstance(payload, list) and payload and payload[0] == name:
        return payload[1] if len(payload) > 1 else None
    return payload


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


@dataclass(frozen=True)
class SymbolChange:
    """The instrument and timeframe the user just moved to."""

    asset: str
    period_seconds: int

    @property
    def display(self) -> str:
        return display_symbol(self.asset)


def parse_symbol_change(payload: Any) -> SymbolChange | None:
    body = _unwrap(payload, "changeSymbol")
    if not isinstance(body, dict):
        return None
    asset = body.get("asset")
    period = _number(body.get("period"))
    if not isinstance(asset, str) or period is None or period <= 0:
        return None
    return SymbolChange(asset=asset, period_seconds=int(period))


# Sub-minute chart periods the platform actually offers. Anything below a
# minute that is not one of these is more likely a count or an index than a
# timeframe, and a wrong period silently rebuckets every candle.
_SUB_MINUTE_PERIODS = {1, 5, 10, 15, 30}

# How far into the platform's settings nest to look for instrument names.
MAX_NEST_DEPTH = 10


def _plausible_period(value: Any) -> int | None:
    period = _number(value)
    if period is None or period <= 0:
        return None
    seconds = int(period)
    if seconds >= 60 or seconds in _SUB_MINUTE_PERIODS:
        return seconds
    return None


def parse_chart_request(payload: Any) -> tuple[str | None, int | None]:
    """The instrument and period named by a history request the page sent.

    The page asks for history for the chart it is drawing, so this says what is
    on screen even when the symbol was chosen before GateKeeper attached — the
    case that used to leave the panel stuck on whichever instrument happened to
    tick first.
    """
    body = payload
    if isinstance(body, list) and body and isinstance(body[0], str):
        body = body[1] if len(body) > 1 else None
    if not isinstance(body, dict):
        return None, None
    asset = body.get("asset")
    if not isinstance(asset, str) or not asset:
        return None, None
    period = _plausible_period(body.get("period"))
    return asset, period if period else _plausible_period(body.get("chartPeriod"))


def parse_displayed_chart(payload: Any) -> tuple[str | None, int | None]:
    """The chart the page has open, read out of its own ``saveCharts`` state.

    The shape is a nest of the platform's settings rather than a message
    designed to be read, so this walks it looking for a ``symbol``. If the nest
    names more than one instrument it is a workspace of several charts and
    there is no single answer — better to report nothing than to pick one.
    """
    found: dict[str, int | None] = {}
    _collect_symbols(payload, found)
    if len(found) != 1:
        return None, None
    asset, period = next(iter(found.items()))
    return asset, period


def parse_workspace_charts(payload: Any) -> set[str]:
    """Every instrument named by ``saveCharts`` — the user's own chart tabs.

    The same nest :func:`parse_displayed_chart` walks, read for the other
    question. That one asks "which chart is open" and gives up when the answer
    is several; this asks "which charts does the user keep", where several is
    the whole point. The platform's stream carries far more instruments than
    anyone trades, so without this the watchlist fills with whatever ticked
    first rather than with the pairs actually on screen.
    """
    found: dict[str, int | None] = {}
    _collect_symbols(payload, found)
    return {symbol for symbol in found if symbol}


def _collect_symbols(value: Any, out: dict[str, int | None], depth: int = 0) -> None:
    # Deep enough for a settings nest nobody designed to be read, shallow
    # enough to stay a bounded walk. Stopping short costs a workspace that is
    # simply further down, and the only cost of going further is the walk.
    if depth > MAX_NEST_DEPTH:
        return
    if isinstance(value, dict):
        symbol = value.get("symbol")
        if isinstance(symbol, str) and symbol:
            seconds = _plausible_period(value.get("chartPeriod")) or _plausible_period(
                value.get("period")
            )
            # A later, more specific entry should not lose a period an earlier
            # one carried for the same instrument.
            out[symbol] = seconds or out.get(symbol)
        for item in value.values():
            _collect_symbols(item, out, depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_symbols(item, out, depth + 1)


def parse_history_candles(payload: Any) -> tuple[str | None, list[Candle]]:
    """Settled candles from ``loadHistoryPeriodFast``.

    Returns the asset alongside them: history for the pair the user just left
    can arrive after they have already switched away, and folding those into
    the current chart would splice two instruments into one series.
    """
    body = _unwrap(payload, "loadHistoryPeriodFast")
    if not isinstance(body, dict):
        return None, []

    asset = body.get("asset") if isinstance(body.get("asset"), str) else None
    rows = body.get("data")
    if not isinstance(rows, list):
        return asset, []

    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        when = _number(row.get("time"))
        open_ = _number(row.get("open"))
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        close = _number(row.get("close"))
        if None in (when, open_, high, low, close):
            continue
        volume = _number(row.get("volume")) or 0.0
        candles.append(
            Candle(
                timestamp=datetime.fromtimestamp(float(when), tz=timezone.utc),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume),
            )
        )
    candles.sort(key=lambda candle: candle.timestamp)
    return asset, candles


def parse_tick_history(payload: Any) -> tuple[str | None, int | None, list[Tick]]:
    """Recent ticks from ``updateHistoryNewFast``."""
    body = _unwrap(payload, "updateHistoryNewFast")
    if not isinstance(body, dict):
        return None, None, []

    asset = body.get("asset") if isinstance(body.get("asset"), str) else None
    period = _number(body.get("period"))
    rows = body.get("history")
    if not isinstance(rows, list) or asset is None:
        return asset, int(period) if period else None, []

    ticks: list[Tick] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        when, price = _number(row[0]), _number(row[1])
        if when is None or price is None:
            continue
        ticks.append(Tick(symbol=asset, timestamp=when, price=price))
    return asset, int(period) if period else None, ticks


def infer_period(candles: list[Candle]) -> int | None:
    """The spacing between settled candles, which is the chart's timeframe.

    A cross-check rather than a guess: the platform states the period in
    ``changeSymbol``, and history that disagrees with it means the two messages
    describe different charts.
    """
    if len(candles) < 3:
        return None
    gaps = [
        int((candles[i + 1].timestamp - candles[i].timestamp).total_seconds())
        for i in range(len(candles) - 1)
    ]
    gaps = [gap for gap in gaps if gap > 0]
    if not gaps:
        return None
    return max(set(gaps), key=gaps.count)


def parse_payouts(payload: Any) -> dict[str, float]:
    """Per-asset payout percentages from ``updateAssets``.

    The platform sends its whole instrument table as positional rows rather
    than named fields, so this reads by position and checks the shape before
    believing any of it::

        [170, "#AAPL_otc", "Apple OTC", "stock", 3, 92, 60, ...]
           ^index      ^symbol      ^name    ^kind   ^payout %

    Worth having because the payout decides the rate a win has to beat, it
    differs per instrument, and the platform changes it through the day. Typed
    in by hand it goes stale silently and in the flattering direction.
    """
    rows = payload
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("assets")
    if not isinstance(rows, (list, tuple)):
        return {}

    payouts: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        symbol, percent = row[1], row[5]
        if not isinstance(symbol, str) or not symbol:
            continue
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        # A payout is a percentage of the stake returned as profit. Outside
        # this range the column is not the one being read.
        if not 1 <= float(percent) <= 100:
            continue
        payouts[symbol] = round(float(percent) / 100.0, 4)
    return payouts


def parse_settled_trade(payload: Any) -> list[dict[str, Any]]:
    """Trades the user actually placed, and how they actually settled.

    From ``successcloseOrder``, which the platform sends when one of its own
    expires. This is the best evidence there is about anything: a real trade,
    at a real fill, on a real account, settled by the broker rather than by a
    replay's opinion of where price would have been.

    ``command`` is the direction — 1 is a put in every captured example, and
    the recorded prices agree with it. The price move is checked against the
    profit before a trade is believed, so a wrong reading of that field
    discards the row rather than teaching the record something backwards.
    """
    body = payload
    if isinstance(body, list) and body and isinstance(body[0], str):
        body = body[1] if len(body) > 1 else None
    if not isinstance(body, dict):
        return []
    deals = body.get("deals")
    if not isinstance(deals, list):
        return []

    settled: list[dict[str, Any]] = []
    for deal in deals:
        if not isinstance(deal, dict):
            continue
        asset = deal.get("asset")
        opened, closed = deal.get("openPrice"), deal.get("closePrice")
        profit = deal.get("profit")
        if not isinstance(asset, str) or not asset:
            continue
        if not all(isinstance(v, (int, float)) for v in (opened, closed, profit)):
            continue
        if not closed:  # still open; a close price of zero is not a price
            continue

        direction = "PUT" if deal.get("command") == 1 else "CALL"
        won = float(profit) > 0
        # The direction, the price move and the outcome have to agree. When
        # they do not, the reading of one of them is wrong, and a record
        # taught backwards is worse than one not taught at all.
        moved_down = float(closed) < float(opened)
        if float(closed) != float(opened):
            expected = moved_down if direction == "PUT" else not moved_down
            if expected != won:
                log.debug("inconsistent settled trade on %s, skipping", asset)
                continue

        start = deal.get("openTimestamp")
        end = deal.get("closeTimestamp")
        settled.append(
            {
                "asset": asset,
                "direction": direction,
                "won": won,
                "open_price": float(opened),
                "close_price": float(closed),
                "payout": (
                    round(float(deal["percentProfit"]) / 100.0, 4)
                    if isinstance(deal.get("percentProfit"), (int, float))
                    else None
                ),
                "duration": (
                    int(end - start)
                    if isinstance(start, (int, float)) and isinstance(end, (int, float))
                    else None
                ),
                "opened_at": start,
            }
        )
    return settled
