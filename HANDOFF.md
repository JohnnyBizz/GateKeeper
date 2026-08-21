# Handoff

Written to survive a lost conversation. The chat this project was built in hit
the context limit and could not be compacted, so the transcript is gone — but
nothing in the transcript was load-bearing. Everything that matters is in the
commits, in `FINDINGS.md`, and in this file.

Read `README.md` for what the tool is and how to run it, `FINDINGS.md` for what
has actually been measured, and this file for where the work stopped and what
it was about to do next.

---

## 1. State as of the last commit

| | |
|---|---|
| Branch | `main` at `a955c9a`, pushed, clean tree |
| Commits | 50 |
| Tests | **1134 passing, 0 failing** (verified, 3m11s) |
| Python | 3.11 |

There is no unfinished edit in the working tree and nothing stashed. The last
commit — "Keep the history the platform sends for the tabs behind the front
one" — is complete and tested.

### Setting up a fresh checkout

Two steps, and the second one is the one that gets forgotten:

```bash
pip install -r requirements.txt
apt-get install -y tesseract-ocr     # macOS: brew install tesseract
```

**Without the Tesseract binary, 14 tests fail** — all of them in
`test_chart_detection.py` and `test_overlay.py`, all of them label-reading. It
looks like broken code and is not. `pytesseract` from `requirements.txt` is
only the Python wrapper; the OCR engine itself is a system package. Install it
before concluding anything is wrong.

The suite takes a little over three minutes:

```bash
python -m pytest
```

---

## 2. What the project is, in one paragraph

A local, read-only technical-analysis assistant that watches a trading chart —
via screen capture or by reading the platform's own WebSocket traffic — scores
the setup 0–100 across ten components, and shows CALL / PUT / WAIT with its
reasoning on an always-on-top overlay panel. It places no trades, touches no
account, and asks for no credentials. Most conditions should read WAIT.

## 3. Where things live

```
poa/feed/          the platform WebSocket: protocol, ticks, frames, recording
poa/chart_detection/  screen capture path: find the chart, OCR the labels,
                      extract candles, calibrate the price axis
poa/analysis/      Heikin Ashi, structure, levels, momentum, regime, volatility
poa/signals/       the engine proper: scoring, gates, duration, narrative,
                   tracker, autotune
poa/backtesting/   calibration, paper trading, attribution, gate checks, stats
poa/overlay/       the Tk panel — app.py, panel.py, viewmodel.py, proof.py
poa/storage/       journal.db (SQLite): every call with its component breakdown
poa/reporting/     end-of-session report
tools/             backtest, components, diagnose_screenshot, record_feed,
                   select_region, make_sample_data
data/recorded/     four committed real-market recordings (2026-08-19, 08-20)
```

Entry points: `python overlay.py` (panel), `python run.py` (browser dashboard
on :8765). `config.yaml` is written on first run; everything in it is editable
from the ⚙ settings dialog.

---

## 4. The open question — pick this up first

**Does any component of the score predict anything, measured across sessions
rather than within one?**

Everything needed to answer it already exists. The journal has always stored
each signal's full component breakdown, so every call the tool has ever made is
sitting in `storage/journal.db` with all ten scores against a real outcome:

```bash
python tools/components.py --live
```

Calls are grouped into sessions by a thirty-minute gap. A component counts only
if it holds the same direction in **every** group independently. The tool
refuses to report on a single session outright, by design.

**About 465 calls per group** are needed before a 55% edge clears fifty. The app
produces roughly ninety an hour with several pairs watched, so a few hours of
ordinary use settles it. As of the last commit there were four sessions of data
— not yet enough. The next session should run the app, accumulate calls, and
then run the tool. Either something real shows up in every group, or the
scoring approach is finished and that should be said plainly.

---

## 5. The hypothesis on the record, deliberately unacted on

One live session (2026-08-21, 28 minutes, six OTC pairs) came back with the
score running **backwards**: 32.7% settled, AUC 0.303, beaten by sixteen points
by always buying and never changing your mind. Cluster-bootstrapping over whole
episodes made it worse, not better — 0.59% of 20,000 resamples put the AUC at
or above 0.5.

