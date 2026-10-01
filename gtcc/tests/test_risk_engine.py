"""The deterministic risk engine.

The first class here is the one that matters. Specification section 41
names seven conditions that must refuse an order, and if any of those
tests ever goes green by accident — because a check was renamed, a
default changed, a context field was added — the platform can lose
money in a way no other test would catch.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from gtcc.data.quality import check_quote
from gtcc.domain.enums import (
    AssetClass,
    Market,
    OrderType,
    RiskAction,
    Side,
    Timeframe,
    TradingMode,
)
from gtcc.risk.safety import LIVE_CONFIRMATION_PHRASE, TripReason, initial_state
from gtcc.domain.events import EconomicEvent, EventImpact
from gtcc.domain.market_data import Quote
from gtcc.domain.money import D
from gtcc.domain.orders import OrderRequest, Position
from gtcc.risk.engine import Check, Outcome, RiskEngine, exposure_from_positions


def _request(**overrides) -> OrderRequest:
    base = dict(
        symbol="AAPL",
        market=Market.STOCKS,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        protective_stop=D("198.00"),
        targets=(D("204.00"),),
        strategy="breakout",
    )
    base.update(overrides)
    return OrderRequest(**base)


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine()


class TestTheSevenRefusals:
    """Specification section 41's critical risk tests, one per method."""

    def test_an_oversized_position_is_cut_or_refused(self, engine, context):
        """A caller asking for more than the limits allow gets less.

        The engine never honours a requested size it did not derive; it
        sizes independently and takes the smaller of the two.
        """
        verdict = engine.evaluate(_request(quantity=D("100000")), context)

        assert verdict.action is RiskAction.REDUCE
        assert verdict.approved_quantity < D("100000")
        # 0.5% of 100,000 equity over a 2.02 stop distance.
        assert verdict.sizing.projected_risk <= context.account.equity * D("0.005")

    def test_the_daily_loss_limit_blocks_new_orders(self, engine, context):
        losing = context.state.record_settled_trade(D("-2500"))  # 2.5% of 100k
        verdict = engine.evaluate(_request(), replace(context, state=losing))

        assert verdict.action is RiskAction.REJECT
        assert Check.DAILY_LOSS_LIMIT in verdict.failure_codes

    def test_a_stale_price_is_refused(self, engine, context, now):
        stale = Quote(
            symbol="AAPL",
            timestamp=now - timedelta(seconds=120),
            bid=D("199.98"),
            ask=D("200.02"),
            received_at=now,
        )
        verdict = engine.evaluate(
            _request(),
            replace(context, quote=stale, data_quality=check_quote(stale, now=now)),
        )

        assert verdict.action is RiskAction.REJECT
        assert Check.DATA_QUALITY in verdict.failure_codes

    def test_a_broker_disconnect_is_refused(self, engine, context):
        verdict = engine.evaluate(_request(), replace(context, broker_healthy=False))

        assert verdict.action is RiskAction.REJECT
        assert Check.BROKER_HEALTHY in verdict.failure_codes

    def test_an_invalid_stop_is_refused(self, engine, context):
        """A long whose stop sits above the entry has no definable risk."""
        verdict = engine.evaluate(_request(protective_stop=D("201.00")), context)

        assert verdict.action is RiskAction.REJECT
        assert Check.STOP_ON_CORRECT_SIDE in verdict.failure_codes

    def test_a_missing_stop_is_refused(self, engine, context):
        verdict = engine.evaluate(_request(protective_stop=None), context)

        assert verdict.action is RiskAction.REJECT
        assert Check.STOP_PRESENT in verdict.failure_codes
        assert verdict.approved_quantity == 0

    def test_a_live_order_is_impossible_while_live_is_unarmed(self, engine, context):
        """The one that must never regress.

        Reaching LIVE mode without a person having armed it in this
        process is not a warning state. No quantity is approved,
        whatever else about the request is sound.

        The state here is constructed by hand precisely because the
        supported path cannot produce it: ``arm_live`` is the only way
        into LIVE and it sets ``live_armed``. This proves the engine
        would still refuse if some future code path manufactured the
        inconsistent state.
        """
        from dataclasses import replace as dc_replace

        unarmed_live = dc_replace(
            initial_state(TradingMode.PAPER), mode=TradingMode.LIVE, live_armed=False
        )
        verdict = engine.evaluate(_request(), replace(context, execution=unarmed_live))

        assert verdict.action is RiskAction.REJECT
        assert Check.LIVE_MODE_PERMITTED in verdict.failure_codes
        assert verdict.approved_quantity == 0

    def test_a_latched_breaker_refuses_every_order(self, engine, context):
        """The eighth refusal, added by this repair pass.

        A latched safety trip stops orders in every mode, and recovery
        of the dependency does not clear it.
        """
        tripped = context.execution.trip(TripReason.BROKER_UNHEALTHY, "connection lost")

        verdict = engine.evaluate(_request(), replace(context, execution=tripped))

        assert verdict.action is RiskAction.REJECT
        assert Check.EXECUTION_NOT_TRIPPED in verdict.failure_codes

    def test_an_armed_live_order_is_allowed(self, engine, context):
        """The positive case, so the refusal above is not vacuous."""
        armed = context.execution.arm_live(
            actor="owner@example.com",
            confirmation=LIVE_CONFIRMATION_PHRASE,
            deployment_allows_live=True,
        )

        verdict = engine.evaluate(_request(), replace(context, execution=armed))

        assert verdict.action in (RiskAction.ALLOW, RiskAction.REDUCE)
        assert Check.LIVE_MODE_PERMITTED not in verdict.failure_codes


