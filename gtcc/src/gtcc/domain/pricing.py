"""Order-aware price normalisation.

A price has to sit on the venue's tick grid, and moving it there moves
it in some direction. Which direction is a safety question, not a
formatting one, so every case is named and tested rather than left to a
single "round it" helper.

**Entry limits round in the trader's favour.** A buy limit may only move
down and a sell limit may only move up, so normalisation can never make
the order pay more or accept less than the trader asked for. The cost is
that the order may not fill; that is the right trade, because the
alternative is an execution at a price nobody authorised.

**Stops and targets round toward the entry.** Both are measured against
entry to produce risk and reward, and rounding toward entry is the
conservative direction for each:

* A stop rounded toward entry risks slightly *less* than budgeted.
  Rounded away, the position would risk more than the limit allowed,
  which is the breach the risk engine exists to prevent.
* A target rounded toward entry reports slightly *less* reward. Rounded
  away, it would flatter the reward-to-risk ratio and let a trade pass a
  minimum it does not really meet.

So a long stop ceils, a short stop floors, a long target floors and a
short target ceils. The tests spell out each one.
"""

from __future__ import annotations

from decimal import Decimal

from gtcc.domain.enums import Side
from gtcc.domain.money import D, ZERO, ceil_to_tick, floor_to_tick


def normalise_entry_limit(price: Decimal, tick_size: Decimal, side: Side) -> Decimal:
    """Put an entry limit on the grid without worsening it.

    BUY floors: the maximum the trader will pay never rises.
    SELL ceils: the minimum they will accept never falls.
    """
    return (
        floor_to_tick(price, tick_size)
        if side is Side.BUY
        else ceil_to_tick(price, tick_size)
    )


def normalise_stop(price: Decimal, tick_size: Decimal, *, entry: Decimal) -> Decimal:
    """Put a protective stop on the grid, moving it toward *entry*.

    A long's stop sits below entry and so rounds up; a short's sits
    above and rounds down. Either way the distance from entry does not
    grow, so the realised risk cannot exceed what was sized for.
    """
    price, entry = D(price), D(entry)
    if price == entry:
        raise ValueError("a stop at the entry price has no definable risk")
    return ceil_to_tick(price, tick_size) if price < entry else floor_to_tick(price, tick_size)


def normalise_target(price: Decimal, tick_size: Decimal, *, entry: Decimal) -> Decimal:
    """Put a target on the grid, moving it toward *entry*.

    A long's target sits above entry and so rounds down; a short's sits
    below and rounds up. The reported reward is never flattered by the
    rounding.
    """
    price, entry = D(price), D(entry)
    if price == entry:
        raise ValueError("a target at the entry price offers no reward")
    return floor_to_tick(price, tick_size) if price > entry else ceil_to_tick(price, tick_size)


def normalise_stop_trigger(price: Decimal, tick_size: Decimal, side: Side) -> Decimal:
    """Put a stop *entry* trigger on the grid.

    This is the trigger of a stop or stop-limit order used to enter, not
    a protective stop. A buy-stop sits above the market and rounds up, a
    sell-stop sits below and rounds down, so neither triggers earlier
    than the trader specified.
    """
    return (
        ceil_to_tick(price, tick_size)
        if side is Side.BUY
        else floor_to_tick(price, tick_size)
    )


def respects_limit(filled: Decimal, limit: Decimal, side: Side) -> bool:
    """Would this fill honour the limit? Used as a post-condition."""
    filled, limit = D(filled), D(limit)
    return filled <= limit if side is Side.BUY else filled >= limit


def is_on_tick(price: Decimal, tick_size: Decimal) -> bool:
    price, tick_size = D(price), D(tick_size)
    if tick_size <= ZERO:
        raise ValueError("tick_size must be positive")
    return (price / tick_size) % 1 == 0