There is a plausible mechanism. Seventy of the score's hundred points are
trend-continuation (trend 20, structure 15, Heikin Ashi 15, momentum 10, EMA
alignment 10); only RSI (5) and support/resistance (10) lean against an
extended move. So a 92+ score may not mean "good entry" so much as "this move
has already travelled" — which on a 5 SEC chart at 30-second expiry is six bars
and a plausible worst moment to enter.

**It did not replicate.** Re-run on the four committed recordings at the same
settings, it pooled to AUC 0.484 with an interval straddling 0.5, and per
recording ran 0.21 / 0.43 / 0.67 / 0.29 — no consistency, no component sitting
on one side across all four.

So nothing was inverted. One group is not a finding, however large the effect
inside it. **Do not reweight the engine on the strength of that session.** If a
second live session repeats it, that changes; the point of §4 is to find out.

---

## 6. The rule this project runs on

From `FINDINGS.md`, learned the expensive way:

> **A finding counts once it repeats in a group it was not found in.** A group
> is a session or a recording — not a currency pair. Six pairs inside one
> twenty-eight-minute window are one market sampled six ways.

This has already been violated twice and caught twice — once on a threshold
fitted to twenty-nine calls that did not survive the next batch, once on
`ema_alignment` reading 41.6% pooled (an obvious "component wired backwards"
fix) that split into 35.9% and 48.1% and evaporated. Both times the honest move
was to change nothing and write down why.

`FINDINGS.md` exists because the failure mode here is remembering the exciting
half of a result and forgetting the half that refused to replicate. Keep adding
to it, including the measurements that come back against the tool.

---

## 7. Dead ends — do not re-litigate these

* **Tuning the confidence threshold was never the problem.** Two days went into
  it while the real cause was that setups found on watched charts were
  announced and then never journalled — so ten consecutive sessions read
  "0 calls" while the tool was working fine.
* **The shown gate is 62, chosen for rate, not accuracy.** Measured: dropping
  85 → 62 roughly doubles how often the tool speaks and does *not* reliably
  improve the win rate (66.7% in one recording, 54.8% in another at the same
  setting). The comment above the setting says so. 85 was itself fitted to
  noise. Alerts are now tied to the same gate, with a test asserting they agree.
* **Latency work is done.** Bar-close detection samples four times a bar with a
  one-second floor; chart re-read 2.0s → 0.5s, scan hold 2.4s → 0.6s, silence
  after a signal 60s → 15s, after an alert 120s → 20s. The engine loop never
  schedules the next read sooner than the last one took, so an expensive screen
  source paces itself instead of being throttled by a number picked for the
  worst case.

## 8. Recently fixed — context for reading the diff

The last several commits were mostly measurement defects, all fixed:

1. **One setup was journalled as many calls** — every material update filed a
   fresh row, so 57 rows covered about 25 real moves and every rate computed
   over them counted one answer several times. `TrackedChange.opens_a_call` now
   separates "a call started" from "something changed".
2. **Hand trades could not find the call they were taken on** — the timestamp
   match demanded `0.0 <=` with no tolerance, so a local clock two seconds fast
   disqualified all 21 placed trades in a session. Skew is allowed for and the
   nearest call wins.
3. **Hand trades were filed at settlement time, not open time.**
4. **Calls were voided by another chart's price** — ten of sixteen came back
   VOID because the engine settled pending rows against whatever chart was open.
5. **History for every tab but the front one was thrown away** — which is why a
   watchlist filled in minutes on 5 SEC charts and never at all on 1 MIN.
6. **`NO TRADE` overflowed the panel** — the verdict label now measures itself
   rather than hard-coding a smaller face per string.

> Numbers from before 2026-08-21 are inflated by defect 1 and are **not
> comparable** with anything measured after it.

---

## 9. Elsewhere

An earlier version of this assistant lives in `JohnnyBizz/AlphaEdge` as the two
commits on **PR #8** ("Add a Pocket Option technical-analysis assistant", "Add
an always-on-top overlay panel with a Scan cycle", both 2026-08-13). The work
moved here and continued; that PR is superseded. The branch has been deleted
but GitHub keeps the commits permanently — `git fetch origin
refs/pull/8/head:pr8` in that repo retrieves them if they are ever wanted.

CI: `.github/workflows/build.yml` runs the suite and publishes `GateKeeper.exe`
and `RecordFeed.exe` to the `latest` release on every push to `main`, so an
executable only ever comes from a build where every test passed.
