"""Data validation, instrument arithmetic and the replay adapter's honesty."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from gtcc.adapters.errors import FeatureUnavailable
from gtcc.adapters.replay import ReplayAdapter
from gtcc.data.quality import Finding, Severity, check_bars, check_quote, combine
from gtcc.domain.enums import AssetClass, DataQuality, Market, Timeframe
from gtcc.domain.instruments import InstrumentError, InstrumentSpec
from gtcc.domain.market_data import Bar, Quote
from gtcc.domain.money import D, quantize_down
from gtcc.risk.sizing import size_position


def _bars(now, count=60, timeframe=Timeframe.M5):
    step = timedelta(seconds=timeframe.seconds)
    return [
        Bar(
            symbol="X", timeframe=timeframe, timestamp=now - step * (count - i),
            open=D("100"), high=D("101"), low=D("99"), close=D("100.5"), volume=D("10"),
        )
        for i in range(count)
    ]


class TestQuoteValidation:
    def test_a_fresh_two_sided_quote_is_good(self, now):
        quote = Quote(symbol="X", timestamp=now, bid=D("100"), ask=D("100.1"))
        assert check_quote(quote, now=now).status is DataQuality.GOOD

    def test_a_stale_quote_is_invalid(self, now):
        quote = Quote(symbol="X", timestamp=now - timedelta(minutes=1), bid=D("100"), ask=D("100.1"))
        report = check_quote(quote, now=now)

        assert report.status is DataQuality.INVALID
        assert Finding.STALE_QUOTE in report.codes

    def test_a_crossed_book_is_invalid(self, now):
        quote = Quote(symbol="X", timestamp=now, bid=D("101"), ask=D("100"))
        assert Finding.CROSSED_BOOK in check_quote(quote, now=now).codes

    def test_a_future_timestamp_is_a_clock_problem_not_a_fresh_quote(self, now):
        quote = Quote(symbol="X", timestamp=now + timedelta(seconds=30), bid=D("100"), ask=D("100.1"))
        report = check_quote(quote, now=now, max_clock_skew_seconds=2)

        assert Finding.CLOCK_SKEW in report.codes
        assert report.status is DataQuality.INVALID

    def test_a_wide_spread_degrades_rather_than_invalidates(self, now):
        quote = Quote(symbol="X", timestamp=now, bid=D("100"), ask=D("110"))
        report = check_quote(quote, now=now, max_spread_bps=D("50"))

        assert report.status is DataQuality.DEGRADED
        assert report.tradeable


class TestBarValidation:
    def test_a_clean_series_is_good(self, now):
        assert check_bars(_bars(now), now=now).status is DataQuality.GOOD

    def test_an_empty_series_is_invalid(self, now):
        report = check_bars([], now=now)
        assert report.status is DataQuality.INVALID
        assert Finding.EMPTY_SERIES in report.codes

    def test_a_candle_whose_high_is_below_its_low_is_invalid(self, now):
        bars = _bars(now)
        bars[-1] = Bar(
            symbol="X", timeframe=Timeframe.M5, timestamp=bars[-1].timestamp,
            open=D("100"), high=D("98"), low=D("99"), close=D("100"),
        )
        assert Finding.INCOHERENT_BAR in check_bars(bars, now=now).codes

    def test_a_duplicate_timestamp_is_invalid(self, now):
        bars = _bars(now)
        bars.append(bars[-1])
        assert Finding.DUPLICATE_BAR in check_bars(bars, now=now).codes

    def test_a_few_missing_candles_degrade_the_series(self, now):
        bars = _bars(now)
        del bars[10]
        report = check_bars(bars, now=now)

        assert Finding.MISSING_BARS in report.codes
        assert report.status is DataQuality.DEGRADED

    def test_a_series_that_is_mostly_holes_is_invalid(self, now):
        bars = [bar for index, bar in enumerate(_bars(now, 60)) if index % 3 == 0 or index > 55]
        report = check_bars(bars, now=now, min_history=5)

        assert Finding.MISSING_BARS in report.codes
        assert report.status is DataQuality.INVALID

    def test_an_old_last_bar_is_stale(self, now):
        bars = _bars(now + timedelta(hours=-2))
        assert Finding.STALE_BARS in check_bars(bars, now=now).codes

    def test_nothing_is_repaired(self, now):
        """A gap is reported, never filled in. An invented candle is
        indistinguishable from one that happened."""
        bars = _bars(now)
        del bars[10]
        before = len(bars)
        check_bars(bars, now=now)

        assert len(bars) == before

    def test_combining_reports_keeps_the_worst_status(self, now):
        good = check_quote(Quote(symbol="X", timestamp=now, bid=D("1"), ask=D("1.1")), now=now)
        bad = check_bars([], now=now)

        assert combine(good, bad).status is DataQuality.INVALID


class TestInstrumentArithmetic:
    def test_a_futures_contract_without_a_tick_value_is_refused(self):
        with pytest.raises(InstrumentError, match="tick_value"):
            InstrumentSpec(
                symbol="ES", market=Market.FUTURES, asset_class=AssetClass.FUTURE,
                quote_currency="USD",
            )

    def test_a_forex_pair_without_a_pip_size_is_refused(self):
        with pytest.raises(InstrumentError, match="pip_size"):
            InstrumentSpec(
                symbol="EURUSD", market=Market.FOREX, asset_class=AssetClass.FOREX_SPOT,
                quote_currency="USD",
            )

    def test_an_entry_equal_to_the_stop_has_no_definable_risk(self, equity_spec):
        with pytest.raises(InstrumentError, match="no definable risk"):
            equity_spec.risk_per_unit(D("100"), D("100"))

    def test_the_three_asset_shapes_give_three_different_answers(
        self, equity_spec, futures_spec, forex_spec
    ):
        """Same ten-unit price move, three correct and different results."""
        shares = equity_spec.risk_per_unit(D("200"), D("190"))
        contracts = futures_spec.risk_per_unit(D("5000"), D("4990"))

        assert shares == D("10")
        assert contracts == D("500")  # 40 ticks x 12.50
        assert contracts != shares

    def test_futures_notional_uses_the_point_value(self, futures_spec):
        assert futures_spec.notional(D("5000"), D("2")) == D("500000")

    def test_rounding_is_always_downward(self, crypto_spec):
        assert crypto_spec.round_quantity(D("1.99999")) == D("1.9999")
        assert quantize_down(D("7.9"), D("1")) == D("7")

    def test_a_quote_currency_rate_is_required_not_guessed(self, forex_spec):
        at_par = forex_spec.risk_per_unit(D("1.0850"), D("1.0830"))
        converted = forex_spec.risk_per_unit(
            D("1.0850"), D("1.0830"), quote_to_account_rate=D("0.8")
        )

        assert converted == at_par * D("0.8")


class TestSizing:
    def test_a_position_too_small_for_one_lot_is_refused_not_rounded_up(self, futures_spec):
        result = size_position(
            instrument=futures_spec, equity=D("5000"), risk_fraction=D("0.005"),
            entry=D("5000"), stop=D("4990"),
        )

        assert result.quantity == 0
        assert not result.is_tradeable
        assert "less than one lot" in result.rejected_reason

    def test_projected_risk_is_recomputed_after_rounding(self, equity_spec):
        result = size_position(
            instrument=equity_spec, equity=D("100000"), risk_fraction=D("0.005"),
            entry=D("200"), stop=D("197"),
        )

        assert result.quantity == D("166")  # 500 / 3, rounded down
        assert result.projected_risk == D("498")
        assert result.projected_risk <= D("500")

    def test_zero_equity_is_refused(self, equity_spec):
        result = size_position(
            instrument=equity_spec, equity=D("0"), risk_fraction=D("0.005"),
            entry=D("200"), stop=D("198"),
        )

        assert not result.is_tradeable


class TestTheReplayAdapterDoesNotInvent:
    def test_a_bar_only_recording_refuses_to_produce_a_quote(self, tmp_path, now):
        path = tmp_path / "X_5m.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume\n"
            "2026-10-01T12:00:00Z,100,101,99,100.5,10\n",
            encoding="utf-8",
        )
        adapter = ReplayAdapter(directory=tmp_path)
        adapter.get_bars("X", Timeframe.M5)

        with pytest.raises(FeatureUnavailable, match="not a quote"):
            adapter.get_quote("X")

    def test_a_recording_with_bid_and_ask_serves_quotes(self, tmp_path):
        path = tmp_path / "X_5m.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume,bid,ask\n"
            "2026-10-01T12:00:00Z,100,101,99,100.5,10,100.4,100.6\n",
            encoding="utf-8",
        )
        adapter = ReplayAdapter(directory=tmp_path)
        adapter.get_bars("X", Timeframe.M5)

        quote = adapter.get_quote("X")
        assert quote.bid == D("100.4")
        assert quote.ask == D("100.6")

    def test_the_cursor_hides_the_future(self, tmp_path):
        from datetime import datetime, timezone

        rows = "\n".join(
            f"2026-10-01T{12 + i:02d}:00:00Z,100,101,99,100.5,10" for i in range(5)
        )
        (tmp_path / "X_1h.csv").write_text(
            "timestamp,open,high,low,close,volume\n" + rows + "\n", encoding="utf-8"
        )
        adapter = ReplayAdapter(directory=tmp_path)

        adapter.advance_to(datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc))
        visible = adapter.get_bars("X", Timeframe.H1)

        assert len(visible) == 3
        assert visible[-1].timestamp.hour == 14

    def test_an_unregistered_instrument_is_refused_rather_than_assumed(self, tmp_path):
        adapter = ReplayAdapter(directory=tmp_path)

        with pytest.raises(FeatureUnavailable, match="contract spec"):
            adapter.get_instrument("X")

    def test_a_missing_column_is_an_error(self, tmp_path):
        (tmp_path / "X_5m.csv").write_text("timestamp,open,close\n1,2,3\n", encoding="utf-8")
        adapter = ReplayAdapter(directory=tmp_path)

        with pytest.raises(ValueError, match="missing column"):
            adapter.get_bars("X", Timeframe.M5)
