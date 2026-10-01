"""The risk explainer must not flatter the configuration.

Every line it prints is something the owner will act on. Two failures
matter more than the rest, and both have already happened once:

  - quoting a ceiling that another, tighter ceiling overrides, so the
    owner believes they have room they do not have;
  - quoting the risk budget as the loss, when a cap shrank the position
    and the real loss is a fraction of it.

These tests pin both against the engine itself, so the text cannot
drift away from the behaviour it describes.
"""

from __future__ import annotations

import copy
import re
from decimal import Decimal

import pytest

from gtcc.data.quality import check_quote
from gtcc.domain.enums import Market, OrderType, RiskAction, Side
from gtcc.domain.market_data import Quote
from gtcc.domain.money import D
from gtcc.domain.orders import OrderRequest
from gtcc.risk.engine import Check, RiskEngine
from gtcc.risk.explain import binding_face_value_cap, explain, worked_example
from gtcc.risk.limits import parse_limits

from tests.conftest import LIMITS_RAW

EQUITY = D("100000")


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine()


def _limits(**markets) -> object:
    raw = copy.deepcopy(LIMITS_RAW)
    for market, block in markets.items():
        raw["market_overrides"][market] = block
    return parse_limits(raw)


def _text(lines: list[str]) -> str:
    return "\n".join(lines)


_MONEY = re.compile(r"USD ([\d,]+\.\d\d)")


def _money_after(text: str, marker: str) -> Decimal:
    """The first money figure printed after *marker*."""
    assert marker in text, f"{marker!r} is not in the output"
    match = _MONEY.search(text.split(marker, 1)[1])
    assert match is not None, f"no money figure follows {marker!r}"
    return D(match.group(1).replace(",", ""))


class TestTheBindingCap:
    def test_the_smallest_of_the_four_ceilings_wins(self):
        """A generous override under a tight exposure cap is not room."""
        raw = copy.deepcopy(LIMITS_RAW)
        raw["concentration"]["max_market_exposure_pct"]["FOREX"] = 40
        raw["market_overrides"]["FOREX"] = {
            "max_position_notional_pct": 300,
            "max_leverage": 3,
            "max_exposure_per_asset_pct": 300,
        }
        limits = parse_limits(raw)

        name, fraction = binding_face_value_cap(limits, Market.FOREX)

        assert fraction == D("0.4"), "the 40% market cap binds, not the 300% override"
        assert "FOREX" in name and "exposure" in name
        assert "USD 40,000.00" in _text(explain(limits, equity=EQUITY))
        assert "USD 300,000.00" not in _text(explain(limits, equity=EQUITY))

    def test_a_leverage_cap_counts_as_a_face_value_cap(self):
        """Face value over equity IS leverage for one position."""
        raw = copy.deepcopy(LIMITS_RAW)
        raw["market_overrides"]["FOREX"] = {
            "max_position_notional_pct": 900,
            "max_leverage": 1.5,
            "max_exposure_per_asset_pct": 900,
        }
        limits = parse_limits(raw)

        name, fraction = binding_face_value_cap(limits, Market.FOREX)

        assert fraction == D("1.5")
        assert name == "leverage cap"

    def test_the_printed_cap_is_what_the_engine_actually_allows(self, limits):
        """The claim is checked against the engine, not against arithmetic.

        If these drift apart the text is wrong, whichever one moved.
        """
        _, fraction = binding_face_value_cap(limits, Market.FOREX)
        cap = EQUITY * fraction

        text = _text(worked_example(limits, equity=EQUITY))
        face = _money_after(text, "Face value ")

        assert face <= cap, "the engine approved more face value than the text promises"
        # Within one lot of the cap: the engine rounds down to a tradeable size.
        assert cap - face < cap * D("0.01")


