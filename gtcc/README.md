# Grok Trading Command Center

A multi-market trading research and execution platform: crypto, equities,
forex and futures, with an institutional-style decision pipeline and a
deterministic risk engine that has final authority over every order.

**Phase 1 is complete.** The foundation, the risk engine, paper
execution, the database, the API and the dashboard shell are built and
tested. There is no live market-data adapter yet, so the platform cannot
currently trade anything, which is the correct state for this phase.

```
MODE = PAPER        LIVE_TRADING = FALSE        AUTOMATIC_EXECUTION = FALSE
```

## The two rules

**The deterministic risk engine decides.** Every order reaches a broker
through one method, and that method calls `RiskEngine.evaluate` first.
No bypass flag, no trusted caller, no second path. Grok's output arrives
as advisory data and the engine does not read it.

**Nothing is invented.** No fabricated market data, fills, balances,
news, backtest results or model confidence. Non-finite numbers are
refused at the domain boundary, and connection URLs are never rendered
with their credentials. When a datum is unavailable
the code says so: a bar-only recording refuses to produce a quote, a
position with no mark reports `None` rather than zero, and a page with no
engine behind it says which phase delivers it instead of showing a
plausible number.

## Quick start

```bash
cd gtcc
pip install -r requirements.txt

cp .env.example .env
python -c "import secrets; print('GTCC_SECRET_KEY=' + secrets.token_urlsafe(48))" >> .env

python -m gtcc init-db
python -m gtcc create-user you@example.com
python -m gtcc check          # configuration and adapter health
python -m gtcc risk           # what your risk numbers mean in money
python -m gtcc scan EUR_USD GBP_USD   # analyse symbols, honestly
python -m gtcc serve          # http://127.0.0.1:8000
```

`gtcc risk` is the one to read before trading anything. It converts every
limit into money for a given account size, names the ceiling that
actually binds each market (four of them cap face value and only the
smallest is real), says at what stop distance a cap starts to override
your risk budget, and runs one concrete trade through the real engine so
the numbers come from the code rather than from the documentation:

```bash
python -m gtcc risk --equity 25000
python -m gtcc risk --example      # read the shipped example first
```

Tests:

```bash
pytest                       # 642
pytest -k TheSevenRefusals   # the critical risk tests
```

## Risk configuration is yours

There is no default risk-per-trade in this codebase. A default would be a
recommendation, and this platform is not qualified to make one.
`config/risk.example.yaml` shows the file format and carries a marker
line; `load_limits` refuses to read it while that marker is present, so a
deployment cannot quietly run on numbers chosen by a stranger.

`config/risk.yaml` holds this deployment's own numbers and is tracked, so
that a change to a limit appears in history like a change to any other
code — the limits are the most consequential thing here, and an edit to
them should be as reviewable as an edit to the engine. Read
`python -m gtcc risk` before and after any change: the point is to decide
in money, not in percentages.

Four separate limits cap one position's face value, and only the smallest
of them is real. `gtcc risk` names the binding one per market, and prints
the stop distance at which a cap starts to override the risk budget
entirely.

## Layout

```
src/gtcc/
  domain/        value objects, Decimal money, instrument and price arithmetic
  features/      indicators and the regime classifier
  structure/     market structure, every detection carrying its rule
  strategies/    strategy framework, gates, and the first module
  scanner/       many symbols at once, reporting what it could not read
  journal/       every considered setup, including the refused ones
  backtest/      event-driven replay that cannot see the next bar, and
                 the checks that try to break its results
  data/          validation: GOOD / DEGRADED / INVALID
  risk/          limits, state, sizing, the deterministic engine
  execution/     paper fill engine, order state machine
  adapters/      broker and market-data interfaces, paper broker, CSV replay
  ai/            Grok input and output contract
  storage/       SQLAlchemy models, Alembic migrations, repositories
  api/           FastAPI app, auth, routes
  web/           dashboard templates and stylesheet
  runtime.py     the application container and the single submission path
docs/            architecture, schema, adapters, pipeline, risk, AI, paper, testing, backtesting
config/          risk.yaml and the example it was written from
migrations/      Alembic
tests/           642 tests
```

## Documentation

| Document | Covers |
|---|---|
| `docs/01-architecture.md` | Pipeline mapping, layering, technology choices |
| `docs/02-database-schema.md` | All 20 tables and why each column exists |
| `docs/03-adapter-interface.md` | The broker/data interface and planned venues |
| `docs/04-market-data-pipeline.md` | Validation, timeframes, look-ahead prevention |
| `docs/05-risk-engine.md` | The 36 checks, the latch, breakers, sizing, reward:risk |
| `docs/06-grok-contract.md` | Structured input, validated output, rejection rules |
| `docs/07-paper-trading.md` | What is modelled, and what is explicitly not |
| `docs/08-testing-strategy.md` | The seven critical tests and the rest |
| `docs/09-credentials-and-services.md` | Keys, sandboxes, what must be verified |
| `docs/10-implementation-checklist.md` | Phase by phase, with known limitations |

## Connecting OANDA

Phase 2 reads real prices from an OANDA practice account: simulated
funds, live market data. Orders still pass through the deterministic
risk engine; OANDA is only the venue at the far end.

**Do not send your token to anybody, including an AI assistant.** It is
not your username and password: it is a v20 personal access token you
generate from your own account's API access page, and this server is the
only thing that should ever hold it.

```bash
# In .env, which is gitignored, or in a secret manager.
GTCC_OANDA_TOKEN=<your v20 personal access token>
GTCC_OANDA_ACCOUNT_ID=101-004-1234567-001
GTCC_OANDA_ENVIRONMENT=practice

python -m gtcc oanda-check     # read-only: places no order
```

`oanda-check` calls each endpoint the adapter uses and prints which
fields came back against what the adapter expects, then lists your
tradeable instruments as the platform sees them. It never prints the
token.

Run it once before anything else. The adapter was written without access
to OANDA's API documentation, so its field names are an assumption until
that command confirms them. If something does not match, the output
names the exact field and the fix is a small change in one file.

Nothing about an instrument is written into this codebase. Tick size,
pip size, lot step, minimum size and leverage all come from your
account, which is why a wrong guess in the adapter cannot reach the
position sizing.

## Live trading

Disabled, and reaching it is deliberately two separate things.

**Deployment permission** is `GTCC_ALLOW_LIVE_TRADING=true` in the
server environment. It means this server may *offer* live mode. It arms
nothing.

**Runtime arming** is a signed-in owner posting the exact phrase
`ENABLE LIVE TRADING` to `/api/control/live/arm`. Arming lasts for the
life of that process only. Every restart comes back disarmed, and there
is no environment variable that carries the phrase, so a stale deploy
environment cannot stand in for a person.

A critical safety failure **latches** execution off: stale or invalid
market data, an unhealthy broker, a failed order reconciliation, an
unvaluable position, or a daily, weekly or drawdown loss breaker.
Recovery of the underlying dependency does **not** clear the latch. An
authorised person must reset it, the reset is refused while anything is
still unhealthy, and the reset leaves live disarmed, so resuming costs
two deliberate actions.

Phase 7 is extended forward testing. Live infrastructure is enabled only
after that and an explicit authorisation from the account owner.

## Pocket Option

Analysis and signal only. No official execution API is assumed to exist,
and no private endpoint, credential interception or anti-bot
circumvention is used. Automated execution waits for a documented,
authorised integration that can actually be verified.

## Not a promise

This is research and execution tooling. Nothing in it predicts a market,
no figure it produces is a guarantee, and paper mode will not reproduce a
live broker exactly. Trading risks capital.
