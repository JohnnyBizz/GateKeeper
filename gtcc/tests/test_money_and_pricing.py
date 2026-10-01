"""Decimal safety and price normalisation.

Two classes of defect, both of which are silent when they happen.

A non-finite Decimal is dangerous in a different way for each form,
and the tests below pin down which. Decimal NaN raises at an arbitrary
later comparison rather than where it entered. Decimal Infinity raises
nowhere at all and passes every bounds check. Float NaN really does
compare False both ways and so silently satisfies an upper bound.

A price rounded to the nearest tick moves in whichever direction is
closer, which for a buy limit can be upward, so the fill pays more than
the trader authorised. One cent on one order; the point is that nothing
in the system would have reported it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from gtcc.domain.enums import AssetClass, Market, OrderStatus, OrderType, Side
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, Quote, utcnow
from gtcc.domain.money import (
    D,
    ceil_to_tick,
    floor_to_tick,
    quantize_down,
    quantize_floor,
    round_to_tick,
)
from gtcc.domain.orders import Fill, Order, OrderRequest
from gtcc.domain.pricing import (
    is_on_tick,
    normalise_entry_limit,
    normalise_stop,
    normalise_stop_trigger,
    normalise_target,
    respects_limit,
)
from gtcc.execution.paper_engine import FillModel, PaperFillEngine

TICK = Decimal("0.01")


class TestNonFiniteValuesAreRefused:
    """Regressions 1 to 4."""

    @pytest.mark.parametrize(
        "value",
        [
            "NaN", "nan", "-NaN", "sNaN", "Infinity", "-Infinity", "inf", "-inf",
            Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"),
            float("inf"), float("-inf"), float("nan"),
        ],
    )
    def test_D_refuses_every_non_finite_form(self, value):
        with pytest.raises(ValueError, match="finite"):
            D(value)

    def test_decimal_nan_raises_at_an_arbitrary_later_comparison(self):
        """Pins the real behaviour, which is not what one might assume.

        Decimal NaN does not quietly compare False the way float NaN
        does: it raises, but only when something orders it, which may be
        several layers from the feed that produced it.
        """
        from decimal import InvalidOperation

        nan = Decimal("NaN")

        assert (nan * 2).is_nan(), "arithmetic propagates it silently"
        assert (nan == Decimal("1000")) is False, "equality is silent"
        with pytest.raises(InvalidOperation):
            nan > Decimal("1000")

    def test_decimal_infinity_passes_every_bounds_check(self):
        """The genuinely silent one. Infinity raises nowhere."""
        infinity = Decimal("Infinity")

        assert infinity > Decimal("1000")
        assert (infinity * 2) == infinity
        # An infinite equity would make every percentage limit infinite.
        assert infinity * Decimal("0.005") == infinity

    def test_float_nan_is_the_one_that_compares_false(self):
        not_a_number = float("nan")

        assert not (not_a_number > 1000)
        assert not (not_a_number < 1000)

    def test_a_boolean_is_not_a_monetary_value(self):
        with pytest.raises(ValueError, match="boolean"):
            D(True)

    def test_finite_values_still_convert(self):
        assert D("1.5") == Decimal("1.5")
        assert D(0.1) == Decimal("0.1")
        assert D(7) == Decimal("7")


class TestDomainObjectsRejectNonFinite:
    """The boundary matters as much as the helper: a value that never
    passes through D() would otherwise arrive intact."""

    def test_a_quote_cannot_carry_a_non_finite_price(self):
        with pytest.raises(ValueError, match="finite"):
            Quote(symbol="X", timestamp=utcnow(), bid=Decimal("NaN"), ask=Decimal("100"))

    def test_a_bar_cannot_carry_a_non_finite_price(self):
        from gtcc.domain.enums import Timeframe

        with pytest.raises(ValueError, match="finite"):
            Bar(
                symbol="X", timeframe=Timeframe.M5, timestamp=utcnow(),
                open=Decimal("1"), high=Decimal("Infinity"),
                low=Decimal("1"), close=Decimal("1"),
            )

    def test_an_order_cannot_carry_a_non_finite_quantity(self):
        with pytest.raises(ValueError, match="finite"):
            Order(
                symbol="X", market=Market.CRYPTO, side=Side.BUY,
                order_type=OrderType.MARKET, quantity=Decimal("NaN"),
            )

    def test_a_request_cannot_carry_a_non_finite_stop(self):
        with pytest.raises(ValueError, match="finite"):
            OrderRequest(
                symbol="X", market=Market.CRYPTO, side=Side.BUY,
                order_type=OrderType.MARKET, protective_stop=Decimal("Infinity"),
            )

    def test_a_fill_cannot_carry_a_non_finite_price(self):
        with pytest.raises(ValueError, match="finite"):
            Fill(
                order_id="o", symbol="X", side=Side.BUY, quantity=Decimal("1"),
                price=Decimal("NaN"), fee=Decimal("0"), timestamp=utcnow(),
            )

    def test_a_fill_of_zero_is_not_an_execution(self):
        with pytest.raises(ValueError, match="not an execution"):
            Fill(
                order_id="o", symbol="X", side=Side.BUY, quantity=Decimal("0"),
                price=Decimal("1"), fee=Decimal("0"), timestamp=utcnow(),
            )


class TestQuantizeDownIsForSizesOnly:
    """Regression 5.

    Python's ROUND_DOWN rounds toward zero. On a negative number that is
    a ceiling, not a floor, so a function named "down" would have
    returned a larger position than asked for.
    """

    def test_a_negative_quantity_is_refused(self):
        with pytest.raises(ValueError, match="non-negative"):
            quantize_down(Decimal("-5.5"), Decimal("1"))

    def test_the_refusal_names_the_alternative(self):
        with pytest.raises(ValueError, match="quantize_floor"):
            quantize_down(Decimal("-0.1"), Decimal("1"))

    def test_positive_quantities_round_down_as_before(self):
        assert quantize_down(Decimal("7.9"), Decimal("1")) == Decimal("7")
        assert quantize_down(Decimal("1.99999"), Decimal("0.0001")) == Decimal("1.9999")

    def test_quantize_floor_handles_negatives_correctly(self):
        """A true floor, for anywhere negatives are genuinely meant."""
        assert quantize_floor(Decimal("-5.5"), Decimal("1")) == Decimal("-6")
        assert quantize_floor(Decimal("5.5"), Decimal("1")) == Decimal("5")

    def test_a_zero_or_negative_step_is_refused(self):
        with pytest.raises(ValueError, match="positive"):
            quantize_down(Decimal("1"), Decimal("0"))


class TestTickRoundingDirection:
    """Regressions 6 and 7, plus the stop and target semantics."""

    def test_nearest_rounding_would_raise_a_buy_limit(self):
        """The defect, demonstrated before the fix is applied."""
        assert round_to_tick(Decimal("100.005"), TICK) == Decimal("100.01")

    def test_a_buy_limit_never_rises(self):
        for raw in ("100.005", "100.009", "100.001", "100.0000001"):
            normalised = normalise_entry_limit(Decimal(raw), TICK, Side.BUY)
            assert normalised <= Decimal(raw), raw
            assert is_on_tick(normalised, TICK)

    def test_a_sell_limit_never_falls(self):
        for raw in ("100.005", "100.001", "100.009"):
            normalised = normalise_entry_limit(Decimal(raw), TICK, Side.SELL)
            assert normalised >= Decimal(raw), raw
            assert is_on_tick(normalised, TICK)

    def test_a_price_already_on_the_grid_does_not_move(self):
        for side in (Side.BUY, Side.SELL):
            assert normalise_entry_limit(Decimal("100.01"), TICK, side) == Decimal("100.01")

    def test_exactly_halfway_still_respects_the_side(self):
        """The case nearest-rounding gets wrong by construction."""
        halfway = Decimal("100.005")

        assert normalise_entry_limit(halfway, TICK, Side.BUY) == Decimal("100.00")
        assert normalise_entry_limit(halfway, TICK, Side.SELL) == Decimal("100.01")

    def test_a_long_stop_moves_toward_entry_so_risk_cannot_grow(self):
        entry = Decimal("100")
        normalised = normalise_stop(Decimal("98.005"), TICK, entry=entry)

        assert normalised == Decimal("98.01")
        assert abs(entry - normalised) <= abs(entry - Decimal("98.005"))

    def test_a_short_stop_moves_toward_entry_so_risk_cannot_grow(self):
        entry = Decimal("100")
        normalised = normalise_stop(Decimal("102.005"), TICK, entry=entry)

        assert normalised == Decimal("102.00")
        assert abs(entry - normalised) <= abs(entry - Decimal("102.005"))

    def test_a_long_target_moves_toward_entry_so_reward_is_not_flattered(self):
        entry = Decimal("100")
        normalised = normalise_target(Decimal("104.005"), TICK, entry=entry)

        assert normalised == Decimal("104.00")
        assert abs(normalised - entry) <= abs(Decimal("104.005") - entry)

    def test_a_short_target_moves_toward_entry(self):
        entry = Decimal("100")
        normalised = normalise_target(Decimal("95.995"), TICK, entry=entry)

        assert normalised == Decimal("96.00")
        assert abs(normalised - entry) <= abs(Decimal("95.995") - entry)

    def test_a_stop_at_the_entry_price_is_refused(self):
        with pytest.raises(ValueError, match="no definable risk"):
            normalise_stop(Decimal("100"), TICK, entry=Decimal("100"))

    def test_a_stop_entry_trigger_never_fires_earlier_than_asked(self):
        """A buy-stop sits above the market: rounding it down would
        trigger before the level the trader named."""
        assert normalise_stop_trigger(Decimal("100.001"), TICK, Side.BUY) == Decimal("100.01")
        assert normalise_stop_trigger(Decimal("99.999"), TICK, Side.SELL) == Decimal("99.99")

    def test_floor_and_ceil_bracket_the_value(self):
        for raw in ("100.004", "100.005", "100.006", "0.001", "99999.999"):
            value = Decimal(raw)
            assert floor_to_tick(value, TICK) <= value <= ceil_to_tick(value, TICK)

    def test_respects_limit_is_the_post_condition(self):
        assert respects_limit(Decimal("99.99"), Decimal("100"), Side.BUY)
        assert not respects_limit(Decimal("100.01"), Decimal("100"), Side.BUY)
        assert respects_limit(Decimal("100.01"), Decimal("100"), Side.SELL)
        assert not respects_limit(Decimal("99.99"), Decimal("100"), Side.SELL)


class TestPaperFillsRespectTheLimit:
    """The rounding fix mattered because of this path: the engine used
    to clamp to the limit and round afterwards, so the rounding could
    push the fill back past the limit it had just been clamped to."""

    @pytest.fixture
    def spec(self) -> InstrumentSpec:
        return InstrumentSpec(
            symbol="X", market=Market.STOCKS, asset_class=AssetClass.EQUITY,
            quote_currency="USD", tick_size=TICK, lot_step=Decimal("1"),
            min_qty=Decimal("1"),
        )

    def _order(self, side: Side, limit: str) -> Order:
        return Order(
            symbol="X", market=Market.STOCKS, side=side, order_type=OrderType.LIMIT,
            quantity=Decimal("10"), status=OrderStatus.ACCEPTED, limit_price=Decimal(limit),
        )

    def test_a_buy_limit_fill_never_exceeds_the_limit(self, spec, now):
        quote = Quote(
            symbol="X", timestamp=now, bid=Decimal("99.99"), ask=Decimal("100.00"),
            bid_size=Decimal("1000"), ask_size=Decimal("1000"), received_at=now,
        )
        engine = PaperFillEngine(FillModel(base_slippage_bps=Decimal("50")))

        outcome = engine.execute(self._order(Side.BUY, "100.00"), spec, quote, now=now)

        assert outcome.fills
        assert outcome.fills[0].price <= Decimal("100.00")

    def test_a_sell_limit_fill_never_falls_below_the_limit(self, spec, now):
        quote = Quote(
            symbol="X", timestamp=now, bid=Decimal("100.00"), ask=Decimal("100.01"),
            bid_size=Decimal("1000"), ask_size=Decimal("1000"), received_at=now,
        )
        engine = PaperFillEngine(FillModel(base_slippage_bps=Decimal("50")))

        outcome = engine.execute(self._order(Side.SELL, "100.00"), spec, quote, now=now)

        assert outcome.fills
        assert outcome.fills[0].price >= Decimal("100.00")

    def test_every_fill_lands_on_the_tick_grid(self, spec, now):
        quote = Quote(
            symbol="X", timestamp=now, bid=Decimal("99.99"), ask=Decimal("100.01"),
            bid_size=Decimal("1000"), ask_size=Decimal("1000"), received_at=now,
        )
        order = Order(
            symbol="X", market=Market.STOCKS, side=Side.BUY,
            order_type=OrderType.MARKET, quantity=Decimal("10"),
            status=OrderStatus.ACCEPTED,
        )

        outcome = PaperFillEngine().execute(order, spec, quote, now=now)

        assert is_on_tick(outcome.fills[0].price, TICK)

    def test_a_market_fill_rounds_against_the_trader(self, spec, now):
        """A paper fill must not be handed a fraction of a tick that a
        real venue would keep."""
        quote = Quote(
            symbol="X", timestamp=now, bid=Decimal("99.99"), ask=Decimal("100.001"),
            bid_size=Decimal("1000"), ask_size=Decimal("1000"), received_at=now,
        )
        order = Order(
            symbol="X", market=Market.STOCKS, side=Side.BUY,
            order_type=OrderType.MARKET, quantity=Decimal("10"),
            status=OrderStatus.ACCEPTED,
        )

        outcome = PaperFillEngine(FillModel(base_slippage_bps=Decimal("0"))).execute(
            order, spec, quote, now=now
        )

        assert outcome.fills[0].price == Decimal("100.01")
