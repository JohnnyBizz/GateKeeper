# Findings

What has actually been measured, including the measurements that came back
against the tool. Kept as a file rather than a memory because the failure mode
this project keeps hitting is remembering the exciting half of a result and
forgetting the half that refused to replicate.

The rule, learned the expensive way: **a finding counts once it repeats in a
group it was not found in.** A group is a session or a recording — not a
currency pair. Six pairs inside one twenty-eight-minute window are one market
sampled six ways, and they move together far more than the count suggests.

---

## 2026-08-21 — the score ran backwards for one session, and only that session

**Session:** 01:42–02:11 UTC, 28 minutes, six OTC pairs (AUD/CAD, CAD/CHF,
CHF/NOK, EUR/NZD, UAH/USD, USD/MXN), 5 SEC charts, 30 SEC expiry.

### What the session said

| | |
|---|---|
| Settled | 18W / 37L |
| Win rate | 32.7%, Wilson 95% CI [21.8%, 45.9%] |
| Break-even at 92% payout | 52.1% |
| Always BUY would have scored | 49.1% |
| Always SELL would have scored | 50.9% |
| AUC(score → win) | 0.303 |

The tool finished **16 points below** the better of the two do-nothing
baselines. Not "failed to beat break-even" — beaten by never changing your
mind.

The clustering check was the important one, because the 57 rows were not 57
independent reads (see the defect below). Grouping them into 25 contiguous
same-pair, same-direction episodes and bootstrapping over whole episodes:

- mean episode win-fraction **26.3%**, 95% CI [11.0%, 41.7%]
- AUC 0.303, cluster-bootstrap 95% CI [0.183, 0.444]
- **0.59%** of 20,000 bootstrap resamples put the AUC at or above 0.5
- score ≥ 92 went **4W/25L**; score < 92 went **14W/12L**; the gap is −40
  points, CI [−62, −16], and ≥92 came out the better band in **0.04%** of
  resamples

Clustering did not rescue it. It made it worse.

### The mechanism that would explain it

70 of the score's 100 points are trend-continuation: trend 20, structure 15,
Heikin Ashi 15, momentum 10, EMA alignment 10. The only components that lean
against an extended move are RSI (5 points) and support/resistance (10).

So a 92+ score does not mean "this is a good entry". It means *every*
continuation measure agrees at once, which is the definition of a move that has
already travelled. On a 5-second chart with a 30-second expiry — six bars — that
is a plausible worst moment to enter, and the tape agrees: EUR/NZD was called
PUT six times as it drifted down and lost all six, because each individual
30-second window retraced. USD/MXN won its first seven CALLs up the move and
lost every one from 17.642 onward, which was the top.

### Why it is not being acted on

**It did not replicate.** Re-run on the four committed 5 SEC real-market
recordings at the same 30 SEC expiry (`data/recorded/`):

| recording | settled | rate | AUC |
|---|---|---|---|
| 2026-08-19 AUD/CAD OTC | 23 | 78.3% | 0.211 |
| 2026-08-19 EUR/USD OTC | 20 | 65.0% | 0.429 |
| 2026-08-20 AUD/CAD OTC | 17 | 47.1% | 0.674 |
| 2026-08-20 EUR/USD | 16 | 56.2% | 0.286 |
| **pooled** | **76** | **63.2%** | **0.484, CI [0.348, 0.620]** |

Pooled AUC straddles 0.5. Per recording it is 0.21, 0.43, 0.67, 0.29 — three
below, one above, no consistency. No individual component sits on one side of
0.5 across all four either.

That is one live session saying the score is inverted and four recordings
saying it is uninformative. **One group is not a finding**, however large the
effect inside it, and inverting a scoring model on a single 28-minute sample is
the exact mistake the replication rule exists to prevent. The hypothesis is on
the record and unacted on until a second live session either repeats it or
kills it.

### Defects this session exposed, all now fixed

1. **One setup was journalled as many calls.** Every material update on a live
   setup — the expiry window re-arming, the score drifting ten points, the
   regime being relabelled — was written as a fresh call. A setup holding for
   two minutes at a 30-second expiry filed four rows at four prices a hundredth
   of a percent apart. Those settle together, so a win rate over them counts one
   answer several times over and dilutes the independent ones: 57 rows covering
   roughly 25 moves. `TrackedChange.opens_a_call` now separates "a call
   started" from "something changed", and the journal takes the first.

2. **Hand trades could not find the call they were taken on.** All 21 placed
   trades in the session reported no score. The match compared the broker's
   `openTimestamp` against this machine's clock and demanded `0.0 <=` — no
   tolerance — so a local clock running even a couple of seconds fast
   disqualified every one. Those are the trades that carry information the
   gates cannot get anywhere else, and none of them taught anything. Skew is
   now allowed for, and the nearest call wins rather than the latest.

3. **Hand trades were filed at settlement time, not open time.** The report
   promises the score "at the moment the trade opened" and printed the moment
   GateKeeper noticed it close — an expiry later, often in the following
   minute, lined up against nothing.

4. **History for every tab but the front one was thrown away.** The platform
   loads candles for each chart it draws and sends all of it across the socket
   once. Anything not belonging to the chart in focus was dropped, so a watched
   pair started at zero candles and grew one per bar off the live stream — 60
   bars before it could be read, which on a 1 MIN chart is an hour. Switching
   to that tab did not help: the browser already held the candles and asked for
   nothing, so the panel restarted the hour from two candles with a fully drawn
   chart on screen beside it. It is why a six-pair watchlist filled in minutes
   on 5 SEC charts and never filled at all on 1 MIN.

Numbers from before this date are inflated by defect 1 and are not comparable
with what follows.

### The question left open

Whether any component of the score predicts anything at all, measured across
sessions rather than within one. The journal already stores every call's
component breakdown, and now stores one row per call, so:

```
python tools/components.py --live
```

answers it once a few sessions have accumulated. Roughly 465 calls per group
are needed for a component's AUC to separate from noise.
