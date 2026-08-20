# Recorded markets

Real candles, captured from the platform's own socket. These exist because
everything the tool claims about its own accuracy has to be measured against
*something*, and until this folder had anything in it that something was a
generated random walk.

That is a harsher limit than it sounds. **A random walk is unpredictable by
construction.** Past the drift laid over it there is nothing in it to find, so
a score that ranks setups perfectly and a score that ranks them by coin toss
produce the same number on it. "Does the score separate winners from losers"
cannot be answered on generated data in either direction, and answering it
there anyway would be worse than not asking.

## What is here

### `2026-08-19-pocketoption/`

A thirty-minute capture, EUR/USD OTC and AUD/CAD OTC, taken 22:26–22:56 UTC
with a 1 MIN EUR/USD chart open on the platform.

| chart | candles | span |
|---|---|---|
| `EUR-USD-OTC-60s.csv` | 193 | 18:19 → 22:56 (4.6h) |
| `EUR-USD-OTC-5s.csv` | 354 | 22:26 → 22:56 |
| `EUR-USD-OTC-10s.csv` | 177 | 22:26 → 22:56 |
| `EUR-USD-OTC-15s.csv` | 119 | 22:26 → 22:56 |
| `EUR-USD-OTC-30s.csv` | 60 | 22:26 → 22:56 |
| `AUD-CAD-OTC-5s.csv` | 354 | 22:26 → 22:56 |
| `AUD-CAD-OTC-10s.csv` | 177 | 22:26 → 22:56 |
| `AUD-CAD-OTC-15s.csv` | 119 | 22:26 → 22:56 |
| `AUD-CAD-OTC-30s.csv` | 60 | 22:26 → 22:56 |

The sub-minute charts are built from live ticks, so they cover the recording
and no more. The 1 MIN chart reaches back 4.6 hours because the platform sends
its own candle history for whichever chart is open — which is why only EUR/USD
has one, and why leaving the chart on the timeframe you trade matters.

**This capture is also the reason the history arrives at all.** The offline
replay used to read each frame on its own, and the platform announces its
larger messages in a header frame before the payload. Every announced message
therefore replayed as an anonymous payload matching no handler —
`loadHistoryPeriodFast` among them. A hundred and fifty M1 candles were being
sent and dropped, and the symptom looked like the platform never sending them.
`tests/test_recording.py` guards it now; this folder is the data that showed
it.

### `2026-08-20-falling/`

A second thirty-minute capture, 16:40–17:10 UTC, taken specifically because
everything above happened while price rose — and a rising market flatters any
tool that leans towards BUY. Here **AUD/CAD OTC fell 0.46%** over the window.

| chart | candles | span | direction |
|---|---|---|---|
| `EUR-USD-60s.csv` | 350 | 10:03 → 17:10 (7.1h) | −0.08% |
| `AUD-CAD-OTC-5s.csv` | 359 | 16:40 → 17:10 | **−0.48%** |
| `AUD-CAD-OTC-10s.csv` | 180 | 16:40 → 17:10 | −0.46% |
| `AUD-CAD-OTC-15s.csv` | 120 | 16:40 → 17:10 | −0.46% |
| `AUD-CAD-OTC-30s.csv` | 61 | 16:40 → 17:10 | −0.46% |
| `EUR-USD-5s.csv` | 359 | 16:40 → 17:10 | +0.01% |
| `EUR-USD-10s.csv` | 180 | 16:40 → 17:10 | +0.01% |
| `EUR-USD-15s.csv` | 120 | 16:40 → 17:10 | +0.01% |
| `EUR-USD-30s.csv` | 61 | 16:40 → 17:10 | +0.01% |

This one also cost a wasted half hour before it worked. The first attempt
captured 15,800 frames and produced nothing: the page announces which chart it
is showing when it *loads*, the chart had been open for hours, and the
recorder — unlike the live source — never asked it to reload. Prices arrive
for the whole market regardless, so the run looked busy the entire time and
built no candles at all.

## The two together

Pooled across both regimes, at the chart-and-expiry pairings actually traded
(5s → 30s, 1m → 3min, 1m → 5min):

```
rising market     33/52   63.5%   CI [49.9 .. 75.2]   said CALL 80%
falling market    27/50   54.0%   CI [40.4 .. 67.0]   said CALL 18%
POOLED           60/102   58.8%   CI [49.1 .. 67.9]   said CALL 50%
                                  break-even 52.1%

AUC 62.2%   CI [51.3 .. 73.0]     50% = the score says nothing
```

Two things follow, and only two.

**The engine is not stuck on BUY.** It called BUY 80% of the time when price
rose and 18% when it fell. The single-regime capture made that look like a
permanent bias; it is not.

**The score carries some information.** With the direction split near even
across the pool, the drift that confounded the first reading largely cancels,
and the AUC interval clears 50 — the first evidence here that the number on
the panel is doing anything at all. It clears it by 1.3 points, on 102 calls,
so it is the beginning of an answer rather than one.

The win rate still straddles break-even. **This does not show a profitable
tool.**

## The open hypothesis

Filtering the same pool by score:

| threshold | calls | win rate | 95% interval |
|---|---|---|---|
| 75+ (the current gate) | 100 | 59.0% | 49.2 .. 68.1 |
| 80+ | 86 | 61.6% | 51.1 .. 71.2 |
| **85+** | **53** | **73.6%** | **60.4 .. 83.6** |
| 90+ | 25 | 68.0% | 48.4 .. 82.8 |

85 clears break-even and 75 does not. But 85 was chosen by looking at this
table, which is how a threshold gets fitted to the noise in the sample that
produced it — and 90 falling back is what that looks like. Nothing has been
changed on the strength of it. The test is whether it holds on a recording
that had no part in choosing it.

## What is not here

Only `timestamp,open,high,low,close`. The recording also produced a protocol
summary containing account balances and settled trades — that stays out, and
so do the raw frames. Nothing in this folder identifies anybody.

## Using it

```bash
python tools/backtest.py --csv data/recorded/2026-08-19-pocketoption/EUR-USD-OTC-60s.csv \
                         --timeframe 60 --duration 180
```

## What it said, on the day it was added

Every chart above, every expiry from 60s to 300s, walked forward with a strict
prefix and entry at the next bar's open — **87 settled calls**:

```
Win rate   50/87 = 57.5%    95% CI [47.0% .. 67.3%]    break-even 52.1% at 92%
AUC        59.4%            95% CI [47.4% .. 71.3%]    50% = no information
```

Both intervals contain the null. **This does not show an edge**, and it is
recorded here so that nobody — including whoever writes the next change —
mistakes the absence of a measurement for a good one.

One caveat that only a real market produces: CALLs won 70.5% and PUTs 44.2%,
and price rose in 55 of the 87 windows. That gap is the market drifting up
over half an hour, not the score being right. A single window on two OTC pairs
cannot separate the two, which is the argument for more captures rather than
deeper analysis of this one.
