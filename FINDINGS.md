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

---

## 2026-08-21 — 85 does not beat 62, and the reason is that neither changes its mind

Asked whether the shown gate should go back to 85. Measured rather than
argued, on all four committed 5 SEC recordings at 30 SEC expiry, 92% payout:

```
python tools/backtest.py --csv data/recorded/<recording>/<pair>-5s.csv \
    --timeframe 5 --duration 30 --window 120 --min-confidence <62|85> --payout 0.92
```

| gate | settled | win rate | 95% CI | EV per unit |
|---|---|---|---|---|
| 62 | 76 | 63.2% | [51.9, 73.1] | +0.213 |
| 85 | 33 | 69.7% | [52.7, 82.6] | +0.338 |

85 is 6.5 points ahead. Cluster-bootstrapped over whole recordings, 20,000
resamples, that gap is **[−9.8, +25.1]** and 85 comes out ahead in 79% of them.
That is not a difference; it is the same number measured twice with less data
the second time. Per recording it is 100.0 / 71.4 / 50.0 / 75.0 against 65.0 /
78.3 / 56.2 / 47.1 — neither ordering holds and neither gate replicates.

The gate comparison, then, is the third threshold question in a row to come
back empty. What came out of it instead was not about the gate.

### The tool does not change its mind inside a session

Direction counts per pair-recording:

| | gate 62 | gate 85 |
|---|---|---|
| 2026-08-19 EUR/USD OTC | 15 CALL / 6 PUT | **3 CALL / 0 PUT** |
| 2026-08-19 AUD/CAD OTC | 22 CALL / 1 PUT | **14 CALL / 0 PUT** |
| 2026-08-20 EUR/USD | 15 PUT / 1 CALL | **8 PUT / 0 CALL** |
| 2026-08-20 AUD/CAD OTC | 16 PUT / 1 CALL | 7 PUT / 1 CALL |

At 85, three of the four emit **one direction and nothing else**, and the
fourth is 7:1. At 62 it is 88.7% one-directional on average, with only the
first recording anywhere near a mix.

This explains a result that otherwise looked like a coincidence. Against the
do-nothing baselines on its own entries, gate 85 scored:

```
    19/EUR-USD   tool 100.0%   best baseline 100.0%    +0.0
    19/AUD-CAD   tool  71.4%   best baseline  71.4%    +0.0
    20/EUR-USD   tool  50.0%   best baseline  50.0%    +0.0
    20/AUD-CAD   tool  75.0%   best baseline  62.5%   +12.5
```

Three exact ties, and they are exact by construction: a tool that only ever
says CALL in a window *is* always-BUY over that window. The baseline comparison
printed in every session report cannot detect an edge that is not there to
detect, because at 85 the tool and the baseline are the same strategy.

Gate 62 at least dissents often enough to differ — +10.0, +4.4, −6.2, −11.7,
averaging −0.9. Still no edge, but a measurable one rather than a tautology.

### What this says about the score

The score is behaving as a trend detector, not a setup detector. Raising the
gate does not select better entries out of a mixed pool; it selects *harder
trending moments*, and in a trending window never changing your mind wins too.
That is the same mechanism written down against the live session above — 70 of
the 100 points are trend-continuation, so a high score is the definition of a
move that has already travelled — arriving this time from the recordings rather
than from one session.

It also explains why clustering hurt rather than helped there. Six PUTs on
EUR/NZD as it drifted down, seven CALLs on USD/MXN up the move: those were
never six and seven reads. They are one opinion, repeated.

By the replication rule this clears the bar, barely. The concentration holds in
both recordings independently and in all four pair-recordings, and the live
session is a third group showing the same behaviour. Two groups is the minimum
the rule allows and a third recording would settle it properly.

### What was decided

**The gate stays at 62.** Not because 62 is better — nothing here shows that —
but because 85 is indistinguishable from it on win rate, produces 43% of the
call volume, and concentrates every call into the band the live session above
measured going 4W/25L. Restoring 85 on a 6.5-point pooled gap from 33 settled
calls would be the same error it was dropped for, with the sign reversed.

The rate matters for a reason that has nothing to do with trading: the open
question needs about 465 calls per group, which is roughly five hours of
watching at 62 against twelve at 85. The gate is a knob on a device whose
function is still unknown, and 85 more than doubles the time to find out.

### The question this raises

Whether the score ever disagrees with the prevailing direction, and whether it
is any good when it does. The minority-direction calls are the only ones that
are not trend-following, and there are 9 of them among the 77 signals at gate 62 — far too
few to read, but they are the ones worth counting as sessions accumulate. If
the score has an edge that is not simply the trend, that is where it lives.
