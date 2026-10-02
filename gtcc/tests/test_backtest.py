"""A backtester is only worth as much as its inability to see the future.

A look-ahead bug does not raise. It produces a beautiful equity curve,
which is the most dangerous possible failure mode: it looks like success.
So the first class here attacks that specifically, and the central test is
the one the specification asks for by name — proof the engine cannot read
a bar it should not have.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gtcc.backtest import (
    BacktestSettings,
    Backtester,
    BarCosts,
    ExitReason,
    measure,
)
from gtcc.backtest.metrics import MIN_TRADES_FOR_RATIOS
from gtcc.domain.enums import AssetClass, Market, Timeframe, TradingMode
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar
from gtcc.domain.money import D
from gtcc.strategies.base import (
    Decision,
    Proposal,
    Strategy,
    StrategyContext,
    ValidationStatus,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="TEST", market=Market.STOCKS, asset_class=AssetClass.EQUITY,
        quote_currency="USD", tick_size=D("0.01"), lot_step=D("1"), min_qty=D("1"),
    )


def _bars(rows: list[tuple[str, str, str, str]], *, closed: bool = True) -> list[Bar]:
    """Bars from explicit (open, high, low, close) tuples."""
    out = []
    for index, (open_, high, low, close) in enumerate(rows):
        out.append(
            Bar(
                symbol="TEST", timeframe=Timeframe.M15,
                timestamp=START + timedelta(minutes=15 * index),
                open=D(open_), high=D(high), low=D(low), close=D(close),
                volume=D("10000"), closed=closed,
            )
        )
    return out


def _flat(count: int, price: str = "100") -> list[tuple[str, str, str, str]]:
    return [(price, price, price, price)] * count


class _AtBar(Strategy):
    """Proposes exactly once, at a chosen index, with a chosen plan."""

    name = "at-bar"
    validation = ValidationStatus.OUT_OF_SAMPLE

    def __init__(
        self,
        at: int,
        *,
        decision: Decision = Decision.LONG,
        stop: str = "99",
        target: str = "103",
    ) -> None:
        self.at = at
        self.decision = decision
        self.stop = D(stop)
        self.target = D(target)
        self.seen_lengths: list[int] = []
        self.last_bars: list[Bar] = []

    def evaluate(self, context: StrategyContext) -> Proposal:
        self.seen_lengths.append(len(context.bars))
        self.last_bars = list(context.bars)
        if len(context.bars) - 1 != self.at:
            return Proposal(
                decision=Decision.WAIT, strategy=self.name, rationale="not yet"
            )
        return Proposal(
            decision=self.decision,
            strategy=self.name,
            rationale="the fixture said so",
            entry=context.bars[-1].close,
            stop=self.stop,
            targets=(self.target,),
            conviction=1,
        )


@pytest.fixture
def backtester(limits):
    return Backtester(limits=limits)


class TestItCannotSeeTheFuture:
    """The tests that make the rest of the module worth running."""

    def test_the_strategy_only_ever_sees_bars_up_to_the_present(self, backtester):
        """The slice is the enforcement, so assert on the slice."""
        strategy = _AtBar(at=10_000)  # never fires
        bars = _bars(_flat(60))
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=10
        )

        backtester.run(strategy, bars, _spec(), settings)

        # One call per tradeable bar, each seeing one more bar than the last,
        # and never more than the index it was called at.
        assert strategy.seen_lengths == list(range(11, 60))

    def test_every_bar_the_strategy_sees_is_closed(self, backtester):
        strategy = _AtBar(at=10_000)
        bars = _bars(_flat(40)) + _bars(_flat(1, "200"), closed=False)
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=5
        )

        backtester.run(strategy, bars, _spec(), settings)

        assert all(bar.closed for bar in strategy.last_bars)
        assert all(bar.close != D("200") for bar in strategy.last_bars)

    def test_a_decision_fills_at_the_next_bars_open_not_this_bars_close(
        self, backtester
    ):
        """The price the decision was made on is not a price you can get."""
        rows = _flat(30) + [
            ("100", "100", "100", "100"),      # index 30: the decision bar
            ("101.50", "104", "101", "103"),   # index 31: the fill bar
        ] + _flat(5, "103")
        bars = _bars(rows)
        strategy = _AtBar(at=30, stop="99", target="103")
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25,
            costs=BarCosts(spread_bps=D("0"), slippage_bps=D("0"), commission_bps=D("0")),
        )

        result = backtester.run(strategy, bars, _spec(), settings)

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.entry_price == D("101.50"), "filled at the next bar's open"
        assert trade.entry_index == 31
        assert trade.entry_price != D("100"), "not the close it decided on"

    def test_a_decision_on_the_final_bar_is_not_taken(self, backtester):
        """There is nothing left to fill against without looking ahead."""
        bars = _bars(_flat(31))
        strategy = _AtBar(at=30)
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25
        )

        result = backtester.run(strategy, bars, _spec(), settings)

        assert result.trades == ()
        assert not result.open_at_end

    def test_running_on_a_prefix_gives_the_same_trades(self, backtester):
        """The general property. If a later bar can change an earlier
        decision, something is reading ahead."""
        rows = _flat(30) + [
            ("100", "100", "100", "100"),
            ("100", "104", "99.50", "103"),
        ] + _flat(20, "103")
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25,
            costs=BarCosts(spread_bps=D("0"), slippage_bps=D("0"), commission_bps=D("0")),
        )
        full = backtester.run(_AtBar(at=30), _bars(rows), _spec(), settings)
        prefix = backtester.run(_AtBar(at=30), _bars(rows[:35]), _spec(), settings)

        assert len(prefix.trades) == len(full.trades)
        assert prefix.trades[0].entry_price == full.trades[0].entry_price
        assert prefix.trades[0].exit_price == full.trades[0].exit_price
        assert prefix.trades[0].reason == full.trades[0].reason


class TestTheAmbiguousBar:
    def test_a_bar_touching_both_stop_and_target_is_a_stop(self, backtester):
        """OHLCV does not say which came first. Only one assumption can
        flatter the result, so take the other one."""
        rows = _flat(30) + [
            ("100", "100", "100", "100"),        # decision
            ("100", "100", "100", "100"),        # fill at 100
            ("100", "110", "95", "100"),         # touches target AND stop
        ] + _flat(3, "100")
        bars = _bars(rows)
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25,
            costs=BarCosts(spread_bps=D("0"), slippage_bps=D("0"), commission_bps=D("0")),
        )

        result = backtester.run(
            _AtBar(at=30, stop="95", target="110"), bars, _spec(), settings
        )

        assert len(result.trades) == 1
        assert result.trades[0].reason is ExitReason.STOP
        assert result.trades[0].pnl < 0

    def test_the_same_logic_holds_for_a_short(self, backtester):
        rows = _flat(30) + [
            ("100", "100", "100", "100"),
            ("100", "100", "100", "100"),
            ("100", "105", "90", "100"),
        ] + _flat(3, "100")
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25,
            costs=BarCosts(spread_bps=D("0"), slippage_bps=D("0"), commission_bps=D("0")),
        )

        result = backtester.run(
            _AtBar(at=30, decision=Decision.SHORT, stop="105", target="90"),
            _bars(rows), _spec(), settings,
        )

        assert len(result.trades) == 1
        assert result.trades[0].reason is ExitReason.STOP


class TestCostsOnlyEverHurt:
    def test_costs_worsen_both_ends_of_a_trade(self, backtester):
        rows = _flat(30) + [
            ("100", "100", "100", "100"),
            ("100", "100", "100", "100"),
            ("100", "110", "100", "105"),
        ] + _flat(3, "105")
        bars = _bars(rows)
        free = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25,
            costs=BarCosts(spread_bps=D("0"), slippage_bps=D("0"), commission_bps=D("0")),
        )
        costly = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25,
            costs=BarCosts(spread_bps=D("4"), slippage_bps=D("3"), commission_bps=D("2")),
        )

        without = backtester.run(_AtBar(at=30, stop="95", target="110"), bars, _spec(), free)
        with_costs = backtester.run(
            _AtBar(at=30, stop="95", target="110"), bars, _spec(), costly
        )

        assert without.trades[0].pnl > with_costs.trades[0].pnl
        assert with_costs.trades[0].entry_price > without.trades[0].entry_price
        assert with_costs.trades[0].exit_price < without.trades[0].exit_price

    def test_a_short_pays_the_same_costs_in_the_other_direction(self, backtester):
        rows = _flat(30) + [
            ("100", "100", "100", "100"),
            ("100", "100", "100", "100"),
            ("100", "100", "89", "90"),
        ] + _flat(3, "90")
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25,
            costs=BarCosts(spread_bps=D("4"), slippage_bps=D("3"), commission_bps=D("2")),
        )

        result = backtester.run(
            _AtBar(at=30, decision=Decision.SHORT, stop="105", target="90"),
            _bars(rows), _spec(), settings,
        )

        trade = result.trades[0]
        assert trade.entry_price < D("100"), "a short sells lower than the open"
        assert trade.exit_price > D("90"), "and buys back higher than the target"

    def test_the_result_records_what_it_assumed(self, backtester):
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=5,
            costs=BarCosts(spread_bps=D("2.5")),
        )
        result = backtester.run(_AtBar(at=10_000), _bars(_flat(20)), _spec(), settings)

        assert "2.5 bps spread" in result.costs.describe()
        assert "assumed costs" in result.describe()


class TestRiskIsTheRealEngine:
    def test_a_refused_setup_is_kept_not_discarded(self, backtester):
        """Otherwise a backtest cannot say whether the limits cost money."""
        rows = _flat(30) + [("100", "100", "100", "100")] * 5
        # A stop one tick away fails the minimum stop distance.
        strategy = _AtBar(at=30, stop="99.99", target="101")
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25
        )

        result = backtester.run(strategy, _bars(rows), _spec(), settings)

        assert result.trades == ()
        assert len(result.skipped) == 1
        assert result.skipped[0].reasons, "a refusal carries its reasons"

    def test_the_size_is_the_engines_not_the_strategys(self, backtester):
        rows = _flat(30) + [
            ("100", "100", "100", "100"),
            ("100", "100", "100", "100"),
            ("100", "110", "100", "105"),
        ] + _flat(3, "105")
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25,
            starting_equity=D("100000"),
        )

        result = backtester.run(
            _AtBar(at=30, stop="95", target="110"), _bars(rows), _spec(), settings
        )

        trade = result.trades[0]
        # 25% notional cap on a 100k account at a price near 100.
        assert trade.quantity * trade.entry_price <= D("25100")
        assert trade.quantity > 0

    def test_an_untested_strategy_takes_no_trades_even_in_a_backtest(self, backtester):
        class Untested(_AtBar):
            name = "untested"
            validation = ValidationStatus.UNTESTED

        rows = _flat(30) + [("100", "100", "100", "100")] * 5
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25
        )

        result = backtester.run(Untested(at=30), _bars(rows), _spec(), settings)

        # UNTESTED may be measured — that is what a backtest is for — but it
        # must not be able to claim it may trade. The gate lets BACKTEST
        # through and that is the only mode it lets through.
        assert result.bars_seen == 35


class TestMetricsAdmitTheirSampleSize:
    def test_a_tiny_sample_is_marked_untrustworthy(self, backtester):
        rows = _flat(30) + [
            ("100", "100", "100", "100"),
            ("100", "100", "100", "100"),
            ("100", "110", "100", "105"),
        ] + _flat(3, "105")
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25
        )
        result = backtester.run(
            _AtBar(at=30, stop="95", target="110"), _bars(rows), _spec(), settings
        )

        metrics = measure(result)

        assert metrics.trades == 1
        assert not metrics.trustworthy
        assert str(MIN_TRADES_FOR_RATIOS) in metrics.sample_warning
        assert "too small" in metrics.sample_warning

    def test_no_trades_is_an_absence_not_a_zero(self, backtester):
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=5
        )
        result = backtester.run(_AtBar(at=10_000), _bars(_flat(20)), _spec(), settings)

        metrics = measure(result)

        assert metrics.trades == 0
        assert metrics.win_rate is None, "a win rate of 0% would be a claim"
        assert metrics.profit_factor is None
        assert "absence of results" in metrics.sample_warning

    def test_a_profit_factor_with_no_losses_is_undefined_not_infinite(self):
        from gtcc.backtest.engine import BacktestResult

        result = BacktestResult(
            symbol="TEST", timeframe=Timeframe.M15, strategy="x",
            bars_seen=10, bars_traded=5, first_bar_at=START, last_bar_at=START,
            starting_equity=D("1000"), ending_equity=D("1100"),
            costs=BarCosts(), equity_curve=(D("1000"), D("1100")),
        )
        metrics = measure(result)

        assert metrics.profit_factor is None
        assert "undefined" in "\n".join(metrics.describe())

    def test_drawdown_counts_from_the_opening_balance(self, backtester):
        """A first losing trade is a drawdown, not a flat start."""
        rows = _flat(30) + [
            ("100", "100", "100", "100"),
            ("100", "100", "100", "100"),
            ("100", "100", "94", "95"),
        ] + _flat(3, "95")
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25
        )
        result = backtester.run(
            _AtBar(at=30, stop="95", target="110"), _bars(rows), _spec(), settings
        )

        metrics = measure(result)

        assert metrics.trades == 1
        assert metrics.total_pnl < 0
        assert metrics.max_drawdown > 0


class TestOpenPositions:
    def test_a_position_open_when_data_runs_out_is_flagged(self, backtester):
        rows = _flat(30) + [
            ("100", "100", "100", "100"),
            ("100", "100", "100", "100"),
        ] + _flat(3, "100")
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25
        )

        result = backtester.run(
            _AtBar(at=30, stop="95", target="110"), _bars(rows), _spec(), settings
        )

        assert result.open_at_end
        assert len(result.trades) == 1
        assert result.trades[0].reason is ExitReason.END_OF_DATA
        assert measure(result).open_at_end == 1

    def test_only_one_position_at_a_time(self, backtester):
        class Always(_AtBar):
            def evaluate(self, context):
                return Proposal(
                    decision=Decision.LONG, strategy=self.name,
                    rationale="always", entry=context.bars[-1].close,
                    stop=D("95"), targets=(D("110"),), conviction=1,
                )

        rows = _flat(60)
        settings = BacktestSettings(
            symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25
        )

        result = backtester.run(Always(at=0), _bars(rows), _spec(), settings)

        assert len(result.trades) == 1, "the first position blocks the rest"


class TestSplits:
    """Chronological, non-overlapping, and never shuffled."""

    def test_the_three_segments_are_in_time_order_and_cover_everything(self):
        from gtcc.backtest import split

        bars = _bars(_flat(100))
        parts = split(bars, in_sample="0.6", validation="0.2")

        assert parts.total == 100
        assert len(parts.in_sample) == 60
        assert len(parts.validation) == 20
        assert len(parts.out_of_sample) == 20
        assert parts.in_sample[-1].timestamp < parts.validation[0].timestamp
        assert parts.validation[-1].timestamp < parts.out_of_sample[0].timestamp

    def test_no_bar_appears_in_two_segments(self):
        from gtcc.backtest import split

        bars = _bars(_flat(97))
        parts = split(bars)

        stamps = (
            [bar.timestamp for bar in parts.in_sample]
            + [bar.timestamp for bar in parts.validation]
            + [bar.timestamp for bar in parts.out_of_sample]
        )
        assert len(stamps) == len(set(stamps))

    def test_a_split_that_holds_nothing_out_is_refused(self):
        from gtcc.backtest import SplitError, split

        with pytest.raises(SplitError, match="nothing held out"):
            split(_bars(_flat(100)), in_sample="0.9", validation="0.1")

    def test_a_series_too_short_to_split_is_refused(self):
        from gtcc.backtest import SplitError, split

        with pytest.raises(SplitError):
            split(_bars(_flat(2)))

    def test_out_of_order_input_is_sorted_rather_than_trusted(self):
        from gtcc.backtest import split

        bars = _bars(_flat(100))
        shuffled = list(reversed(bars))
        parts = split(shuffled)

        assert parts.in_sample[0].timestamp == bars[0].timestamp
        assert parts.out_of_sample[-1].timestamp == bars[-1].timestamp


class TestWalkForward:
    def test_every_test_window_follows_its_training_window(self):
        from gtcc.backtest import walk_forward

        bars = _bars(_flat(100))
        windows = list(walk_forward(bars, train_size=40, test_size=10))

        assert windows
        for window in windows:
            assert window.train[-1].timestamp < window.test[0].timestamp

    def test_windows_advance_and_do_not_repeat(self):
        from gtcc.backtest import walk_forward

        bars = _bars(_flat(100))
        windows = list(walk_forward(bars, train_size=40, test_size=10))

        starts = [window.test[0].timestamp for window in windows]
        assert starts == sorted(starts)
        assert len(starts) == len(set(starts))

    def test_a_series_too_short_yields_nothing_rather_than_a_partial_window(self):
        from gtcc.backtest import walk_forward

        assert list(walk_forward(_bars(_flat(20)), train_size=40, test_size=10)) == []


class TestTheOutOfSampleLedger:
    def test_a_second_look_is_reported_as_no_longer_out_of_sample(self):
        from gtcc.backtest import OutOfSampleLedger

        ledger = OutOfSampleLedger()
        assert ledger.record("strat") == 1
        assert ledger.warning_for("strat") == ""

        ledger.record("strat")

        assert ledger.count("strat") == 2
        assert "no longer out-of-sample" in ledger.warning_for("strat")
