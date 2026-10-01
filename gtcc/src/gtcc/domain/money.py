"""Decimal arithmetic, and the rounding rules for prices and sizes.

Prices, quantities and money are :class:`~decimal.Decimal` everywhere in
this package. Binary floats lose cents on long sums and round lot sizes
to values an exchange will reject, and a trading ledger that does not
foot is worse than no ledger.

Two classes of hazard are handled here rather than at every call site.

**Non-finite values.** ``Decimal("NaN")`` and ``Decimal("Infinity")``
are perfectly valid Decimals and each is dangerous in its own way. NaN
propagates through arithmetic and then raises ``InvalidOperation`` at an
unpredictable comparison far from its source. Infinity does not raise at
all: it compares and multiplies cleanly, so it passes bounds checks and
produces an infinite size. :func:`D` refuses both at the boundary.

**Rounding direction.** Rounding a price to the venue's tick grid moves
it, and which way it moves decides whether the order can execute worse
than the trader asked for. The direction is therefore never implicit:
callers use :func:`floor_to_tick`, :func:`ceil_to_tick` or the
order-aware helpers in :mod:`gtcc.domain.pricing`, and
:func:`round_to_tick` exists only for display.
"""

from __future__ import annotations

from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Decimal,
    InvalidOperation,
)
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")


def D(value: Any) -> Decimal:
    """Coerce to Decimal, refusing anything that is not a finite number.

    Floats are converted via ``repr`` so ``D(0.1)`` is ``0.1`` and not
    ``0.1000000000000000055511151231257827``.

    NaN and the infinities are rejected, for three distinct reasons:

    * ``Decimal("NaN")`` propagates silently through arithmetic and then
      raises ``InvalidOperation`` at whichever ordering comparison
      happens to touch it first. The crash lands far from the bad datum,
      typically mid-submission.
    * ``Decimal("Infinity")`` does *not* raise. It compares and
      multiplies cleanly, so an infinite equity or price flows through
      every bounds check and produces an infinite position size with no
      error at all.
    * ``float("nan")``, which can arrive through this function, really
      does compare False both ways, so a float NaN silently passes an
      upper-bound check.

    All three are bugs upstream, and the useful place to find out is at
    the boundary rather than three layers in.
    """
    if isinstance(value, bool):
        # bool is an int subclass; True becoming Decimal("1") is never
        # what a caller meant by a price or a quantity.
        raise ValueError("a boolean is not a monetary value")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, float):
        result = Decimal(repr(value))
    else:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"cannot read {value!r} as a decimal") from exc

    if not result.is_finite():
        raise ValueError(
            f"{value!r} is not a finite number. NaN raises at an arbitrary "
            "later comparison; Infinity raises nowhere and passes every "
            "bounds check instead."
        )
    return result


# -- size rounding -------------------------------------------------------


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    """Round a non-negative quantity down to a multiple of *step*.

    Sizing always rounds down: taking slightly less risk than the limit
    allows is safe, taking slightly more is a breach.

    Negative input is refused rather than handled. Python's
    ``ROUND_DOWN`` rounds toward zero, so on a negative number it would
    round *up* in the mathematical sense and silently return a larger
    position than asked for. Short positions are represented by a signed
    quantity on the position, not by a negative size here; if that ever
    changes, use :func:`quantize_floor` and say so at the call site.
    """
    value = D(value)
    step = D(step)
    if step <= ZERO:
        raise ValueError("step must be positive")
    if value < ZERO:
        raise ValueError(
            f"quantize_down is for non-negative sizes and was given {value}. "
            "Decimal's ROUND_DOWN rounds toward zero, so on a negative it "
            "would return a larger magnitude than requested. Use "
            "quantize_floor if a true floor is intended."
        )
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def quantize_floor(value: Decimal, step: Decimal) -> Decimal:
    """Mathematical floor onto the *step* grid, negatives included."""
    value, step = D(value), D(step)
    if step <= ZERO:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


# -- price rounding ------------------------------------------------------


def floor_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    """Largest valid price at or below *value*."""
    value, tick_size = D(value), D(tick_size)
    if tick_size <= ZERO:
        raise ValueError("tick_size must be positive")
    return (value / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size


def ceil_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    """Smallest valid price at or above *value*."""
    value, tick_size = D(value), D(tick_size)
    if tick_size <= ZERO:
        raise ValueError("tick_size must be positive")
    return (value / tick_size).to_integral_value(rounding=ROUND_CEILING) * tick_size


def round_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    """Nearest valid price. **For display and for fills, not for limits.**

    A limit price rounded to nearest can move against the trader: a buy
    limit of 100.005 on a 0.01 tick becomes 100.01, which pays a cent
    more than they said they would. Use the order-aware helpers in
    :mod:`gtcc.domain.pricing` for anything that goes on an order.
    """
    value, tick_size = D(value), D(tick_size)
    if tick_size <= ZERO:
        raise ValueError("tick_size must be positive")
    return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size


def bps(value: Decimal, basis_points: Decimal) -> Decimal:
    """*basis_points* of *value*. 25 bps of 100 is 0.25."""
    return D(value) * D(basis_points) / Decimal("10000")
