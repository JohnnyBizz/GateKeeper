"""A recording is the input to every measurement, so it must not lie.

Three rules carry the weight. A forming candle is never written, because
its close is not a close. An existing recording is extended rather than
rewritten, so two backtests of the same period cannot disagree without a
record of why. And the contract specification is saved with the data, so a
backtest sizes against what was true when the bars were taken.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gtcc.data.recorder import (
    read_sidecar,
    record,
    spec_from_dict,
    spec_to_dict,
    write_recording,
)
from gtcc.domain.enums import AssetClass, Market, Timeframe
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar
from gtcc.domain.money import D

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _spec(symbol: str = "EUR_USD") -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol, market=Market.FOREX, asset_class=AssetClass.FOREX_SPOT,
        quote_currency="USD", base_currency="EUR", tick_size=D("0.00001"),
        pip_size=D("0.0001"), lot_step=D("1"), min_qty=D("1"),
        max_leverage=D("30"), taker_fee_bps=D("0.5"),
    )


def _bar(index: int, close: str, *, closed: bool = True, symbol: str = "EUR_USD") -> Bar:
    value = D(close)
    return Bar(
        symbol=symbol, timeframe=Timeframe.M15,
        timestamp=START + timedelta(minutes=15 * index),
        open=value, high=value * D("1.001"), low=value * D("0.999"),
        close=value, volume=D("1000"), closed=closed,
    )


class TestFormingCandlesAreNeverWritten:
    def test_an_unclosed_bar_is_dropped_and_counted(self, tmp_path):
        bars = [_bar(0, "1.08"), _bar(1, "1.09"), _bar(2, "1.10", closed=False)]

        report = write_recording(tmp_path, "EUR_USD", Timeframe.M15, bars)

        assert report.received == 3
        assert report.unclosed_dropped == 1
        assert report.total_on_disk == 2
        assert "has no close" in "\n".join(report.describe())

    def test_the_dropped_bar_is_absent_from_the_file(self, tmp_path):
        write_recording(
            tmp_path, "EUR_USD", Timeframe.M15,
            [_bar(0, "1.08"), _bar(1, "9.99", closed=False)],
        )

        text = (tmp_path / "EUR_USD_15m.csv").read_text()
        assert "9.99" not in text


class TestRecordingsAreExtendedNotRewritten:
    def test_a_second_run_adds_only_new_bars(self, tmp_path):
        write_recording(tmp_path, "EUR_USD", Timeframe.M15, [_bar(0, "1.08")])

        report = write_recording(
            tmp_path, "EUR_USD", Timeframe.M15, [_bar(0, "1.08"), _bar(1, "1.09")]
        )

        assert report.already_present == 1
        assert report.added == 1
        assert report.total_on_disk == 2

    def test_a_venue_revising_history_is_reported_not_applied(self, tmp_path):
        """Two backtests of the same period must not silently disagree."""
        write_recording(tmp_path, "EUR_USD", Timeframe.M15, [_bar(0, "1.08")])

        report = write_recording(
            tmp_path, "EUR_USD", Timeframe.M15, [_bar(0, "1.5")]
        )

        assert report.conflicts == (START,)
        assert report.added == 0
        text = (tmp_path / "EUR_USD_15m.csv").read_text()
        assert "1.08" in text, "the original bar is untouched"
        assert "overwrite=True" in "\n".join(report.describe())

    def test_overwrite_takes_the_venues_version(self, tmp_path):
        write_recording(tmp_path, "EUR_USD", Timeframe.M15, [_bar(0, "1.08")])

        report = write_recording(
            tmp_path, "EUR_USD", Timeframe.M15, [_bar(0, "1.5")], overwrite=True
        )

        assert report.conflicts == (START,)
        assert "1.5" in (tmp_path / "EUR_USD_15m.csv").read_text()

    def test_bars_are_written_in_time_order(self, tmp_path):
        write_recording(
            tmp_path, "EUR_USD", Timeframe.M15,
            [_bar(3, "1.11"), _bar(0, "1.08"), _bar(2, "1.10"), _bar(1, "1.09")],
        )

        lines = (tmp_path / "EUR_USD_15m.csv").read_text().strip().splitlines()[1:]
        stamps = [line.split(",")[0] for line in lines]
        assert stamps == sorted(stamps)


class TestTheSpecTravelsWithTheData:
    def test_the_spec_round_trips_exactly(self):
        spec = _spec()

        restored = spec_from_dict(spec_to_dict(spec))

        assert restored == spec

    def test_a_recording_carries_the_spec(self, tmp_path):
        write_recording(
            tmp_path, "EUR_USD", Timeframe.M15, [_bar(0, "1.08")], spec=_spec()
        )

        sidecar = read_sidecar(tmp_path, "EUR_USD", Timeframe.M15)

        assert sidecar["instrument"]["tick_size"] == "0.00001"
        assert spec_from_dict(sidecar["instrument"]) == _spec()

    def test_a_later_run_without_a_spec_keeps_the_recorded_one(self, tmp_path):
        """A run with no live connection must not strip what an earlier one knew."""
        write_recording(
            tmp_path, "EUR_USD", Timeframe.M15, [_bar(0, "1.08")], spec=_spec()
        )

        write_recording(tmp_path, "EUR_USD", Timeframe.M15, [_bar(1, "1.09")])

        sidecar = read_sidecar(tmp_path, "EUR_USD", Timeframe.M15)
        assert spec_from_dict(sidecar["instrument"]) == _spec()

    def test_a_missing_tick_size_raises_rather_than_defaulting(self):
        """A default tick size would mis-size every position, quietly."""
        raw = spec_to_dict(_spec())
        del raw["tick_size"]

        with pytest.raises(ValueError, match="tick_size"):
            spec_from_dict(raw)

    def test_a_missing_sidecar_is_an_error_not_an_empty_dict(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="unknown"):
            read_sidecar(tmp_path, "NOTHING", Timeframe.M15)

    def test_provenance_records_the_venue_and_the_moment(self, tmp_path):
        when = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

        write_recording(
            tmp_path, "EUR_USD", Timeframe.M15, [_bar(0, "1.08")],
            venue="oanda", now=when,
        )

        sidecar = read_sidecar(tmp_path, "EUR_USD", Timeframe.M15)
        assert sidecar["venue"] == "oanda"
        assert sidecar["recorded_at"] == when.isoformat()
        assert sidecar["bars"] == 1


class TestRecordingFromAnAdapter:
    def test_it_asks_the_adapter_and_saves_what_it_gets(self, tmp_path):
        from tests.support import StrictDataAdapter

        adapter = StrictDataAdapter(
            instruments={"EUR_USD": _spec()},
            quotes={},
            bars={"EUR_USD": [_bar(index, f"1.0{index}") for index in range(5)]},
        )

        report = record(adapter, tmp_path, "EUR_USD", Timeframe.M15)

        assert report.total_on_disk == 5
        assert report.first_at == START
        sidecar = read_sidecar(tmp_path, "EUR_USD", Timeframe.M15)
        assert spec_from_dict(sidecar["instrument"]) == _spec()

    def test_an_adapter_failure_propagates(self, tmp_path):
        """A short recording that looks like a quiet market is worse."""
        from gtcc.adapters.errors import ConnectionUnhealthy
        from tests.support import StrictDataAdapter

        adapter = StrictDataAdapter(
            instruments={"EUR_USD": _spec()}, quotes={},
            bars={"EUR_USD": [_bar(0, "1.08")]},
            bars_error=ConnectionUnhealthy("test", "the venue is down"),
        )

        with pytest.raises(ConnectionUnhealthy):
            record(adapter, tmp_path, "EUR_USD", Timeframe.M15)

        assert not (tmp_path / "EUR_USD_15m.csv").exists()


class TestTheWholeChain:
    def test_a_recording_can_be_replayed_and_backtested_offline(self, tmp_path):
        """Record, then measure, with no live connection in between."""
        from gtcc.adapters.replay import ReplayAdapter
        from gtcc.backtest import BacktestSettings, Backtester
        from gtcc.risk.limits import parse_limits
        from tests.conftest import LIMITS_RAW
        from tests.support import StrictDataAdapter
        import tests.test_backtest as bt

        # A rising series, recorded through the normal path.
        bars = [
            Bar(
                symbol="EUR_USD", timeframe=Timeframe.M15,
                timestamp=START + timedelta(minutes=15 * index),
                open=D(f"{100 + index * 0.5:.2f}"),
                high=D(f"{100 + index * 0.5 + 0.3:.2f}"),
                low=D(f"{100 + index * 0.5 - 0.3:.2f}"),
                close=D(f"{100 + index * 0.5:.2f}"),
                volume=D("1000"), closed=True,
            )
            for index in range(80)
        ]
        source = StrictDataAdapter(
            instruments={"EUR_USD": _spec()}, quotes={}, bars={"EUR_USD": bars}
        )
        record(source, tmp_path, "EUR_USD", Timeframe.M15)

        # Replay it back with nothing but the files.
        replayed = ReplayAdapter(directory=tmp_path).get_bars(
            "EUR_USD", Timeframe.M15, limit=0
        )
        assert len(replayed) == 80

        sidecar = read_sidecar(tmp_path, "EUR_USD", Timeframe.M15)
        spec = spec_from_dict(sidecar["instrument"])

        result = Backtester(limits=parse_limits(LIMITS_RAW)).run(
            bt._AtBar(at=60, stop="125", target="145"),
            replayed,
            spec,
            BacktestSettings(
                symbol="EUR_USD", timeframe=Timeframe.M15, warmup_bars=50
            ),
        )

        assert len(result.trades) == 1
        assert result.trades[0].quantity > 0
