"""Data validation — specification section 35.

Every analysis carries a data quality status, and INVALID means no
trade. The checks here are deliberately boring and deterministic: they
look at the data we actually received and say what is wrong with it.
They never repair a feed by interpolating a missing candle, because a
candle we invented is indistinguishable from one that happened, and the
backtest would never know the difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from statistics import median

from gtcc.domain.enums import DataQuality
from gtcc.domain.market_data import Bar, Quote, utcnow
from gtcc.domain.money import ZERO, D


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    FATAL = "FATAL"


class Finding(StrEnum):
    """Stable codes so dashboards and alerts can key on them."""

    STALE_QUOTE = "STALE_QUOTE"
    STALE_BARS = "STALE_BARS"
    CROSSED_BOOK = "CROSSED_BOOK"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    INCOHERENT_BAR = "INCOHERENT_BAR"
    DUPLICATE_BAR = "DUPLICATE_BAR"
    OUT_OF_ORDER_BAR = "OUT_OF_ORDER_BAR"
    MISSING_BARS = "MISSING_BARS"
    WIDE_SPREAD = "WIDE_SPREAD"
    PRICE_OUTLIER = "PRICE_OUTLIER"
    CLOCK_SKEW = "CLOCK_SKEW"
    EMPTY_SERIES = "EMPTY_SERIES"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    UNCLOSED_BAR = "UNCLOSED_BAR"


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: Finding
    severity: Severity
    detail: str


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    """The verdict on one symbol's data at one moment."""

    symbol: str
    checked_at: datetime
    issues: tuple[QualityIssue, ...] = ()
    #: Feeds that were asked for but are not present. Analysts must mark
    #: their conclusions as unavailable rather than substituting.
    unavailable_feeds: tuple[str, ...] = ()

    @property
    def status(self) -> DataQuality:
        if any(issue.severity is Severity.FATAL for issue in self.issues):
            return DataQuality.INVALID
        if any(issue.severity is Severity.WARNING for issue in self.issues):
            return DataQuality.DEGRADED
        return DataQuality.GOOD

    @property
    def tradeable(self) -> bool:
        return self.status.tradeable

    @property
    def codes(self) -> tuple[Finding, ...]:
        return tuple(issue.code for issue in self.issues)

    def summary(self) -> str:
        if not self.issues:
            return f"{self.symbol}: data GOOD"
        parts = ", ".join(f"{issue.code}({issue.severity})" for issue in self.issues)
        return f"{self.symbol}: data {self.status} — {parts}"


@dataclass
class _Collector:
    issues: list[QualityIssue] = field(default_factory=list)

    def add(self, code: Finding, severity: Severity, detail: str) -> None:
        self.issues.append(QualityIssue(code=code, severity=severity, detail=detail))


def check_quote(
    quote: Quote,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 5.0,
    max_spread_bps: Decimal | None = None,
    max_clock_skew_seconds: float = 2.0,
) -> DataQualityReport:
    """Validate a top-of-book quote before anything prices off it."""
    now = now or utcnow()
    found = _Collector()

    if quote.bid <= ZERO or quote.ask <= ZERO:
        found.add(
            Finding.NON_POSITIVE_PRICE,
            Severity.FATAL,
            f"bid={quote.bid} ask={quote.ask}",
        )
    if quote.is_crossed:
        found.add(
            Finding.CROSSED_BOOK,
            Severity.FATAL,
            f"bid {quote.bid} is above ask {quote.ask}",
        )

    age = quote.age_seconds(now)
    if age > max_age_seconds:
        found.add(
            Finding.STALE_QUOTE,
            Severity.FATAL,
            f"quote is {age:.1f}s old, limit is {max_age_seconds:.1f}s",
        )

    # A timestamp from the future means our clock or theirs is wrong, and
    # every age calculation downstream is then meaningless.
    if age < -max_clock_skew_seconds:
        found.add(
            Finding.CLOCK_SKEW,
            Severity.FATAL,
            f"quote is timestamped {-age:.1f}s in the future",
        )

    if max_spread_bps is not None and quote.spread_bps > max_spread_bps:
        found.add(
            Finding.WIDE_SPREAD,
            Severity.WARNING,
            f"spread {quote.spread_bps:.1f}bps exceeds {max_spread_bps}bps",
        )

    return DataQualityReport(symbol=quote.symbol, checked_at=now, issues=tuple(found.issues))