class TestTheHappyPath:
    def test_a_sound_order_is_approved_with_a_size(self, engine, context):
        verdict = engine.evaluate(_request(), context)

        assert verdict.action is RiskAction.ALLOW
        assert verdict.approved_quantity > 0
        assert not verdict.failures

    def test_every_check_is_recorded_whether_it_passed_or_not(self, engine, context):
        """Section 14: the platform must show exactly why. A verdict that
        only lists failures cannot answer "what did you consider?"."""
        verdict = engine.evaluate(_request(), context)

        recorded = {check.code for check in verdict.checks}
        for required in (
            Check.LIVE_MODE_PERMITTED,
            Check.KILL_SWITCH,
            Check.DATA_QUALITY,
            Check.SPREAD_WITHIN_LIMIT,
            Check.STOP_DISTANCE,
            Check.REWARD_RISK,
            Check.DAILY_LOSS_LIMIT,
            Check.MAX_DRAWDOWN,
            Check.RISK_PER_TRADE,
            Check.LEVERAGE,
            Check.ASSET_EXPOSURE,
            Check.BUYING_POWER,
        ):
            assert required in recorded, f"{required} was not evaluated"
        assert all(check.detail for check in verdict.checks)

    def test_the_verdict_is_reproducible(self, engine, context):
        """Same inputs, same answer. The engine holds no hidden state."""
        first = engine.evaluate(_request(), context)
        second = engine.evaluate(_request(), context)

        assert first.action == second.action
        assert first.approved_quantity == second.approved_quantity
        assert [c.outcome for c in first.checks] == [c.outcome for c in second.checks]

    def test_risk_is_sized_to_the_budget_not_to_the_request(self, engine, context):
        verdict = engine.evaluate(_request(), context)
        budget = context.account.equity * context.limits.max_risk_per_trade

        assert verdict.sizing.projected_risk <= budget
        # And it uses most of it: a sizer that always returns one share
        # would pass the line above and be useless.
        assert verdict.sizing.projected_risk > budget * D("0.9")


