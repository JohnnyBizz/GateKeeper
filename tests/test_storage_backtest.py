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
        signal = self._record(tmp_journal, source="screen", asset="EUR/USD")
        later = utcnow() + timedelta(seconds=90)
        tmp_journal.resolve_outcomes(1.0805, later, source="screen", asset="GBP/USD")
        assert tmp_journal.get(signal.id)["outcome"] == "void"

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
