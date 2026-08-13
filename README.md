# Pocket Option Technical Analysis Assistant

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

## Contents

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
4. **Pitch check** — the spacing of candle centres tells us whether we found a
   coherent series or a mess. Irregular spacing lowers confidence.
5. **Price mapping** — pixel rows become prices via calibration (below).

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

---

## 6. Installation

Requires **Python 3.10+**.

```bash
cd pocket-option-assistant

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

```bash
cp config.example.yaml config.yaml     # optional; defaults work as-is
python run.py
```

Open **http://127.0.0.1:8765**. Out of the box it runs on synthetic demo data
so you can see everything working before wiring it to a real chart.

```bash
python run.py -c my-config.yaml    # a specific config
POA_SERVER__PORT=9000 python run.py   # override any setting via env
```

---

## 8. Connecting it to your chart

1. Open your chart and set it up the way you trade it.
2. Select the chart area:

   ```bash
   python tools/select_region.py
   ```

   Drag a box around the plot area — **candles plus the price axis, and none of
   the platform's buttons, order panel or asset list**. Anything else in the
   region gets mistaken for candles and lowers recognition confidence. The tool
   immediately reports how many candles it can see.

3. When prompted, press **Y** and click two horizontal lines whose prices you
   can read off the axis, then type those prices. This calibrates the price
   scale exactly.

4. Paste the printed YAML into `config.yaml` and set:

   ```yaml
   capture:
     source: screen
   market:
     asset: EUR/USD
     chart_timeframe: 60     # match your chart
     trade_duration: 180     # the expiration you intend to buy
   ```

5. `python run.py`

**If recognition confidence is low**, widen the region, zoom the chart so more
candles are visible, or set `capture.colors` if your theme uses unusual candle
colours. The dashboard shows the confidence at all times — do not trade off a
reading the tool says it is unsure about.

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

## 12. Tests

```bash
pytest -q                    # 254 tests
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
pocket-option-assistant/
├── poa/
│   ├── analysis/          Heikin Ashi, structure, levels, regime,
│   │                      volatility, momentum, resampling, multi-timeframe
│   ├── chart_detection/   sources (screen/CSV/synthetic), OpenCV candle
│   │                      extraction, calibration, data validation, renderer
│   ├── indicators/        EMA, RSI, MACD, ATR, ADX, Bollinger, VWAP
│   ├── signals/           scoring, gates, duration, narrative, tracker, engine
│   ├── alerts/            manager (dedupe + cooldown), notifiers
│   ├── backtesting/       paper trading, statistics
│   ├── storage/           SQLite journal, screenshots
│   ├── dashboard/         index.html, app.js, styles.css
│   ├── config.py  engine.py  server.py  models.py  logging_setup.py
├── tests/                 254 tests
├── tools/                 select_region.py, make_sample_data.py, backtest.py
├── data/                  sample candle CSV
├── config.example.yaml    fully commented
├── requirements.txt
└── run.py
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
