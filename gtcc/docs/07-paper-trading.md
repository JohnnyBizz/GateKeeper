# Paper trading

The default mode, and the thing that decides whether anything measured
here means anything. A paper engine that fills at the mid, instantly, in
full, at any size produces an equity curve no broker will reproduce.

## Modelled

| Friction | How |
|---|---|
| Crossing the spread | A market buy lifts the ask. Never the mid, never the bid |
| Slippage | Base basis points plus an impact term that grows with size against displayed depth |
| Depth | With an order book, the order walks levels and the average price degrades |
| Partial fills | Size beyond the participation limit does not fill |
| Commission | The instrument's own maker and taker schedule |
| Latency | Fills are stamped after the configured delay, so a strategy cannot act on its own fill before it would have heard |
| Market hours | An equity order outside its session rests rather than filling |
| Limit protection | A limit order never fills worse than its limit. The fill is put on the tick grid **first**, in the venue's favour, and clamped to the limit **after**. The reverse order let a buy limit of 100.00 round up to 100.01 |
| Tick direction | A buy pays up to the next tick, a sell receives down to the previous one. Rounding to nearest would hand the strategy a fraction of a tick no venue would give |

## Not modelled, and documented rather than faked

- **Queue position** for resting limit orders. A marketable limit fills;
  a resting one is not simulated as joining a queue.
- **Hidden liquidity** and iceberg orders.
- **Auction mechanics** at the open and close.
- **Borrow availability** for shorts.
- **Margin.** This is the big one. Equity is
  `starting cash + realised P&L - fees + unrealised`, so a futures or
  perpetual position consumes no buying power. That makes the paper
  broker useful for measuring strategy behaviour and useless for
  measuring margin calls. Guessing at a venue's margin formula would be
  worse than saying so.

## The ledger

Every fill moves the ledger: weighted average entry when adding,
realised P&L on the closing portion when reducing, a fresh basis at the
fill price when flipping through flat. Fees accumulate separately so they
can be reported against gross.

A round trip must lose money at zero price movement, because the spread
is real. There is a test asserting exactly that.

## Marks

A position with no mark price reports `unrealised_pnl() is None`. It does
not report zero. The dashboard shows "no mark" and `exposure_from_positions`
reports exposure as unknown, which the risk engine turns into a refusal:
section 42 forbids trading when account state cannot be reliably
determined.

## A paper position can close

The simulated venue triggers its own protective exits. Without that a
paper trade with a stop at 95 stays open while price goes to 50: the loss
shows as unrealised forever, no loss breaker ever trips because nothing is
realised, and the journal row that planned the trade never records what
happened. The simulation would be systematically kinder than reality in
the one direction that matters, and the kindness would be invisible —
an open position looks like a position that has not finished yet.

### The fill is never better than the price observed

If a long's stop was 59,000 and the mark is 50,000, the only price this
simulation has actually seen is 50,000. Filling at the stop level would
claim a fill at a price that was never observed, which is precisely how a
simulator hides gap risk.

This is not a rounding detail. When the rule was inverted as a test, the
difference was large enough that a loss which **should** have latched the
daily breaker no longer did — the flattering fill price hid a
breaker-worthy loss. Pessimism applies to targets too: a gap past the
target pays the target, not the better mark.

### Settling happens before new risk is evaluated

`submit()` settles protective exits first, and that ordering is a safety
requirement rather than housekeeping. An unsettled stop means today's
realised loss is understated, so a loss breaker that should already have
latched has not, and the next order would be approved against a tally
missing the loss that should have stopped it. There is a test for exactly
that sequence.

`POST /api/positions/settle` exists for an operator who is not placing
anything and wants the account brought up to date. It reports what it
closed **and** whether the settlement latched execution, because a caller
reading only the list of exits would not know trading had stopped.

### What a close writes to the journal

The outcome lands on the row that planned it — realised P&L, R multiple,
exit price, exit reason and the closing time — matched on the most recent
TAKEN row for that symbol and strategy with no outcome yet.

A close with no matching row is logged at ERROR and **no row is created**.
Inventing one would produce a journal entry with no setup behind it, which
reads as a trade nobody planned; a loud gap is better than a quiet
fabrication.

### A real venue is not simulated here

At a live broker the stop sits at the venue (OANDA's `stopLossOnFill`),
and the close arrives as a fill through reconciliation. `settle_protective_exits`
returns nothing for a broker that does not offer the simulation, which is
the correct answer rather than a missing feature.