def check_bars(
    bars: list[Bar],
    *,
    now: datetime | None = None,
    min_history: int = 50,
    max_age_multiple: float = 2.0,
    outlier_range_multiple: Decimal = D(12),
    unavailable_feeds: tuple[str, ...] = (),
) -> DataQualityReport:
    """Validate an OHLCV series.

    *max_age_multiple* is in units of the bar's own timeframe: a 5m
    series whose newest bar opened 20 minutes ago is stale at a multiple
    of 2, because two bars never arrived.
    """
    now = now or utcnow()
    found = _Collector()
    symbol = bars[0].symbol if bars else "?"

    if not bars:
        found.add(Finding.EMPTY_SERIES, Severity.FATAL, "no bars supplied")
        return DataQualityReport(
            symbol=symbol,
            checked_at=now,
            issues=tuple(found.issues),
            unavailable_feeds=unavailable_feeds,
        )

    timeframe = bars[0].timeframe
    step = timedelta(seconds=timeframe.seconds)

    if len(bars) < min_history:
        found.add(
            Finding.INSUFFICIENT_HISTORY,
            Severity.WARNING,
            f"{len(bars)} bars, {min_history} wanted for stable indicators",
        )

    seen: set[datetime] = set()
    previous: Bar | None = None
    gaps = 0
    for bar in bars:
        if not bar.is_coherent:
            found.add(
                Finding.INCOHERENT_BAR,
                Severity.FATAL,
                f"{bar.timestamp.isoformat()} o={bar.open} h={bar.high} "
                f"l={bar.low} c={bar.close}",
            )
        if bar.timestamp in seen:
            found.add(
                Finding.DUPLICATE_BAR, Severity.FATAL, f"{bar.timestamp.isoformat()} appears twice"
            )
        seen.add(bar.timestamp)

        if previous is not None:
            if bar.timestamp <= previous.timestamp:
                found.add(
                    Finding.OUT_OF_ORDER_BAR,
                    Severity.FATAL,
                    f"{bar.timestamp.isoformat()} follows {previous.timestamp.isoformat()}",
                )
            else:
                missing = int((bar.timestamp - previous.timestamp) / step) - 1
                if missing > 0:
                    gaps += missing
        previous = bar

    if gaps:
        # A handful of absent candles is normal on illiquid symbols and
        # across session breaks; a series that is mostly holes is not.
        severity = Severity.FATAL if gaps > len(bars) * 0.1 else Severity.WARNING
        found.add(Finding.MISSING_BARS, severity, f"{gaps} candle(s) absent from the series")

    newest = bars[-1]
    age = (now - newest.close_time).total_seconds()
    allowed = timeframe.seconds * max_age_multiple
    if age > allowed:
        found.add(
            Finding.STALE_BARS,
            Severity.FATAL,
            f"newest {timeframe} bar closed {age:.0f}s ago, limit is {allowed:.0f}s",
        )
    if not newest.closed:
        found.add(
            Finding.UNCLOSED_BAR,
            Severity.INFO,
            "newest bar is still forming; strategies must not read its close",
        )

    ranges = [bar.range for bar in bars if bar.range > ZERO]
    if len(ranges) >= 20:
        typical = median(ranges)
        if typical > ZERO:
            for bar in bars[-20:]:
                if bar.range > typical * outlier_range_multiple:
                    found.add(
                        Finding.PRICE_OUTLIER,
                        Severity.WARNING,
                        f"{bar.timestamp.isoformat()} range {bar.range} is "
                        f"{bar.range / typical:.0f}x the median",
                    )

    return DataQualityReport(
        symbol=symbol,
        checked_at=now,
        issues=tuple(found.issues),
        unavailable_feeds=unavailable_feeds,
    )


def combine(*reports: DataQualityReport) -> DataQualityReport:
    """Merge reports; the worst status wins."""
    if not reports:
        raise ValueError("combine() needs at least one report")
    issues: list[QualityIssue] = []
    feeds: list[str] = []
    for report in reports:
        issues.extend(report.issues)
        feeds.extend(report.unavailable_feeds)
    return DataQualityReport(
        symbol=reports[0].symbol,
        checked_at=max(report.checked_at for report in reports),
        issues=tuple(issues),
        unavailable_feeds=tuple(dict.fromkeys(feeds)),
    )
