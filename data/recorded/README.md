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

### `2026-08-20-eurusd/`

A second thirty-minute capture, 16:40–17:10 UTC, EUR/USD open on 1 MIN.

**Read the drift column carefully — this folder was first named
`2026-08-20-falling` and that was wrong.** AUD/CAD fell half a percent in it
and EUR/USD, the pair actually being traded, was flat. The socket carries the
whole market and GateKeeper builds a chart for every instrument on it, so a
capture taken while trading one pair also contains several others; nothing in
the files said which was which, and an accuracy figure was published from a
pool that included a pair the user had never traded. `WHAT-IS-IN-HERE.txt`
now says which is which, in every recording.

| chart | candles | span | direction | traded? |
|---|---|---|---|---|
| `EUR-USD-60s.csv` | 350 | 10:03 → 17:10 (7.1h) | −0.08% | **yes** |
| `AUD-CAD-OTC-5s.csv` | 359 | 16:40 → 17:10 | −0.48% | no |
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

Measured on **EUR/USD only** — the pair actually being traded — at the
chart-and-expiry pairings actually used (5s → 30s, 1m → 3min, 1m → 5min),
at the old gate of 75:

```
recording 1   31 calls   won 54%   said CALL 67%   price rose 74% of windows
recording 2   35 calls   won 54%   said CALL 22%   price rose 22% of windows
POOLED        66 calls   54.5%     CI [42.6 .. 66.0]     break-even 52.1%
```

Two things follow, and only two.

**The engine is not stuck on BUY.** It called BUY on two thirds of its entries
in the first capture and on a fifth in the second, tracking which way the
windows it chose actually went. A single capture made that look like a
permanent bias; it is not.

**The win rate straddles break-even.** 54.5% with an interval from 42.6 to
66.0 is consistent with a profitable tool and equally consistent with a losing
one. **Nothing here shows an edge.**

### The pair that was not traded

AUD/CAD OTC streamed past in both captures and won 66% over 36 calls — better
than EUR/USD did. That is worth exactly nothing as evidence about the tool:
it is one instrument over two half hours, and it is the pair whose inclusion
turned a pooled 54.5% into a pooled 58.8% in an earlier version of this file.
Pooling the watchlist with the traded chart flatters or damns a result
according to which pairs happened to be streaming, so the headline number is
the traded pair alone.

## The open hypothesis

Filtering the traded pair by score:

| threshold | calls | win rate | 95% interval | |
|---|---|---|---|---|
| 75+ (the old gate) | 66 | 54.5% | 42.6 .. 66.0 | straddles break-even |
| 80+ | 54 | 57.4% | 44.2 .. 69.7 | straddles |
| **85+ (current)** | **29** | **75.9%** | **57.9 .. 87.8** | **clears it** |
| 90+ | 10 | 70.0% | 39.7 .. 89.2 | straddles |

85 clears break-even and 75 does not, and it survived being recomputed on the
traded pair alone after the first version of this table had AUD/CAD mixed into
it. That is one check passed, not a result.

It remains a threshold chosen by looking at the table that scored it — 90
falling back to 70% on ten calls is what fitting to noise looks like — and it
is now the shipped default, so every future recording is a test of it. If it
does not hold on captures that had no part in choosing it, it goes back.

## What is not here

Only `timestamp,open,high,low,close`. The recording also produced a protocol
summary containing account balances and settled trades — that stays out, and
so do the raw frames. Nothing in this folder identifies anybody.

## Using it

```bash
python tools/backtest.py --csv data/recorded/2026-08-19-pocketoption/EUR-USD-OTC-60s.csv \
                         --timeframe 60 --duration 180
```

## A note on reading these numbers

Every figure in this file is from two half-hour windows on one pair. The
intervals are wide because the samples are small, and they are printed beside
every number for exactly that reason: 54.5% and 75.9% both sound like
findings, and neither is one yet.

The failure this file exists to prevent has now happened twice in different
forms — measuring against a random walk that could not answer the question,
and measuring against a watchlist pair nobody was trading. Both produced
confident numbers. Both were wrong. More captures, on the pair being traded,
is the only thing that fixes it.
