"""A breakdown table is the most persuasive object in trading software.

Forty trades across five regimes and three sessions gives fifteen cells
averaging under three trades each, and every cell gets a win rate to two
decimal places. The table looks like analysis and is mostly arithmetic on
noise — and a reader skimming for "the best regime" has no way to tell
which cells are which.

These tests pin the two defences: a bucket below the minimum reports no
ratios at all, and a bucket that does not exist is absent rather than
zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gtcc.backtest.attribution import (
    MIN_TRADES_PER_BUCKET,
    all_breakdowns,
    breakdown,
    by_regime,
    by_session,
    by_side,
    by_strategy,
    session_of,
)
from gtcc.backtest.engine import ClosedTrade, ExitReason
from gtcc.domain.enums import Side
from gtcc.domain.money import D

START = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)  # a Monday, London


def _trade(
    pnl: str,
    *,
    regime: str | None = "TRENDING_UP",
    strategy: str = "s",
    side: Side = Side.BUY,
    hours: int = 0,
    days: int = 0,
    reason: ExitReason = ExitReason.TARGET,
) -> ClosedTrade:
    at = START + timedelta(days=days, hours=hours)
    return ClosedTrade(
        symbol="TEST", strategy=strategy, side=side, quantity=D("10"),
        entry_index=0, entry_at=at, entry_price=D("100"),
        exit_index=1, exit_at=at + timedelta(hours=1), exit_price=D("101"),
        reason=reason, planned_stop=D("99"), planned_target=D("103"),
        costs=D("1"), pnl=D(pnl), r_multiple=D(pnl) / D("10"), regime=regime,
    )


class TestSmallBucketsReportNoRatios:
    def test_a_bucket_under_the_minimum_has_no_win_rate(self):
        trades = [_trade("10") for _ in range(MIN_TRADES_PER_BUCKET - 1)]

        result = by_regime(trades)

        assert len(result.buckets) == 1
        bucket = result.buckets[0]
        assert bucket.trades == MIN_TRADES_PER_BUCKET - 1
        assert not bucket.rated
        assert bucket.win_rate is None
        assert bucket.expectancy is None
        assert bucket.average_r is None
        assert "too few to rate" in bucket.caveat

    def test_a_bucket_at_the_minimum_is_rated(self):
        trades = [_trade("10") for _ in range(MIN_TRADES_PER_BUCKET)]

        bucket = by_regime(trades).buckets[0]

        assert bucket.rated
        assert bucket.win_rate == D("100")
        assert bucket.average_r is not None

    def test_the_count_and_pnl_are_always_shown(self):
        """Those are facts even in a tiny bucket; the ratios are not."""
        trades = [_trade("10"), _trade("-4")]

        bucket = by_regime(trades).buckets[0]

        assert bucket.trades == 2
        assert bucket.total_pnl == D("6")
        assert bucket.win_rate is None
        assert "2 trades" in bucket.describe()
        assert "%" not in bucket.describe()

    def test_a_table_where_nothing_is_rated_says_so(self):
        trades = [
            _trade("10", regime="TRENDING_UP"),
            _trade("10", regime="RANGING"),
            _trade("-5", regime="CHOPPY"),
        ]

        text = "\n".join(by_regime(trades).describe())

        assert "NO BUCKET reaches" in text
        assert "nothing about which conditions work better" in text

    def test_a_rated_table_does_not_carry_that_warning(self):
        trades = [_trade("10") for _ in range(MIN_TRADES_PER_BUCKET)]

        text = "\n".join(by_regime(trades).describe())

        assert "NO BUCKET reaches" not in text


class TestMissingBucketsAreAbsent:
    def test_a_regime_never_traded_has_no_row(self):
        """Printing it with 0% would read as "tried it and it failed"."""
        trades = [_trade("10", regime="TRENDING_UP") for _ in range(12)]

        result = by_regime(trades)

        keys = {bucket.key for bucket in result.buckets}
        assert keys == {"TRENDING_UP"}
        assert "RANGING" not in keys
        assert "HIGH_VOLATILITY" not in keys

    def test_a_trade_with_no_regime_is_unattributed_not_a_bucket(self):
        """An "unknown" row invites comparison against the real ones."""
        trades = [_trade("10", regime="TRENDING_UP") for _ in range(11)]
        trades += [_trade("10", regime=None) for _ in range(3)]

        result = by_regime(trades)

        assert [bucket.key for bucket in result.buckets] == ["TRENDING_UP"]
        assert result.unattributed == 3
        assert "no regime recorded" in "\n".join(result.describe())

    def test_no_trade_carrying_the_dimension_says_so(self):
        trades = [_trade("10", regime=None) for _ in range(5)]

        result = by_regime(trades)

        assert result.buckets == ()
        assert "no trade carried a value" in "\n".join(result.describe())


class TestOrdering:
    def test_buckets_sort_by_trade_count_not_by_profit(self):
        """Sorting by P&L puts a two-trade fluke at the top of the table."""
        trades = [_trade("1", regime="RANGING") for _ in range(20)]
        trades += [_trade("500", regime="BREAKOUT") for _ in range(2)]

        result = by_regime(trades)

        assert result.buckets[0].key == "RANGING"
        assert result.buckets[0].trades == 20
        assert result.buckets[1].key == "BREAKOUT"


class TestTheDimensions:
    def test_sessions_come_from_the_entry_hour(self):
        assert session_of(START.replace(hour=2)) == "ASIA"
        assert session_of(START.replace(hour=9)) == "LONDON"
        assert session_of(START.replace(hour=14)) == "LONDON_NY_OVERLAP"
        assert session_of(START.replace(hour=18)) == "NEW_YORK"
        assert session_of(START.replace(hour=23)) == "LATE"

    def test_a_session_breakdown_splits_by_entry_time(self):
        trades = [_trade("10", hours=0) for _ in range(5)]      # 09:00 London
        trades += [_trade("10", hours=9) for _ in range(5)]     # 18:00 New York

        result = by_session(trades)

        keys = {bucket.key for bucket in result.buckets}
        assert keys == {"LONDON", "NEW_YORK"}

    def test_a_strategy_breakdown_separates_strategies(self):
        trades = [_trade("10", strategy="a") for _ in range(11)]
        trades += [_trade("-5", strategy="b") for _ in range(11)]

        result = by_strategy(trades)

        assert {bucket.key for bucket in result.buckets} == {"a", "b"}
        winning = next(b for b in result.buckets if b.key == "a")
        losing = next(b for b in result.buckets if b.key == "b")
        assert winning.total_pnl > 0 and losing.total_pnl < 0

    def test_long_and_short_are_reported_separately(self):
        trades = [_trade("10", side=Side.BUY) for _ in range(11)]
        trades += [_trade("-5", side=Side.SELL) for _ in range(11)]

        result = by_side(trades)

        assert {bucket.key for bucket in result.buckets} == {"BUY", "SELL"}

    def test_all_breakdowns_covers_every_dimension(self):
        trades = [_trade("10") for _ in range(12)]

        dimensions = {report.dimension for report in all_breakdowns(trades)}

        assert dimensions == {"strategy", "regime", "session", "direction", "weekday"}

    def test_an_empty_trade_list_produces_empty_breakdowns(self):
        for report in all_breakdowns([]):
            assert report.buckets == ()
            assert "no trade carried a value" in "\n".join(report.describe())


class TestTheRegimeIsFromTheDecision:
    def test_a_backtest_records_the_regime_it_decided_in(self):
        """Attributing a trade to the conditions it ENDED in answers a
        different question from the one a breakdown is read for."""
        import tests.test_backtest as bt

        rows = bt._flat(30) + [
            ("100", "100", "100", "100"),
            ("100", "100", "100", "100"),
            ("100", "110", "100", "105"),
        ] + bt._flat(3, "105")
        from gtcc.backtest import BacktestSettings, Backtester
        from gtcc.domain.enums import Timeframe
        from gtcc.risk.limits import parse_limits
        from tests.conftest import LIMITS_RAW

        backtester = Backtester(limits=parse_limits(LIMITS_RAW))
        result = backtester.run(
            bt._AtBar(at=30, stop="95", target="110"),
            bt._bars(rows),
            bt._spec(),
            BacktestSettings(
                symbol="TEST", timeframe=Timeframe.M15, warmup_bars=25
            ),
        )

        assert len(result.trades) == 1
        # A flat series gives the classifier nothing to be confident about,
        # so the honest answer is None rather than a regime name. Asserting
        # "None or a string" would have been vacuous.
        assert result.trades[0].regime is None

    def test_a_trending_series_records_the_trend_it_decided_in(self):
        import tests.test_backtest as bt
        from gtcc.backtest import BacktestSettings, Backtester
        from gtcc.domain.enums import Timeframe
        from gtcc.risk.limits import parse_limits
        from tests.conftest import LIMITS_RAW

        # A clean uptrend, so the classifier has something to be sure about.
        rows = [
            (f"{100 + i * 0.5:.2f}", f"{100 + i * 0.5 + 0.3:.2f}",
             f"{100 + i * 0.5 - 0.3:.2f}", f"{100 + i * 0.5:.2f}")
            for i in range(80)
        ]
        backtester = Backtester(limits=parse_limits(LIMITS_RAW))
        result = backtester.run(
            bt._AtBar(at=60, stop="125", target="145"),
            bt._bars(rows),
            bt._spec(),
            BacktestSettings(
                symbol="TEST", timeframe=Timeframe.M15, warmup_bars=50
            ),
        )

        assert len(result.trades) == 1
        assert result.trades[0].regime == "TRENDING_UP"
        # And it lands in a named bucket rather than as unattributed.
        report = by_regime(result.trades)
        assert [bucket.key for bucket in report.buckets] == ["TRENDING_UP"]
        assert report.unattributed == 0


class TestTheRemainingDimensions:
    """by_weekday and the warning ordering had no direct test. Both are
    reachable only through a render path, which means a regression in
    either would have shown up as a cosmetic difference nobody checked."""

    def test_weekday_buckets_use_day_names(self):
        from gtcc.backtest.attribution import by_weekday

        # START is a Monday.
        trades = [_trade("10", days=offset) for offset in range(3)]

        result = by_weekday(trades)

        assert {bucket.key for bucket in result.buckets} == {"MON", "TUE", "WED"}

    def test_the_sample_warning_comes_before_the_statistics(self):
        """A reader who meets a profit factor first has already formed an
        impression by the time the caveat arrives."""
        from gtcc.backtest.engine import BacktestResult, BarCosts
        from gtcc.backtest.metrics import measure
        from gtcc.domain.enums import Timeframe as _TF

        result = BacktestResult(
            symbol="TEST", timeframe=_TF.M15, strategy="s",
            bars_seen=100, bars_traded=50, first_bar_at=START, last_bar_at=START,
            starting_equity=D("1000"), ending_equity=D("1100"),
            costs=BarCosts(), trades=(_trade("100"),),
            equity_curve=(D("1000"), D("1100")),
        )
        lines = measure(result).describe_with_warning()

        assert "ONLY 1 CLOSED TRADES" in lines[0]
        warning_at = next(i for i, line in enumerate(lines) if "ONLY" in line)
        stats_at = next(i for i, line in enumerate(lines) if "closed trade(s):" in line)
        assert warning_at < stats_at

    def test_a_trustworthy_sample_has_no_warning_prefix(self):
        from gtcc.backtest.engine import BacktestResult, BarCosts
        from gtcc.backtest.metrics import measure
        from gtcc.domain.enums import Timeframe as _TF

        trades = tuple(_trade("10", days=index) for index in range(40))
        result = BacktestResult(
            symbol="TEST", timeframe=_TF.M15, strategy="s",
            bars_seen=100, bars_traded=50, first_bar_at=START, last_bar_at=START,
            starting_equity=D("1000"), ending_equity=D("1400"),
            costs=BarCosts(), trades=trades,
            equity_curve=(D("1000"), D("1400")),
        )
        lines = measure(result).describe_with_warning()

        assert "ONLY" not in "\n".join(lines)
        assert lines == measure(result).describe()