class TestWhatTheTradeActuallyRisks:
    """`sizing.projected_risk` is what the risk budget ASKED for. Once a
    face-value, leverage or exposure ceiling shrinks the position, the
    money at risk shrinks with it, and only `approved_risk` is true.

    The distinction was briefly lost: the API and the risk explainer both
    reported the budget figure, which overstated the loss on every capped
    trade. Anything that tells a human "if the stop is hit you lose this
    much" must read `approved_risk`.
    """

    def test_approved_risk_is_the_loss_at_the_approved_quantity(self, engine, context):
        verdict = engine.evaluate(_request(), context)

        assert verdict.approved_risk == (
            verdict.approved_quantity * verdict.sizing.risk_per_unit
        )

    def test_a_capped_trade_risks_less_than_the_budget_asked_for(self, engine, context):
        """The default request is capped: 247 shares at 200 is half the
        account, and the position cap is a quarter of it."""
        verdict = engine.evaluate(_request(), context)

        assert verdict.binding_limits, "this request should hit the position cap"
        assert verdict.approved_risk < verdict.sizing.projected_risk
        assert verdict.approved_risk < context.account.equity * D("0.005")

    def test_binding_limits_name_the_ceilings_tightest_last(self, engine, context):
        verdict = engine.evaluate(_request(), context)

        assert all(check.reduced_to is not None for check in verdict.binding_limits)
        quantities = [check.reduced_to for check in verdict.binding_limits]
        assert quantities == sorted(quantities, reverse=True)
        assert verdict.approved_quantity <= quantities[-1]

    def test_an_uncapped_trade_risks_the_whole_budget(self, engine, context):
        """A stop wide enough that the budget, not the cap, sizes it."""
        verdict = engine.evaluate(
            _request(protective_stop=D("192.00"), targets=(D("225.00"),)), context
        )

        assert verdict.action is RiskAction.ALLOW
        assert verdict.binding_limits == ()
        assert verdict.approved_risk == verdict.sizing.projected_risk
        assert verdict.approved_risk > context.account.equity * D("0.004")

    def test_a_refusal_that_was_sized_puts_nothing_at_risk(self, engine, context):
        """Refused on reward:risk, so sizing happened and the answer is a
        real zero."""
        verdict = engine.evaluate(_request(targets=(D("200.80"),)), context)

        assert verdict.action is RiskAction.REJECT
        assert Check.REWARD_RISK in verdict.failure_codes
        assert verdict.approved_quantity == D("0")
        assert verdict.approved_risk == D("0")

    def test_a_refusal_before_sizing_reports_no_figure_rather_than_zero(
        self, engine, context
    ):
        """An unknown is not a zero.

        The stop is inside the minimum distance, so the engine never
        sized the trade. Reporting 0.00 would imply a position that
        risks nothing; None says there is no number, which is the
        truth.
        """
        verdict = engine.evaluate(_request(protective_stop=D("200.015")), context)

        assert verdict.action is RiskAction.REJECT
        assert verdict.sizing is None
        assert verdict.approved_risk is None


class TestRewardRisk:
    def test_a_target_too_close_to_pay_for_the_costs_is_refused(self, engine, context):
        verdict = engine.evaluate(_request(targets=(D("200.60"),)), context)

        assert verdict.action is RiskAction.REJECT
        assert Check.REWARD_RISK in verdict.failure_codes

    def test_the_nearest_target_is_scored_not_the_furthest(self, engine, context):
        """Section 15 forbids inventing a distant target to clear the
        minimum ratio. Scoring the nearest one removes the incentive."""
        verdict = engine.evaluate(
            _request(targets=(D("200.60"), D("260.00"))), context
        )

        assert verdict.reward_risk.target == D("200.60")
        assert verdict.action is RiskAction.REJECT

    def test_costs_are_charged_against_reward_and_added_to_risk(self, engine, context):
        verdict = engine.evaluate(_request(), context)
        rr = verdict.reward_risk

        assert rr.costs > 0
        assert rr.net_reward < rr.gross_reward
        assert rr.net_risk > rr.gross_risk
        assert rr.ratio < rr.gross_reward / rr.gross_risk


