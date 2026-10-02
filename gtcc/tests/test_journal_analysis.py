"""Paper results are measured by the backtester's own code.

A platform that scores backtests one way and paper trading another cannot
answer the only question worth asking of a backtest — did the thing it
predicted actually happen — because any difference between the two is then
indistinguishable from a difference in the strategy.

So these tests check the conversion, and check that nothing recomputes a
statistic along the way.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from gtcc.backtest.engine import ExitReason
from gtcc.journal.analysis import summarise, to_closed_trade
from gtcc.domain.enums import Side
from gtcc.domain.money import D

from tests.test_journal import journal_db  # noqa: F401

START = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)


def _row(
    *,
    outcome: str = "TAKEN",
    realised: str | None = "100",
    entry: str = "100",
    exit_price: str | None = "101",
    stop: str = "99",
    direction: str = "BUY",
    strategy: str = "swing",
    regime: str | None = "TRENDING_UP",
    reason: str | None = "TARGET",
    r_multiple: str | None = "1.0",
    days: int = 0,
    failures: list[dict] | None = None,
) -> SimpleNamespace:
    at = START + timedelta(days=days)
    return SimpleNamespace(
        symbol="EURUSD", strategy=strategy, direction=direction,
        outcome=outcome, realised_pnl=D(realised) if realised is not None else None,
        actual_entry=D(entry), planned_entry=D(entry),
        actual_exit=D(exit_price) if exit_price is not None else None,
        planned_stop=D(stop), planned_targets=["103"],
        planned_size=D("10"), actual_size=D("10"),
        r_multiple=D(r_multiple) if r_multiple is not None else None,
        exit_reason=reason, regime=regime, fees=D("1"),
        opened_at=at, closed_at=at + timedelta(hours=2), considered_at=at,
        notes="", risk_verdict={"failures": failures or []},
    )


class TestOnlyMeasuredRowsBecomeTrades:
    def test_a_refused_setup_is_not_a_trade_with_zero_pnl(self):
        """Counting it as flat would drag every average toward nothing."""
        rows = [
            _row(),
            _row(outcome="REJECTED_BY_RISK", realised=None, exit_price=None,
                 failures=[{"code": "REWARD_RISK", "detail": "too low"}]),
        ]

        summary = summarise(rows, starting_equity=D("10000"))

        assert summary.metrics.trades == 1
        assert summary.refused == 1
        assert summary.metrics.total_pnl == D("100")

    def test_an_open_position_is_unmeasured_not_flat(self):
        rows = [_row(), _row(realised=None, exit_price=None, reason=None)]

        summary = summarise(rows, starting_equity=D("10000"))

        assert summary.metrics.trades == 1
        assert summary.unmeasured == 1

    def test_a_row_with_no_exit_price_cannot_be_measured(self):
        assert to_closed_trade(_row(exit_price=None)) is None

    def test_a_row_with_no_realised_pnl_cannot_be_measured(self):
        assert to_closed_trade(_row(realised=None)) is None


class TestTheConversionIsFaithful:
    def test_the_fields_carry_across(self):
        trade = to_closed_trade(_row(direction="SELL", regime="RANGING"))

        assert trade is not None
        assert trade.symbol == "EURUSD"
        assert trade.side is Side.SELL
        assert trade.pnl == D("100")
        assert trade.regime == "RANGING"
        assert trade.reason is ExitReason.TARGET
        assert trade.r_multiple == D("1.0")

    def test_an_unknown_exit_reason_is_not_forced_into_a_known_one(self):
        trade = to_closed_trade(_row(reason="something-else"))

        assert trade is not None
        assert trade.reason is ExitReason.END_OF_DATA

    def test_a_short_is_recognised_from_either_word(self):
        assert to_closed_trade(_row(direction="SHORT")).side is Side.SELL
        assert to_closed_trade(_row(direction="SELL")).side is Side.SELL
        assert to_closed_trade(_row(direction="BUY")).side is Side.BUY


class TestNothingIsRecomputed:
    def test_the_metrics_are_the_backtesters_metrics(self):
        """Same inputs through both paths, identical answers."""
        from gtcc.backtest.engine import BacktestResult, BarCosts
        from gtcc.backtest.metrics import measure
        from gtcc.domain.enums import Timeframe
        from gtcc.domain.money import ZERO

        rows = [_row(realised="50", days=index) for index in range(12)]
        rows += [_row(realised="-20", days=20 + index) for index in range(8)]
        summary = summarise(rows, starting_equity=D("10000"))

        trades = tuple(
            trade for trade in (to_closed_trade(row) for row in rows)
            if trade is not None
        )
        running = D("10000")
        curve = [running]
        for trade in sorted(trades, key=lambda t: t.entry_at):
            running += trade.pnl
            curve.append(running)
        direct = measure(
            BacktestResult(
                symbol="(account)", timeframe=Timeframe.M15, strategy="(all)",
                bars_seen=0, bars_traded=0, first_bar_at=None, last_bar_at=None,
                starting_equity=D("10000"), ending_equity=running,
                costs=BarCosts(spread_bps=ZERO, slippage_bps=ZERO, commission_bps=ZERO),
                trades=tuple(sorted(trades, key=lambda t: t.entry_at)),
                equity_curve=tuple(curve),
            )
        )

        assert summary.metrics == direct

    def test_small_buckets_obey_the_same_rule_as_a_backtest(self):
        rows = [_row(realised="50", days=index) for index in range(3)]

        summary = summarise(rows, starting_equity=D("10000"))

        regime = next(b for b in summary.breakdowns if b.dimension == "regime")
        assert regime.buckets[0].trades == 3
        assert regime.buckets[0].win_rate is None, "three trades earn no ratio"

    def test_the_sample_caveat_comes_through(self):
        rows = [_row(realised="50", days=index) for index in range(3)]

        summary = summarise(rows, starting_equity=D("10000"))

        assert not summary.metrics.trustworthy
        assert "too small" in summary.metrics.sample_warning
        assert summary.metrics.sample_warning in "\n".join(summary.describe())


class TestRefusalsAreCounted:
    def test_refusal_reasons_are_tallied_most_common_first(self):
        rows = [
            _row(outcome="REJECTED_BY_RISK", realised=None, exit_price=None,
                 failures=[{"code": "REWARD_RISK"}])
            for _ in range(5)
        ]
        rows += [
            _row(outcome="REJECTED_BY_RISK", realised=None, exit_price=None,
                 failures=[{"code": "SPREAD"}])
            for _ in range(2)
        ]

        summary = summarise(rows, starting_equity=D("10000"))

        assert summary.refused == 7
        assert summary.refusal_reasons[0] == ("REWARD_RISK", 5)
        assert summary.refusal_reasons[1] == ("SPREAD", 2)

    def test_a_strategy_whose_every_idea_is_refused_is_visible(self):
        """Otherwise it looks identical to a strategy with no ideas."""
        rows = [
            _row(outcome="REJECTED_BY_RISK", realised=None, exit_price=None,
                 failures=[{"code": "REWARD_RISK"}])
            for _ in range(20)
        ]

        text = "\n".join(summarise(rows, starting_equity=D("10000")).describe())

        assert "20 setup(s) were refused" in text
        assert "looks identical to one with no ideas" in text

    def test_an_empty_account_measures_nothing_rather_than_zero(self):
        summary = summarise([], starting_equity=D("10000"))

        assert summary.metrics.trades == 0
        assert summary.metrics.win_rate is None
        assert "absence of results" in summary.metrics.sample_warning


class TestTheAnalyticsPage:
    def test_it_shows_closed_trades(self, owner_api, runtime, quotes, now, journal_db):
        from gtcc.storage.repositories import TradeJournalRepository

        runtime.journal_store = TradeJournalRepository(journal_db)
        owner_api.post("/api/orders", json={
            "symbol": "BTCUSDT", "market": "CRYPTO", "side": "BUY",
            "order_type": "MARKET", "protective_stop": "59000",
            "targets": ["62500"], "strategy": "breakout",
        })
        quotes["BTCUSDT"] = quotes["BTCUSDT"].__class__(
            symbol="BTCUSDT", timestamp=now, bid=D("63000"), ask=D("63006"),
            bid_size=D("5"), ask_size=D("5"), received_at=now,
        )
        owner_api.post("/api/positions/settle")

        text = owner_api.get("/analytics").text

        assert "Closed trades" in text
        assert "too few trades to rate" in text, "one trade earns no rating"

    def test_no_journal_says_so_rather_than_showing_zeros(self, owner_api, runtime):
        runtime.journal_store = None

        text = owner_api.get("/analytics").text

        assert "No journal is configured" in text
        assert "not a flat month" in text

    def test_an_empty_journal_is_not_a_result(self, owner_api, runtime, journal_db):
        from gtcc.storage.repositories import TradeJournalRepository

        runtime.journal_store = TradeJournalRepository(journal_db)

        text = owner_api.get("/analytics").text

        assert "Nothing has closed yet" in text
        assert "stays empty rather than showing zeros" in text

    def test_an_empty_regime_table_explains_itself(
        self, owner_api, runtime, quotes, now, journal_db
    ):
        """Otherwise a reader takes it for a broken classifier."""
        from gtcc.storage.repositories import TradeJournalRepository

        runtime.journal_store = TradeJournalRepository(journal_db)
        owner_api.post("/api/orders", json={
            "symbol": "BTCUSDT", "market": "CRYPTO", "side": "BUY",
            "order_type": "MARKET", "protective_stop": "59000",
            "targets": ["62500"], "strategy": "breakout",
        })
        quotes["BTCUSDT"] = quotes["BTCUSDT"].__class__(
            symbol="BTCUSDT", timestamp=now, bid=D("63000"), ask=D("63006"),
            bid_size=D("5"), ask_size=D("5"), received_at=now,
        )
        owner_api.post("/api/positions/settle")

        text = owner_api.get("/analytics").text

        assert "No trade carried a regime" in text
        assert "placed by hand has none" in text
        assert "not that the" in text and "classifier failed" in text
