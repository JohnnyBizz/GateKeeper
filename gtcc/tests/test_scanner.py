"""The scanner's job is to report what it found AND what it could not see.

A scanner that silently drops the symbols it failed to read turns "I
could not look at these" into "there is nothing in these". The second is
a market conclusion, and it is the one that makes somebody stop watching
a market. Most of these tests exist to pin that distinction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gtcc.adapters.errors import ConnectionUnhealthy, FeatureUnavailable
from gtcc.domain.enums import AssetClass, Market, Timeframe, TradingMode
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, Quote
from gtcc.domain.money import D
from gtcc.scanner import ScanSettings, ScanStatus, Scanner, SortKey
from gtcc.strategies.base import (
    Decision,
    Proposal,
    Strategy,
    StrategyContext,
    ValidationStatus,
)
from gtcc.strategies.registry import StrategyRegistry

from tests.support import FixtureMisuse, StrictDataAdapter

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _spec(symbol: str, market: Market = Market.STOCKS) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol, market=market, asset_class=AssetClass.EQUITY,
        quote_currency="USD", tick_size=D("0.01"), lot_step=D("1"), min_qty=D("1"),
    )


def _bars(
    symbol: str,
    closes: list[str],
    *,
    volumes: list[str] | None = None,
    timeframe: Timeframe = Timeframe.M15,
    closed: bool = True,
    end: datetime = NOW,
) -> list[Bar]:
    """A series ending at *end*, so freshness checks see current data."""
    step = timedelta(seconds=timeframe.seconds)
    out = []
    count = len(closes)
    for index, close in enumerate(closes):
        value = D(close)
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=end - step * (count - 1 - index),
                open=value,
                high=value * D("1.004"),
                low=value * D("0.996"),
                close=value,
                volume=D(volumes[index]) if volumes else D("10000"),
                closed=closed,
            )
        )
    return out


def _rising(symbol: str, count: int = 200, **kwargs) -> list[Bar]:
    return _bars(symbol, [str(100 + index * 0.25) for index in range(count)], **kwargs)


def _quote(symbol: str, price: str = "150") -> Quote:
    mid = D(price)
    return Quote(
        symbol=symbol, timestamp=NOW, bid=mid - D("0.01"), ask=mid + D("0.01"),
        bid_size=D("1000"), ask_size=D("1000"), received_at=NOW,
    )


class AlwaysLong(Strategy):
    """A strategy that proposes on every bar, so the plumbing is visible."""

    name = "always-long"
    validation = ValidationStatus.OUT_OF_SAMPLE

    def __init__(self, conviction: int = 5) -> None:
        self._conviction = conviction

    def evaluate(self, context: StrategyContext) -> Proposal:
        last = context.bars[-1].close
        return Proposal(
            decision=Decision.LONG,
            strategy=self.name,
            rationale="the fixture always proposes",
            entry=last,
            stop=last * D("0.99"),
            targets=(last * D("1.03"),),
            conviction=self._conviction,
        )


@pytest.fixture
def adapter():
    symbols = ["AAA", "BBB", "CCC"]
    return StrictDataAdapter(
        instruments={symbol: _spec(symbol) for symbol in symbols},
        quotes={symbol: _quote(symbol) for symbol in symbols},
        bars={symbol: _rising(symbol) for symbol in symbols},
        now=NOW,
    )


@pytest.fixture
def scanner(adapter):
    registry = StrategyRegistry()
    registry.register(AlwaysLong())
    return Scanner(
        adapter,
        registry=registry,
        settings=ScanSettings(higher_timeframe=None, min_history=60),
    )


class TestEverySymbolGetsARow:
    def test_a_row_per_symbol_requested(self, scanner):
        result = scanner.scan(["AAA", "BBB", "CCC"], now=NOW)

        assert len(result.rows) == 3
        assert result.requested == 3
        assert [row.symbol for row in result.rows] == ["AAA", "BBB", "CCC"]

    def test_an_unlisted_symbol_is_reported_not_dropped(self, scanner):
        result = scanner.scan(["AAA", "NOSUCHTHING", "BBB"], now=NOW)

        assert len(result.rows) == 3
        missing = next(row for row in result.rows if row.symbol == "NOSUCHTHING")
        assert missing.status is ScanStatus.UNAVAILABLE
        assert "not listed" in missing.detail
        # And it is not counted among the analysed rows.
        assert missing not in result.analysed
        assert len(result.analysed) == 2

    def test_a_venue_failure_is_reported_not_dropped(self):
        adapter = StrictDataAdapter(
            instruments={"AAA": _spec("AAA")},
            quotes={"AAA": _quote("AAA")},
            bars={"AAA": _rising("AAA")},
            bars_error=ConnectionUnhealthy("test-venue", "the venue returned 503"),
            now=NOW,
        )
        result = Scanner(
            adapter, settings=ScanSettings(higher_timeframe=None)
        ).scan(["AAA"], now=NOW)

        row = result.rows[0]
        assert row.status is ScanStatus.FAILED
        assert "503" in row.detail
        assert row.price is None, "a failed read must not carry a price"

    def test_thin_history_is_refused_rather_than_analysed(self):
        """Five bars where sixty are required is not a market finding.

        The structure engine would answer from five bars. That answer
        would be a trend of UNCLEAR, which reads as "looked, saw nothing
        definite" rather than "never had enough to look".
        """
        adapter = StrictDataAdapter(
            instruments={"AAA": _spec("AAA")},
            quotes={"AAA": _quote("AAA")},
            bars={"AAA": _rising("AAA", count=5)},
            now=NOW,
        )
        result = Scanner(
            adapter, settings=ScanSettings(higher_timeframe=None, min_history=60)
        ).scan(["AAA"], now=NOW)

        row = result.rows[0]
        assert row.status is ScanStatus.REFUSED
        assert row.bars_seen == 5
        assert "60" in row.detail, "say what was needed, not just that it failed"
        assert row.trend is None, "five bars have not earned a trend"
        assert row.regime is None, "nor a regime"
        assert row.atr is None and row.relative_volume is None


class TestMissingMeasurementsAreNotZero:
    def test_a_short_series_reports_no_relative_volume_rather_than_zero(self):
        """Warm-up is not a dead market."""
        adapter = StrictDataAdapter(
            instruments={"AAA": _spec("AAA")},
            quotes={"AAA": _quote("AAA")},
            bars={"AAA": _rising("AAA", count=70)},
            now=NOW,
        )
        settings = ScanSettings(
            higher_timeframe=None, min_history=60, volume_period=100, change_period=100
        )
        row = Scanner(adapter, settings=settings).scan(["AAA"], now=NOW).rows[0]

        assert row.analysed
        assert row.relative_volume is None
        assert row.change_pct is None

    def test_a_missing_quote_leaves_the_spread_absent(self):
        adapter = StrictDataAdapter(
            instruments={"AAA": _spec("AAA")},
            quotes={"AAA": _quote("AAA")},
            bars={"AAA": _rising("AAA")},
            quote_error=FeatureUnavailable("test-venue", "quotes"),
            now=NOW,
        )
        row = (
            Scanner(adapter, settings=ScanSettings(higher_timeframe=None))
            .scan(["AAA"], now=NOW)
            .rows[0]
        )

        assert row.analysed, "bar analysis does not need a quote"
        assert row.spread_bps is None
        assert row.price is not None, "the last close is still a real number"


class TestTheRequestBudget:
    def test_symbols_beyond_the_budget_are_marked_never_looked_at(self, adapter):
        # Two venue calls per symbol with no quote and no higher timeframe.
        settings = ScanSettings(
            higher_timeframe=None, include_quote=False, max_requests=4
        )
        result = Scanner(adapter, settings=settings).scan(["AAA", "BBB", "CCC"], now=NOW)

        statuses = {row.symbol: row.status for row in result.rows}
        assert statuses["AAA"] is ScanStatus.SCANNED
        assert statuses["BBB"] is ScanStatus.SCANNED
        assert statuses["CCC"] is ScanStatus.NOT_ATTEMPTED
        assert result.truncated

    def test_the_summary_says_so_in_words(self, adapter):
        settings = ScanSettings(
            higher_timeframe=None, include_quote=False, max_requests=2
        )
        result = Scanner(adapter, settings=settings).scan(["AAA", "BBB"], now=NOW)

        summary = result.summary()
        assert "budget" in summary
        assert "not a finding" in summary

    def test_a_truncated_scan_never_asked_the_venue_again(self, adapter):
        settings = ScanSettings(
            higher_timeframe=None, include_quote=False, max_requests=2
        )
        Scanner(adapter, settings=settings).scan(["AAA", "BBB", "CCC"], now=NOW)

        asked = {symbol for _, symbol in adapter.calls}
        assert asked == {"AAA"}, "the budget must stop calls, not just mark rows"

    def test_no_budget_means_every_symbol_is_attempted(self, scanner):
        result = scanner.scan(["AAA", "BBB", "CCC"], now=NOW)

        assert not result.truncated
        assert all(row.analysed for row in result.rows)


class TestSignalsAndSilence:
    def test_a_proposal_is_surfaced_on_the_row(self, scanner):
        row = scanner.scan(["AAA"], now=NOW).rows[0]

        assert row.signal is not None
        assert row.signal.strategy == "always-long"

    def test_an_untested_strategy_proposes_nothing_and_says_why(self, adapter):
        class Untested(AlwaysLong):
            name = "untested"
            validation = ValidationStatus.UNTESTED

        registry = StrategyRegistry()
        registry.register(Untested())
        row = (
            Scanner(
                adapter, registry=registry,
                settings=ScanSettings(higher_timeframe=None),
            )
            .scan(["AAA"], now=NOW)
            .rows[0]
        )

        assert row.signal is None
        assert row.silent_because, "silence must carry a reason"
        assert any("untested" in reason.lower() for reason in row.silent_because)

    def test_silence_from_no_strategies_is_distinguishable(self, adapter):
        """An empty registry is not a quiet market."""
        row = (
            Scanner(adapter, settings=ScanSettings(higher_timeframe=None))
            .scan(["AAA"], now=NOW)
            .rows[0]
        )

        assert row.analysed
        assert row.outcomes == ()
        assert row.signal is None
        assert row.silent_because == ()


class TestRanking:
    def test_rows_with_a_signal_sort_first(self, adapter):
        class Quiet(AlwaysLong):
            name = "quiet"

            def evaluate(self, context):
                if context.symbol == "BBB":
                    return super().evaluate(context)
                return Proposal(
                    decision=Decision.WAIT, strategy=self.name, rationale="not BBB"
                )

        registry = StrategyRegistry()
        registry.register(Quiet())
        result = Scanner(
            adapter, registry=registry, settings=ScanSettings(higher_timeframe=None)
        ).scan(["AAA", "BBB", "CCC"], now=NOW)

        assert result.ranked(SortKey.SIGNAL)[0].symbol == "BBB"
        assert [row.symbol for row in result.with_signals] == ["BBB"]

    def test_unread_rows_are_never_ranked(self, scanner):
        result = scanner.scan(["AAA", "NOSUCHTHING"], now=NOW)

        ranked = result.ranked(SortKey.RELATIVE_VOLUME)
        assert [row.symbol for row in ranked] == ["AAA"]
        assert [row.symbol for row in result.not_analysed] == ["NOSUCHTHING"]

    def test_a_missing_measurement_sorts_last_not_as_zero(self):
        """Otherwise an unmeasurable symbol looks like the quietest one."""
        measured = _rising("AAA", volumes=["100"] * 199 + ["90000"])
        adapter = StrictDataAdapter(
            instruments={"AAA": _spec("AAA"), "BBB": _spec("BBB")},
            quotes={"AAA": _quote("AAA"), "BBB": _quote("BBB")},
            bars={"AAA": measured, "BBB": _rising("BBB", count=70)},
            now=NOW,
        )
        settings = ScanSettings(higher_timeframe=None, min_history=60, volume_period=65)
        result = Scanner(adapter, settings=settings).scan(["BBB", "AAA"], now=NOW)

        ranked = result.ranked(SortKey.RELATIVE_VOLUME)
        assert ranked[0].symbol == "AAA", "the spike should lead"
        assert ranked[-1].symbol == "BBB"

    def test_sorting_by_symbol_is_stable_and_total(self, scanner):
        result = scanner.scan(["CCC", "AAA", "BBB"], now=NOW)

        assert [row.symbol for row in result.ranked(SortKey.SYMBOL)] == [
            "AAA", "BBB", "CCC"
        ]


class TestTheScannerCannotTrade:
    def test_it_holds_no_broker(self, scanner):
        assert not hasattr(scanner, "broker")
        assert not hasattr(scanner, "runtime")
        assert not hasattr(scanner, "engine")

    def test_it_never_asks_for_an_unknown_symbol_quietly(self, scanner):
        """The fixture refuses to invent data, so a typo surfaces."""
        with pytest.raises(FixtureMisuse):
            scanner.scan(["TYPO"], now=NOW)


class TestUnclosedBars:
    def test_a_forming_bar_is_excluded_from_analysis(self, adapter):
        """Reading a forming bar's close is reading the future."""
        closed = _rising("AAA", count=200)
        forming = _bars("AAA", ["999"], closed=False, end=NOW)
        adapter._bars["AAA"] = closed + forming

        row = (
            Scanner(adapter, settings=ScanSettings(higher_timeframe=None))
            .scan(["AAA"], now=NOW)
            .rows[0]
        )

        assert row.bars_seen == 200
        assert row.price == closed[-1].close
        assert row.price != D("999")


