# Market-data pipeline

```
venue -> adapter -> validation -> (features) -> (strategies) -> risk -> OMS
                        |
                        +-> DataQualityReport travels with the analysis
```

## Validation gate

`data/quality.py`. Every analysis carries a status and `INVALID` means no
trade, enforced by the `DATA_QUALITY` check in the risk engine.

Quote checks: non-positive prices, crossed book, staleness against
`max_quote_age_seconds`, clock skew (a timestamp from the future means
someone's clock is wrong and every age calculation downstream is
meaningless), spread width.

Bar checks: candle coherence (`low <= open,close <= high`), duplicate
timestamps, out-of-order timestamps, missing candles, staleness measured
in multiples of the bar's own timeframe, range outliers against the
median, and an `UNCLOSED_BAR` note so a strategy does not read a forming
candle's close.

Severity maps to status: any `FATAL` makes the report `INVALID`, any
`WARNING` makes it `DEGRADED`, otherwise `GOOD`.

## Nothing is repaired

A gap is reported, never interpolated. A candle we invented is
indistinguishable from one that happened, and a backtest would never
know the difference. There is a test that asserts the bar list is not
mutated by validation.

## Timeframes

1m, 3m, 5m, 15m, 30m, 1h, 4h, 1d, 1w. Section 4 says not to require
every timeframe blindly: higher frames give context, lower frames give
execution, and each strategy declares what it needs. The intended
default cascade is 4h dominant structure, 1h trend, 15m confirmation, 5m
entry.

## Look-ahead prevention

Three mechanisms, because this is the bias that silently invents an edge:

1. `Bar.closed` is False while a candle forms.
2. `ReplayAdapter.cursor` hides every bar after the replay position.
3. The Phase 4 backtester will be event-driven, so a strategy is called
   with a bar only once that bar has closed.

## The feature engine

`gtcc/features/indicators.py`. Every function takes closed bars and
returns a series aligned to them, with `None` through the warm-up
rather than zero: an RSI of 0 means a collapse, and using it for "no
value yet" would make the start of every series look like one.

Unclosed bars are refused outright. A strategy reading the close of a
forming candle is using information that did not exist, which is the
cheapest way to invent an edge that evaporates in production.

ADX ships alongside `directional_movement`, which exposes +DI and -DI.
ADX is deliberately direction-agnostic, and keeping the directional
lines next to it makes that harder to forget.

## Market structure

`gtcc/structure/engine.py`. Section 6's caution is a design constraint,
not a disclaimer: these concepts are partly subjective, so every
`Detection` carries the rule in plain language and the exact parameters
that produced it, and is marked OBJECTIVE or INTERPRETED. A fair value
gap is arithmetic; a liquidity sweep is a judgement about a threshold
somebody chose, and the threshold travels with the result.

The causal walks register a swing at the bar where it becomes
**knowable**, not at the bar where it occurred. A pivot needs
`swing_lookback` bars after it before anyone could confirm it. The
first version registered swings at their own index, which let a
breakout bar's own swing raise the ceiling it was breaking and let a
break of structure cite a level that was not yet established. Both
produced entirely plausible output, which is why there is now a test
asserting that analysing a prefix agrees with analysing the whole
series.

## Regime

`gtcc/features/regime.py` reports one primary regime and any number of
qualifiers, because trend and volatility are different axes and a
market can be trending and volatile at once. An imminent high-impact
release overrides the technical reading. Every reading carries its
evidence and its thresholds, so a journal entry can be re-read later
against different numbers.

## Still to come in this phase

A live data adapter, the scanner, the Next.js terminal, order-flow
analysis where a real feed exists, and writing the structure and regime
context onto journal rows.
