# Trade ideas

`gtcc ideas` and the **Trade Ideas** page scan symbols for five named
setups and put every pattern they find on trial. An idea is shown only when
the evidence for it is substantial, the evidence against it is small, and
nothing disqualifies it — and every idea shown carries its case against in
full.

Nothing in this package can place an order. It is handed a market-data
adapter, a track-record lookup and a dry-run risk check; CI asserts it
never references a broker or the submission path. Taking a trade is a
decision a person makes on the chart.

## Why it does not read TradingView

TradingView's policy licenses its market data for display to a person and
prohibits machine-driven use of it. Reading charts off its site would be a
terms violation and worse data than the numbers a broker serves directly.
So the analysis runs on the broker's own feed — OANDA's, once its token is
configured — and every idea carries a link that opens the same instrument
on TradingView (`OANDA:EURUSD` for OANDA's `EUR_USD`) for the person to
check. Charting the same venue matters: two brokers' EUR/USD differ in
their spreads and candle boundaries, so a level that is clean on one may
not exist on the other. For a venue this package does not know, the link
falls back to the bare symbol and the page says TradingView may be showing
another exchange's prices.

## The five setups

Each is a written rule with its numbers, an invalidation point derived from
structure rather than a fixed distance, and a target at the next level
where price is likely to stall.

| Setup | Style | The rule |
|---|---|---|
| Trend pullback | continuation | EMA20 above a rising EMA50; price wicked back to the 20 within the last two bars without breaking the 50; closed back above the 20 |
| Range breakout | continuation | first close more than 0.1 ATR beyond a 20-bar range no more than 4 ATR tall; target a measured move the height of the range |
| Liquidity sweep | reversal | a wick through a clean swing level (never closed through since it formed) that closed back inside on one of the last two bars |
| Range edge rejection | reversal | a bar closing back up off the floor (or down off the ceiling) of a 30-bar range 2–6 ATR tall; target the middle |
| Failed breakout | reversal | a close beyond a 20-bar range that was back inside within three bars and still is |

**Targets are the next opposing level, not a multiple of the risk.** When
the next level is too close, the reward to risk fails and the idea is
dropped. That costs ideas and it is correct: a trade whose obvious target
is half a risk unit away is not improved by drawing the target further out.
Where no level exists in the visible history, the target is an assumed 2R
and the idea says so.

Stops and targets are put on the instrument's tick grid, rounded toward
entry — the stop can only get tighter and the target only closer, so
rounding never flatters an idea.

The pullback's freshness rule was tuned once and then left alone. Its first
version fired on a third of all bars in a test trend — every up-bar after a
dip — and the second never fired at all. The third fires at the turn.
Tuning further until synthetic data looked good would be exactly the
curve-fitting the rest of this platform is built to catch.

## Evidence is counted by family, not by item

RSI, MACD and stochastic are different formulas over the same closes; when
all three agree that is one observation reported three times. So evidence
is grouped into families and the threshold counts families. Three
momentum readings in agreement are one reason.

Two further rules stop one fact being counted twice:

- **A trigger cannot vouch for itself.** The pullback fires *because* the
  averages are aligned, so that alignment is not reported again as trend
  evidence. On the first end-to-end run, before this rule existed, an idea
  showed "6 independent reasons for"; with it, the honest count was 3.
- **There is no separate regime family.** The regime classifier reads
  mostly off ADX and the directional lines — the numbers trend strength
  already uses — so regime fit is filed under trend strength, where it
  counts once.

Even so, families are not statistically independent: trend, momentum,
structure and location are all functions of one price series and tend to
agree for the same underlying reason. Grouping reduces the double-counting;
it cannot remove it. The one family different in kind is the **track
record**, which is why ideas with a measured edge rank first.

### Directional and practical

*Directional* families say whether price is likely to move the trade's
way: the setup trigger, trend on this timeframe, the higher timeframe,
momentum, trend strength and regime, participation, location, and the
track record. The threshold counts these: **at least four in favour, at
most one against.**

*Practical* families — reward to risk, costs, stop distance against
normal noise, scheduled news, your risk limits — say whether the trade is
worth taking if the move happens. A good reward to risk is not evidence
that price will rise, so these never count toward the threshold. They can
only warn or disqualify.

### What disqualifies an idea outright

- net reward to risk below your configured minimum, after the spread
- a spread above 20% of the stop distance
- the risk engine refusing it in a dry run — the same engine, limits and
  sizing a real order would face
- a measured track record of losing on held-out data

### What is always listed as not checked

An unchecked item is listed rather than omitted, because an omitted check
reads as a passed one:

- **scheduled news** — no economic calendar is connected, and every idea
  says so;
- **the track record**, when none has been measured;
- **volume**, where a venue reports none. For forex, a broker's volume is a
  tick count — a proxy for activity, not traded size — and the evidence
  says so.

## The track record

`gtcc track-record` replays every setup over every recording through the
real backtester and risk engine, on the first 60% of the data and on the
held-out final 20%, and stores the result.

| Verdict | Means |
|---|---|
| MEASURED_EDGE | ≥30 in-sample and ≥10 held-out trades, positive average R in both |
| NO_EDGE | ≥10 held-out trades averaging zero R or worse — disqualifying |
| INSUFFICIENT | measured, but too few trades; treated as unknown, never as a pass |

What is measured is the setup's trigger alone, without the evidence filter;
the filter can only remove trades, so this is the base rate it works from.
The filter itself is unmeasured. Each re-measurement increments a count of
looks at the held-out data, which travels with the record — after the
first look the data is no longer truly held out.

Measuring a setup never promotes it. `SetupStrategy` is always UNTESTED,
and CI fails the build if the track-record module references any higher
validation status.

### Costs decide short-timeframe results

This turned up while writing the tests, and it is the most important
practical finding in this document. On a series with an ATR like EUR/USD
on 15 minutes, the backtester's generic default costs (2.5 basis points per
side, about 2.7 pips each way) leave **no** tight-stop pullback able to
clear a 1.5 reward to risk. At a low-cost broker's figures, the same chart
produces trades. Same rules, same prices: the cost assumption alone flips
the answer. A test pins this.

So `gtcc track-record` uses the venue's **live** spread by default, labelled
as today's spread rather than history's; assumes 0.5 bps of slippage and
no commission unless told otherwise; and every record states the costs it
assumed. Without a live quote it falls back to a generic default and says
so on every line.
