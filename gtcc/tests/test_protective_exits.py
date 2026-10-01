"""A paper position must be able to close.

Without this a paper trade with a stop at 95 stays open while price goes
to 50: the loss shows as unrealised forever, no loss breaker ever trips
because nothing is realised, and the journal row that planned the trade
never records what happened. The simulation would be systematically
kinder than reality in the one direction that matters — and the kindness
would be invisible, because an open position looks like a position that
has not finished yet.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gtcc.adapters.paper import ExitReason
from gtcc.domain.enums import Market, OrderType, Side, TradingMode
from gtcc.domain.market_data import Quote
from gtcc.domain.money import D
from gtcc.domain.orders import OrderRequest
from gtcc.risk.safety import TripReason
from gtcc.storage.repositories import TradeJournalRepository

from tests.test_journal import journal_db  # noqa: F401


def _order(**overrides) -> OrderRequest:
    base = dict(
        symbol="BTCUSDT", market=Market.CRYPTO, side=Side.BUY,
        order_type=OrderType.MARKET, protective_stop=D("59000"),
        targets=(D("62500"),), strategy="breakout",
    )
    base.update(overrides)
    return OrderRequest(**base)


def _at(quotes: dict, now: datetime, symbol: str, bid: str, ask: str) -> None:
    """Move the market by replacing the quote the fixtures serve."""
    quotes[symbol] = Quote(
        symbol=symbol, timestamp=now, bid=D(bid), ask=D(ask),
        bid_size=D("5"), ask_size=D("5"), received_at=now,
    )


class TestStopsAndTargetsFire:
    def test_a_long_stopped_out_is_closed_and_realised(
        self, runtime, quotes, now
    ):
        result = runtime.submit(_order())
        assert result.placed

        _at(quotes, now, "BTCUSDT", "58000", "58006")
        exits = runtime.settle_protective_exits()

        assert len(exits) == 1
        assert exits[0].reason is ExitReason.STOP
        assert exits[0].realised_pnl < 0
        assert runtime.broker.get_positions() == ()

    def test_a_long_reaching_its_target_is_closed_in_profit(
        self, runtime, quotes, now
    ):
        runtime.submit(_order())

        _at(quotes, now, "BTCUSDT", "63000", "63006")
        exits = runtime.settle_protective_exits()

        assert len(exits) == 1
        assert exits[0].reason is ExitReason.TARGET
        assert exits[0].realised_pnl > 0

    def test_a_position_inside_its_levels_stays_open(self, runtime, quotes, now):
        runtime.submit(_order())

        _at(quotes, now, "BTCUSDT", "60500", "60506")

        assert runtime.settle_protective_exits() == []
        assert len(runtime.broker.get_positions()) == 1

    def test_settling_twice_closes_nothing_the_second_time(
        self, runtime, quotes, now
    ):
        runtime.submit(_order())
        _at(quotes, now, "BTCUSDT", "58000", "58006")

        first = runtime.settle_protective_exits()
        second = runtime.settle_protective_exits()

        assert len(first) == 1
        assert second == []

    def test_a_short_stops_out_on_the_way_up(self, runtime, quotes, now):
        runtime.submit(
            _order(side=Side.SELL, protective_stop=D("61000"), targets=(D("57000"),))
        )

        _at(quotes, now, "BTCUSDT", "62000", "62006")
        exits = runtime.settle_protective_exits()

        assert len(exits) == 1
        assert exits[0].reason is ExitReason.STOP
        assert exits[0].realised_pnl < 0


class TestTheFillIsNeverBetterThanObserved:
    def test_a_gap_through_the_stop_fills_at_the_observed_price(
        self, runtime, quotes, now
    ):
        """Filling at the stop level would claim a price never seen.

        This is how a simulation hides gap risk: the stop was 59,000, the
        market is at 50,000, and only one of those is a price this
        simulation has actually observed.
        """
        runtime.submit(_order())

        _at(quotes, now, "BTCUSDT", "50000", "50006")
        exits = runtime.settle_protective_exits()

        assert exits[0].exit_price == D("50000")
        assert exits[0].exit_price < D("59000")

    def test_a_gap_past_the_target_does_not_pay_the_better_price(
        self, runtime, quotes, now
    ):
        """Pessimism cuts both ways: it is applied, not chosen per case."""
        runtime.submit(_order())

        _at(quotes, now, "BTCUSDT", "70000", "70006")
        exits = runtime.settle_protective_exits()

        assert exits[0].reason is ExitReason.TARGET
        assert exits[0].exit_price == D("62500"), "the target, not the better mark"

    def test_the_stop_is_checked_before_the_target(self, runtime, quotes, now):
        """Defensive ordering, for a case a coherent plan cannot reach.

        The backtester sees a bar's high AND low at once, so "both levels
        touched" is a real ambiguity it must resolve. A paper mark is a
        single price: reaching both requires target <= mark <= stop, which
        for a long means the target sits below the stop — an inverted plan
        the risk engine refuses. So this is unreachable through `submit`,
        and the ordering is asserted against a position placed directly on
        the broker rather than by pretending the normal path can produce it.
        """
        from gtcc.domain.enums import AssetClass
        from gtcc.domain.orders import Position

        broker = runtime.broker
        broker._positions["BTCUSDT"] = Position(
            symbol="BTCUSDT", market=Market.CRYPTO,
            asset_class=AssetClass.CRYPTO_SPOT,
            quantity=D("1"), average_entry_price=D("60000"),
            protective_stop=D("61000"), targets=(D("59000"),),
            strategy="breakout",
        )

        _at(quotes, now, "BTCUSDT", "60000", "60006")
        exits = broker.settle_protective_exits()

        assert len(exits) == 1
        assert exits[0].reason is ExitReason.STOP, "the stop is evaluated first"


class TestTheRiskTallySeesIt:
    def test_a_realised_loss_counts_against_the_daily_limit(
        self, runtime, quotes, now
    ):
        runtime.submit(_order())
        before = runtime.ensure_state().realised_pnl_today

        _at(quotes, now, "BTCUSDT", "58000", "58006")
        runtime.settle_protective_exits()

        after = runtime.ensure_state().realised_pnl_today
        assert after < before

    def test_a_large_enough_loss_latches_the_breaker(self, runtime, quotes, now):
        """The whole point: an unrealised loss trips nothing."""
        runtime.submit(_order())

        # A catastrophic gap, well past the 2% daily limit on 100k.
        _at(quotes, now, "BTCUSDT", "30000", "30006")
        runtime.settle_protective_exits()

        execution = runtime.ensure_execution()
        assert execution.tripped
        assert TripReason.DAILY_LOSS_LIMIT in {t.reason for t in execution.trips}

    def test_an_unrealised_loss_alone_trips_nothing(self, runtime, quotes, now):
        """Which is why settling has to happen at all."""
        runtime.submit(_order())

        _at(quotes, now, "BTCUSDT", "30000", "30006")

        assert not runtime.ensure_execution().tripped


class TestTheJournalRowIsCompleted:
    def test_the_outcome_lands_on_the_row_that_planned_it(
        self, runtime, quotes, now, journal_db
    ):
        store = TradeJournalRepository(journal_db)
        runtime.journal_store = store
        runtime.submit(_order())

        _at(quotes, now, "BTCUSDT", "63000", "63006")
        runtime.settle_protective_exits()

        rows = store.recent(runtime.account().account_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.outcome == "TAKEN"
        assert row.realised_pnl is not None and row.realised_pnl > 0
        assert row.exit_reason == "TARGET"
        assert row.closed_at is not None
        assert row.actual_exit == D("62500")

    def test_the_r_multiple_is_recorded(self, runtime, quotes, now, journal_db):
        store = TradeJournalRepository(journal_db)
        runtime.journal_store = store
        runtime.submit(_order())

        _at(quotes, now, "BTCUSDT", "58000", "58006")
        runtime.settle_protective_exits()

        row = store.recent(runtime.account().account_id)[0]
        assert row.r_multiple is not None
        assert row.r_multiple < 0, "a stopped-out trade is a negative R"

    def test_a_close_with_no_planning_row_is_logged_not_invented(
        self, runtime, quotes, now, journal_db, caplog
    ):
        """A journal entry with no setup behind it reads as a trade nobody
        planned, which is worse than a loud gap."""
        import logging

        store = TradeJournalRepository(journal_db)
        runtime.submit(_order())          # journal not attached yet
        runtime.journal_store = store     # attached only for the close

        _at(quotes, now, "BTCUSDT", "58000", "58006")
        with caplog.at_level(logging.ERROR):
            runtime.settle_protective_exits()

        assert "no journal row" in caplog.text
        assert store.recent(runtime.account().account_id) == []

    def test_a_second_trade_completes_its_own_row(
        self, runtime, quotes, now, journal_db
    ):
        """Matching must not keep writing onto the first row."""
        store = TradeJournalRepository(journal_db)
        runtime.journal_store = store

        runtime.submit(_order())
        _at(quotes, now, "BTCUSDT", "63000", "63006")
        runtime.settle_protective_exits()

        _at(quotes, now, "BTCUSDT", "60000", "60006")
        runtime.submit(_order())
        _at(quotes, now, "BTCUSDT", "58000", "58006")
        runtime.settle_protective_exits()

        rows = store.recent(runtime.account().account_id)
        closed = [row for row in rows if row.realised_pnl is not None]
        assert len(closed) == 2
        assert {row.exit_reason for row in closed} == {"TARGET", "STOP"}


class TestARealVenueIsNotSimulated:
    def test_a_broker_without_protective_simulation_settles_nothing(self, runtime):
        """At a real venue the stop lives at the venue, and the close arrives
        as a fill through reconciliation rather than being simulated here."""

        class VenueWithoutSimulation:
            name = "venue"

            def get_account(self):
                return runtime_account

            def get_positions(self):
                return ()

        runtime_account = runtime.broker.get_account()
        runtime.registry.register_broker(VenueWithoutSimulation())
        runtime.broker_name = "venue"

        assert runtime.settle_protective_exits() == []


class TestSettlingHappensBeforeNewRisk:
    """An unsettled stop understates today's loss, so a breaker that should
    already have latched has not — and the next order is approved against a
    tally missing the loss that should have stopped it."""

    def test_a_new_order_settles_the_old_position_first(
        self, runtime, quotes, now
    ):
        runtime.submit(_order())
        _at(quotes, now, "BTCUSDT", "58000", "58006")

        # No explicit settle call: submitting is what triggers it.
        runtime.submit(_order(symbol="AAPL", market=Market.STOCKS,
                              protective_stop=D("195"), targets=(D("215"),)))

        assert runtime.ensure_state().realised_pnl_today < 0

    def test_a_stop_that_should_have_latched_blocks_the_next_order(
        self, runtime, quotes, now
    ):
        """The property that makes this a safety requirement."""
        runtime.submit(_order())
        _at(quotes, now, "BTCUSDT", "30000", "30006")

        second = runtime.submit(
            _order(symbol="AAPL", market=Market.STOCKS,
                   protective_stop=D("195"), targets=(D("215"),))
        )

        assert not second.placed, "the breaker should have latched first"
        execution = runtime.ensure_execution()
        assert execution.tripped
        assert TripReason.DAILY_LOSS_LIMIT in {t.reason for t in execution.trips}


class TestTheSettlementEndpoint:
    def test_it_closes_and_reports_what_it_closed(self, owner_api, runtime, quotes, now):
        owner_api.post("/api/orders", json={
            "symbol": "BTCUSDT", "market": "CRYPTO", "side": "BUY",
            "order_type": "MARKET", "protective_stop": "59000",
            "targets": ["62500"], "strategy": "breakout",
        })
        _at(quotes, now, "BTCUSDT", "63000", "63006")

        payload = owner_api.post("/api/positions/settle").json()

        assert len(payload["exits"]) == 1
        assert payload["exits"][0]["reason"] == "TARGET"
        assert Decimal(payload["realised_today"]) > 0
        assert payload["breaker_tripped"] is False

    def test_a_settlement_that_latches_says_so(self, owner_api, runtime, quotes, now):
        """A caller reading only `exits` would not know trading had stopped."""
        owner_api.post("/api/orders", json={
            "symbol": "BTCUSDT", "market": "CRYPTO", "side": "BUY",
            "order_type": "MARKET", "protective_stop": "59000",
            "targets": ["62500"], "strategy": "breakout",
        })
        _at(quotes, now, "BTCUSDT", "30000", "30006")

        payload = owner_api.post("/api/positions/settle").json()

        assert payload["breaker_tripped"] is True
        assert "DAILY_LOSS_LIMIT" in payload["trips"]

    def test_settling_nothing_is_an_empty_list_not_an_error(self, owner_api):
        payload = owner_api.post("/api/positions/settle").json()

        assert payload["exits"] == []

    def test_a_viewer_cannot_settle(self, viewer_api):
        """It changes the account, so it is an owner action."""
        assert viewer_api.post("/api/positions/settle").status_code in (401, 403)
