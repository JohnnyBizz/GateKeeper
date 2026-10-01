# Backtesting

The specification asks for an event-driven backtester with a test proving
it cannot see the future. That proof is the only reason to trust anything
else in this document.

## Why look-ahead is the whole problem

A look-ahead bug does not raise an exception. It produces a better equity
curve, which is the most dangerous failure mode available: the symptom of
the bug is the thing you were hoping for. Nobody investigates a good
result as hard as a crash.

Three rules, each with a test, and each verified by reintroducing the bug
and confirming the matching test fails.

### A decision at a bar's close fills no earlier than the next bar

The strategy is handed `bars[:index + 1]` — a slice, so there is nothing
to read ahead *from*. If it wants in, the fill is at `bars[index + 1]`'s
open. Filling at the decision bar's close uses a price knowable only at
the instant of the decision, which live trading will not give you.

A decision on the final bar is therefore not taken at all, rather than
filled against a bar that does not exist.

The general property has its own test: running on a prefix of the series
produces the same trades as running on the whole series. If a later bar
can change an earlier decision, something reads ahead.

### When a bar touches both the stop and the target, the stop wins

OHLCV does not record the order of ticks inside a bar. Either assumption
is a guess, and only one of them can be wrong in the direction that
flatters the result — and the flattery grows with the strategy's
reward:risk, so the more ambitious the target the bigger the lie.

### Costs are assumptions, declared and recorded

A bar close is not a quote. The spread is not derivable from OHLCV, and
the replay adapter refuses to invent one. `BarCosts` is where the caller
states the spread, slippage and commission being assumed; the costs are
applied against the trade in both directions, never for it, and
`BacktestResult` carries them so no statistic can be read without them.

The risk engine refuses an order it cannot price, correctly — sizing
against a stale close is how a position opens at a price that no longer
exists. So the backtester builds a quote from the *declared* spread,
which is the same number the cost model charges. The alternative was
relaxing the check in backtest mode, and that is the wrong one: a
backtest whose risk engine is not the live risk engine measures a system
that does not exist. CI greps the package for the obvious ways that could
creep back in.

## Refusals are kept

A setup the strategy wanted and the risk engine refused is recorded in
`BacktestResult.skipped` with its reasons, for the same reason the live
journal keeps refusals: a backtest that discards them cannot answer
whether the limits cost money or saved it.

## Statistics carry their own reliability

A Sharpe ratio from nine trades is noise with a decimal point, and the
reader cannot tell that from the number. So `Metrics.trustworthy` is False
below 30 closed trades — a convention, stated as a convention rather than
a finding — and `sample_warning` says so in words. `describe_with_warning`
prints the caveat *before* the statistics, because a reader who meets a
profit factor first has already formed an impression.

Anything uncomputable is None rather than zero. A profit factor with no
losing trades is undefined, not infinite; a win rate with no trades is
absent, not 0%.

## Splits are chronological and never shuffled

Shuffling a time series leaks the future through autocorrelation:
tomorrow's bar lands in-sample while today's is held out. There is no
option to randomise, because the option would eventually be used, and CI
asserts the package calls nothing from `random`.

`split()` partitions in time order with out-of-sample as the *remainder*,
so the segments cannot silently fail to cover everything.
`walk_forward()` yields rolling windows whose test set always follows its
training set. An anchored variant would be a different function rather
than a flag, so a result cannot be ambiguous about which was run.

`OutOfSampleLedger` counts how many times a strategy has been measured
against its held-out data. Nothing can stop a second look; the point is
that the count travels with the result, so a strategy promoted on its
fourth look cannot present that as a first look.

## What this does not do

**It does not promote a strategy.** `ValidationStatus` is still a
deliberate edit by a person. An automatic promotion on a good backtest
would be the single most dangerous feature in this codebase: it would let
a curve-fit become live-eligible without anybody looking at it. The
backtester's job is to produce the evidence; deciding what the evidence is
worth stays with the owner.

**It does not trade a portfolio.** One position at a time, stated in
`BacktestSettings` rather than implied. Concurrent positions interact
through the exposure limits, and simulating that honestly needs portfolio
accounting that does not exist yet.

**It does not model intrabar execution, gaps against a stop, or
liquidity.** A stop is assumed to fill at the stop price. In a real gap it
fills worse, so results here are optimistic in exactly that respect and
nowhere else — which is worth knowing when a strategy's edge turns out to
live in its stops.

## Attacking a result before believing it

A backtest produces a number, and the number's job is to feel like
evidence — which it does whether or not it is. The strategies that lose
money live are overwhelmingly the ones whose backtest nobody attacked. So
`robustness.assess()` attacks it five ways, each answering a specific way
a result lies.

**Out-of-sample degradation.** Positive in-sample and negative on held-out
data means the result describes the data it was built on. This is the
clearest signal there is, and the reason the splits exist. Some decay is
normal and is not flagged; turning negative is. A fall of 70% or more is
reported but not called disqualifying, because a weakened edge may still
be real and calling it fatal would be a judgement this module is not
entitled to make.

**One lucky trade.** Remove the single best trade. If expectancy turns
negative, the strategy has one outlier and a crowd of noise around it. An
already-losing strategy is not flagged for this, because it has a
different problem and naming this one would mislead.

**One lucky period.** Split the trades in half by time. All the profit in
one half describes a market condition that has ended.

**Costs.** Re-run at rising cost assumptions and report at what level the
edge dies. An edge that dies within twice the assumed cost is flagged, but
**the headroom multiple is always reported whether or not it is flagged**:
the threshold is a judgement and the ratio is a fact the reader can judge
for themselves. Surviving every level tested is reported as "the range
tested, not a claim about higher costs".

**Parameters.** Nudge each parameter and re-run. An edge that exists at
exactly 14 and vanishes at 13 and 15 belongs to the number, not to the
market. One bad neighbour out of four is tolerated; half or more is a
finding.

### A clean report is not an endorsement

This is the property most worth protecting, and CI protects it. A report
with no findings says:

> No finding from the checks that were run. This is NOT evidence the
> strategy works — it means these particular ways of being wrong were not
> detected.

The words "robust", "validated" and "proven" do not appear in the module,
and CI greps for them. "Passed robustness checks" would be read as "this
works", and it does not mean that.

Checks that were not run are visible by their absence from `checks_run`,
and a result rendered with no robustness report at all says so explicitly
— because figures with nothing attacking them are the most favourable
reading available, and that should be stated rather than assumed.
