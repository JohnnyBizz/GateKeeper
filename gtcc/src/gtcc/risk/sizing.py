"""Position sizing — specification section 17.

Risk capital is equity times the configured risk fraction. The size that
risks exactly that much depends on the instrument, so the arithmetic
lives on :class:`~gtcc.domain.instruments.InstrumentSpec` and this module
only handles the money, the rounding and the honesty about what rounding
did to the intended risk.

Rounding is always downward. After rounding the *projected* risk is
recomputed from the actual quantity, because a position rounded up onto
a lot grid can exceed the limit the sizing was supposed to respect.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from gtcc.domain.instruments import InstrumentError, InstrumentSpec
from gtcc.domain.money import ONE, ZERO, D


@dataclass(frozen=True, slots=True)
class SizingResult:
    """What size the risk budget buys, and what it actually risks."""

    quantity: Decimal
    risk_capital: Decimal
    risk_per_unit: Decimal
    projected_risk: Decimal
    notional: Decimal
    leverage: Decimal
    entry_fee_estimate: Decimal
    exit_fee_estimate: Decimal
    rejected_reason: str | None = None

    @property
    def is_tradeable(self) -> bool:
        return self.rejected_reason is None and self.quantity > ZERO

    @property
    def total_fee_estimate(self) -> Decimal:
        return self.entry_fee_estimate + self.exit_fee_estimate


def size_position(
    *,
    instrument: InstrumentSpec,
    equity: Decimal,
    risk_fraction: Decimal,
    entry: Decimal,
    stop: Decimal,
    quote_to_account_rate: Decimal = ONE,
    max_quantity: Decimal | None = None,
) -> SizingResult:
    """Largest position whose stop-out loss stays inside the risk budget.

    *max_quantity* caps the result before rounding — used when another
    limit (notional, leverage, exposure) binds tighter than risk.
    """
    equity = D(equity)
    risk_capital = equity * D(risk_fraction)

    if equity <= ZERO:
        return _rejected("account equity is zero or negative", risk_capital)
    if risk_fraction <= ZERO:
        return _rejected("configured risk per trade is zero", risk_capital)

    try:
        risk_per_unit = instrument.risk_per_unit(
            entry, stop, quote_to_account_rate=quote_to_account_rate
        )
    except InstrumentError as exc:
        return _rejected(str(exc), risk_capital)

    if risk_per_unit <= ZERO:
        return _rejected("risk per unit computed as zero", risk_capital)

    raw_quantity = risk_capital / risk_per_unit
    if max_quantity is not None:
        raw_quantity = min(raw_quantity, D(max_quantity))

    quantity = instrument.round_quantity(raw_quantity)

    if quantity <= ZERO:
        return _rejected(
            f"risk budget of {risk_capital} buys less than one lot of "
            f"{instrument.lot_step} {instrument.symbol}",
            risk_capital,
        )
    if quantity < instrument.min_qty:
        return _rejected(
            f"sized {quantity} but the venue minimum is {instrument.min_qty}",
            risk_capital,
        )

    notional = instrument.notional(
        D(entry), quantity, quote_to_account_rate=quote_to_account_rate
    )
    if notional < instrument.min_notional:
        return _rejected(
            f"notional {notional} is under the venue minimum {instrument.min_notional}",
            risk_capital,
        )

    projected_risk = quantity * risk_per_unit
    leverage = notional / equity if equity > ZERO else ZERO

    return SizingResult(
        quantity=quantity,
        risk_capital=risk_capital,
        risk_per_unit=risk_per_unit,
        projected_risk=projected_risk,
        notional=notional,
        leverage=leverage,
        entry_fee_estimate=instrument.fee(notional),
        exit_fee_estimate=instrument.fee(notional),
    )


def _rejected(reason: str, risk_capital: Decimal) -> SizingResult:
    return SizingResult(
        quantity=ZERO,
        risk_capital=risk_capital,
        risk_per_unit=ZERO,
        projected_risk=ZERO,
        notional=ZERO,
        leverage=ZERO,
        entry_fee_estimate=ZERO,
        exit_fee_estimate=ZERO,
        rejected_reason=reason,
    )


def quantity_for_notional(
    *,
    instrument: InstrumentSpec,
    price: Decimal,
    notional_cap: Decimal,
    quote_to_account_rate: Decimal = ONE,
) -> Decimal:
    """Largest rounded quantity whose notional stays under *notional_cap*."""
    unit_notional = instrument.notional(
        D(price), ONE, quote_to_account_rate=quote_to_account_rate
    )
    if unit_notional <= ZERO:
        return ZERO
    return instrument.round_quantity(D(notional_cap) / unit_notional)
