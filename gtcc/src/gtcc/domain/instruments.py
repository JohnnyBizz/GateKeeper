"""Instrument specifications and the maths that depends on them.

Specification section 17 is explicit: never apply share arithmetic to
forex, futures or leveraged crypto. The way that rule is kept here is
that no caller computes a position size itself — they ask an
:class:`InstrumentSpec`, and the spec dispatches on its asset class.

The three shapes of risk-per-unit:

* **Linear** (equity, ETF, crypto spot and perpetuals) — one unit gains
  or loses the price difference, times the contract size.
* **Tick-valued** (listed futures) — one contract gains or loses a fixed
  cash amount per tick, and the price difference is meaningless without
  it. An ES contract moving 1.00 is 50 dollars, not 1 dollar.
* **Quoted** (forex) — the price difference is in the quote currency and
  must be converted to the account currency before it means anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from gtcc.domain.enums import AssetClass, Market
from gtcc.domain.money import ONE, ZERO, D, quantize_down

_LINEAR = frozenset(
    {
        AssetClass.EQUITY,
        AssetClass.ETF,
        AssetClass.CRYPTO_SPOT,
        AssetClass.CRYPTO_PERP,
        AssetClass.CRYPTO_FUTURE,
    }
)


class InstrumentError(ValueError):
    """The instrument cannot support the requested calculation."""


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Everything needed to size, price and cost a position correctly.

    These values come from the venue, not from us. An adapter that cannot
    report them should not offer the symbol for trading.
    """

    symbol: str
    market: Market
    asset_class: AssetClass
    quote_currency: str
    base_currency: str = ""
    tick_size: Decimal = Decimal("0.01")
    lot_step: Decimal = Decimal("1")
    min_qty: Decimal = Decimal("1")
    max_qty: Decimal | None = None
    min_notional: Decimal = Decimal("0")
    #: Units of the base instrument in one tradeable unit. 1 for shares
    #: and crypto; 100_000 for a standard forex lot; 50 for ES.
    contract_size: Decimal = Decimal("1")
    #: Account-currency value of one tick, for one contract. Futures only.
    tick_value: Decimal | None = None
    #: 0.0001 for most pairs, 0.01 for JPY crosses. Forex only.
    pip_size: Decimal | None = None
    max_leverage: Decimal = Decimal("1")
    allows_fractional: bool = False
    maker_fee_bps: Decimal = Decimal("0")
    taker_fee_bps: Decimal = Decimal("0")
    #: Equities only: the venue's regular session, for the paper engine.
    session: str = "24x7"

    def __post_init__(self) -> None:
        if self.tick_size <= ZERO:
            raise InstrumentError(f"{self.symbol}: tick_size must be positive")
        if self.lot_step <= ZERO:
            raise InstrumentError(f"{self.symbol}: lot_step must be positive")
        if self.contract_size <= ZERO:
            raise InstrumentError(f"{self.symbol}: contract_size must be positive")
        if self.max_leverage <= ZERO:
            raise InstrumentError(f"{self.symbol}: max_leverage must be positive")
        if self.asset_class is AssetClass.FUTURE and not self.tick_value:
            raise InstrumentError(
                f"{self.symbol}: a futures contract needs a tick_value — "
                "a price difference alone does not say what a tick is worth"
            )
        if self.asset_class is AssetClass.FOREX_SPOT and not self.pip_size:
            raise InstrumentError(f"{self.symbol}: a forex pair needs a pip_size")

    # -- risk arithmetic ---------------------------------------------------

    def risk_per_unit(
        self,
        entry: Decimal,
        stop: Decimal,
        *,
        quote_to_account_rate: Decimal = ONE,
    ) -> Decimal:
        """Account-currency loss if one unit is stopped out.

        *quote_to_account_rate* converts the instrument's quote currency
        into the account's currency. It is 1 when they are the same, and
        callers must supply a real rate when they are not — there is no
        default guess here.
        """
        distance = abs(D(entry) - D(stop))
        if distance <= ZERO:
            raise InstrumentError(
                f"{self.symbol}: entry and stop are the same price, so the "
                "trade has no definable risk"
            )
        if quote_to_account_rate <= ZERO:
            raise InstrumentError(f"{self.symbol}: quote_to_account_rate must be positive")

        if self.asset_class is AssetClass.FUTURE:
            assert self.tick_value is not None  # guaranteed by __post_init__
            ticks = distance / self.tick_size
            return ticks * self.tick_value * quote_to_account_rate

        if self.asset_class is AssetClass.FOREX_SPOT:
            # The difference is in the quote currency, per unit of base.
            return distance * self.contract_size * quote_to_account_rate

        if self.asset_class in _LINEAR:
            return distance * self.contract_size * quote_to_account_rate

        raise InstrumentError(
            f"{self.symbol}: sizing for {self.asset_class} is not implemented. "
            "Options sizing needs the greeks and is deliberately out of scope."
        )

    def notional(
        self,
        price: Decimal,
        quantity: Decimal,
        *,
        quote_to_account_rate: Decimal = ONE,
    ) -> Decimal:
        """Account-currency face value of *quantity* units at *price*."""
        if self.asset_class is AssetClass.FUTURE:
            assert self.tick_value is not None
            # Point value is what one full price point is worth.
            point_value = self.tick_value / self.tick_size
            return abs(D(price) * D(quantity) * point_value * quote_to_account_rate)
        return abs(D(price) * D(quantity) * self.contract_size * quote_to_account_rate)

    def round_quantity(self, quantity: Decimal) -> Decimal:
        """Round *quantity* down onto the venue's lot grid."""
        rounded = quantize_down(abs(D(quantity)), self.lot_step)
        if self.max_qty is not None and rounded > self.max_qty:
            rounded = quantize_down(self.max_qty, self.lot_step)
        return rounded

    def is_tradeable_quantity(self, quantity: Decimal) -> bool:
        return D(quantity) >= self.min_qty > ZERO or (
            self.min_qty <= ZERO and D(quantity) > ZERO
        )

    def fee(self, notional: Decimal, *, maker: bool = False) -> Decimal:
        rate = self.maker_fee_bps if maker else self.taker_fee_bps
        return abs(D(notional)) * rate / Decimal("10000")