class TestTheWorkedExample:
    def test_reports_the_loss_at_the_approved_size_not_the_budget(self, limits):
        """The regression that mattered.

        With these limits the 25% face-value cap binds on EUR/USD, so
        the position is a fraction of what the risk budget asked for and
        so is the loss. Printing the budget would overstate the loss by
        4.5x.
        """
        budget = EQUITY * limits.max_risk_per_trade
        text = _text(worked_example(limits, equity=EQUITY))

        loss = _money_after(text, "you lose about ")
        units = _money_after(text, "Face value ") / D("1.08500")

        assert loss < budget, "a capped trade cannot risk the full budget"
        # The loss is the stop distance times the size actually approved.
        assert abs(loss - units * D("0.003")) < D("0.02")

    def test_names_the_ceiling_that_cut_the_size(self, limits):
        text = _text(worked_example(limits, equity=EQUITY))
        assert "The size was cut from what the risk budget asked for by" in text
        assert "POSITION_NOTIONAL" in text or "ASSET_EXPOSURE" in text or (
            "MARKET_EXPOSURE" in text
        )

    def test_says_nothing_about_a_cap_when_the_budget_governs(self):
        """With room to size properly, there is no reduction to report."""
        limits = _limits(
            FOREX={
                "max_position_notional_pct": 300,
                "max_leverage": 3,
                "max_exposure_per_asset_pct": 300,
            }
        )
        text = _text(worked_example(limits, equity=EQUITY))

        assert "The engine approves" in text
        assert "The size was cut" not in text
        # The full budget is reachable, so the loss is the budget.
        assert "you lose about USD 500.00" in text

    def test_refuses_without_inventing_a_size(self, limits):
        """A refusal quotes the engine's own checks, not a guess at why."""
        text = _text(worked_example(limits, equity=D("0")))

        assert "The engine REFUSES this trade" in text
        assert "approves" not in text
        assert "POSITION_SIZEABLE" in text

    def test_a_tiny_account_does_not_divide_by_zero(self, limits):
        for equity in (D("0"), D("1"), D("100")):
            explain(limits, equity=equity)
            worked_example(limits, equity=equity)


class TestTheCrossoverClaim:
    """`explain` tells the owner the stop distance at which the cap takes
    over from the risk budget. That is a claim about the engine, so it is
    tested against the engine."""

    @staticmethod
    def _verdict(engine, context, quote, stop_distance: Decimal):
        entry = quote.ask
        return engine.evaluate(
            OrderRequest(
                symbol="AAPL", market=Market.STOCKS, side=Side.BUY,
                order_type=OrderType.MARKET,
                protective_stop=entry - stop_distance,
                targets=(entry + stop_distance * D("3"),),
                strategy="crossover",
            ),
            context,
        )

    def test_a_wider_stop_is_sized_by_the_budget(self, engine, context, quote, limits):
        # Crossover for STOCKS: 500 / 25,000 = 2% of a 200 price = 4.00.
        verdict = self._verdict(engine, context, quote, D("8.00"))

        assert verdict.action is RiskAction.ALLOW
        assert verdict.binding_limits == (), "no cap should bind at a 4% stop"
        assert verdict.approved_risk == verdict.sizing.projected_risk

    def test_a_tighter_stop_is_sized_by_the_cap(self, engine, context, quote, limits):
        verdict = self._verdict(engine, context, quote, D("2.00"))

        assert verdict.allowed
        assert verdict.binding_limits, "the face-value cap should bind at a 1% stop"
        assert verdict.approved_risk < verdict.sizing.projected_risk
        assert verdict.approved_risk <= EQUITY * limits.max_risk_per_trade

    def test_the_printed_crossover_matches_the_arithmetic(self, limits):
        text = _text(explain(limits, equity=EQUITY))
        assert "STOCKS   stops inside 2.00% of the entry price" in text


class TestTheExampleWarning:
    def test_shipped_numbers_are_flagged_as_not_yet_adopted(self):
        limits = parse_limits(LIMITS_RAW, is_example=True)
        assert "THESE ARE THE SHIPPED EXAMPLE NUMBERS" in _text(
            explain(limits, equity=EQUITY)
        )

    def test_adopted_numbers_are_not(self, limits):
        assert "SHIPPED EXAMPLE" not in _text(explain(limits, equity=EQUITY))


class TestTheMoneyLines:
    def test_each_headline_limit_appears_in_money(self, limits):
        text = _text(explain(limits, equity=EQUITY))
        assert "USD 500.00" in text          # 0.5% per trade
        assert "USD 2,000.00" in text        # 2% daily
        assert "USD 5,000.00" in text        # 5% weekly
        assert "USD 10,000.00" in text       # 10% drawdown
        assert "about 4 full losses" in text

    def test_every_market_gets_a_cap_line(self, limits):
        text = _text(explain(limits, equity=EQUITY))
        for market in Market:
            assert str(market) in text
