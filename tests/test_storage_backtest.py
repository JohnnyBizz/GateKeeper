"""Journal persistence, outcome settlement, statistics and the backtester."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import choppy_series, good_quality, pullback_trend
from poa.backtesting import Backtester, breakeven_rate, summarise_outcomes
from poa.backtesting.stats import MIN_MEANINGFUL_SAMPLE
from poa.chart_detection import generate_series
from poa.models import Direction, utcnow
from poa.signals import GateSettings, SignalEngine, SignalRequest


def make_signal(series, trade_duration=180, asset="EUR/USD"):
    return SignalEngine().evaluate(
        SignalRequest(
            series=series,
            asset=asset,
            chart_timeframe=60,
            trade_duration=trade_duration,
            quality=good_quality(series),
            settings=GateSettings(),
        )
    )


class TestJournal:
    def test_a_signal_is_recorded_and_retrievable(self, tmp_journal):
        signal = make_signal(pullback_trend(400, direction=1))
        tmp_journal.record(signal)
        entry = tmp_journal.get(signal.id)
        assert entry is not None
        assert entry["direction"] == signal.direction.value
        assert entry["asset"] == "EUR/USD"

    def test_the_full_analysis_context_is_stored(self, tmp_journal):
        signal = make_signal(pullback_trend(400, direction=1))
        tmp_journal.record(signal)
        entry = tmp_journal.get(signal.id)
        for column in (
            "market_regime",
            "heikin_ashi",
            "trend",
            "rsi",
            "macd",
            "ema_condition",
            "support",
            "resistance",
            "recommended_duration",
            "reason",
            "invalidation",
        ):
            assert column in entry

    def test_recording_the_same_id_twice_does_not_duplicate(self, tmp_journal):
        signal = make_signal(pullback_trend(400, direction=1))
        tmp_journal.record(signal)
        tmp_journal.record(signal)
        assert tmp_journal.count() == 1

    def test_recent_is_ordered_newest_first(self, tmp_journal):
        first = make_signal(pullback_trend(400, direction=1))
        second = make_signal(pullback_trend(400, direction=-1))
        second.timestamp = first.timestamp + timedelta(seconds=60)
        tmp_journal.record(first)
        tmp_journal.record(second)
        assert tmp_journal.recent()[0]["id"] == second.id

    def test_filtering_by_asset(self, tmp_journal):
        tmp_journal.record(make_signal(pullback_trend(400, direction=1), asset="EUR/USD"))
        tmp_journal.record(make_signal(pullback_trend(400, direction=-1), asset="GBP/USD"))
        assert len(tmp_journal.recent(asset="EUR/USD")) == 1

    def test_notes_can_be_attached(self, tmp_journal):
        signal = make_signal(pullback_trend(400, direction=1))
        tmp_journal.record(signal)
        assert tmp_journal.annotate(signal.id, "entered late")
        assert tmp_journal.get(signal.id)["notes"] == "entered late"

    def test_annotating_an_unknown_id_reports_failure(self, tmp_journal):
        assert not tmp_journal.annotate("nope", "x")


class TestOutcomeSettlement:
    def _record_call(self, journal, price=1.08):
        signal = make_signal(pullback_trend(400, direction=1), trade_duration=60)
        assert signal.direction is Direction.CALL
        signal.price = price
        journal.record(signal)
        return signal

    def test_nothing_settles_before_the_window_elapses(self, tmp_journal):
        self._record_call(tmp_journal)
        assert tmp_journal.pending_outcomes(utcnow()) == []

    def test_a_call_that_rose_is_a_win(self, tmp_journal):
        signal = self._record_call(tmp_journal, price=1.08)
        later = utcnow() + timedelta(seconds=120)
        tmp_journal.resolve_outcomes(1.0805, later)
        assert tmp_journal.get(signal.id)["outcome"] == "win"

    def test_a_call_that_fell_is_a_loss(self, tmp_journal):
        signal = self._record_call(tmp_journal, price=1.08)
        later = utcnow() + timedelta(seconds=120)
        tmp_journal.resolve_outcomes(1.0795, later)
        assert tmp_journal.get(signal.id)["outcome"] == "loss"

    def test_an_unchanged_price_is_recorded_as_flat_not_a_win(self, tmp_journal):
        signal = self._record_call(tmp_journal, price=1.08)
        later = utcnow() + timedelta(seconds=120)
        tmp_journal.resolve_outcomes(1.08, later)
        assert tmp_journal.get(signal.id)["outcome"] == "flat"

    def test_a_put_settles_on_the_inverse(self, tmp_journal):
        signal = make_signal(pullback_trend(400, direction=-1), trade_duration=60)
        assert signal.direction is Direction.PUT
        signal.price = 1.08
        tmp_journal.record(signal)
        tmp_journal.resolve_outcomes(1.0795, utcnow() + timedelta(seconds=120))
        assert tmp_journal.get(signal.id)["outcome"] == "win"

    def test_wait_signals_are_never_settled(self, tmp_journal):
        signal = make_signal(choppy_series(400))
        assert signal.direction in (Direction.WAIT, Direction.NO_TRADE)
        signal.price = 1.08
        tmp_journal.record(signal)
        tmp_journal.resolve_outcomes(1.09, utcnow() + timedelta(seconds=600))
        assert tmp_journal.get(signal.id)["outcome"] is None

    def test_settling_twice_does_not_change_the_result(self, tmp_journal):
        signal = self._record_call(tmp_journal, price=1.08)
        later = utcnow() + timedelta(seconds=120)
        tmp_journal.resolve_outcomes(1.0805, later)
        tmp_journal.resolve_outcomes(1.0700, later + timedelta(seconds=60))
        assert tmp_journal.get(signal.id)["outcome"] == "win"


class TestSettlementIsolation:
    """A price may only settle a signal it could actually have decided."""

    def _record(self, journal, *, price=1.08, source=None, asset="EUR/USD"):
        signal = make_signal(
            pullback_trend(400, direction=1), trade_duration=60, asset=asset
        )
        assert signal.direction is Direction.CALL
        signal.price = price
        journal.record(signal, source=source)
        return signal

    def test_a_signal_from_another_source_is_voided_not_settled(self, tmp_journal):
        signal = self._record(tmp_journal, price=1.08, source="synthetic")
        later = utcnow() + timedelta(seconds=90)
        tmp_journal.resolve_outcomes(1.0805, later, source="screen")
        entry = tmp_journal.get(signal.id)
        assert entry["outcome"] == "void"
        assert entry["outcome_price"] is None
        assert "synthetic" in entry["notes"]

    def test_a_signal_from_the_same_source_settles_normally(self, tmp_journal):
        signal = self._record(tmp_journal, price=1.08, source="screen")
        later = utcnow() + timedelta(seconds=90)
        tmp_journal.resolve_outcomes(1.0805, later, source="screen")
        assert tmp_journal.get(signal.id)["outcome"] == "win"

    def test_rows_from_before_sources_were_recorded_are_voided(self, tmp_journal):
        signal = self._record(tmp_journal, price=1.08, source=None)
        later = utcnow() + timedelta(seconds=90)
        tmp_journal.resolve_outcomes(1.0805, later, source="screen")
        assert tmp_journal.get(signal.id)["outcome"] == "void"

    def test_a_different_pair_cannot_settle_the_row(self, tmp_journal):
        """And must not destroy it either — it waits for its own chart.

        This used to assert ``void``. That was safe while the open chart was
        the only one ever journalled, because no other chart's rows existed to
        destroy. Once a setup on any watched chart became a call, one price
        voided eight charts' worth of pending rows: ten of sixteen calls in
        the first real session died this way, none of them for any reason to
        do with the trade.

        Not settling it is the part that mattered. Killing it never was.
        """
        signal = self._record(tmp_journal, source="screen", asset="EUR/USD")
        later = utcnow() + timedelta(seconds=90)
        tmp_journal.resolve_outcomes(1.0805, later, source="screen", asset="GBP/USD")

        entry = tmp_journal.get(signal.id)
        assert entry["outcome"] in (None, "", "pending"), entry["outcome"]
        assert [r["id"] for r in tmp_journal.pending_outcomes(later)] == [signal.id]

    def test_an_incompatible_price_scale_is_voided(self, tmp_journal):
        # Entry recorded on an uncalibrated 0-100 relative scale, settlement
        # price read off a real axis. Comparing them is meaningless.
        signal = self._record(tmp_journal, price=54.0, source="screen")
        later = utcnow() + timedelta(seconds=90)
        tmp_journal.resolve_outcomes(1.0805, later, source="screen")
        entry = tmp_journal.get(signal.id)
        assert entry["outcome"] == "void"
        assert "scale" in entry["notes"]

    def test_a_price_arriving_long_after_expiry_is_voided(self, tmp_journal):
        # The app was closed over the expiry; today's price is not the price
        # at expiry, and pretending otherwise invents an outcome.
        signal = self._record(tmp_journal, price=1.08, source="screen")
        much_later = utcnow() + timedelta(hours=3)
        tmp_journal.resolve_outcomes(1.0805, much_later, source="screen")
        assert tmp_journal.get(signal.id)["outcome"] == "void"

    def test_settling_within_the_grace_window_still_works(self, tmp_journal):
        signal = self._record(tmp_journal, price=1.08, source="screen")
        later = utcnow() + timedelta(seconds=200)
        tmp_journal.resolve_outcomes(1.0805, later, source="screen")
        assert tmp_journal.get(signal.id)["outcome"] == "win"

    def test_voided_rows_are_excluded_from_the_win_rate(self, tmp_journal):
        self._record(tmp_journal, price=1.08, source="synthetic")
        later = utcnow() + timedelta(seconds=90)
        tmp_journal.resolve_outcomes(1.0805, later, source="screen")
        stats = tmp_journal.statistics()
        assert stats["wins"] == 0
        assert stats["losses"] == 0
        assert stats["voided"] == 1
        assert stats["win_rate"] is None

    def test_statistics_can_be_scoped_to_one_source(self, tmp_journal):
        demo = self._record(tmp_journal, price=1.08, source="synthetic")
        live = make_signal(pullback_trend(400, direction=1), trade_duration=60)
        live.price = 1.08
        tmp_journal.record(live, source="screen")
        later = utcnow() + timedelta(seconds=90)
        tmp_journal.resolve_outcomes(1.0805, later, source="screen")
        assert tmp_journal.get(demo.id)["outcome"] == "void"
        assert tmp_journal.statistics(source="screen")["wins"] == 1
        assert tmp_journal.statistics(source="synthetic")["settled"] == 0

    def test_an_old_journal_file_gains_the_source_column(self, tmp_path):
        import sqlite3

        from poa.storage import Journal

        path = tmp_path / "legacy.db"
        # A journal written before the column existed.
        with sqlite3.connect(str(path)) as connection:
            connection.execute(
                "CREATE TABLE signals (id TEXT PRIMARY KEY, timestamp TEXT, "
                "asset TEXT, direction TEXT, trade_duration INTEGER, price REAL, "
                "outcome TEXT, notes TEXT)"
            )
        journal = Journal(path)
        try:
            columns = {
                row[1]
                for row in journal._connection.execute("PRAGMA table_info(signals)")
            }
            assert "source" in columns
        finally:
            journal.close()


class TestStatistics:
    def _rows(self, wins, losses, **kwargs):
        rows = []
        for _ in range(wins):
            rows.append({"direction": "CALL", "outcome": "win", "confidence": 85, **kwargs})
        for _ in range(losses):
            rows.append({"direction": "CALL", "outcome": "loss", "confidence": 78, **kwargs})
        return rows

    def test_win_rate_is_computed_over_decided_signals(self):
        stats = summarise_outcomes(self._rows(30, 10))
        assert stats["win_rate"] == pytest.approx(75.0)

    def test_flat_outcomes_do_not_count_as_wins(self):
        rows = self._rows(10, 10)
        rows.append({"direction": "CALL", "outcome": "flat", "confidence": 80})
        stats = summarise_outcomes(rows)
        assert stats["win_rate"] == pytest.approx(50.0)
        assert stats["flat"] == 1

    def test_breakeven_reflects_the_payout_not_fifty_percent(self):
        # An 80% payout needs ~55.6% wins to break even, not 50%.
        assert breakeven_rate(0.80) == pytest.approx(55.6, abs=0.1)
        assert breakeven_rate(1.00) == pytest.approx(50.0)

    def test_expected_value_is_negative_at_a_coin_flip_win_rate(self):
        stats = summarise_outcomes(self._rows(50, 50), payout=0.80)
        assert stats["expected_value"] < 0

    def test_expected_value_is_positive_above_breakeven(self):
        stats = summarise_outcomes(self._rows(70, 30), payout=0.80)
        assert stats["expected_value"] > 0

    def test_a_small_sample_is_labelled(self):
        stats = summarise_outcomes(self._rows(3, 1))
        assert not stats["sufficient_sample"]
        assert stats["sample_warning"]

    def test_a_large_sample_is_not_labelled(self):
        stats = summarise_outcomes(self._rows(MIN_MEANINGFUL_SAMPLE, 5))
        assert stats["sufficient_sample"]
        assert stats["sample_warning"] is None

    def test_streaks_are_measured(self):
        rows = [{"direction": "CALL", "outcome": o, "confidence": 80} for o in
                ["win", "win", "win", "loss", "loss", "win"]]
        stats = summarise_outcomes(rows)
        assert stats["streaks"]["max_winning_streak"] == 3
        assert stats["streaks"]["max_losing_streak"] == 2

    def test_grouping_splits_by_duration_and_regime(self):
        rows = self._rows(10, 5, trade_duration=180, regime="STRONG_UPTREND")
        rows += self._rows(4, 6, trade_duration=60, regime="RANGE")
        stats = summarise_outcomes(rows)
        assert {g["group"] for g in stats["by_duration"]} == {"3 MIN", "1 MIN"}
        assert {g["group"] for g in stats["by_regime"]} == {"STRONG_UPTREND", "RANGE"}

    def test_an_empty_set_does_not_invent_a_win_rate(self):
        stats = summarise_outcomes([])
        assert stats["win_rate"] is None
        assert stats["expected_value"] is None

    def test_the_disclaimer_is_always_present(self):
        assert "not predict" in summarise_outcomes([])["disclaimer"]


class TestBacktester:
    def test_a_run_produces_settled_trades(self):
        series = generate_series(900, seed=5)
        result = Backtester(window=200).run(series, trade_duration=180, step=5)
        assert result.evaluated_bars > 0
        for trade in result.trades:
            assert trade.outcome in ("win", "loss", "flat")
            assert trade.exit_price is not None

    def test_most_bars_do_not_produce_a_signal(self):
        # The engine is supposed to be conservative; a signal on every bar
        # would mean the gates are not doing their job.
        series = generate_series(900, seed=5)
        result = Backtester(window=200).run(series, trade_duration=180, step=5)
        assert result.signal_rate < 50

    def test_there_is_no_lookahead(self):
        # Every trade must settle strictly after the bar it was signalled on.
        series = generate_series(700, seed=6)
        result = Backtester(window=200).run(series, trade_duration=180, step=5)
        for trade in result.trades:
            assert trade.exit_index > trade.index

    def test_trades_that_cannot_settle_are_dropped(self):
        series = generate_series(700, seed=6)
        result = Backtester(window=200).run(series, trade_duration=180, step=5)
        for trade in result.trades:
            assert trade.exit_index < len(series)

    def test_settlement_matches_the_direction(self):
        series = generate_series(900, seed=5)
        result = Backtester(window=200).run(series, trade_duration=180, step=5)
        for trade in result.trades:
            if trade.outcome == "flat":
                continue
            rose = trade.exit_price > trade.entry_price
            expected_win = rose if trade.direction == "CALL" else not rose
            assert (trade.outcome == "win") == expected_win

    def test_too_short_a_series_returns_an_empty_result(self):
        result = Backtester(window=250).run(generate_series(100, seed=1))
        assert result.trades == []
        assert result.evaluated_bars == 0

    def test_the_recommended_duration_mode_uses_the_engines_choice(self):
        series = generate_series(900, seed=5)
        result = Backtester(window=200).run(
            series, trade_duration=180, step=5, use_recommended_duration=True
        )
        for trade in result.trades:
            assert trade.trade_duration == trade.recommended_duration

    def test_a_higher_confidence_floor_produces_fewer_signals(self):
        series = generate_series(900, seed=5)
        permissive = Backtester(settings=GateSettings(min_confidence=70), window=200)
        strict = Backtester(settings=GateSettings(min_confidence=95), window=200)
        assert len(strict.run(series, step=5).trades) <= len(
            permissive.run(series, step=5).trades
        )

    def test_the_result_serialises(self):
        import json

        result = Backtester(window=200).run(generate_series(700, seed=6), step=10)
        payload = json.dumps(result.to_dict())
        assert "NaN" not in payload


class TestMeasuringTheEngineOnItsOwnChart:
    """The one number on the panel that is a fact rather than a forecast.

    It has to be honest in both directions: no look-ahead in how it is
    produced, and no confidence in how it is reported until the sample can
    carry it.
    """

    def test_a_short_history_measures_nothing_and_says_so(self):
        from poa.overlay.proof import measure

        result = measure(pullback_trend(80, direction=1), trade_duration=180, payout=0.92)
        assert result.settled == 0
        assert result.win_rate is None
        assert "150 are needed" in result.summary()

    def test_a_real_history_produces_a_measured_rate(self):
        from poa.overlay.proof import measure

        result = measure(
            pullback_trend(500, direction=1), trade_duration=180, payout=0.92
        )
        assert result.bars == 508
        assert result.settled > 0
        assert result.win_rate is not None
        assert result.breakeven == pytest.approx(52.1, abs=0.2)
        assert result.edge == pytest.approx(result.win_rate - result.breakeven, abs=0.2)

    def test_a_small_sample_is_never_reported_as_an_edge(self):
        """A green 100% over three trades is the worst thing it could paint."""
        from poa.overlay.proof import ProofResult

        tiny = ProofResult(
            asset="EUR/USD", timeframe_seconds=60, trade_duration=180,
            bars=400, evaluated=100, signals=3, wins=3, losses=0, payout=0.92,
        )
        assert tiny.win_rate == 100.0
        assert not tiny.meaningful
        assert "too few to read" in tiny.summary()

    def test_it_splits_by_direction(self):
        """All-calls-right on a rising chart is the trend, not an edge."""
        from poa.overlay.proof import measure

        result = measure(
            pullback_trend(500, direction=1), trade_duration=180, payout=0.92
        )
        assert set(result.by_direction) == {"CALL", "PUT"}

    def test_a_flat_expiry_counts_as_neither_a_win_nor_a_loss(self):
        from poa.overlay.proof import ProofResult

        result = ProofResult(
            asset="EUR/USD", timeframe_seconds=60, trade_duration=180,
            bars=400, evaluated=100, signals=25, wins=12, losses=8, payout=0.92,
        )
        assert result.settled == 20  # the five flats are not in it
        assert result.win_rate == 60.0


class TestCalibration:
    """The score is an opinion until something checks it against an outcome.

    A setup scoring 78 is not thereby 78% likely to win — the number has no
    units. Calibration turns it into an index into a measurement, and the
    measurement is allowed to say the score is worthless.
    """

    def _records(self, score, rate, n=40, regime="STRONG_UPTREND"):
        """``rate`` is the win rate as a percentage, stated outright.

        An earlier version of this helper took "one loss every N", which reads
        as the loss rate and is in fact the inverse — and duly produced 75%
        where a test wanted 25%.
        """
        from poa.backtesting.calibration import Record

        wins = round(n * rate / 100.0)
        return [
            Record(score=score, won=(i < wins), regime=regime, hour=9,
                   direction="CALL")
            for i in range(n)
        ]

    def test_a_band_reports_what_that_score_actually_settled_at(self):
        from poa.backtesting.calibration import build_calibration

        cal = build_calibration(self._records(75, rate=75), payout=0.92)
        band = cal.band_for(75)
        assert band.label == "70-80"
        assert band.settled == 40
        assert band.win_rate == 75.0
        assert band.beats(cal.breakeven) is True

    def test_it_refuses_to_have_an_opinion_on_a_small_sample(self):
        """The most confident-looking number here is also the least useful."""
        from poa.backtesting.calibration import build_calibration

        # Five trades, every one of them a win: flawless, and worth nothing.
        cal = build_calibration(
            self._records(75, rate=100, n=5), payout=0.92
        )
        band = cal.band_for(75)
        assert band.win_rate == 100.0
        assert band.settled < 20
        assert not band.meaningful
        assert band.beats(cal.breakeven) is None
        assert cal.measured_rate(75) is None
        beats, _why = cal.verdict(75)
        assert beats is None  # no opinion, which is not the same as "no"

    def test_the_thresholds_answer_where_to_set_the_gate(self):
        from poa.backtesting.calibration import build_calibration

        # Weak setups below 70, strong ones above.
        cal = build_calibration(
            self._records(60, rate=50, n=40)      # below break-even
            + self._records(80, rate=80, n=40),   # well above
            payout=0.92,
        )
        rows = dict(cal.thresholds)
        assert rows[50].settled == 80
        assert rows[75].settled == 40
        assert rows[75].win_rate == 80.0
        # Taking everything is worse than being selective, and it says so.
        assert rows[50].win_rate < rows[75].win_rate

    def test_the_recommended_gate_is_the_one_worth_the_most(self):
        """A gate trades signal count for hit rate; rank by what it returned.

        Taking everything here settles at 65%, which clears break-even — but
        excluding the weak half returns more in total despite half the trades,
        so that is the gate the record recommends.
        """
        from poa.backtesting.calibration import build_calibration

        cal = build_calibration(
            self._records(60, rate=50, n=40) + self._records(80, rate=80, n=40),
            payout=0.92,
        )
        rows = dict(cal.thresholds)
        assert rows[50].win_rate == 65.0  # everything clears break-even …
        recommended = cal.recommended_threshold()
        assert recommended is not None
        threshold, bucket = recommended
        # … but the strong half alone was worth more, and 65 is the loosest
        # gate that isolates it — no reason to pay for a stricter one.
        assert threshold == 65
        assert bucket.settled == 40
        assert bucket.win_rate == 80.0

    def test_no_threshold_clears_when_nothing_does(self):
        from poa.backtesting.calibration import build_calibration

        cal = build_calibration(self._records(80, rate=50), payout=0.92)
        assert cal.recommended_threshold() is None

    def test_a_losing_regime_overrides_a_band_that_looks_fine(self):
        from poa.backtesting.calibration import build_calibration

        cal = build_calibration(
            self._records(80, rate=80, n=40, regime="STRONG_UPTREND")
            + self._records(80, rate=25, n=40, regime="CHOPPY"),
            payout=0.92,
        )
        good, _ = cal.verdict(80, "STRONG_UPTREND")
        bad, why = cal.verdict(80, "CHOPPY")
        assert good is True
        assert bad is False
        assert "choppy" in why.lower()

    def test_a_shortfall_inside_the_noise_is_not_a_finding(self):
        """50% over 40 trades against a 52.1% break-even is not evidence.

        The standard error on forty trades is about eight points. Refusing to
        trade on a two-point gap is superstition with a decimal point, and it
        would silence the tool on any record that is merely unremarkable.
        """
        from poa.backtesting.calibration import build_calibration

        cal = build_calibration(self._records(80, rate=50, n=40), payout=0.92)
        band = cal.band_for(80)
        assert band.win_rate == 50.0
        assert band.beats(cal.breakeven) is False   # strictly, it is below …
        assert not band.clearly_below(cal.breakeven)  # … but not measurably
        beats, _why = cal.verdict(80)
        assert beats is True

    def test_a_shortfall_bigger_than_the_noise_is_a_finding(self):
        from poa.backtesting.calibration import build_calibration

        cal = build_calibration(self._records(80, rate=25, n=40), payout=0.92)
        band = cal.band_for(80)
        assert band.win_rate == 25.0
        assert band.clearly_below(cal.breakeven)
        beats, _why = cal.verdict(80)
        assert beats is False

    def test_flat_expiries_are_left_out_of_the_record(self):
        from poa.backtesting.calibration import records_from_trades

        class Trade:
            def __init__(self, outcome):
                self.outcome = outcome
                self.direction_confidence = 70.0
                self.regime = "STRONG_UPTREND"
                self.timestamp = "2026-08-14T09:00:00+00:00"
                self.direction = "CALL"

        records = records_from_trades(
            [Trade("win"), Trade("loss"), Trade("flat"), Trade(None)]
        )
        assert len(records) == 2
        assert records[0].hour == 9


class TestTheMeasuredEdgeGate:
    """A setup whose own record says it loses is not a setup."""

    def _signal(self, calibration=None):
        from poa.models import DataQuality
        from poa.signals.engine import SignalEngine, SignalRequest

        series = pullback_trend(400, direction=1)
        quality = DataQuality(
            ok=True, confidence=95.0, candle_count=len(series), source="test"
        )
        return SignalEngine().evaluate(
            SignalRequest(
                series=series,
                asset="EUR/USD",
                chart_timeframe=60,
                trade_duration=180,
                quality=quality,
                calibration=calibration,
            )
        )

    def _calibration(self, score, rate, n=40, real=True):
        """``rate`` is the win rate as a percentage."""
        from poa.backtesting.calibration import Record, build_calibration

        wins = round(n * rate / 100.0)
        calibration = build_calibration(
            [
                Record(score=score, won=(i < wins), regime="STRONG_UPTREND")
                for i in range(n)
            ],
            payout=0.92,
        )
        calibration.from_real_trades = real
        return calibration

    def test_without_a_record_nothing_changes(self):
        """A gate that blocked until a record existed would prevent one forming."""
        signal = self._signal()
        assert signal.direction is Direction.CALL
        assert signal.actionable

    def test_a_measurably_losing_record_turns_the_setup_into_a_wait(self):
        score = self._signal().direction_confidence
        signal = self._signal(self._calibration(score, rate=25))
        assert signal.direction is Direction.WAIT
        assert not signal.actionable
        assert "settled at 25%" in signal.reason

    def test_a_winning_record_leaves_it_alone(self):
        score = self._signal().direction_confidence
        signal = self._signal(self._calibration(score, rate=80))
        assert signal.direction is Direction.CALL
        assert signal.actionable

    def test_a_tiny_losing_record_is_not_acted_on(self):
        """Six trades cannot condemn a setup any more than they can bless one."""
        score = self._signal().direction_confidence
        signal = self._signal(self._calibration(score, rate=25, n=6))
        assert signal.direction is Direction.CALL
        assert signal.actionable

    def test_a_replay_never_gets_a_veto(self):
        """The loop that closes on itself and never reopens.

        Live setups land in the same score band a replay is dominated by, so a
        band that replayed badly would block every signal — and a tool that
        signals nothing takes no trades, builds no real record, and stays
        blocked forever. Only trades that were actually placed can veto.
        """
        score = self._signal().direction_confidence
        replayed = self._calibration(score, rate=25, real=False)
        signal = self._signal(replayed)
        assert signal.direction is Direction.CALL
        assert signal.actionable
        # The finding is still reported — it just does not hold the veto.
        assert any("25%" in text for text in signal.warnings + [signal.reason])


class TestTheTradeHasToBePlaceable:
    """A signal exists only after its bar has closed, so nobody can buy at that
    bar's closing price — it is already history by the time the panel lights up.
    Settling against it measures a trade that could not be taken.
    """

    def test_entry_is_the_first_price_available_after_the_signal(self):
        from poa.backtesting.paper import Backtester

        series = pullback_trend(400, direction=1)
        result = Backtester(window=120).run(
            series, trade_duration=180, step=3, realistic_entry=True
        )
        assert result.trades
        for trade in result.trades[:10]:
            # Entered at the open of the bar *after* the signal, never at the
            # close of the bar that produced it.
            assert trade.entry_price == series[trade.index + 1].open

    def test_the_impossible_entry_is_still_available_for_comparison(self):
        """Knowing how much a measured edge owes to entering in the past."""
        from poa.backtesting.paper import Backtester

        series = pullback_trend(400, direction=1)
        optimistic = Backtester(window=120).run(
            series, trade_duration=180, step=3, realistic_entry=False
        )
        assert optimistic.trades
        for trade in optimistic.trades[:10]:
            assert trade.entry_price == series[trade.index].close

    def test_expiry_runs_from_entry_not_from_the_signal(self):
        from poa.backtesting.paper import Backtester

        series = pullback_trend(400, direction=1)
        result = Backtester(window=120).run(
            series, trade_duration=180, step=3, realistic_entry=True
        )
        bars_ahead = 180 // 60
        for trade in result.trades[:10]:
            assert trade.exit_index == trade.index + 1 + bars_ahead


class TestTheRecordRemembersRealTrades:
    """A replay is what the engine would have done; the journal is what it did.

    Real trades carry the delay between the panel lighting up and the button
    being pressed, the broker's own settlement, and the user's hesitation.
    None of that is reproducible in a backtest.
    """

    def _journal(self, tmp_path):
        from poa.storage.journal import Journal

        return Journal(tmp_path / "j.db")

    def _record(self, journal, *, score, won, asset="EUR/USD", duration=180):
        from datetime import timedelta

        from poa.models import Direction, utcnow

        signal = make_signal(pullback_trend(200, direction=1), asset=asset)
        signal.direction = Direction.CALL
        signal.direction_confidence = score
        signal.trade_duration = duration
        signal.price = 1.10
        row_id = journal.record(signal, source="feed")
        journal.resolve_outcomes(
            1.11 if won else 1.09,
            utcnow() + timedelta(seconds=duration + 30),
            source="feed",
            asset=asset,
        )
        return row_id

    def test_settled_trades_become_calibration_records(self, tmp_path):
        journal = self._journal(tmp_path)
        try:
            for i in range(6):
                self._record(journal, score=75.0, won=i % 2 == 0)
            records = journal.calibration_records(asset="EUR/USD", source="feed")
            assert len(records) == 6
            assert {r.won for r in records} == {True, False}
            assert all(r.score == 75.0 for r in records)
        finally:
            journal.close()

    def test_another_chart_is_a_different_experiment(self, tmp_path):
        journal = self._journal(tmp_path)
        try:
            self._record(journal, score=75.0, won=True, asset="EUR/USD")
            self._record(journal, score=75.0, won=True, asset="GBP/USD")
            assert len(journal.calibration_records(asset="EUR/USD")) == 1
            assert len(journal.calibration_records(asset="GBP/USD")) == 1
            # A different expiry is a different experiment too.
            assert journal.calibration_records(trade_duration=999) == []
        finally:
            journal.close()

    def test_unsettled_trades_are_not_counted(self, tmp_path):
        """A signal with no outcome yet says nothing about anything."""
        journal = self._journal(tmp_path)
        try:
            from poa.models import Direction

            signal = make_signal(pullback_trend(200, direction=1))
            signal.direction = Direction.CALL
            journal.record(signal, source="feed")
            assert journal.calibration_records() == []
        finally:
            journal.close()

    def test_real_trades_replace_the_replay_once_there_are_enough(self):
        from poa.backtesting.calibration import Record
        from poa.overlay.proof import measure

        real = [
            Record(score=75.0, won=(i % 5 != 0), regime="STRONG_UPTREND")
            for i in range(30)
        ]
        result = measure(
            pullback_trend(500, direction=1),
            trade_duration=180,
            payout=0.92,
            real_records=real,
        )
        assert result.calibration.from_real_trades is True
        assert result.calibration.total == 30  # the replay's trades are not mixed in
        assert result.calibration.band_for(75).win_rate == 80.0

    def test_too_few_real_trades_falls_back_to_the_replay(self):
        from poa.backtesting.calibration import Record
        from poa.overlay.proof import measure

        real = [Record(score=75.0, won=True) for _ in range(3)]
        result = measure(
            pullback_trend(500, direction=1),
            trade_duration=180,
            payout=0.92,
            real_records=real,
        )
        assert result.calibration.from_real_trades is False
        assert result.calibration.real_available == 3


class TestTheSurveyBreaksTheDeadlock:
    """Strict gates cannot measure whether they should be strict.

    Running the replay at the live gates measures only what already passes, so
    it can never answer the question the threshold table exists for. Worse, it
    closes a loop: strict gates yield few setups, few setups cannot carry a
    recommendation, so the gates never move off whatever they were set to.
    """

    def _mixed(self, bars=200):
        """Trending and choppy stretches, as a real session contains."""
        from poa.models import Candle, Series

        parts = [
            pullback_trend(bars, direction=1),
            choppy_series(bars),
            pullback_trend(bars, direction=-1),
        ]
        candles, stamp, step = [], None, None
        for part in parts:
            step = step or (part[1].timestamp - part[0].timestamp)
            for candle in part:
                stamp = candle.timestamp if stamp is None else stamp
                candles.append(
                    Candle(stamp, candle.open, candle.high, candle.low,
                           candle.close, candle.volume)
                )
                stamp = stamp + step
        return Series(candles, 60, "GBP/USD OTC")

    def test_the_survey_runs_looser_than_the_live_gates(self):
        from poa.overlay.proof import SURVEY_CONFIDENCE, measure
        from poa.signals.gates import GateSettings

        strict = GateSettings(min_confidence=95, min_duration_compatibility=95)
        result = measure(
            self._mixed(), trade_duration=180, payout=0.92, settings=strict
        )
        # Almost nothing passes gates that strict …
        assert result.signals < result.surveyed
        # … but the survey still found a population to calibrate against.
        assert result.surveyed >= 20
        assert result.calibration.total == result.surveyed
        assert SURVEY_CONFIDENCE < strict.min_confidence

    def test_the_headline_still_reports_the_users_own_gates(self):
        """A number describing a looser tool answers a question nobody asked."""
        from poa.overlay.proof import measure
        from poa.signals.gates import GateSettings

        strict = GateSettings(min_confidence=95, min_duration_compatibility=95)
        loose = GateSettings(min_confidence=50, min_duration_compatibility=30)
        series = self._mixed()
        tight = measure(series, trade_duration=180, payout=0.92, settings=strict)
        wide = measure(series, trade_duration=180, payout=0.92, settings=loose)

        assert tight.signals < wide.signals
        # Both surveyed the same population; only the taking differs.
        assert tight.surveyed == wide.surveyed

    def test_a_chart_with_no_workable_gate_says_so(self):
        """The answer is a different chart, not a looser gate.

        Loosening until something fires would manufacture calls the record
        says lose money, which is the worst thing this tool could do.
        """
        from poa.backtesting.calibration import Record, build_calibration
        from poa.overlay.viewmodel import COLORS, OverlayViewModel
        from poa.risk import SessionStats

        # Forty setups, none of them profitable at any threshold.
        cal = build_calibration(
            [Record(score=80, won=i < 12, duration_score=70) for i in range(40)],
            payout=0.92,
        )
        assert cal.recommended_threshold() is None

        vm = OverlayViewModel(session=SessionStats())
        vm.connected = True
        vm.signal = make_signal(pullback_trend(400, direction=1))
        vm.signal.direction_confidence = 45.0  # outside any measured band

        from poa.overlay.proof import ProofResult

        vm.proof = ProofResult(
            asset="GBP/USD OTC", timeframe_seconds=60, trade_duration=180,
            bars=600, evaluated=240, signals=0, wins=12, losses=28,
            payout=0.92, calibration=cal,
        )
        line = vm.render()["calibration"]
        assert "no gate setting measured above break-even" in line["text"].lower()
        assert "try another pair" in line["text"].lower()
        assert line["color"] == COLORS["put"]


class TestTheGateStillMovesOnAMarginalChart:
    """Ranking gates only by what clears break-even froze them on most charts.

    A chart where no score threshold beats the payout's bar returned no
    recommendation at all, so min_confidence stayed wherever it started — for
    the whole session, on every pair that was not already profitable. But "no
    gate clears this payout" is a fact about the payout, not about which gate
    reads the chart best, and those are two separate questions.

    The window this opens is widest exactly where it matters. At a 92% payout
    break-even is 52.1% and there is barely any gap; at 72% it is 58.1%, and a
    gate reading 55% — clearly better than guessing, clearly separating — was
    invisible. Pocket Option pays 47% on some pairs, where break-even is 68%
    and nearly every honest gate fell in the gap.
    """

    def _calibration(self, rates, payout=0.72):
        """One calibration where score band -> win rate is under my control."""
        from poa.backtesting.calibration import Record, build_calibration

        records = []
        for score, (wins, losses) in rates.items():
            for _ in range(wins):
                records.append(Record(score=score, won=True, duration_score=score))
            for _ in range(losses):
                records.append(Record(score=score, won=False, duration_score=score))
        return build_calibration(records, payout=payout)

    def test_a_gate_below_breakeven_that_still_separates_is_recommended(self):
        # Break-even at 72% is 58.1%. Nothing here reaches it, but the strict
        # end reads the chart far better than taking everything does.
        calibration = self._calibration(
            {55.0: (18, 42), 65.0: (24, 36), 75.0: (33, 27), 85.0: (22, 18)}
        )
        recommended = calibration.recommended_threshold()
        assert recommended is not None
        threshold, bucket = recommended
        assert threshold >= 70
        assert bucket.win_rate is not None and bucket.win_rate > 50.0
        assert bucket.win_rate < calibration.breakeven  # still under the bar

    def test_tightening_is_not_recommended_when_it_makes_things_worse(self):
        """Taking everything is 70% here and every stricter gate is worse.

        Under the old expected-value ranking this recommended tightening
        anyway, because volume multiplied by payout outweighed the drop in
        accuracy. Judged on being right, the answer is to leave the gate
        alone — and "take everything" is not a gate to recommend.
        """
        calibration = self._calibration(
            {55.0: (45, 15), 65.0: (30, 10), 75.0: (12, 8), 85.0: (11, 9)}
        )
        assert calibration.recommended_threshold() is None

    def test_a_clearly_better_band_is_still_recommended(self):
        """Weak setups below 60, strong ones above. Tightening earns its place.

        The gate lands on the loosest threshold that isolates the strong band
        rather than the strictest one that contains it — there is no reason to
        pay for selectivity that excludes nothing extra.
        """
        calibration = self._calibration({55.0: (20, 40), 85.0: (34, 6)})
        threshold, bucket = calibration.recommended_threshold()

        assert threshold == 60
        assert bucket.settled == 40
        assert bucket.win_rate == 85.0

    def test_the_same_record_recommends_the_same_gate_at_any_payout(self):
        """The property the expected-value ranking could not have.

        It handed the choice to the broker: the same chart picked a different
        gate on a 50% morning than on a 92% one, having learned nothing about
        the market. The user's instruction was the other way round — they deal
        with the money, the tool finds the correct calls.
        """
        chosen = set()
        for payout in (0.30, 0.50, 0.75, 0.92, 1.10):
            calibration = self._calibration({55.0: (20, 40), 85.0: (34, 6)})
            calibration.payout = payout
            chosen.add(calibration.recommended_threshold()[0])

        assert len(chosen) == 1, f"the payout moved the gate: {sorted(chosen)}"

    def test_a_chart_that_loses_to_a_coin_recommends_nothing(self):
        """Loosening toward the best of a bad lot is manufacturing calls.

        Every band here is worse than guessing. Walking the gate toward the
        least-bad one would take *more* of exactly the trades that are not
        working, which is the one thing this must never do.
        """
        calibration = self._calibration(
            {55.0: (10, 50), 65.0: (14, 46), 75.0: (16, 44), 85.0: (9, 21)}
        )
        assert calibration.recommended_threshold() is None

    def test_a_gate_no_better_than_taking_everything_recommends_nothing(self):
        """A flat chart has nothing to find.

        When every band settles at the same rate the score is not separating
        anything, and moving the gate along it only changes how many of the
        same trades get taken.
        """
        calibration = self._calibration(
            {55.0: (33, 27), 65.0: (33, 27), 75.0: (33, 27), 85.0: (33, 27)}
        )
        assert calibration.recommended_threshold() is None

    def test_an_improvement_inside_the_noise_recommends_nothing(self):
        """Four points on eighty trades is not a finding."""
        calibration = self._calibration({55.0: (40, 40), 75.0: (42, 38)})
        assert calibration.recommended_threshold() is None

    def test_too_little_data_still_recommends_nothing(self):
        """The fallback ranks what exists; it does not invent a sample."""
        calibration = self._calibration({55.0: (2, 3), 75.0: (1, 2)})
        assert calibration.recommended_threshold() is None

    def test_the_duration_gate_moves_the_same_way(self):
        calibration = self._calibration(
            {55.0: (18, 42), 65.0: (24, 36), 75.0: (33, 27), 85.0: (22, 18)}
        )
        assert calibration.recommended_duration_threshold() is not None


class TestTradesYouTookYourselfTeachIt:
    """The record only ever learned from calls the tool made.

    The verdict is WAIT most of the time, so the trades that carry the
    information it is missing — what actually happens at scores it currently
    refuses — were exactly the ones it never saw. Three winners on a chart it
    declined to call taught it nothing at all.
    """

    def _journal(self, tmp_path):
        from poa.storage.journal import Journal

        return Journal(str(tmp_path / "j.db"))

    def _fill(self, journal, n=24, won=lambda i: i % 3 != 0, score=72.0):
        for i in range(n):
            journal.record_manual(
                asset="EUR/USD OTC",
                chart_timeframe=60,
                trade_duration=180,
                direction="CALL",
                direction_confidence=score,
                duration_confidence=88.0,
                won=won(i),
                market_regime="STRONG_UPTREND",
                source="feed",
            )

    def _read(self, journal):
        return journal.calibration_records(
            asset="EUR/USD OTC", source="feed",
            chart_timeframe=60, trade_duration=180,
        )

    def test_a_hand_entered_outcome_reaches_the_calibration(self, tmp_path):
        journal = self._journal(tmp_path)
        self._fill(journal)
        records = self._read(journal)
        assert len(records) == 24
        assert records[0].score == 72.0
        assert records[0].direction == "CALL"

    def test_the_expiry_fit_survives_the_round_trip(self, tmp_path):
        """It was being dropped, so the expiry table was built from zeros.

        Real trades outrank replayed ones and replace them outright at twenty.
        Reading back every column but this one meant the expiry gate then
        tuned itself against a column that said nothing.
        """
        journal = self._journal(tmp_path)
        self._fill(journal)
        assert self._read(journal)[0].duration_score == 88.0

    def test_wins_at_a_refused_score_open_the_gate(self, tmp_path):
        """The whole point. Setups scoring 72 were being refused at a gate of
        75; twenty-four of them settling at 67% is the evidence that says so.
        """
        from poa.backtesting.calibration import build_calibration

        journal = self._journal(tmp_path)
        self._fill(journal)
        calibration = build_calibration(self._read(journal), payout=0.92)
        threshold, bucket = calibration.recommended_threshold()
        assert threshold <= 72
        assert bucket.win_rate is not None and bucket.win_rate > calibration.breakeven

    def test_losses_at_a_score_close_it_again(self, tmp_path):
        """It has to be able to learn the other way, or it is not learning."""
        from poa.backtesting.calibration import build_calibration

        journal = self._journal(tmp_path)
        self._fill(journal, won=lambda i: i % 4 == 0)  # 25%
        calibration = build_calibration(self._read(journal), payout=0.92)
        assert calibration.recommended_threshold() is None

    def test_they_are_scoped_like_every_other_record(self, tmp_path):
        """A record from another pair or another expiry is another experiment."""
        journal = self._journal(tmp_path)
        self._fill(journal, n=5)
        assert journal.calibration_records(asset="GBP/USD OTC") == []
        assert journal.calibration_records(
            asset="EUR/USD OTC", trade_duration=300
        ) == []

    def test_a_demo_run_stays_out_of_a_live_record(self, tmp_path):
        journal = self._journal(tmp_path)
        self._fill(journal, n=5)
        assert journal.calibration_records(asset="EUR/USD OTC", source="demo") == []

    def test_they_are_marked_as_hand_entered(self, tmp_path):
        """Indistinguishable to the calibration, still tellable apart.

        It is real money on a real chart either way — which of us pressed the
        button does not change what the market did next — but anything that
        needs the difference can still find it.
        """
        journal = self._journal(tmp_path)
        self._fill(journal, n=3)
        rows = journal.recent(limit=5)
        assert rows and all(row["notes"] == "manual" for row in rows)


class TestTheConditionsAChartReadsWorstIn:
    """Where the reading works varies by instrument, so it is measured.

    The score says how strong a setup looks. This asks a different question:
    in which market conditions has this chart's reading actually been right
    least often? Trending, ranging and choppy markets are not one population,
    and averaging across them hides exactly the thing worth knowing.
    """

    def _calibration(self, regimes, payout=0.92):
        from poa.backtesting.calibration import Record, build_calibration

        records = []
        for regime, (wins, losses) in regimes.items():
            records += [Record(score=70.0, won=True, regime=regime)] * wins
            records += [Record(score=70.0, won=False, regime=regime)] * losses
        return build_calibration(records, payout=payout)

    def test_it_names_the_condition_that_reads_worst(self):
        calibration = self._calibration(
            {"STRONG_UPTREND": (46, 14), "WEAK_UPTREND": (25, 35)}
        )
        weak = calibration.weak_regimes()
        assert set(weak) == {"WEAK_UPTREND"}
        assert weak["WEAK_UPTREND"].win_rate is not None

    def test_it_is_judged_against_this_chart_not_a_fixed_number(self):
        """A chart reading at 45% everywhere has no worst condition.

        Every regime being below some constant says the constant is wrong,
        not that the market is. What matters is the spread within the chart.
        """
        calibration = self._calibration(
            {"STRONG_UPTREND": (27, 33), "WEAK_UPTREND": (27, 33)}
        )
        assert calibration.weak_regimes() == {}

    def test_a_shortfall_inside_the_noise_is_not_a_finding(self):
        """Fifty trades a few points below average is a bad afternoon.

        The standard error on fifty trades near 60% is about seven points, so
        a four-point shortfall is well inside what chance produces. Acting on
        it would be superstition with a decimal point.
        """
        calibration = self._calibration(
            {"STRONG_UPTREND": (40, 20), "WEAK_UPTREND": (30, 20)}
        )
        assert "WEAK_UPTREND" not in calibration.weak_regimes()

    def test_too_few_trades_in_a_condition_is_no_verdict(self):
        calibration = self._calibration(
            {"STRONG_UPTREND": (45, 15), "BREAKOUT": (1, 5)}
        )
        assert "BREAKOUT" not in calibration.weak_regimes()

    def test_it_never_rules_out_every_condition(self):
        """A tool that has argued itself into never trading is broken.

        If each regime reads below the average then the average is being
        pulled by something other than the regimes, and refusing all of them
        would leave nothing.
        """
        calibration = self._calibration(
            {"STRONG_UPTREND": (30, 30), "WEAK_UPTREND": (30, 30)}
        )
        assert calibration.weak_regimes() == {}

    def test_the_overall_rate_is_across_every_condition(self):
        calibration = self._calibration(
            {"STRONG_UPTREND": (45, 15), "WEAK_UPTREND": (15, 45)}
        )
        assert calibration.overall_rate() == 50.0

    def test_nothing_measured_is_no_opinion(self):
        from poa.backtesting.calibration import build_calibration

        empty = build_calibration([], payout=0.92)
        assert empty.overall_rate() is None
        assert empty.weak_regimes() == {}


class TestTheRegimeRecordGate:
    """It blocks on replayed evidence, and that is deliberate.

    A verdict on the score band may not: live setups land in the band the
    replay is dominated by, so blocking there silences the tool, and a silent
    tool never earns the record that would reopen the question. Ruling out one
    market condition leaves the others open — trading continues, the record
    keeps growing, and the decision stays revisable.
    """

    def _gates(self, calibration, avoid=True):
        import random
        from datetime import datetime, timedelta, timezone

        from poa.analysis import build_multi_timeframe
        from poa.models import Candle, DataQuality, Direction, Series
        from poa.signals.gates import GateSettings, evaluate_gates
        from poa.signals.scoring import score_direction

        random.seed(3)
        start = datetime(2026, 8, 16, tzinfo=timezone.utc)
        bars, price = [], 1.19
        for i in range(400):
            price += 0.00012 + random.gauss(0, 0.0002)
            o = price
            c = price + 0.00008
            bars.append(
                Candle(start + timedelta(minutes=i), o, max(o, c) + 0.00005,
                       min(o, c) - 0.00005, c)
            )
        series = Series(bars, 60, "TEST")
        mtf = build_multi_timeframe(series, higher_multiple=5, entry_multiple=1)
        score = score_direction(mtf, Direction.CALL)
        quality = DataQuality(ok=True, confidence=95.0, candle_count=len(series),
                              issues=[], source="test")
        report = evaluate_gates(
            mtf, Direction.CALL, score, quality,
            GateSettings(avoid_weak_regimes=avoid), calibration=calibration,
        )
        return report, mtf.current.regime.regime.name

    def _calibration(self, worst):
        """A record where ``worst`` reads clearly below every other condition."""
        from poa.backtesting.calibration import Record, build_calibration

        records = [Record(score=70.0, won=i < 50, regime="OTHER_CONDITION")
                   for i in range(60)]
        records += [Record(score=70.0, won=i < 15, regime=worst) for i in range(60)]
        return build_calibration(records, payout=0.92)

    def test_no_record_means_no_such_gate(self):
        report, _ = self._gates(None)
        assert not any(r.name == "regime_record" for r in report.results)

    def test_the_worst_condition_is_refused(self):
        report, regime = self._gates(self._calibration("PLACEHOLDER"))
        report, _ = self._gates(self._calibration(regime))
        failed = [r for r in report.results if r.name == "regime_record"]
        assert failed and not failed[0].passed and failed[0].blocking

    def test_a_condition_that_reads_fine_is_not_refused(self):
        report, regime = self._gates(self._calibration("PLACEHOLDER"))
        assert regime != "SOMETHING_ELSE"
        report, _ = self._gates(self._calibration("SOMETHING_ELSE"))
        assert not any(r.name == "regime_record" for r in report.results)

    def test_it_can_be_switched_off(self):
        report, regime = self._gates(self._calibration("PLACEHOLDER"))
        report, _ = self._gates(self._calibration(regime), avoid=False)
        assert not any(r.name == "regime_record" for r in report.results)

    def test_the_reason_names_the_condition_and_both_rates(self):
        """A refusal nobody can check is a refusal nobody can correct."""
        report, regime = self._gates(self._calibration("PLACEHOLDER"))
        report, _ = self._gates(self._calibration(regime))
        detail = [r for r in report.results if r.name == "regime_record"][0].detail
        assert "%" in detail and "against" in detail


class TestAWinRateIsReportedNextToWhatItIsWorth:
    """Two numbers that stop a result being read as better than it is.

    A rate on its own invited exactly one mistake, twice. On a real recording
    the engine scored 66.7% over 36 trades, which reads as a working edge and
    is consistent with anything from 50.3% to 79.8% — straddling break-even,
    so equally consistent with a losing tool. And on those same entries, a
    rule that just said BUY every time scored 72.2%. Both facts were found by
    happening to check. Now the report says them.
    """

    def test_the_interval_is_wide_when_the_sample_is_small(self):
        from poa.backtesting.stats import wilson_interval

        low, high = wilson_interval(2, 3)
        assert high - low > 50  # three trades tell you almost nothing

        low, high = wilson_interval(600, 1000)
        assert high - low < 10  # a thousand tell you something

    def test_it_never_promises_more_than_certainty(self):
        from poa.backtesting.stats import wilson_interval

        assert wilson_interval(0, 0) is None
        low, high = wilson_interval(5, 5)
        assert high <= 100.0 and low < 100.0   # five in a row is not proof
        low, high = wilson_interval(0, 5)
        assert low >= 0.0 and high > 0.0

    def test_the_baselines_count_which_way_price_went(self):
        from poa.backtesting.stats import directional_baselines

        buy, sell = directional_baselines([0.1, 0.2, -0.3, 0.4, -0.5])
        assert (buy.name, buy.wins, buy.settled) == ("always BUY", 3, 5)
        assert (sell.name, sell.wins, sell.settled) == ("always SELL", 2, 5)

    def test_an_unmoved_market_settles_neither_baseline(self):
        from poa.backtesting.stats import directional_baselines

        buy, sell = directional_baselines([0.0, None, 0.5])
        assert buy.settled == sell.settled == 1
        assert buy.wins == 1 and sell.wins == 0

    def test_a_backtest_reports_both_without_being_asked(self):
        """The point is that nobody has to think to run this."""
        from conftest import trending_series
        from poa.backtesting.paper import Backtester

        series = trending_series(400, step=0.00012)
        result = Backtester(window=120, payout=0.92).run(
            series, trade_duration=180, asset="EUR/USD", step=1
        )
        stats = result.statistics

        assert "baselines" in stats
        assert {b["name"] for b in stats["baselines"]} == {
            "always BUY", "always SELL"
        }
        if stats["wins"] + stats["losses"]:
            assert stats["interval"] is not None
            low, high = stats["interval"]
            assert 0.0 <= low <= high <= 100.0

    def test_a_rising_market_makes_always_buy_the_one_to_beat(self):
        """Which is the whole reason the comparison is printed."""
        from conftest import trending_series
        from poa.backtesting.paper import Backtester

        series = trending_series(400, step=0.00012)
        result = Backtester(window=120, payout=0.92).run(
            series, trade_duration=180, asset="EUR/USD", step=1
        )
        buy, sell = result.statistics["baselines"]
        if buy["settled"]:
            assert buy["wins"] + sell["wins"] == buy["settled"]
            assert buy["win_rate"] > sell["win_rate"]


class TestAPriceOnlySettlesItsOwnChart:
    """Ten calls out of sixteen came back VOID in the first real session.

    The engine settles with the open chart's price. Any pending row for a
    different instrument was voided on the spot — permanently — for the crime
    of not being the chart on screen. That was invisible while the open chart
    was the only one ever journalled. The moment setups on watched charts
    became calls, one chart's price destroyed the other eight charts' rows.
    """

    def _journal(self, tmp_path):
        from poa.storage.journal import Journal

        return Journal(tmp_path / "j.db")

    def _record(self, journal, asset, direction="CALL", price=1.10):
        """A real evaluated signal, dressed as a just-expired call on ``asset``."""
        from datetime import timedelta

        from poa.models import Direction, utcnow

        signal = make_signal(pullback_trend(400, direction=1), asset=asset)
        signal.id = f"{asset}-{direction}-{price}"
        signal.timestamp = utcnow() - timedelta(seconds=40)
        signal.trade_duration = 30
        signal.chart_timeframe = 5
        signal.price = price
        signal.direction = Direction[direction]
        journal.record(signal, None, source="feed")
        return signal

    def test_another_chart_s_call_is_left_pending_not_voided(self, tmp_path):
        journal = self._journal(tmp_path)
        try:
            self._record(journal, "EUR/USD", price=1.10)
            journal.resolve_outcomes(1.36, source="feed", asset="GBP/USD")

            pending = journal.pending_outcomes()
            assert len(pending) == 1, "the EUR/USD call was destroyed"
        finally:
            journal.close()

    def test_and_its_own_chart_settles_it_afterwards(self, tmp_path):
        journal = self._journal(tmp_path)
        try:
            self._record(journal, "EUR/USD", direction="CALL", price=1.10)
            journal.resolve_outcomes(1.36, source="feed", asset="GBP/USD")
            journal.resolve_outcomes(1.11, source="feed", asset="EUR/USD")

            assert journal.pending_outcomes() == []
            rows = journal.recent(limit=5)
            assert [r["outcome"] for r in rows] == ["win"]
        finally:
            journal.close()

    def test_each_chart_settles_only_its_own(self, tmp_path):
        journal = self._journal(tmp_path)
        try:
            self._record(journal, "EUR/USD", direction="CALL", price=1.10)
            self._record(journal, "GBP/USD", direction="PUT", price=1.36)

            journal.resolve_outcomes(1.11, source="feed", asset="EUR/USD")
            journal.resolve_outcomes(1.35, source="feed", asset="GBP/USD")

            outcomes = {r["asset"]: r["outcome"] for r in journal.recent(limit=5)}
            assert outcomes == {"EUR/USD": "win", "GBP/USD": "win"}
        finally:
            journal.close()

    def test_a_genuinely_unsettleable_row_is_still_voided(self, tmp_path):
        """The protection this replaces still has to work — wrong scale."""
        journal = self._journal(tmp_path)
        try:
            self._record(journal, "EUR/USD", price=1.10)
            journal.resolve_outcomes(161.36, source="feed", asset="EUR/USD")

            rows = journal.recent(limit=5)
            assert rows[0]["outcome"] == "void"
        finally:
            journal.close()


class TestCallsAreNotIndependentReads:
    """A tool watching a pair drift down and saying PUT six times has made one
    read, not six. They settle together, so counting the rows counts one
    right-or-wrong answer several times — and every interval printed over them
    comes out narrower than the evidence deserves.

    A live session made 106 settled calls in 77 episodes. Read as 106 the
    interval is a claim the session cannot support.
    """

    def _call(self, asset, direction, score=90.0, outcome="win"):
        from poa.backtesting.stats import Outcome

        return Outcome(
            direction=direction, outcome=outcome, confidence=score,
            trade_duration=30, chart_timeframe=5, asset=asset,
        )

    def test_a_run_on_one_pair_in_one_direction_is_one_episode(self):
        from poa.backtesting.stats import episodes

        calls = [self._call("EUR/USD", "PUT") for _ in range(6)]
        assert len(episodes(calls)) == 1

    def test_changing_direction_starts_a_new_one(self):
        from poa.backtesting.stats import episodes

        calls = [self._call("EUR/USD", "PUT"), self._call("EUR/USD", "CALL")]
        assert len(episodes(calls)) == 2

    def test_another_pair_in_between_breaks_the_run(self):
        """Several charts are watched at once, so consecutive rows are not
        consecutive on one chart."""
        from poa.backtesting.stats import episodes

        calls = [
            self._call("EUR/USD", "PUT"),
            self._call("AUD/CAD", "PUT"),
            self._call("EUR/USD", "PUT"),
        ]
        assert len(episodes(calls)) == 3

    def test_the_interval_over_episodes_is_wider_than_over_rows(self):
        """The whole point. Six samples of one opinion are not six opinions."""
        from poa.backtesting.stats import cluster_bootstrap, episodes, wilson_interval

        calls = (
            [self._call("EUR/USD", "PUT", outcome="win") for _ in range(6)]
            + [self._call("AUD/CAD", "CALL", outcome="loss") for _ in range(6)]
            + [self._call("USD/JPY", "PUT", outcome="win") for _ in range(6)]
            + [self._call("GBP/USD", "CALL", outcome="loss") for _ in range(6)]
        )

        def rate(rows):
            wins = sum(1 for c in rows if c.outcome == "win")
            return wins / len(rows) * 100 if rows else None

        naive = wilson_interval(12, 24)
        clustered = cluster_bootstrap(episodes(calls), rate)
        assert naive is not None and clustered is not None
        low, high, _draws = clustered
        assert (high - low) > (naive[1] - naive[0])

    def test_the_same_session_prints_the_same_interval_twice(self):
        """A report whose numbers move when nothing else did cannot be trusted
        with the ones that are supposed to move."""
        from poa.backtesting.stats import cluster_bootstrap, episodes

        calls = [
            self._call(f"P{i}", "PUT", outcome="win" if i % 3 else "loss")
            for i in range(12)
        ]

        def rate(rows):
            return sum(1 for c in rows if c.outcome == "win") / len(rows) * 100

        groups = episodes(calls)
        first = cluster_bootstrap(groups, rate, rounds=500)
        second = cluster_bootstrap(groups, rate, rounds=500)
        assert first[:2] == second[:2]

    def test_one_episode_cannot_be_resampled_into_evidence(self):
        from poa.backtesting.stats import cluster_bootstrap, episodes

        calls = [self._call("EUR/USD", "PUT") for _ in range(9)]
        assert cluster_bootstrap(episodes(calls), lambda rows: 1.0) is None


class TestTheScoreIsReadInBandsFixedInAdvance:
    def _call(self, score, outcome):
        from poa.backtesting.stats import Outcome

        return Outcome(direction="CALL", outcome=outcome, confidence=score,
                       trade_duration=30, chart_timeframe=5, asset="EUR/USD")

    def test_a_backwards_score_shows_as_a_decline(self):
        """What a score wired the wrong way looks like: worse as it rises."""
        from poa.backtesting.stats import score_bands

        calls = (
            [self._call(86, "win") for _ in range(8)]
            + [self._call(86, "loss") for _ in range(2)]
            + [self._call(92, "win") for _ in range(2)]
            + [self._call(92, "loss") for _ in range(8)]
        )
        rates = [b["win_rate"] for b in score_bands(calls)]
        assert rates == [80.0, 20.0]

    def test_the_bands_are_not_chosen_after_seeing_the_outcomes(self):
        """A split picked to fit a result finds a split in noise."""
        from poa.backtesting.stats import SCORE_BANDS

        assert SCORE_BANDS[0][0] == 0 and SCORE_BANDS[-1][1] == 100
        for (_, high), (low, _) in zip(SCORE_BANDS, SCORE_BANDS[1:]):
            assert low == high + 1, "the bands must tile without a gap"

    def test_unsettled_calls_are_left_out(self):
        from poa.backtesting.stats import score_bands

        calls = [self._call(86, "win"), self._call(86, "flat"),
                 self._call(86, "void")]
        assert [b["settled"] for b in score_bands(calls)] == [1]


class TestHowMuchOfOneMindTheToolWas:
    """A pair called sixteen times one way and never the other *is* always-BUY
    over that window, so the baseline comparison beside it cannot find an edge
    — both sides of it are the same strategy. A live session did exactly that
    on AUD/USD and lost thirteen of sixteen."""

    def _call(self, asset, direction, outcome):
        from poa.backtesting.stats import Outcome

        return Outcome(direction=direction, outcome=outcome, confidence=90.0,
                       trade_duration=30, chart_timeframe=5, asset=asset)

    def test_one_direction_only_reads_as_fully_one_way(self):
        from poa.backtesting.stats import by_asset

        calls = [self._call("AUD/USD", "CALL", "loss") for _ in range(13)]
        calls += [self._call("AUD/USD", "CALL", "win") for _ in range(3)]
        row = by_asset(calls)[0]
        assert row["one_way"] == 100.0
        assert (row["calls_up"], row["calls_down"]) == (16, 0)
        assert row["win_rate"] == 18.8

    def test_an_even_split_reads_as_half(self):
        from poa.backtesting.stats import by_asset

        calls = [self._call("EUR/USD", "CALL", "win"),
                 self._call("EUR/USD", "PUT", "loss")]
        assert by_asset(calls)[0]["one_way"] == 50.0

    def test_pairs_come_back_busiest_first(self):
        from poa.backtesting.stats import by_asset

        calls = [self._call("EUR/USD", "CALL", "win")]
        calls += [self._call("AUD/CAD", "PUT", "win") for _ in range(3)]
        assert [r["asset"] for r in by_asset(calls)] == ["AUD/CAD", "EUR/USD"]


class TestATradeIsSettledAtItsOwnExpiry:
    """A binary settles on where price sat when it expired.

    The journal used whatever price was current when the check ran, and the
    grace on that is ``max(2 x duration, 180s)`` — so a thirty-second trade
    could be decided by a price nearly three minutes past its expiry and filed
    as a clean win, with nothing marking it. On a 5 SEC chart feeding a 30 SEC
    trade even two seconds of lateness is a large share of the horizon.

    The chart's own candles can answer exactly, so they do.
    """

    def _series(self, closes, timeframe=5):
        from datetime import datetime, timedelta, timezone

        from poa.models import Candle, Series

        start = datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc)
        return Series(
            [
                Candle(
                    timestamp=start + timedelta(seconds=timeframe * i),
                    open=c, high=c, low=c, close=c, volume=1.0,
                )
                for i, c in enumerate(closes)
            ],
            timeframe,
            "EUR/USD",
        )

    def _journalled(self, tmp_path, direction, entry, duration=30):
        """One pending CALL/PUT at ``entry``, timed to the series above."""
        from datetime import datetime, timezone

        from poa.storage.journal import Journal

        journal = Journal(str(tmp_path / "j.db"))
        start = datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc)
        journal._connection.execute(
            "INSERT INTO signals (id, timestamp, asset, chart_timeframe, "
            "trade_duration, direction, state, direction_confidence, "
            "duration_confidence, overall_confidence, setup_quality, price, "
            "source) VALUES ('x', ?, 'EUR/USD', 5, ?, ?, 'ACTIVE', 90, 90, 90, "
            "'STRONG', ?, 'feed')",
            (start.isoformat(), duration, direction, entry),
        )
        journal._connection.commit()
        return journal, start

    def test_the_price_at_expiry_decides_it_not_the_price_now(self, tmp_path):
        """Price is below entry at expiry and above it by the time the app
        looks. The trade lost."""
        from datetime import timedelta

        # 30s expiry = bar 6. Down at bar 6, up long afterwards.
        closes = [1.0] * 6 + [0.9] * 6 + [1.5] * 20
        series = self._series(closes)
        journal, start = self._journalled(tmp_path, "CALL", 1.0)
        journal.resolve_outcomes(
            1.5,  # what the app can see now
            now=start + timedelta(seconds=170),
            source="feed",
            asset="EUR/USD",
            price_at=series.price_at,
        )
        row = journal.recent(limit=1)[0]
        assert row["outcome"] == "loss", "settled on the move it was betting on"
        assert float(row["outcome_price"]) == 0.9

    def test_without_the_history_it_still_settles_as_before(self, tmp_path):
        """The fallback has to keep working: not every caller has candles."""
        from datetime import timedelta

        journal, start = self._journalled(tmp_path, "CALL", 1.0)
        journal.resolve_outcomes(
            1.5, now=start + timedelta(seconds=35), source="feed", asset="EUR/USD"
        )
        assert journal.recent(limit=1)[0]["outcome"] == "win"

    def test_a_row_the_history_cannot_reach_falls_back(self, tmp_path):
        """A chart that does not go back far enough is not an answer."""
        from datetime import timedelta

        far_future = self._series([2.0] * 5)
        journal, start = self._journalled(tmp_path, "CALL", 1.0)
        journal.resolve_outcomes(
            1.5,
            now=start + timedelta(seconds=35),
            source="feed",
            asset="EUR/USD",
            price_at=lambda when: far_future.price_at(when + timedelta(days=400)),
        )
        assert journal.recent(limit=1)[0]["outcome"] == "win"

    def test_lateness_cannot_void_a_row_the_history_decided(self, tmp_path):
        """Being slow to look is not a reason to destroy an answer that the
        candles hold exactly."""
        from datetime import timedelta

        series = self._series([1.0] * 6 + [1.4] * 30)
        journal, start = self._journalled(tmp_path, "CALL", 1.0)
        journal.resolve_outcomes(
            1.4,
            now=start + timedelta(seconds=3600),  # an hour late
            source="feed",
            asset="EUR/USD",
            price_at=series.price_at,
        )
        row = journal.recent(limit=1)[0]
        assert row["outcome"] == "win", "the candles knew, however late this was"

    def test_a_put_reads_the_same_way_round(self, tmp_path):
        from datetime import timedelta

        series = self._series([1.0] * 6 + [0.9] * 30)
        journal, start = self._journalled(tmp_path, "PUT", 1.0)
        journal.resolve_outcomes(
            0.9,
            now=start + timedelta(seconds=40),
            source="feed",
            asset="EUR/USD",
            price_at=series.price_at,
        )
        assert journal.recent(limit=1)[0]["outcome"] == "win"


class TestAChartCanBeAskedWhatItWasShowing:
    def _series(self):
        from datetime import datetime, timedelta, timezone

        from poa.models import Candle, Series

        start = datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc)
        return Series(
            [
                Candle(timestamp=start + timedelta(seconds=5 * i),
                       open=1.0 + i, high=1.0 + i, low=1.0 + i,
                       close=1.0 + i, volume=1.0)
                for i in range(6)
            ],
            5,
            "T",
        )

    def test_a_moment_inside_a_bar_reads_that_bar(self):
        from datetime import timedelta

        series = self._series()
        start = series.candles[0].timestamp
        assert series.price_at(start + timedelta(seconds=12)) == 3.0

    def test_before_the_history_is_not_a_price(self):
        from datetime import timedelta

        series = self._series()
        assert series.price_at(series.candles[0].timestamp - timedelta(seconds=1)) is None

    def test_beyond_the_history_is_not_a_price(self):
        """Answering with the last close would silently settle a trade that
        expires in the future at today's price."""
        from datetime import timedelta

        series = self._series()
        assert series.price_at(series.candles[-1].timestamp + timedelta(hours=1)) is None

    def test_an_empty_chart_answers_nothing(self):
        from datetime import datetime, timezone

        from poa.models import Series

        assert Series([], 5, "T").price_at(datetime.now(tz=timezone.utc)) is None


