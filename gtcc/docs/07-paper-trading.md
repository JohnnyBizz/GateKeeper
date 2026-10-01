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
| Limit protection | A limit order never fills worse than its limit, whatever the slippage model says |

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
