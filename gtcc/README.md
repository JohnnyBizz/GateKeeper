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
news, backtest results or model confidence. When a datum is unavailable
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

# Your risk numbers. Read every line; the platform refuses the example.
cp config/risk.example.yaml config/risk.yaml
#   ... edit, then delete the marker line ...

python -m gtcc init-db
python -m gtcc create-user you@example.com
python -m gtcc check          # configuration and adapter health
python -m gtcc serve          # http://127.0.0.1:8000
```

Tests:

```bash
pytest                       # 171
pytest -k TheSevenRefusals   # the critical risk tests
```

## Risk configuration is yours

There is no default risk-per-trade in this codebase. A default would be a
recommendation, and this platform is not qualified to make one.
`config/risk.example.yaml` shows the file format and carries a marker
line; `load_limits` refuses to read it while that marker is present, so a
deployment cannot quietly run on numbers chosen by a stranger.

## Layout

```
src/gtcc/
  domain/        value objects, Decimal money, instrument arithmetic
  data/          validation: GOOD / DEGRADED / INVALID
  risk/          limits, state, sizing, the deterministic engine
  execution/     paper fill engine, order state machine
  adapters/      broker and market-data interfaces, paper broker, CSV replay
  ai/            Grok input and output contract
  storage/       SQLAlchemy models, Alembic migrations, repositories
  api/           FastAPI app, auth, routes
  web/           dashboard templates and stylesheet
  runtime.py     the application container and the single submission path
docs/            architecture, schema, adapters, pipeline, risk, AI, paper, testing
config/          risk.example.yaml
migrations/      Alembic
tests/           171 tests
```

## Documentation

| Document | Covers |
|---|---|
| `docs/01-architecture.md` | Pipeline mapping, layering, technology choices |
| `docs/02-database-schema.md` | All 20 tables and why each column exists |
| `docs/03-adapter-interface.md` | The broker/data interface and planned venues |
| `docs/04-market-data-pipeline.md` | Validation, timeframes, look-ahead prevention |
| `docs/05-risk-engine.md` | The 33 checks, breakers, sizing, reward:risk |
| `docs/06-grok-contract.md` | Structured input, validated output, rejection rules |
| `docs/07-paper-trading.md` | What is modelled, and what is explicitly not |
| `docs/08-testing-strategy.md` | The seven critical tests and the rest |
| `docs/09-credentials-and-services.md` | Keys, sandboxes, what must be verified |
| `docs/10-implementation-checklist.md` | Phase by phase, with known limitations |

## Live trading

Disabled. Reaching it needs, independently: `GTCC_LIVE_TRADING=true` in
the server environment, the confirmation phrase typed exactly, no
circuit breaker blocking live execution, and a human decision. Live mode
disables itself when market data goes stale, the broker disconnects,
order state cannot be reconciled, or a loss limit is reached.

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
