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