class TestOperatorSwitches:
    def test_the_kill_switch_stops_everything(self, engine, context):
        verdict = engine.evaluate(
            _request(), replace(context, state=context.state.with_kill_switch(True))
        )

        assert verdict.action is RiskAction.REJECT
        assert Check.KILL_SWITCH in verdict.failure_codes

    def test_pausing_stops_new_trades(self, engine, context):
        verdict = engine.evaluate(_request(), replace(context, state=context.state.paused(True)))

        assert Check.TRADING_NOT_PAUSED in verdict.failure_codes

    def test_a_disabled_symbol_is_refused(self, engine, context):
        state = context.state.disable_symbol("AAPL")
        verdict = engine.evaluate(_request(), replace(context, state=state))

        assert Check.SYMBOL_ENABLED in verdict.failure_codes

    def test_a_disabled_strategy_is_refused(self, engine, context):
        state = context.state.disable_strategy("breakout")
        verdict = engine.evaluate(_request(), replace(context, state=state))

        assert Check.STRATEGY_ENABLED in verdict.failure_codes

    def test_a_disabled_market_is_refused(self, engine, context):
        state = context.state.disable_market(Market.STOCKS)
        verdict = engine.evaluate(_request(), replace(context, state=state))

        assert Check.MARKET_ENABLED in verdict.failure_codes


class TestBreakers:
    def test_the_weekly_limit_blocks_independently_of_the_daily_one(self, engine, context):
        state = replace(context.state, realised_pnl_week=D("-6000"), realised_pnl_today=D("0"))
        verdict = engine.evaluate(_request(), replace(context, state=state))

        assert Check.WEEKLY_LOSS_LIMIT in verdict.failure_codes
        assert Check.DAILY_LOSS_LIMIT not in verdict.failure_codes

    def test_drawdown_is_measured_against_the_high_water_mark(self, engine, context):
        state = replace(context.state, peak_equity=D("120000"))
        verdict = engine.evaluate(_request(), replace(context, state=state))

        assert Check.MAX_DRAWDOWN in verdict.failure_codes

    def test_consecutive_losses_stop_new_trades(self, engine, context):
        state = replace(context.state, consecutive_losses=4)
        verdict = engine.evaluate(_request(), replace(context, state=state))

        assert Check.CONSECUTIVE_LOSSES in verdict.failure_codes

    def test_a_tripped_breaker_survives_a_flat_day(self, engine, context):
        """The tally resets at the roll; the trip does not reset itself."""
        tripped = context.state.trip(daily=True)
        verdict = engine.evaluate(_request(), replace(context, state=tripped))

        assert Check.DAILY_LOSS_LIMIT in verdict.failure_codes

    def test_rolling_the_day_clears_the_daily_breaker_but_not_the_drawdown(self, context):
        state = context.state.trip(daily=True, drawdown=True)
        rolled = state.roll_day(D("98000"))

        assert not rolled.daily_breaker_tripped
        assert rolled.drawdown_breaker_tripped


