"""Decimal arithmetic helpers.

Prices, quantities and money are :class:`~decimal.Decimal` everywhere in
this package. Binary floats lose cents on long sums and round lot sizes
to values an exchange will reject, and a trading ledger that does not
foot is worse than no ledger.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")


def D(value: Any) -> Decimal:
    """Coerce to Decimal without ever going through binary float.

    Floats are converted via ``repr`` so ``D(0.1)`` is ``0.1`` and not
    ``0.1000000000000000055511151231257827``.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:  # pragma: no cover
        raise ValueError(f"cannot read {value!r} as a decimal") from exc


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    """Round *value* down to a multiple of *step*.

    Sizing always rounds down: taking slightly less risk than the limit
    allows is safe, taking slightly more is a breach.
    """
    if step <= ZERO:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_price(value: Decimal, tick_size: Decimal) -> Decimal:
    """Round *value* to the nearest valid price increment."""
    if tick_size <= ZERO:
        raise ValueError("tick_size must be positive")
    return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size


def bps(value: Decimal, basis_points: Decimal) -> Decimal:
    """*basis_points* of *value*. 25 bps of 100 is 0.25."""
    return value * basis_points / Decimal("10000")
