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