@pytest.fixture
def data_adapter(instruments, quotes, now):
    """Overrides the shared fixture, which serves no bars.

    The app fixtures build their runtime from whatever `data_adapter`
    resolves to, so this gives the scan endpoint something to analyse
    without changing what every other suite sees.
    """
    return StrictDataAdapter(
        instruments=instruments,
        quotes=quotes,
        bars={
            "BTCUSDT": _rising("BTCUSDT", count=200, timeframe=Timeframe.M15, end=now),
            "AAPL": _rising("AAPL", count=200, timeframe=Timeframe.M15, end=now),
        },
        now=now,
    )


class TestTheApiEndpoint:
    """The route must not let a caller mistake a short scan for a quiet market."""

    def test_a_scan_returns_rows_and_an_account_of_what_was_missed(self, owner_api):
        response = owner_api.post(
            "/api/scan",
            json={"symbols": ["BTCUSDT", "NOSUCHTHING"]},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["requested"] == 2
        assert len(payload["not_analysed"]) == 1
        assert payload["not_analysed"][0]["symbol"] == "NOSUCHTHING"
        assert payload["not_analysed"][0]["status"] == "UNAVAILABLE"
        assert payload["summary"].startswith("2 requested")

    def test_a_truncated_scan_says_so_in_the_payload(self, owner_api):
        response = owner_api.post(
            "/api/scan",
            json={"symbols": ["BTCUSDT", "AAPL"], "max_requests": 1},
        )

        payload = response.json()
        assert payload["truncated"] is True
        assert "not a finding" in payload["summary"]

    def test_scanning_needs_a_session(self, anonymous_api):
        response = anonymous_api.post("/api/scan", json={"symbols": ["BTCUSDT"]})

        assert response.status_code in (401, 403)

    def test_a_missing_measurement_serialises_as_null_not_zero(self, owner_api):
        """JSON nulls, so a dashboard shows a blank rather than a 0.00."""
        payload = owner_api.post(
            "/api/scan", json={"symbols": ["BTCUSDT"]}
        ).json()

        row = payload["rows"][0] if payload["rows"] else payload["not_analysed"][0]
        for column in ("relative_volume", "atr", "change_pct"):
            assert row[column] is None or Decimal(str(row[column])) != 0