class TestGhostTradesAreSweptOnce:
    """Builds before the deal-clock fix stamped hand trades on the broker's
    clock — an hour ahead one session, two the next — so a trade placed in the
    afternoon was filed into the evening, landed inside the next session's
    window, and reappeared dash for dash in that report's "TRADES YOU PLACED".
    Seventeen of them did exactly that, EUR/HUF included, in a session that
    never watched EUR/HUF.
    """

    BEFORE = "2026-08-21T15:06:00+00:00"
    AFTER = "2026-08-21T18:00:00+00:00"

    def _journal(self, tmp_path):
        from poa.storage.journal import Journal

        return Journal(str(tmp_path / "j.db"))

    def _manual(self, journal, stamp, confidence):
        journal._connection.execute(
            "INSERT INTO signals (id, timestamp, asset, chart_timeframe, "
            "trade_duration, direction, state, direction_confidence, "
            "duration_confidence, overall_confidence, setup_quality, "
            "outcome, notes, source) VALUES (?, ?, 'EURUSD_otc', 5, 30, "
            "'PUT', 'SETTLED', ?, ?, ?, 'MANUAL', 'win', 'manual', 'feed')",
            (f"manual-{stamp}-{confidence}", stamp, confidence, confidence,
             confidence),
        )
        journal._connection.commit()

    def _reopen(self, tmp_path):
        from poa.storage.journal import Journal

        return Journal(str(tmp_path / "j.db"))

    def _manual_stamps(self, journal):
        return [
            row["timestamp"]
            for row in journal._connection.execute(
                "SELECT timestamp FROM signals WHERE notes = 'manual'"
            )
        ]

    def _age_to_pre_fix_build(self, journal):
        """Make the file look like an old build wrote it: no data version."""
        journal._connection.execute("PRAGMA user_version = 0")
        journal._connection.commit()

    def test_a_ghost_row_is_removed_and_a_real_one_kept(self, tmp_path):
        journal = self._journal(tmp_path)
        self._manual(journal, self.BEFORE, 0.0)   # the ghost: pre-fix, no score
        self._manual(journal, self.AFTER, 0.0)    # post-fix dash: stamp is real
        self._manual(journal, self.BEFORE, 77.0)  # scored: not a ghost
        self._age_to_pre_fix_build(journal)
        journal.close()

        reopened = self._reopen(tmp_path)
        try:
            rows = reopened._connection.execute(
                "SELECT timestamp, direction_confidence FROM signals "
                "WHERE notes = 'manual' ORDER BY timestamp"
            ).fetchall()
            kept = {(r["timestamp"], r["direction_confidence"]) for r in rows}
            assert kept == {(self.BEFORE, 77.0), (self.AFTER, 0.0)}
        finally:
            reopened.close()

    def test_the_sweep_runs_once_per_journal(self, tmp_path):
        """A row matching the ghost shape but written after the sweep ran must
        survive: the sweep is a one-off correction for known-bad stamps, not a
        standing rule that manual rows may never score zero."""
        journal = self._journal(tmp_path)  # fresh file: already at version 1
        self._manual(journal, self.BEFORE, 0.0)
        journal.close()
        reopened = self._reopen(tmp_path)
        try:
            assert self._manual_stamps(reopened) == [self.BEFORE]
        finally:
            reopened.close()

    def test_signals_that_are_not_manual_are_untouched(self, tmp_path):
        journal = self._journal(tmp_path)
        self._age_to_pre_fix_build(journal)
        journal._connection.execute(
            "INSERT INTO signals (id, timestamp, asset, chart_timeframe, "
            "trade_duration, direction, state, direction_confidence, "
            "duration_confidence, overall_confidence, setup_quality, source) "
            "VALUES ('x', ?, 'EURUSD_otc', 5, 30, 'PUT', 'ACTIVE', 0, 0, 0, "
            "'WEAK', 'feed')",
            (self.BEFORE,),
        )
        journal._connection.commit()
        journal.close()
        reopened = self._reopen(tmp_path)
        try:
            count = reopened._connection.execute(
                "SELECT COUNT(*) FROM signals"
            ).fetchone()[0]
            assert count == 1
        finally:
            reopened.close()
