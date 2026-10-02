"""Scan a universe of symbols and report what was found — and what was not.

The scanner's output is a list of rows, one per symbol *requested*, never
one per symbol that happened to work. A symbol whose data was stale, whose
venue errored, or that the scan never reached because its request budget
ran out, appears as a row saying so.

That is the whole design. A scanner that quietly drops the symbols it
could not read turns "I could not look at 28 of these" into "there are no
setups in 28 of these", and those are opposite statements. The second one
is what gets somebody to stop watching a market.

There is also deliberately no composite score. Ranking is by named,
explicit keys, and a row with no signal sorts after a row with one.
Blending relative volume, ATR and conviction into one number would
produce an authoritative-looking ordering whose weights nobody chose and
nobody could defend, which section 14 forbids for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from gtcc.adapters.base import MarketDataAdapter
from gtcc.adapters.errors import AdapterError, FeatureUnavailable
from gtcc.data.quality import DataQuality, check_bars, check_quote
from gtcc.domain.enums import Market, Regime, Timeframe, TradingMode
from gtcc.domain.market_data import Bar, Quote, utcnow
from gtcc.domain.money import D, ZERO
from gtcc.features import indicators
from gtcc.features.regime import RegimeClassifier, RegimeReading
from gtcc.strategies.base import Proposal, StrategyContext
from gtcc.strategies.registry import StrategyOutcome, StrategyRegistry
from gtcc.structure.engine import StructureEngine, StructureReport, Trend


class ScanStatus(StrEnum):
    """Why a row looks the way it does.

    The four failure states are distinct on purpose: "the venue does not
    list this symbol" is a universe problem, "the venue errored" is an
    operational problem, "the data is not fit to analyse" is a market-data
    problem, and "the scan never got here" is a budget problem. Collapsing
    them into one empty row would hide which of the four to go and fix.
    """

    SCANNED = "SCANNED"
    #: Analysed, but the data carried warnings. Read the quality report.
    DEGRADED = "DEGRADED"
    #: The venue does not offer this symbol, or not this timeframe.
    UNAVAILABLE = "UNAVAILABLE"
    #: The venue failed to answer.
    FAILED = "FAILED"
    #: The data arrived and is not fit to analyse.
    REFUSED = "REFUSED"
    #: The request budget ran out before this symbol was reached.
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class SortKey(StrEnum):
    SIGNAL = "SIGNAL"
    RELATIVE_VOLUME = "RELATIVE_VOLUME"
    VOLATILITY = "VOLATILITY"
    CHANGE = "CHANGE"
    SYMBOL = "SYMBOL"


@dataclass(frozen=True, slots=True)
class ScanRow:
    """One symbol's result.

    Every measurement is optional and ``None`` means "not computed", with
    the reason in ``detail``. None of them default to zero: a relative
    volume of 0.0 means a dead market and reading it off a warm-up window
    would be a lie about the market rather than about the computation.
    """

    symbol: str
    status: ScanStatus
    detail: str = ""
    market: Market | None = None
    timeframe: Timeframe | None = None
    price: Decimal | None = None
    change_pct: Decimal | None = None
    atr: Decimal | None = None
    atr_pct: Decimal | None = None
    relative_volume: Decimal | None = None
    spread_bps: Decimal | None = None
    trend: Trend | None = None
    regime: Regime | None = None
    regime_confident: bool | None = None
    structure_summary: str = ""
    data_quality: DataQuality | None = None
    bars_seen: int = 0
    outcomes: tuple[StrategyOutcome, ...] = ()

    @property
    def analysed(self) -> bool:
        return self.status in (ScanStatus.SCANNED, ScanStatus.DEGRADED)

    @property
    def proposals(self) -> tuple[Proposal, ...]:
        return tuple(
            outcome.proposal
            for outcome in self.outcomes
            if outcome.proposal is not None and outcome.proposal.is_actionable
        )

    @property
    def signal(self) -> Proposal | None:
        """The highest-conviction actionable proposal, if any.

        Conviction is the strategy's own ordering of its ideas and is not
        calibrated, which is why this picks between a strategy's own
        proposals and never between different strategies' confidence.
        """
        found = self.proposals
        if not found:
            return None
        return max(found, key=lambda proposal: proposal.conviction)

    @property
    def silent_because(self) -> tuple[str, ...]:
        """Why the strategies that said nothing said nothing."""
        return tuple(
            f"{outcome.strategy}: {outcome.reason}"
            for outcome in self.outcomes
            if outcome.proposal is None or not outcome.proposal.is_actionable
        )


@dataclass(frozen=True, slots=True)
class ScanResult:
    rows: tuple[ScanRow, ...]
    requested: int
    started_at: datetime
    finished_at: datetime
    requests_made: int = 0
    request_budget: int | None = None

    @property
    def analysed(self) -> tuple[ScanRow, ...]:
        return tuple(row for row in self.rows if row.analysed)

    @property
    def not_analysed(self) -> tuple[ScanRow, ...]:
        return tuple(row for row in self.rows if not row.analysed)

    @property
    def truncated(self) -> bool:
        return any(row.status is ScanStatus.NOT_ATTEMPTED for row in self.rows)

    @property
    def with_signals(self) -> tuple[ScanRow, ...]:
        return tuple(row for row in self.rows if row.signal is not None)

    def ranked(self, by: SortKey = SortKey.SIGNAL) -> tuple[ScanRow, ...]:
        """Order the analysed rows by one named key.

        Rows that were not analysed are never ranked: they carry no
        measurement to rank by, and padding them to the bottom with a
        zero would make them look like the worst candidates rather than
        like unread ones. They are in ``not_analysed``.
        """
        rows = list(self.analysed)
        if by is SortKey.SYMBOL:
            return tuple(sorted(rows, key=lambda row: row.symbol))

        def missing_last(value: Decimal | None) -> tuple[int, Decimal]:
            # A row with no measurement sorts after every row that has one,
            # rather than being treated as a measurement of zero.
            return (0, value) if value is not None else (1, ZERO)

        if by is SortKey.RELATIVE_VOLUME:
            rows.sort(key=lambda row: (missing_last(row.relative_volume)[0],
                                       -(row.relative_volume or ZERO), row.symbol))
        elif by is SortKey.VOLATILITY:
            rows.sort(key=lambda row: (missing_last(row.atr_pct)[0],
                                       -(row.atr_pct or ZERO), row.symbol))
        elif by is SortKey.CHANGE:
            rows.sort(key=lambda row: (missing_last(row.change_pct)[0],
                                       -abs(row.change_pct or ZERO), row.symbol))
        else:
            rows.sort(
                key=lambda row: (
                    0 if row.signal is not None else 1,
                    -(row.signal.conviction if row.signal else 0),
                    row.symbol,
                )
            )
        return tuple(rows)

    def summary(self) -> str:
        """One line that cannot be mistaken for a market conclusion."""
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[str(row.status)] = counts.get(str(row.status), 0) + 1
        parts = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
        line = f"{self.requested} requested: {parts}"
        if self.truncated:
            line += (
                f" — the request budget of {self.request_budget} ran out, so some "
                "symbols were never looked at. This is not a finding about them."
            )
        return line


@dataclass(frozen=True, slots=True)
class ScanSettings:
    """What to compute, and how much the venue may be asked for.

    *max_requests* is a ceiling on adapter calls for the whole scan, not
    per symbol. A universe of two hundred symbols against a rate-limited
    venue will exhaust any sensible budget, and stopping with an honest
    NOT_ATTEMPTED beats being throttled into a pile of FAILED rows that
    look like a broken venue.
    """

    timeframe: Timeframe = Timeframe.M15
    bar_limit: int = 300
    min_history: int = 60
    atr_period: int = 14
    volume_period: int = 20
    change_period: int = 20
    quote_max_age_seconds: float = 15.0
    max_requests: int | None = None
    #: One higher timeframe for the structure cascade, or None to skip it.
    higher_timeframe: Timeframe | None = Timeframe.H1
    higher_bar_limit: int = 200
    include_quote: bool = True


class _Budget:
    """Counts adapter calls and refuses to start a symbol it cannot finish."""

    def __init__(self, limit: int | None, per_symbol: int) -> None:
        self.limit = limit
        self.per_symbol = per_symbol
        self.used = 0

    def can_start_symbol(self) -> bool:
        if self.limit is None:
            return True
        return self.used + self.per_symbol <= self.limit

    def spend(self) -> None:
        self.used += 1


class Scanner:
    """Analyses symbols. Cannot trade them.

    Holds a market-data adapter, the analysis engines and the strategy
    registry — no broker, no runtime, no risk engine. A scanner that could
    reach a broker would be a second path to a venue, and there is exactly
    one of those by design.
    """

    def __init__(
        self,
        data: MarketDataAdapter,
        *,
        registry: StrategyRegistry | None = None,
        structure: StructureEngine | None = None,
        classifier: RegimeClassifier | None = None,
        settings: ScanSettings | None = None,
    ) -> None:
        self.data = data
        self.registry = registry or StrategyRegistry()
        self.structure = structure or StructureEngine()
        self.classifier = classifier or RegimeClassifier()
        self.settings = settings or ScanSettings()

    # -- public ---------------------------------------------------------------

    def scan(
        self,
        symbols: Sequence[str],
        *,
        mode: TradingMode = TradingMode.PAPER,
        now: datetime | None = None,
    ) -> ScanResult:
        settings = self.settings
        started = now or utcnow()
        per_symbol = 2 + (1 if settings.include_quote else 0)
        if settings.higher_timeframe is not None:
            per_symbol += 1
        budget = _Budget(settings.max_requests, per_symbol)

        rows: list[ScanRow] = []
        for symbol in symbols:
            if not budget.can_start_symbol():
                rows.append(
                    ScanRow(
                        symbol=symbol,
                        status=ScanStatus.NOT_ATTEMPTED,
                        detail=(
                            f"the scan's budget of {settings.max_requests} venue "
                            f"requests was spent before reaching this symbol"
                        ),
                    )
                )
                continue
            rows.append(self._scan_one(symbol, mode=mode, now=started, budget=budget))

        return ScanResult(
            rows=tuple(rows),
            requested=len(symbols),
            started_at=started,
            finished_at=now or utcnow(),
            requests_made=budget.used,
            request_budget=settings.max_requests,
        )

    # -- one symbol -----------------------------------------------------------

    def _scan_one(
        self, symbol: str, *, mode: TradingMode, now: datetime, budget: _Budget
    ) -> ScanRow:
        settings = self.settings
        try:
            budget.spend()
            instrument = self.data.get_instrument(symbol)
            budget.spend()
            bars = self.data.get_bars(
                symbol, settings.timeframe, limit=settings.bar_limit
            )
        except FeatureUnavailable as exc:
            return ScanRow(symbol=symbol, status=ScanStatus.UNAVAILABLE, detail=str(exc))
        except KeyError as exc:
            return ScanRow(
                symbol=symbol,
                status=ScanStatus.UNAVAILABLE,
                detail=f"the venue does not list {symbol}: {exc}",
            )
        except AdapterError as exc:
            return ScanRow(symbol=symbol, status=ScanStatus.FAILED, detail=str(exc))

        closed = [bar for bar in bars if bar.closed]

        # min_history is the operator saying how much history an answer
        # needs to be worth having. The structure engine has its own, much
        # lower, floor — enough bars for the algorithm to run at all — and
        # it will happily return a trend from five bars. Honouring only
        # that floor would make this setting decorative and put a
        # confident-looking trend on a row that has no business carrying
        # one, so the configured floor is enforced here as a hard refusal.
        if len(closed) < settings.min_history:
            return ScanRow(
                symbol=symbol,
                status=ScanStatus.REFUSED,
                detail=(
                    f"{len(closed)} closed bars, and this scan is configured to "
                    f"need {settings.min_history} before it will draw a conclusion"
                ),
                market=instrument.market,
                timeframe=settings.timeframe,
                bars_seen=len(closed),
            )

        quality = check_bars(
            closed, now=now, min_history=settings.min_history
        )
        if quality.status is DataQuality.INVALID:
            return ScanRow(
                symbol=symbol,
                status=ScanStatus.REFUSED,
                detail=quality.summary(),
                market=instrument.market,
                timeframe=settings.timeframe,
                data_quality=quality.status,
                bars_seen=len(closed),
            )

        quote: Quote | None = None
        spread_bps: Decimal | None = None
        if settings.include_quote:
            try:
                budget.spend()
                quote = self.data.get_quote(symbol)
            except (FeatureUnavailable, AdapterError, KeyError):
                # A missing quote is not fatal to bar analysis. It is
                # recorded as absent rather than filled in from the last
                # close, which would read as a live price.
                quote = None
            else:
                quote_quality = check_quote(
                    quote, now=now, max_age_seconds=settings.quote_max_age_seconds
                )
                if quote_quality.status is DataQuality.INVALID:
                    quote = None
                else:
                    spread_bps = self._spread_bps(quote)

        higher: dict[Timeframe, StructureReport] = {}
        if settings.higher_timeframe is not None:
            try:
                budget.spend()
                higher_bars = [
                    bar
                    for bar in self.data.get_bars(
                        symbol,
                        settings.higher_timeframe,
                        limit=settings.higher_bar_limit,
                    )
                    if bar.closed
                ]
            except (FeatureUnavailable, AdapterError, KeyError):
                higher_bars = []
            if higher_bars:
                higher[settings.higher_timeframe] = self.structure.analyse(higher_bars)

        report = self.structure.analyse(closed)
        regime = self.classifier.classify(closed, structure=report)

        context = StrategyContext(
            symbol=symbol,
            market=instrument.market,
            timeframe=settings.timeframe,
            instrument=instrument,
            bars=closed,
            quote=quote,
            structure=report,
            regime=regime,
            higher_timeframes=higher,
            now=now,
        )
        outcomes = tuple(self.registry.evaluate_all(context, mode))

        return ScanRow(
            symbol=symbol,
            status=(
                ScanStatus.SCANNED
                if quality.status is DataQuality.GOOD
                else ScanStatus.DEGRADED
            ),
            detail="; ".join(issue.detail for issue in quality.issues),
            market=instrument.market,
            timeframe=settings.timeframe,
            price=closed[-1].close if closed else None,
            change_pct=self._change_pct(closed),
            atr=self._last(indicators.atr(closed, settings.atr_period)),
            atr_pct=self._atr_pct(closed, settings.atr_period),
            relative_volume=self._relative_volume(closed),
            spread_bps=spread_bps,
            # UNCLEAR means "analysed, and the market has no clear trend".
            # A series too short to analyse has not earned that statement,
            # so it reports no trend at all.
            trend=None if report.insufficient_history else report.trend,
            regime=regime.regime if regime.confident else None,
            regime_confident=regime.confident,
            structure_summary=report.summary(),
            data_quality=quality.status,
            bars_seen=len(closed),
            outcomes=outcomes,
        )

    # -- measurements ---------------------------------------------------------

    @staticmethod
    def _last(series: Sequence[Decimal | None]) -> Decimal | None:
        return series[-1] if series else None

    def _change_pct(self, bars: Sequence[Bar]) -> Decimal | None:
        period = self.settings.change_period
        if len(bars) <= period:
            return None
        earlier = bars[-1 - period].close
        if earlier <= ZERO:
            return None
        return (bars[-1].close - earlier) / earlier * D(100)

    def _atr_pct(self, bars: Sequence[Bar], period: int) -> Decimal | None:
        value = self._last(indicators.atr(bars, period))
        if value is None or not bars or bars[-1].close <= ZERO:
            return None
        return value / bars[-1].close * D(100)

    def _relative_volume(self, bars: Sequence[Bar]) -> Decimal | None:
        period = self.settings.volume_period
        if len(bars) <= period:
            return None
        return self._last(indicators.relative_volume(bars, period))

    @staticmethod
    def _spread_bps(quote: Quote) -> Decimal | None:
        # Quote.spread_bps reports ZERO for a non-positive mid, which in a
        # scan column would read as a zero spread — the most attractive
        # possible value — rather than as an unusable quote.
        if quote.mid <= ZERO:
            return None
        return quote.spread_bps
