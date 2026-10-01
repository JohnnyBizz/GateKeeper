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

## Phase 2 additions

Feature engine (EMA, SMA, VWAP, anchored VWAP, RSI, MACD, ATR, ADX,
Bollinger, stochastic, ROC, OBV, relative volume, historical
volatility), market-structure engine (HH/HL/LH/LL, BOS, CHoCH, ranges,
sweeps, fair value gaps — each storing the exact rule that identified
it, per section 6), and the regime classifier.

Section 6's caution is a design constraint, not a disclaimer: these
concepts are partly subjective, so each detector records its parameters
alongside its output and nothing presents a swing label as a
mathematical certainty.