class TestConcentration:
    def test_too_many_open_positions_is_refused(self, engine, context):
        positions = tuple(
            Position(
                symbol=f"SYM{i}",
                market=Market.STOCKS,
                asset_class=AssetClass.EQUITY,
                quantity=D("10"),
                average_entry_price=D("100"),
                mark_price=D("100"),
            )
            for i in range(5)
        )
        verdict = engine.evaluate(_request(), replace(context, positions=positions))

        assert Check.MAX_OPEN_POSITIONS in verdict.failure_codes

    def test_existing_exposure_shrinks_the_new_position(self, engine, context):
        clean = engine.evaluate(_request(), context).approved_quantity
        crowded = engine.evaluate(
            _request(), replace(context, exposure_by_symbol={"AAPL": D("20000")})
        )

        assert 0 < crowded.approved_quantity < clean

    def test_an_unvaluable_position_stops_trading(self, engine, context):
        """Section 42: never trade when account state cannot be determined."""
        positions = (
            Position(
                symbol="MYSTERY",
                market=Market.STOCKS,
                asset_class=AssetClass.EQUITY,
                quantity=D("10"),
                average_entry_price=D("100"),
                mark_price=None,
            ),
        )
        exposure, known = exposure_from_positions(positions, {})
        verdict = engine.evaluate(
            _request(),
            replace(context, positions=positions, exposure_by_symbol=exposure, exposure_known=known),
        )

        assert not known
        assert Check.EXPOSURE_KNOWN in verdict.failure_codes

    def test_caps_do_not_compound_against_each_other(self, engine, context):
        """Each ceiling yields its own maximum; the smallest wins.

        Scaling the running size through every cap in turn would shrink
        the position once per cap and silently undersize every trade.
        """
        verdict = engine.evaluate(_request(), context)
        notional_cap = context.account.equity * context.limits.max_position_notional
        entry = context.quote.ask

        # The approved size should sit just under the single binding cap.
        assert verdict.approved_quantity * entry <= notional_cap
        assert (verdict.approved_quantity + 1) * entry > notional_cap


class TestSpreadAndSlippage:
    def test_a_wide_spread_is_refused(self, engine, context, now):
        wide = Quote(
            symbol="AAPL", timestamp=now, bid=D("199.00"), ask=D("201.00"), received_at=now
        )
        verdict = engine.evaluate(
            _request(),
            replace(context, quote=wide, data_quality=check_quote(wide, now=now)),
        )

        assert Check.SPREAD_WITHIN_LIMIT in verdict.failure_codes

    def test_excessive_expected_slippage_is_refused(self, engine, context):
        verdict = engine.evaluate(
            _request(), replace(context, estimated_slippage_bps=D("50"))
        )

        assert Check.SLIPPAGE_WITHIN_LIMIT in verdict.failure_codes

    def test_a_missing_quote_is_refused(self, engine, context):
        verdict = engine.evaluate(_request(), replace(context, quote=None))

        assert verdict.action is RiskAction.REJECT
        assert Check.QUOTE_AVAILABLE in verdict.failure_codes


class TestEventBlackout:
    def _event(self, now, minutes: int) -> EconomicEvent:
        return EconomicEvent(
            event_id="CPI",
            name="US CPI",
            scheduled_for=now + timedelta(minutes=minutes),
            impact=EventImpact.HIGH,
            currencies=frozenset({"USD"}),
            source="test-calendar",
        )

    def test_a_high_impact_event_inside_the_window_blocks_a_short_dated_trade(
        self, engine, context, now
    ):
        verdict = engine.evaluate(
            _request(),
            replace(
                context,
                calendar_available=True,
                upcoming_events=(self._event(now, 5),),
                strategy_timeframe=Timeframe.M5,
            ),
        )

        assert Check.EVENT_BLACKOUT in verdict.failure_codes

    def test_an_event_outside_the_window_does_not_block(self, engine, context, now):
        verdict = engine.evaluate(
            _request(),
            replace(
                context,
                calendar_available=True,
                upcoming_events=(self._event(now, 90),),
                strategy_timeframe=Timeframe.M5,
            ),
        )

        assert Check.EVENT_BLACKOUT not in verdict.failure_codes

    def test_a_declared_event_strategy_opts_out(self, engine, context, now):
        verdict = engine.evaluate(
            _request(),
            replace(
                context,
                calendar_available=True,
                upcoming_events=(self._event(now, 5),),
                strategy_timeframe=Timeframe.M5,
                event_strategy=True,
            ),
        )

        skipped = [c for c in verdict.checks if c.code is Check.EVENT_BLACKOUT]
        assert skipped[0].outcome is Outcome.SKIP

    def test_no_calendar_is_recorded_as_a_skip_not_as_an_all_clear(
        self, engine, context
    ):
        """An empty diary is not evidence that nothing is scheduled."""
        verdict = engine.evaluate(_request(), replace(context, calendar_available=False))

        blackout = [c for c in verdict.checks if c.code is Check.EVENT_BLACKOUT][0]
        assert blackout.outcome is Outcome.SKIP
        assert "not evidence" in blackout.detail


