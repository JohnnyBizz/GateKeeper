"""Timeframe resampling.

The chart source gives us one base timeframe. Higher timeframes are built by
aggregating whole groups of base candles, anchored to wall-clock boundaries so
that a "5 minute" candle starts at :00, :05, :10 and so on rather than at an
arbitrary offset.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import Candle, Series


def _bucket_start(ts: datetime, seconds: int) -> datetime:
    """Snap a timestamp down to its timeframe boundary."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)


def resample(series: Series, target_seconds: int) -> Series:
    """Aggregate ``series`` up to ``target_seconds`` per candle.

    Aggregation is only meaningful upwards and on whole multiples. Anything
    else returns the series unchanged rather than inventing data.
    """
    base = series.timeframe_seconds
    if target_seconds <= base or base <= 0:
        return series
    if target_seconds % base != 0:
        return series
    if len(series) == 0:
        return Series((), target_seconds, series.symbol)

    buckets: list[list[Candle]] = []
    current_key: datetime | None = None

    for candle in series:
        key = _bucket_start(candle.timestamp, target_seconds)
        if key != current_key:
            buckets.append([])
            current_key = key
        buckets[-1].append(candle)

    factor = target_seconds // base
    # A series that starts mid-bucket holds only the tail of its first group:
    # the open belongs to a base bar that is not the bucket's first, and the
    # high and low miss whatever the missing bars did. That is not that
    # bucket's candle, so it is dropped rather than shipped with the wrong
    # open — mid-series it would sit there marked incomplete and be read by
    # every indicator anyway.
    if buckets and buckets[0]:
        first = buckets[0][0]
        if _bucket_start(first.timestamp, target_seconds) != first.timestamp:
            buckets = buckets[1:]
    aggregated: list[Candle] = []
    for group in buckets:
        if not group:
            continue
        aggregated.append(
            Candle(
                timestamp=_bucket_start(group[0].timestamp, target_seconds),
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=(
                    sum(c.volume for c in group)
                    if all(c.volume is not None for c in group)
                    else None
                ),
                # A higher-timeframe candle is only closed once every base
                # candle inside it has closed and the bucket is actually full.
                complete=len(group) == factor and all(c.complete for c in group),
            )
        )

    return Series(aggregated, target_seconds, series.symbol)
