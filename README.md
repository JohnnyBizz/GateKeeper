# GateKeeper

[![Build](https://github.com/JohnnyBizz/GateKeeper/actions/workflows/build.yml/badge.svg)](https://github.com/JohnnyBizz/GateKeeper/actions/workflows/build.yml)

A local, read-only technical-analysis assistant that watches a trading chart on
your screen, analyses market structure and price action continuously, and tells
you **CALL**, **PUT**, or **WAIT** — with its full reasoning.

It is decision support, not a prediction machine and not a bot.

> **What this tool will not do.** It does not place trades. It does not click
> buttons on any platform. It does not touch your account or balance. It never
> asks for credentials. It cannot know what the market will do next, and it
> will never tell you a trade is certain. Most market conditions should
> result in WAIT — that is the design working, not the tool failing.
>
> Trading binary options carries a high risk of losing money. Nothing here is
> financial advice.

---

## Download

**Windows, no Python needed.** One file, double-click it.

| | |
|---|---|
| **[GateKeeper.exe](https://github.com/JohnnyBizz/GateKeeper/releases/download/latest/GateKeeper.exe)** | the app — this is the one you want |
| [GateKeeper-windows.zip](https://github.com/JohnnyBizz/GateKeeper/releases/download/latest/GateKeeper-windows.zip) | same thing zipped, for when the browser refuses the .exe |
| [RecordFeed.exe](https://github.com/JohnnyBizz/GateKeeper/releases/download/latest/RecordFeed.exe) | optional — records a chart to a file. The panel's RECORD button already does this. |
| [RecordFeed-windows.zip](https://github.com/JohnnyBizz/GateKeeper/releases/download/latest/RecordFeed-windows.zip) | zipped |

All four live on the [`latest` release](https://github.com/JohnnyBizz/GateKeeper/releases/tag/latest),
which is rebuilt on every push to `main` and always sits at the same address.
The test suite runs first, so an executable is only ever published from a build
where every test passed.

**This repository is private.** The links above resolve for anyone signed in
with access to it and return 404 for everybody else — that is the intended
behaviour, not a broken build. Nothing here is published anywhere else, and the
release exists so there is always a finished, tested executable to fetch from a
fixed address without rebuilding it.

> **If the browser refuses the download** — *"couldn't download, virus
> detected"* — take the `.zip` instead and extract it. These builds are not
> code-signed, so a brand-new unsigned executable is judged on reputation it
> has not had time to earn. The refusal happens as the file comes down the
> wire, which is why there is nothing to click past; a zip is not an
> executable, so it arrives, and Windows judges it on extraction where *keep
> anyway* is at least offered.

**Prefer to run from source?** Three lines, any platform:

```bash
git clone https://github.com/JohnnyBizz/GateKeeper && cd GateKeeper
pip install -r requirements.txt
python overlay.py
```

Full details in [Installation](#6-installation) and [Running it](#7-running-it);
to build the executable yourself see [BUILD.md](BUILD.md).

---

## Contents

- [Download](#download)

1. [Architecture](#1-architecture)
2. [How chart data is obtained](#2-how-chart-data-is-obtained)
3. [How Heikin Ashi recognition works](#3-how-heikin-ashi-recognition-works)
4. [How the signal engine works](#4-how-the-signal-engine-works)
5. [Trade duration vs chart timeframe](#5-trade-duration-vs-chart-timeframe)
6. [Installation](#6-installation)
7. [Running it](#7-running-it)
8. [Connecting it to your chart](#8-connecting-it-to-your-chart)
9. [Configuration](#9-configuration)
10. [The dashboard](#10-the-dashboard)
11. [Backtesting and paper trading](#11-backtesting-and-paper-trading)
12. [Tests](#12-tests)
13. [Folder structure](#13-folder-structure)
14. [Limitations](#14-limitations-read-this)

---

## 1. Architecture

Python does the analysis; the dashboard is plain HTML/CSS/JS served by FastAPI
over a WebSocket. There is no build step and no frontend framework — the tool
should install in one command and start in one more.

```
   ┌──────────────────────────────────────────────────────────────┐
   │  CHART SOURCE          screen capture │ CSV replay │ synthetic│
   └───────────────────────────┬──────────────────────────────────┘
                               │  candles + a recognition confidence
   ┌───────────────────────────▼──────────────────────────────────┐
   │  DATA VALIDATION       enough candles? sane OHLC? regular     │
   │                        spacing? readable prices?              │
   │                        ── fails ⇒ WAIT, never a signal ──     │
   └───────────────────────────┬──────────────────────────────────┘
   ┌───────────────────────────▼──────────────────────────────────┐
   │  ANALYSIS (per timeframe: higher / current / entry)           │
   │  indicators · Heikin Ashi · structure · levels · volatility   │
   │  · momentum · market regime                                   │
   └───────────────────────────┬──────────────────────────────────┘
   ┌───────────────────────────▼──────────────────────────────────┐
   │  SIGNAL ENGINE                                                │
   │   score CALL and PUT independently  →  0-100                  │
   │   run confirmation gates on the better one                    │
   │   analyse trade duration separately  →  0-100                 │
   │   combine  →  CALL / PUT / WAIT / NO TRADE                    │
   └───────────────────────────┬──────────────────────────────────┘
   ┌───────────────────────────▼──────────────────────────────────┐
   │  TRACKER   active → weakening → invalidated → expired         │
   └───────────────────────────┬──────────────────────────────────┘
        ┌──────────────────────┼──────────────────────┐
   ┌────▼─────┐          ┌─────▼──────┐         ┌─────▼──────┐
   │  ALERTS  │          │  JOURNAL   │         │ DASHBOARD  │
   │ dedup +  │          │  SQLite +  │         │ WebSocket  │
   │ cooldown │          │ screenshots│         │  live UI   │
   └──────────┘          └────────────┘         └────────────┘
```

The loop runs on a background thread; the web layer is a thin shell over it.
Every stage is allowed to fail: a failure degrades to WAIT and is reported,
rather than stopping the application or leaving a stale signal on screen.

---

## 2. How chart data is obtained

**Pocket Option publishes no public market-data API.** Three options were
considered:

| Approach | Verdict |
|---|---|
| Private WebSocket protocol | Rejected — undocumented, breaks on every deploy, and sits badly against their terms of service. |
| Browser extension reading the DOM | Rejected — **the chart is drawn on a `<canvas>`**, so the DOM contains no candle values. An extension would end up doing pixel analysis anyway, just inside the page, while also requiring injection into someone else's site. |
| **Screen capture + computer vision** | **Chosen.** Works against the web platform, the desktop client, a demo account, or any other charting window. Requires no injection and no credentials. |

The honest trade-off: this reads pixels, so it can misread them. Every capture
therefore carries a **recognition confidence**, and low confidence produces
WAIT rather than a guess.

### How candles are extracted

1. **Colour segmentation** — bullish and bearish HSV masks (configurable; the
   defaults cover the usual green/red palettes).
2. **One contour per candle** — wick and body share a colour and touch, so a
   single contour is a single candle.
3. **Body separated from wick by width** — inside a candle's column band, rows
   covered edge-to-edge are the *body*; rows covered by a pixel or two are the
   *wick*. This step is what makes open and close recoverable at all; without
   it the bounding box returns the wick extremes and every candle looks like a
   marubozu.
4. **Indicator overlays are stripped first** — a SuperTrend, moving average or
   price level is drawn in the same green or red as the candles, and where one
   *touches* a candle the two merge into one shape and corrupt its high, low
   and body. Pixels belonging to a long horizontal run are removed before the
   candles are read. The removal is surgical rather than a blanket erosion,
   because eroding costs accuracy on charts carrying no overlays at all.
5. **Pitch check** — the spacing of candle centres tells us whether we found a
   coherent series or a mess. Irregular spacing lowers confidence.
6. **Price mapping** — pixel rows become prices via calibration (below).

Measured on rendered charts with known values, this recovers **100% of candles
with correct direction and a median close-price error under 0.1% of the chart's
range** (see `tests/test_chart_detection.py`, which round-trips a known series
through the renderer and back).

### Price calibration

| Method | Confidence | Notes |
|---|---|---|
| **Manual** (two reference points) | 100% | Exact, and immune to platform font changes. Recommended. |
| **OCR** of the price axis | 40–95% | Needs Tesseract. Refuses to return a calibration whose labels do not sit on a straight line — a wrong scale is worse than none. |
| **Relative fallback** | 35% | Shape analysis still works; printed price levels are meaningless. Flagged in the UI. |

### Other sources

* **`synthetic`** — a generated market with trends, ranges, breakouts and
  volatility clustering. Lets you run and evaluate the whole tool with no
  setup. Clearly labelled as demo data in the UI.
* **`csv`** — replays recorded candles, one at a time. Powers backtesting.

---

## 3. How Heikin Ashi recognition works

Heikin Ashi is computed from the OHLC series, not read off the screen as a
separate chart:

```
HA close = (open + high + low + close) / 4          ← the bar's average price
HA open  = (previous HA open + previous HA close) / 2   ← the smoothing term
HA high  = max(high, HA open, HA close)
HA low   = min(low,  HA open, HA close)
```

**A single Heikin Ashi candle is never interpreted in isolation.** The reader
looks at the sequence and the surrounding structure, and reports:

- consecutive bullish / bearish candles (streak length)
- body size trend — expanding, contracting, or flat
- the *flat side*: minimal lower wicks in an uptrend, minimal upper wicks in a
  downtrend, which is the signature of a strong trend
- doji-like indecision
- long wick rejection against the streak direction
- colour changes, and whether one is a genuine reversal or noise
- **momentum weakening** — a streak whose bodies are shrinking, or that is
  printing rejection wicks, even while the colour holds

So a strong bullish trend requires multiple bullish candles *with* large
bodies, minimal lower wicks, price above the moving averages, and supporting
structure. A potential bullish reversal requires the downtrend to lose
momentum first (shrinking bodies), then lower-wick rejection, then support,
then a Heikin Ashi confirmation — in that order.

---

## 4. How the signal engine works

### Step 1 — score both directions

CALL and PUT are scored independently, out of 100. Weights sum to exactly 100:

| Component | Weight |
|---|---|
| Trend (current + higher timeframe) | 20 |
| Market structure | 15 |
| Heikin Ashi | 15 |
| Momentum | 10 |
| EMA alignment | 10 |
| Support / resistance | 10 |
| RSI | 5 |
| MACD | 5 |
| Volatility | 5 |
| Multi-timeframe confirmation | 5 |

Each component returns 0–1 in favour of the direction being scored. A component
that *opposes* the direction scores below 0.5, so it actively drags the total
down rather than merely not helping.

Setup quality bands: **90+ very strong · 80–89 strong · 70–79 moderate ·
60–69 weak · under 60 insufficient.**

### Step 2 — the gates (this is the important part)

**A high score does not authorise a trade.** Every one of these must pass, or
the answer is WAIT:

1. **Data quality** — chart readable, enough candles, confidence above the floor
2. **Regime** — not UNCLEAR, not HIGH_VOLATILITY
3. **Market structure** — agrees with the direction, or a confirmed break of structure does
4. **Momentum** — pushing the right way
5. **Heikin Ashi** — confirms on the entry timeframe
6. **Clear path** — no major level sitting immediately in the way
7. **Higher timeframe** — does not oppose, unless a reversal is confirmed across all three timeframes
8. **Volatility ceiling** — ATR not in its top percentile
9. **Component agreement** — enough of the scoring weight actually agrees, not just a few strong components
10. **Confidence floor** — the configured minimum

Two further checks (reversal risk, Heikin Ashi momentum) are advisory: they add
context and warnings without blocking.

### Step 3 — regimes

`STRONG_UPTREND · WEAK_UPTREND · STRONG_DOWNTREND · WEAK_DOWNTREND · RANGE ·
HIGH_VOLATILITY · LOW_VOLATILITY · POTENTIAL_REVERSAL · BREAKOUT · UNCLEAR`

UNCLEAR and HIGH_VOLATILITY are not tradeable regimes. HIGH_VOLATILITY produces
`🛑 NO TRADE` — a stronger statement than WAIT, meaning conditions are actively
hostile to analysis.

### Step 4 — the four output states

| State | Meaning |
|---|---|
| 🟢 **CALL** / 🔴 **PUT** | Direction confirmed *and* duration compatible |
| 🟡 **WAIT** — duration questionable | Direction is confirmed, the chosen expiration is not |
| 🟡 **WAIT** | No directional confirmation |
| 🛑 **NO TRADE** | Conditions too unstable to analyse reliably |

### Step 5 — lifecycle

A signal does not sit on screen unchanged once conditions move:

```
ACTIVE ──confidence falls 15──▶ WEAKENING ──falls 25──▶ INVALIDATED
   └──────── expiration elapses ────────▶ EXPIRED
```

The tracker also decides what is *material*. The engine re-evaluates every
poll; you are only notified when something actually changed.

---

## 5. Trade duration vs chart timeframe

**These are two different variables and the engine treats them that way.** You
can watch a 1-minute chart and buy a 3-minute expiration; someone else can
watch 5-minute candles and buy 15 minutes.

For every evaluation, each available expiration is scored 0–100 on four axes:

| Axis | Points | Question |
|---|---|---|
| Signal-to-noise | 40 | Will price travel further than the market's own wiggle in that window? |
| Setup lifetime | 30 | Does the setup survive that long, or burn out first? |
| Resolution | 15 | Can a chart of this timeframe even resolve that window? |
| Room to run | 15 | Does the move hit an opposing level before expiry? |

Expected travel uses **`candles^exponent × efficiency`, where the exponent runs
from 0.5 (a random walk) to 1.0 (a pure trend)** depending on how persistent the
current move actually is. This is why a clean trend justifies a longer
expiration and chop does not — and why there is **no fixed rule** like
"1-minute chart ⇒ 3-minute trade". The same chart timeframe yields different
recommendations as the market changes.

The dashboard shows the whole ladder, so you can see *why* one expiration is
preferred:

```
30 SEC →  48/100  🔴      3 MIN →  86/100  🟢   ← recommended
 1 MIN →  61/100  ⚠️      5 MIN →  81/100  🟢
 2 MIN →  74/100  🟡     10 MIN →  63/100  ⚠️
```

If your selected duration scores below the configured minimum, the signal is
downgraded to WAIT and says so explicitly — the direction is still shown, with
its own confidence, so you can decide to switch expiration rather than lose the
setup.

**DURATION FIT on the panel is that score for the expiration you currently have
selected**, and **SUGGESTED** is the one that scored highest. They answer a
different question from the headline score. The headline asks *which way is
price going*; duration fit asks *will it get there before this particular
expiry*. A setup can be 88/100 on direction and 40/100 on duration — a real
move, on a clock too short to contain it — and that combination is a WAIT, not
a trade. When the two disagree, the usual fix is to change the expiry to the
suggested one rather than to abandon the setup.

---

## 6. Installation

Requires **Python 3.10+**.

```bash
cd GateKeeper

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

To try it without the screen-capture dependencies:

```bash
pip install numpy pandas PyYAML fastapi "uvicorn[standard]" pydantic websockets
```

**Optional — OCR price-axis reading.** Needs the Tesseract binary as well as
the Python package:

```bash
sudo apt install tesseract-ocr      # Debian/Ubuntu
brew install tesseract              # macOS
# Windows: https://github.com/UB-Mannheim/tesseract/wiki
```

Manual calibration (two clicks in `tools/select_region.py`) is more reliable
and needs none of this.

---

## 7. Running it

Two front ends share the same engine — run whichever suits, or both:

**No terminal needed.** Double-click **`GateKeeper.bat`** (Windows) or
**`GateKeeper.command`** (macOS/Linux). The first run installs what it needs
and starts the overlay; later runs just start it. Right-click the launcher →
*Send to* → *Desktop (create shortcut)* for a desktop icon.

For a true standalone app that needs no Python installed at all, see
[BUILD.md](BUILD.md) — one command produces `GateKeeper.exe`, and GitHub
Actions builds it automatically on every push.

From a terminal, if you prefer:

```bash
python overlay.py     # compact always-on-top panel, drag it next to your chart
python run.py         # full browser dashboard at http://127.0.0.1:8765
```

`config.yaml` is created for you on first run — you never have to copy or edit
it by hand, because everything in it is editable from the app's ⚙ settings.

Out of the box both run on synthetic demo data so you can see everything
working before wiring anything to a real chart.

```bash
python run.py -c my-config.yaml       # a specific config
POA_SERVER__PORT=9000 python run.py   # override any setting via env
```

### The overlay panel

A frameless, draggable window that stays above the chart. Top to bottom:
**PAIR / PAYOUT / TIME** tiles (with the chart timeframe shown separately —
the two are never conflated), the **watchlist tabs**, the big **SIGNAL** box,
**TAKE NOW**, the 0–100 score with a HIGH/MEDIUM/LOW badge and the named candle
pattern, **duration fit** and the engine's suggested expiration, **Scan /
Reset**, a session WIN/LOSS tally, and a risk block.

**TAKE NOW** is the count of setups that pass every gate at this moment,
**across every chart being watched** — `3 — here, AUD/CAD, GBP/USD`. It used
to be 0 or 1, because one chart yields at most one setup, and that is exactly
the problem: one chart produces a handful of setups in a day, which is a rate
that makes the tool feel broken. The other seven arrive on the same socket and
are read at the same bar against the same gates; counting only the open one
reported a fraction of what was actually available while the rest sat
unmentioned in a tab. The answer to "too few calls" is more charts, not weaker
standards.

A setup on a chart you are **not** looking at also raises an alert, because a
tab quietly turning green only helps somebody already watching the tab row:

```
👀 GBP/USD OTC — PUT
PUT setup on GBP/USD OTC at 84/100. Click its tab to read it.
```

Once, on the transition into being tradeable — not on every fifteen-second
sweep for as long as it holds — and through the same cooldown as every other
alert, so watching eight charts cannot become eight times the interruptions. It exists
because WAIT and NO TRADE both mean zero while looking nothing alike, and a
number is readable at a glance from across the desk. Under the session tally,
**how many setups have been called since the session started** answers the
other half: a quiet panel is either a quiet market or a broken app, and the
count is what tells the two apart.

The **session WIN/LOSS tally is yours alone.** Only you know which trades you
actually placed, so the +/− buttons are the only thing that moves it; settled
journal outcomes never write into it. (`overlay.session_manual: false` hands
it back to the journal, but then two things are writing one column and neither
number means much.)

### The WIN/LOSS buttons are how it learns

Pressing **WIN +** or **LOSS +** does not only move a counter. It files the
trade into the same record the gates are tuned from — the score that was on
screen, how well the expiry fitted, the market regime, the hour, and how it
turned out.

This matters more than it sounds. The record used to learn **only from calls
the tool made**, and the verdict is WAIT most of the time — so the trades
carrying the information it was missing, the ones that answer *what actually
happens at scores it currently refuses*, were exactly the ones it never saw.
Three winners on a chart it declined to call taught it nothing at all.

Now they teach it directly. Twenty-four trades settling at 67% on setups
scoring 72, against a gate sitting at 75, is the evidence that opens that gate
— and it is evidence that could not come from anywhere else, because the tool
was refusing to generate it.

The panel says where that stands, under the tally:

```
7 of your trades recorded — 13 more and they outrank the replay.
Learning from your 24 settled trades on this chart.
```

At twenty, your own trades **replace** the replay rather than joining it. They
are a better experiment: what actually happened, on this account, at this
broker, with the delay between the panel lighting up and the button being
pressed already baked in. None of that can be reproduced in a backtest.

Two things worth knowing:

* **It assumes you traded the way the panel was leaning.** The verdict is
  usually WAIT, which is not a direction, so the trade is filed against the
  direction the *score* was computed for — the case being argued. The MARKET
  line shows you that direction at the moment you press the button, so it is a
  visible assumption rather than a hidden one. A trade taken against the lean
  is one this cannot learn from correctly.
* **Correcting a miscount teaches nothing.** The **−** buttons only fix the
  tally; taking a win back is not a report of a trade, so nothing is filed.

Records are scoped like every other one: this pair, this chart timeframe, this
expiry, this data source. A demo run stays out of a live record.

The **RISK header folds the block away** — click it, and the panel remembers
across restarts. It is the tallest section and the one that changes least once
a stake is set.

### MARKET — which way it is going

Inside the signal box, under the verdict:

```
        S I G N A L
             ●
           WAIT
  MARKET  ▼ FALLING       HIGH ▼   NOW ▼
      100% of the timeframes agree
```

The verdict answers *act or not*, and most of the time the answer is not —
which leaves the panel silent about the thing you can see plainly on the
chart. One word — **RISING**, **FALLING** or **SIDEWAYS** — coloured green,
red or amber, with an arrow per timeframe beside it so a trend that exists on
one view looks different from one they all agree on. When they disagree, that
is usually the reason the verdict is WAIT, and now you can see it rather than
read for it.

It never says BUY or SELL. Those words belong to the verdict box; a trend
word that reads like an instruction is how *"the market is rising"* quietly
becomes *"buy"*.

A view that is not genuinely distinct is not drawn and does not vote. With too
little history to aggregate a higher timeframe, or an entry timeframe equal to
the chart itself, the stack is one view counted twice — and a copy of a view
agreeing with the view it copies is not agreement.

### TRADING PAUSED — the brakes

Position sizing is arithmetic right up until a losing run, at which point it
stops being arithmetic and starts being a decision made badly. Two limits are
set in advance, because the day one is reached is the day it will be argued
with:

```
⏸  TRADING PAUSED — 4 losses in a row. The limit set here was 4.
⏸  TRADING PAUSED — Down 12.0% of the balance today. The limit set here was 10%.
```

`risk.max_losses_in_a_row` (default 4) and `risk.max_daily_loss_percent`
(default 10). Either at zero switches that limit off.

Losses are counted **in order**, which is the whole point: a session at eight
wins and four losses reads healthily right up until the four were the last
four. Four spread across a good afternoon is not a losing run; four in a row
is. Wins offset what the day has cost, so down and then back up does not trip
it.

The stake is never raised after a loss, and nothing here ever suggests trading
larger to recover — that is martingale by another name, and there is a test
asserting the recommended stake after three losses is no larger than before
them. The pause is a statement, not an enforcement: nothing is locked, because
nothing here can place or prevent a trade. It says so where the stake would
otherwise be, and leaves the decision where it belongs.

### Eight charts at once

The platform's socket carries **every instrument you have open, all the time** —
not just the chart in front of you. Reading one and discarding the rest was
throwing away a watchlist that costs nothing to keep, so it no longer does:
each of your charts gets its own candle builder, up to eight behind the one on
screen.

*Your* charts specifically. The stream carries far more instruments than
anyone trades, so the platform's own record of the tabs along its top — the
`saveCharts` message it sends to remember your workspace — decides what gets
built. Without that filter the eight slots filled with whatever ticked first,
which meant pairs you had never opened while the ones actually on your screen
never got in. If that message never arrives, everything on the stream is kept
instead; a noisy list beats an empty one. Closing a tab on the platform does
not throw away the candles already gathered for it.

Each of those is read on its own every 15 seconds and appears as a **tab**,
two to a row under the tiles. A tab shows the pair and its score, and it is
**coloured only when that chart has a setup that passes every gate** — green
for CALL, red for PUT, grey otherwise. That is the point of the row: the pair
worth looking at finds you, instead of you finding it by clicking through
charts and hoping the moment is still there when you arrive.

**Click a tab and the panel reads that chart.** The whole panel switches to it
— signal, score, duration fit, risk, the replay measurement, everything —
because all of it was built from the same stream and was already in hand. The
tab you are reading is highlighted.

Three things about that click are worth being exact about:

* **Nothing happens in the browser.** No chart is opened, nothing is clicked,
  and no order is placed. Picking a tab chooses which of the candles already
  held gets read. This is the same rule the whole tool runs on — it reads and
  tells you; you place trades yourself.
* **Opening a chart on the platform wins.** If you click a pair in the browser,
  that ends whatever tab you had pinned and the panel follows the platform
  again. Whichever you did last is what you meant.
* **A watched chart has no platform history.** The platform only sends the
  backfill for the chart it actually has open, so the others fill from live
  ticks alone and start shallow — the panel says so, in candles, rather than
  pretending otherwise. Open one properly on the platform and its history
  arrives.

Tabs are ordered by name and stay put. Sorting by score would put the best
setup first, but these are click targets, and a tab that moves between the
reach and the press opens a pair you did not ask for. The colour does the
ranking; the colour can change without anything moving.

### Calibration — what the score is actually worth

The 0–100 score is an opinion. Its weights were chosen by judgement, and a
setup scoring 78 is not thereby 78% likely to win — the number has no units,
and until something checks it against an outcome, nothing has.

Under the score the panel now prints what setups in that neighbourhood
**actually settled at** on this chart:

```
Direction 80-90 settled at 50% here over 40 — break-even 52%
```

Green when the band clears the rate the payout needs, red when it does not.
Below twenty settled trades in the band it says nothing at all, because a 100%
win rate over three trades is the most confident-looking and least informative
number this codebase can produce.

**The record prefers your own trades.** A replay is what the engine *would*
have done on history it can see. Your journal holds what it actually did, and
how that actually settled — on this account, at this broker, with the delay
between the panel lighting up and the button being pressed already baked in.
That last part cannot be reproduced in a backtest, so once there are twenty
settled real trades for a chart, the record is built from **those instead** —
not blended with the replayed ones, because a trade that was taken and a trade
that was merely considered are different experiments. The panel says which it
is reading. Below twenty, the replay stands in.

It is keyed on the **direction** score rather than the headline one. That is
what exists when the gates run, and it is the number the structural analysis
actually produced — how well the expiry fits is a separate question with its
own score and its own gate, and folding the two together would blur them.

**And it acts.** `signals.require_measured_edge` (on by default) refuses a
setup whose own record says setups like it lose more often than the payout can
carry — a WAIT with the measurement as the reason. Two guards keep it honest:
it needs a real sample before it will block anything, and *no opinion is not a
failure*. Early in a session, on a new pair, or after a settings change there
is no record yet; a gate that blocked until one existed would prevent one ever
forming.

Three more views come out of the same replay, and answer the question of
*where* the edge lives rather than whether it exists:

* **by score threshold** — "a gate at 75+ would have taken 40 trades at 80%".
  This is what sets `signals.min_confidence`, and it is far better powered than
  the bands because every trade at or above the gate counts toward it. The
  recommendation ranks gates by what the whole sample would have *returned* —
  trades times expected value — rather than by win rate alone, because the
  strictest gate can leave money on the table by refusing forty profitable
  trades to avoid ten bad ones.
* **by regime** — trending markets and chop are different games, and a losing
  regime overrides a score band that looks fine, since the band is averaged
  across every regime.
* **by hour and by direction** — the London open and 3am are not the same
  market, and a tool that only gets calls right on a rising chart has found the
  trend rather than an edge.

### The gates set themselves

`min_confidence` and `min_duration_compatibility` decide how selective the
assistant is, and both shipped as numbers somebody chose. That is the wrong way
round. How selective to be is not a matter of taste — it is whatever the
measured record says was worth the most, and the record can work that out.

After each replay the threshold tables answer *"a gate here would have taken N
trades at X%"*, the best is picked by expected value, and the live gates walk
toward it. The panel says what moved and why:

```
Signal gate 75 → 70 · 65+ measured 85% over 40 in your 80 settled trades
Expiry gate 65 → 60 · 50+ measured 85% over 40 in your 80 settled trades
```

Four guards, because a gate that chases noise is worse than one that is merely
wrong:

* **Evidence.** Nothing moves without a recommendation the sample can carry.
* **Distance.** Five points at a time. A gate that jumped to wherever the last
  replay pointed would swing on every fresh candle, and you would watch the
  tool change its mind rather than settle.
* **Bounds.** Confidence stays within 55–90, expiry fit within 35–85. Never so
  loose the score stops discriminating, never so tight nothing can pass —
  auto-tuning that can reach zero is a tool that eventually takes every trade.
* **Reasons.** Every move states its evidence. A setting that changes silently
  is indistinguishable from a bug.

The values remain editable under ⚙ — changing one sets a new starting point,
and tuning walks on from there. `signals.auto_tune: false` freezes them.

**Two questions, kept apart.** *Which gate reads this chart best?* and *is the
result worth trading at this payout?* are not the same question, and folding
them together froze the tool on any chart that was merely marginal. The
expected-value ranking above only considers gates that clear break-even, so a
chart where **none** of them does returned no recommendation at all — and the
gate then sat wherever it started, for the whole session, on every pair that
was not already profitable.

So when nothing clears the bar, the gate falls back to whichever threshold
*separates winners from losers best* on a sample big enough to carry it, and
walks toward that instead. That is the first question, and it always has an
answer. The second one is printed beside every number on the panel — the
break-even rate for your payout, in green or red — because it belongs to
whoever is placing the trade, not to the software deciding whether to mention
the setup at all.

One veto survives that split: a **measured edge on real settled trades**. Not
a replay's opinion — twenty or more trades you actually placed, losing by more
than the sample's own noise. That is not a payout technicality, it is evidence
the thing is losing money, and it still blocks.

### MEASURED ON THIS CHART

Everything else on the panel is an opinion about what price will do. This line
is the one that checks.

When enough history has loaded, GateKeeper replays *that chart's own candles*
through the same signal engine, bar by bar, at the expiry you have selected,
and reports what the result would have been:

```
Replayed 508 bars: 77 setups, 39W/38L — 51% at 3 MIN, -2 pts vs 52% break-even
```

The replay runs at *looser* gates than the live ones, on purpose. Measuring
at the live gates measures only what already passes, which can never answer the
question the threshold table exists for — would a different gate have been
better? It also closes a loop: strict gates yield few setups, few setups cannot
carry a recommendation, so the gates never move off whatever they were set to.
Only the two selectivity dials are relaxed; regime, structure, multi-timeframe
and volatility are the strategy itself, and loosening those would measure a
different tool. The headline still reports what **your** gates would have
taken.

When a record is big enough to judge and *no* gate setting in it cleared
break-even, the panel says so and names the remedy: a different pair or expiry.
The answer is never a looser gate — loosening until something fires would
manufacture calls the record says lose money, which is the worst thing this
tool could do.

Three properties make it worth reading. **No look-ahead**: the replay hands the
engine a strict prefix of the series and nothing after it, so every decision is
made from what was knowable at that bar — a result that peeked would be a lie
in the most flattering possible direction. **The same settings you are
trading**: gates, payout and expiry come from the live configuration, so it
measures the tool as configured rather than some other version of it. And
**the trade has to have been placeable**: a signal exists only once its bar has
closed, so nobody could ever have bought at that bar's closing price — it was
already history by the time the panel lit up. Entry is the next bar's open, the
first price actually on offer, and the expiry runs from there. Settling against
the signal bar's close would measure a trade nobody could take and flatter the
result by exactly the amount price moved while the user was reading the panel.

It is deliberately unglamorous about its own limits. Below twenty settled
trades it says *too few to read* and stays grey rather than painting a green
100% over three trades. It is recent, in-sample and single-instrument, so the
honest use is comparative — **this pair against that one, this expiry against
another** — and the fastest way to find out that an instrument is untradeable
before putting money on it. It re-runs when you change chart or expiry, on a
background thread, and never blocks the panel.

The **Scan** button forces a fresh evaluation. While it runs, the previous
verdict is deliberately blanked and three dots cycle — the old answer must
never be readable as the new one — then the new verdict is revealed. The
engine also keeps monitoring continuously in the background either way;
alerts and setup-weakening/invalidation tracking stay live between scans.

The **PAIR, Balance and Stake fields are typed in directly.** GateKeeper
cannot read the pair's *name* off the screen, so when you switch charts on the
platform you rename it here — the previous verdict is dropped immediately
rather than lingering over a chart it never analysed. Typing a Stake overrides
the configured percentage, and the panel computes what share of the balance
that actually is, warning you when it's into territory a losing streak would
hurt. Clearing the field returns to percentage sizing.

### Switching charts

The screen is re-read from scratch every couple of seconds, so the candles
always come from whatever chart is in front of you. Three things follow a
switch automatically:

* **The switch is detected.** Two independent tests: a jump in price level
  (no market moves several percent between two polls), and a break in candle
  *shape* continuity — which catches switching between two pairs that happen
  to trade at similar levels, where the price test sees nothing.
* **The price scale is re-derived.** A manual calibration describes one
  specific chart's axis; after a switch it is dropped and the scale is read
  from the new chart, because confident wrong prices are worse than none.
* **The pair name and the chart timeframe update**, if you pointed GateKeeper
  at those two badges during setup (it offers both straight after the chart
  area). Neither can be inferred from candles: the pair is text, and a
  1-minute and a 5-minute chart draw identical-looking bars. The timeframe is
  the one that really matters — every duration recommendation is measured in
  candles of that length, so a 5-minute chart read as 1-minute would suggest
  expirations five times too short. Skip them and set both by hand instead.

Both label readers are deliberately cautious. A change must be read the same
way on consecutive frames before it is accepted, so the panel does not flicker
while the platform animates a transition; and a value that does not look like
a real instrument or a timeframe the platform actually offers is rejected
rather than guessed at — `M999` parses to a perfectly plausible 17 hours, and
adopting it would quietly reshape every recommendation.

Everything derived from those — analysis, tracker state, journal filing —
restarts with them. Pressing **Scan** likewise starts from a clean slate.

The **session tally** fills itself from settled journal outcomes; the +/−
buttons adjust it for trades you took that the assistant never signalled. The
win rate is shown against the **break-even rate for your configured payout**
(52.1% at 92%), and greyed out below 20 trades because a rate over a handful
of trades means nothing.

### Settings, without touching a file

The **⚙ button** in the panel header opens settings: which chart to read
(the platform's data / live screen / demo / CSV), the asset, both timeframes,
payout, balance, the confidence floors, and alerts. Reading the platform's data
shows **Re-read chart**, which asks it again which chart is open. Reading the
screen shows **Find chart**, which locates everything by itself (below), and
**Select…**, the manual fallback that freezes the screen so you can drag a box
and click two known prices. The rows follow the source, because a button for a
thing that does not exist is a button that does nothing when pressed.
Everything is applied immediately and saved to `config.yaml`, so it survives a
restart.

The overlay needs Tkinter, which ships with Python on Windows/macOS; on Linux
it is `sudo apt install python3-tk`. The browser dashboard needs none of this.

---

## 8. Connecting it to your chart

GateKeeper reads the platform's own data. Open it, sign in once in the browser
window it opens, and that is the setup.

### Why not read the screen

It used to. The chart is drawn on a canvas from candles that arrive over a
WebSocket, so reading pixels means reconstructing those numbers from a picture
of them — deciding which coloured shapes are candles, where each one begins,
what price each screen row is. Every stage infers, every stage fails on its
own, and the failures do not announce themselves: a browser tab read as the
instrument, the app's own panel read as the timeframe, a price axis strip
measured in the wrong place. Different bugs, one cause.

The feed has none of that. `loadHistoryPeriodFast` carries settled candles with
volume, `updateStream` carries live ticks, and `changeSymbol` states the
instrument and the timeframe outright. Nothing is measured, so nothing can be
mismeasured.

### Which chart is open

Exact prices for the wrong instrument are no better than a misread of the right
one, and the socket makes that mistake easy: it streams ticks for several
instruments at once, so the first symbol to arrive says nothing about what is
on screen. The instrument is taken only from messages where the page names its
own chart — `changeSymbol`, `saveCharts`, a history request — and those travel
*outbound*, from the browser to the platform. Ticks never choose. Until one
arrives the panel shows no pair at all, because a stale instrument above a
live-looking verdict is a report on a market nobody is watching.

### The bar that has not closed yet

A candlestick pattern is a statement about a *closed* bar. Ten seconds into a
minute the live bar has almost no body and reads as a doji or a pin bar; forty
seconds later it is full-bodied and pointing the other way. Reading it means
the answer changes as the bar builds, and the signal fires on whichever instant
it happened to be looked at rather than on anything the market did.

The whole analysis stack — patterns, Heikin Ashi, market structure, every
indicator, the level detector — is anchored on **closed candles**. The live
price is carried separately and used where it belongs: the price on the panel,
distance to the nearest level, and the expiry arithmetic. *Where price is now*
and *what the last bar did* are different questions and are answered from
different data.

This applies to the higher timeframe too. Four closed 1-minute candles do not
make a 5-minute candle, and the aggregator marks the partial bucket unfinished
so the same rule drops it.

### Confirmation has to be real

The higher timeframe is aggregated from the chart's own candles, so a short
history cannot produce one — five 1-minute candles make one 5-minute candle,
and thirty of those need 150. When that fails the analysis used to fall back to
the base series, which meant the "higher timeframe" was the chart itself:
it agreed with the current view by construction, the multi-timeframe
requirement passed on a tautology, and a read of forty candles scored like a
read of five hundred.

It now knows the difference. A stand-in higher view scores neutral rather than
confirming, and the confirmation gate fails with *no higher timeframe yet*
instead of quietly passing. The practical effect: fewer calls early in a
session, and the ones that come have actually been confirmed against something.

Claims are ranked: the page naming its own chart outranks history, which
outranks a per-instrument tick feed, and a weaker claim never unseats a
stronger one — that is what stops history for the pair you just left from
taking the panel with it. Any claim wins once the instrument being followed has
gone quiet.

Those messages are sent when a chart loads, so attaching to a tab that loaded
minutes ago means they have already gone past. GateKeeper asks the page to
reload — twice at most — rather than asking you to switch timeframe to shake
them loose. **Scan** does the same on demand, and is the only button to press
when the panel and the platform disagree.

| | Feed | Screen |
|---|---|---|
| Prices | exact | inferred from pixel rows |
| Instrument | stated by the platform | OCR of a label |
| Timeframe | stated by the platform | OCR of a badge |
| Volume | included | unavailable |
| Setup | sign in once | region, calibration, two label boxes |

It attaches to the browser the same way the Network tab does, on your machine
and your account. It sends nothing to the platform and places no trades.

Chrome refuses to open a debugging port on an everyday profile, so GateKeeper
starts a browser against a profile directory of its own. That window is signed
out the first time; it remembers afterwards.

Every parser fails quietly on a shape it does not recognise. This protocol was
read off a recording rather than a specification, so a platform deploy that
renames a field stops the feed instead of inventing candles from whatever
arrives instead.

### The screen fallback

Set `capture.source: screen` if the feed cannot be reached. Everything below
describes that path, and it is the one with the caveats.

## 8b. Reading the screen (fallback)

Open your chart, open GateKeeper, press **Scan**. That is the whole setup.

### How it finds the chart

Scan locates the chart itself whenever it has no working region, so there is
nothing to drag and nothing to type. It rests on one property candles have that
interface chrome does not: **rhythm**. Candles are drawn at a fixed pitch,
dozens in a row, all the same width. The BUY button, the SELL button, a green
account balance and the coloured sidebar icons are all candle-coloured too, but
none of them repeats forty times at a constant spacing. So the detector does not
look for green things; it looks for the longest evenly-spaced run of green and
red things, which is the candle field and nothing else.

Everything after that is positional:

| Part | How it is found |
|---|---|
| Price axis | The column of numeric labels immediately right of the candles. Its width is measured, not assumed — a strip narrower than the labels slices the leading digits off *every* one, and what is left is still perfectly linear, so nothing downstream would catch it. |
| Pair name | Symbol-shaped text near the plot (`EUR/USD`, `CAD/JPY OTC`). Confirmed by parsing, so a stray word is never adopted as an instrument. |
| Timeframe badge | The nearest token that parses as an interval (`M1`, `5m`, `H3`) *and* matches one the platform actually offers. |
| GateKeeper's own window | Excluded. The panel is always on top and full of candle-coloured buttons; left in the frame it is a plausible-looking chart sitting over the real one. |

Text is read by finding each word as a shape first and reading it on its own.
Handing a whole strip of interface to Tesseract does not work — it picks one
global threshold, and where the panel, the background and the text are three
different brightnesses, that threshold separates the panel from the background
and loses the text entirely.

Scan re-locates only when it has to: no region configured, or the current one
scoring under 45% recognition confidence. A region that is reading the chart
well is never thrown away, because that would discard a good calibration for
nothing.

**Text recognition needs Tesseract.** The Windows build bundles it. Otherwise:
`sudo apt install tesseract-ocr`, `brew install tesseract`, or the
[UB-Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki). Without
it candle patterns and direction still work, but the pair, the timeframe and the
price axis cannot be read — and GateKeeper says so rather than going quiet.

### If it cannot find the chart

* Make sure the chart is fully visible and not covered by another window.
* Zoom out so more candles are on screen — a field needs at least 18 to register.
* Set `capture.colors` if your theme uses unusual candle colours.
* Fall back to **Select…** and drag the box yourself: candles plus the price
  axis, none of the platform's buttons.

The panel shows recognition confidence at all times. Do not trade off a reading
the tool says it is unsure about.

---

## 9. Configuration

Every setting lives in `config.yaml` (see `config.example.yaml`, which is fully
commented). Any value can be overridden by environment variable using
`POA_SECTION__KEY`, e.g. `POA_SIGNALS__MIN_CONFIDENCE=85`.

The settings you are most likely to touch:

| Setting | Default | What it does |
|---|---|---|
| `market.chart_timeframe` | `60` | Seconds per candle on your chart |
| `market.trade_duration` | `180` | The expiration you intend to buy |
| `signals.min_confidence` | `75` | Setup score needed before a direction is shown |
| `signals.min_duration_compatibility` | `65` | Duration fit needed before a direction is shown |
| `signals.min_data_confidence` | `70` | Chart-reading confidence needed to analyse at all |
| `alerts.cooldown_seconds` | `120` | Suppresses repeat alerts |
| `capture.poll_seconds` | `2.0` | How often the chart is re-read |

Raising `min_confidence` produces fewer, higher-quality signals. Lowering it
does **not** make the tool more accurate — it just makes it talk more.

---

## 10. The dashboard

```
┌──────────────────────────────────────────────────────────────────────┐
│ Asset ▼   Chart timeframe ▼   Trade duration ▼   Min confidence ▼    │
├──────────────────────────────────────────────────────────────────────┤
│ ASSET │ CHART │ DURATION │ REGIME │ SIGNAL │ CONFIDENCE              │
├────────────────────┬─────────────────────┬───────────────────────────┤
│  LIVE CHART        │  ANALYSIS           │  SIGNAL                   │
│  candles /         │  timeframe stack    │  🟢 CALL / BUY            │
│  Heikin Ashi       │  structure, HA,     │  direction   ████ 84      │
│  toggle            │  momentum, RSI,     │  duration    ████ 87      │
│  S/R overlays      │  MACD, ADX, ATR     │  overall     ████ 85      │
│  live price        │  levels + strength  │  [ WHY? ]  ← full         │
│                    │  score breakdown    │    reasoning              │
│                    │                     │  invalidation             │
│                    │                     │  duration ladder          │
│                    │                     │  alerts                   │
├────────────────────┴─────────────────────┴───────────────────────────┤
│  SIGNAL JOURNAL   time · asset · signal · confidence · outcome       │
└──────────────────────────────────────────────────────────────────────┘
```

The **WHY?** button expands the complete reasoning: every timeframe reading,
every scoring component with its detail line, and every confirmation gate with
a ✓ or ✗ — including the gates that passed, so you can see exactly what carried
a signal and what blocked it.

### Alerts

`🟢 BUY SIGNAL · 🔴 SELL SIGNAL · 🟡 SETUP INVALIDATED · ⚠️ SETUP WEAKENING ·
🔄 TREND REVERSAL · ⚠️ HIGH VOLATILITY · ⚠️ CONFLICTING SIGNALS ·
⚠️ CHART DATA UNRELIABLE`

Delivered to the dashboard, the application log, and native desktop
notifications (`notify-send` / `osascript` / `win10toast`), with configurable
minimum confidence, cooldown and per-kind filtering.

### The journal

Every material signal is recorded with its timestamp, asset, both timeframes,
direction, confidence, regime, Heikin Ashi state, trend, RSI, MACD, EMA
condition, support/resistance, full reasoning, a screenshot when the source
provides one — and, once the expiration elapses, **what actually happened**.

---

## 11. Backtesting and paper trading

```bash
python tools/backtest.py                            # over the sample data
python tools/backtest.py --csv data/session.csv --duration 300
python tools/backtest.py --compare-durations        # every expiration
python tools/backtest.py --use-recommended-duration # test the duration engine
```

Also available at `POST /api/backtest`.

Two rules keep it honest:

* **No lookahead.** The engine is handed a strict prefix of the data; the
  settlement price is only read after the expiration has elapsed. Trades that
  cannot settle before the data ends are dropped rather than left open.
* **Settlement matches the instrument.** A binary option is decided by where
  price sits at expiry versus entry — not by whether it moved favourably at
  some point in between.

Reported: signals, win/loss/flat, win rate, **break-even rate**, expected value,
average confidence, max streaks, and breakdowns by direction, duration,
timeframe, setup quality, regime and asset.

**Read the break-even rate, not the win rate.** At an 80% payout a win returns
0.8 and a loss costs 1.0, so you need about **55.6%** just to break even — not
50%. A 54% win rate loses money. Results are labelled "small sample" below 30
settled signals, because a win rate over ten trades says close to nothing.

> **On the sample data:** `data/sample_eurusd_m1.csv` is *synthetic*. Backtests
> against it measure the generator as much as the engine, and its results say
> nothing about live performance. Record your own candles for anything you
> intend to rely on.

---

## 11b. The session report

When the app closes it writes a plain-text account of the session to
`storage/reports/session-YYYY-MM-DD-HHMM.txt` — one file per session, named
for when it started so they sort into the order they happened in.

The panel is a live instrument: it shows what is true at the moment you look
at it, and forgets. That is the wrong shape for judging whether the tool is
any good, or for showing anyone else. Judging it needs the whole session laid
out at once — every call, in order, with the score that produced it and how it
settled — somewhere that outlives the process.

```
CALLS MADE                                                        12 calls
--------------------------------------------------------------------------
TIME  PAIR          DIR    SCORE   EXPIRY      ENTRY    RESULT
09:48 EUR/USD OTC   CALL      78    3 MIN    1.19280       WIN
10:43 GBP/USD OTC   PUT       81    3 MIN    1.36106      LOSS

HOW THEY SETTLED
--------------------------------------------------------------------------
Settled                   7W / 5L
Win rate                  58.3%
Break-even at 92% payout  52.1%   (above)
Notional stake            $20.00 per call
Notional return           +$36.80  (not placed)
```

Also in the file: the trades you placed by hand kept in their own section,
the measured record, any gate the record moved and why, any rule stood down,
and what the losing calls had in common.

**The one thing the document is careful about.** GateKeeper places no trades
and touches no account. Every call in it was made by the assistant and then
followed to expiry against the platform's own prices; the return is priced at
the stake and payout configured at the time, so it is what those calls *would*
have returned had each been taken. A report that could be read as a record of
executed trades would be worse than no report, so the wording never leaves it
ambiguous — the header says it before any number appears, the return figure
carries `(not placed)`, and the caveats repeat it at the end.

Trades you placed yourself are listed separately for the same reason: pooling
them would make it impossible to judge either the assistant or yourself.

A session with no calls says so explicitly, and says what it means — every
setup examined failed at least one confirmation, which is a finding rather
than an empty page. Rates over small samples are labelled as too few to read,
and are always set against the break-even rate for the payout rather than
against 50%.

`storage.report_dir` moves the folder. Losing a report is bad and hanging on
the way out is worse, so a report that cannot be written is logged and the
app still closes.

---

## 11c. Measuring against a real market

Everything the tool knows about its own accuracy is measured against candles,
so where those candles come from decides what the measurement is worth.

Until recently the only market available offline was a generated one, and that
is a harsher limit than it sounds. **A random walk is unpredictable by
construction.** Past the drift laid over it there is nothing in it to find, so
a score that ranks setups perfectly and a score that ranks them by coin toss
produce the same number on it. Measuring *"does the score separate winners
from losers"* against generated data cannot answer the question either way —
and measuring it there and reporting the answer would be worse than not
measuring at all.

### Taking a recording

Press **RECORD 30 MIN FOR ANALYSIS** on the panel. GateKeeper is already
attached to the browser the platform runs in, so it listens to the same socket
it reads prices from, streams the frames to disk, and at the end writes one
file:

    <data folder>/storage/gatekeeper-recording-<date>-<time>.zip

That zip holds the candles — every timeframe the tool would have had at the
time — and a summary of what the platform sent. The raw frames stay behind:
they are tens of megabytes of heartbeats and acknowledgements, and nothing is
measured on them. The button opens the folder when it is finished.

Nothing is sent to the platform and no trade is placed. Passwords, session
tokens and balances are stripped before anything reaches disk.

Recording is in the app on purpose. It used to mean downloading a second
executable, `RecordFeed.exe`, and Windows refuses that download outright —
*"couldn't download, virus detected"* — which is a refusal nobody can click
past, because no file ever arrives. `RecordFeed.exe` is still published, for
running a capture without the app; see BUILD.md.

### Reading one back

Both the button and `RecordFeed` write the platform's own frames to disk with
the secrets stripped. Those can be read back:

```bash
python tools/backtest.py --recording feed.jsonl --list-charts
#    EUR/USD OTC        5 SEC    601 candles
#    EUR/USD OTC       15 SEC    210 candles

python tools/backtest.py --recording feed.jsonl --timeframe 15 --duration 60
```

The frames are replayed through the live source's own handler rather than a
second parser written for the purpose. Two parsers would be two answers to one
question, and the one used offline is the one nobody notices has drifted.

What comes out is every timeframe the tool would have had at the time — the
sub-minute ones built from ticks, the rest aggregated — as real candles from a
real market. Every experiment worth running (which score threshold discriminates,
whether the component weights can be fitted, which hours and conditions the
reading holds up in) only means something when it runs on these.

A recording is a log rather than a database: an unreadable line is skipped, not
a reason to lose the session around it.

---

## 12. Tests

```bash
pytest -q                    # 372 tests
pytest tests/test_signals.py -v
```

Coverage spans indicators (against hand-computed values), Heikin Ashi sequence
analysis, market structure, level detection, volatility, regime classification,
resampling, multi-timeframe reconciliation, the scoring engine, every
confirmation gate, the duration engine, signal lifecycle, alert suppression,
journal persistence and settlement, statistics, the backtester's no-lookahead
guarantee, chart recognition (round-tripped through a renderer with known
values), config loading, the HTTP and WebSocket API, and engine degradation
under a failing chart source.

Two safety invariants are asserted directly:

* **bad data never yields a confident direction** — every path through the
  data-quality gate is tested;
* **no output ever claims certainty** — every generated string across four
  market types is checked against a banned-phrase list ("guaranteed", "100%
  accurate", "cannot lose", "risk free", …), and a sanitiser enforces it as a
  backstop.

---

## 13. Folder structure

```
GateKeeper/
├── poa/
│   ├── analysis/          Heikin Ashi, structure, levels, regime, patterns,
│   │                      volatility, momentum, resampling, multi-timeframe
│   ├── chart_detection/   sources (screen/CSV/synthetic), OpenCV candle
│   │                      extraction, calibration, data validation, renderer
│   ├── indicators/        EMA, RSI, MACD, ATR, ADX, Bollinger, VWAP
│   ├── signals/           scoring, gates, duration, narrative, tracker, engine
│   ├── alerts/            manager (dedupe + cooldown), notifiers
│   ├── backtesting/       paper trading, statistics
│   ├── storage/           SQLite journal, screenshots
│   ├── dashboard/         index.html, app.js, styles.css
│   ├── overlay/           always-on-top panel (view model + Tk renderer)
│   ├── config.py  engine.py  server.py  models.py  risk.py  logging_setup.py
├── tests/                 372 tests
├── tools/                 select_region.py, make_sample_data.py, backtest.py
├── data/                  sample candle CSV
├── config.example.yaml    fully commented
├── requirements.txt
├── GateKeeper.bat         double-click launcher (Windows)
├── GateKeeper.command     double-click launcher (macOS/Linux)
├── gatekeeper.spec        PyInstaller build for a standalone app
├── BUILD.md               how to build the standalone app
├── run.py                 browser dashboard
└── overlay.py             overlay panel
```

---

## 14. Limitations (read this)

**Computer vision is not perfect.** It can miss candles, merge them, or misread
a price scale. That is why every capture carries a confidence and why low
confidence produces WAIT. Check the confidence before acting on anything.

**The engine has no view on news, spreads, slippage or broker execution.** It
sees candles. A technically clean setup can be destroyed by an event it cannot
observe.

**A directional read is not the same as a profitable binary option.** Direction
and expiration are scored separately, and both must line up — but even then,
where price happens to sit at one specific moment carries randomness that no
amount of analysis removes.

**Backtest results do not predict future performance.** They describe how the
engine behaved on the data it was given, and on synthetic data they largely
describe the generator.

**Most of the time the correct answer is WAIT.** If you find yourself lowering
`min_confidence` to get more signals, you are working against the tool's only
real advantage.

**This is analysis and alerts only.** You make every trading decision, and you
place every trade yourself.

**No part of the scoring model has been shown to predict anything yet.** One
live session measured it running *backwards* — 32.7% over 55 settled calls,
beaten by sixteen points by always buying and never changing your mind — and
the committed recordings reproduced neither that nor any edge in the other
direction. [FINDINGS.md](FINDINGS.md) is the running record of what has been
measured, what replicated, and what did not.