class TestAcrossAssetClasses:
    """Section 17: the same risk budget must produce different sizes."""

    def test_futures_sizing_uses_tick_value_not_the_price_difference(
        self, engine, context, futures_spec, now
    ):
        quote = Quote(
            symbol="ES", timestamp=now, bid=D("5000.00"), ask=D("5000.25"),
            bid_size=D("100"), ask_size=D("100"), received_at=now,
        )
        verdict = engine.evaluate(
            OrderRequest(
                symbol="ES",
                market=Market.FUTURES,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                protective_stop=D("4990.00"),
                targets=(D("5030.00"),),
                strategy="breakout",
            ),
            replace(
                context,
                instrument=futures_spec,
                quote=quote,
                data_quality=check_quote(quote, now=now),
            ),
        )

        # Entry 5000.25 to stop 4990.00 is 41 ticks, and a tick is worth
        # 12.50, so ONE contract risks 512.50 against a 500 budget. The
        # engine refuses rather than rounding up to a breach.
        #
        # This is the whole point of section 17. Treating the 10.25
        # point move as 10.25 dollars per unit — share arithmetic — would
        # have sized 48 contracts and risked 24,600 on a 500 budget.
        assert verdict.action is RiskAction.REJECT
        assert Check.POSITION_SIZEABLE in verdict.failure_codes
        assert "less than one lot" in verdict.sizing.rejected_reason

        naive_shares_maths = D("500") / (D("5000.25") - D("4990.00"))
        assert naive_shares_maths > 48

    def test_a_futures_contract_fits_once_the_stop_is_close_enough(
        self, engine, context, futures_spec, now
    ):
        quote = Quote(
            symbol="ES", timestamp=now, bid=D("5000.00"), ask=D("5000.25"),
            bid_size=D("100"), ask_size=D("100"), received_at=now,
        )
        verdict = engine.evaluate(
            OrderRequest(
                symbol="ES",
                market=Market.FUTURES,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                protective_stop=D("4995.25"),
                targets=(D("5020.25"),),
                strategy="breakout",
            ),
            replace(
                context,
                instrument=futures_spec,
                quote=quote,
                data_quality=check_quote(quote, now=now),
            ),
        )

        # 20 ticks x 12.50 is 250 per contract: two fit inside the 500.
        assert verdict.sizing.quantity == D("2")
        assert verdict.sizing.projected_risk == D("500.00")
        assert verdict.action is RiskAction.ALLOW

    def test_a_forex_pair_sizes_in_units_of_base_currency(
        self, engine, context, forex_spec, now
    ):
        quote = Quote(
            symbol="EURUSD", timestamp=now, bid=D("1.08495"), ask=D("1.08505"),
            bid_size=D("1000000"), ask_size=D("1000000"), received_at=now,
        )
        verdict = engine.evaluate(
            OrderRequest(
                symbol="EURUSD",
                market=Market.FOREX,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                protective_stop=D("1.08000"),
                targets=(D("1.09600"),),
                strategy="breakout",
            ),
            replace(
                context,
                instrument=forex_spec,
                quote=quote,
                data_quality=check_quote(quote, now=now),
            ),
        )

        assert verdict.action in (RiskAction.ALLOW, RiskAction.REDUCE)
        assert verdict.sizing.quantity % D("1000") == 0
        assert verdict.sizing.projected_risk <= D("500")
