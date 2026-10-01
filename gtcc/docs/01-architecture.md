# Architecture

## The pipeline

Specification section 1 defines the decision path. This is how it maps
onto modules, and which parts exist today.

```
MARKET DATA            adapters/        MarketDataAdapter         Phase 1 (replay)
      |
DATA VALIDATION        data/quality.py  DataQualityReport          Phase 1  DONE
      |
FEATURE ENGINE         features/        indicators, VWAP, ATR      Phase 2
      |
STRATEGY ENGINES       strategies/      independent modules        Phase 2
      |
ANALYSIS AGENTS        agents/          structure, flow, macro     Phase 3
      |
GROK SYNTHESIS         ai/              MarketSnapshot -> AIDecision  schema Phase 1, client Phase 3
      |
RISK ENGINE            risk/engine.py   RiskEngine.evaluate        Phase 1  DONE
      |
ORDER MANAGEMENT       execution/oms.py OrderManager               Phase 1  DONE
      |
BROKER / EXCHANGE      adapters/        BrokerAdapter              Phase 1 (paper)
      |
TRADE JOURNAL          storage/models   TradeJournalEntry          table Phase 1, writer Phase 4
      |
PERFORMANCE DATABASE   storage/models   PerformanceSnapshot        table Phase 1, metrics Phase 4
```

## Configuration versus state

These are separate on purpose, and conflating them was the worst defect
found in review.

**Settings** describe the deployment: where the database is, what this
server is permitted to offer. They are frozen, so a validator cannot be
walked around by assigning a field afterwards, which an API route was
doing. Settings survive restarts.

**ExecutionState** describes what the process is doing right now: the
mode in force, whether a person armed live execution, whether a safety
breaker has latched. It lives in memory and starts fresh on every
process start. No configuration value can produce an armed state.

## The one invariant

Every order reaches a broker through `TradingRuntime.submit`, and that
method calls `RiskEngine.evaluate` before anything else. There is no
second path, no bypass flag, and no "trusted caller". The API, the
dashboard, a strategy and an operator all enter the same way.

`RiskEngine` is pure: no I/O, no network, no clock of its own, no model
call. It receives a `RiskContext` assembled by the caller and returns a
`RiskVerdict`. Give it the same inputs twice and it answers twice the
same, which is what makes a refusal defensible months later.

Grok is not in that path. Its output arrives as advisory data and the
risk engine does not read it at all.

## Layering

```
api/            HTTP, auth, CSRF, serialisation. No trading logic.
runtime.py      The application container and the single submission path.
risk/safety.py  Runtime execution state: live arming and the latched breaker.
risk/           Deterministic authority. Pure functions over value objects.
execution/      Order lifecycle and paper fills.
adapters/       Venue translation. The only layer that knows a vendor.
ai/             The Grok contract: structured input, validated output.
data/           Validation gates.
domain/         Value objects and arithmetic. Depends on nothing above.
storage/        SQLAlchemy models and migrations.
```

Dependencies point downward only. `domain` imports nothing from the rest
of the package, and `risk` imports no adapter. That is what lets the
engine be tested without a broker, a database or a network.

## Why a modular monolith

One process, clear module boundaries, one database. Section 33 asks not
to introduce microservices in the first version and the reasoning holds:
the risk engine and the order manager must agree about state, and the
cheapest way to guarantee that is a function call rather than a network
hop. The boundaries above are where services would later be cut, if
throughput ever demands it.

## Decimal everywhere

Prices, sizes and money are `Decimal`. Floats lose cents over long sums
and produce lot sizes a venue rejects. `domain/money.py` converts
through `repr` so `D(0.1)` is exactly `0.1`.

## Timezones

Every timestamp is timezone-aware UTC. The `UtcDateTime` column type
enforces it at the database boundary: naive values are refused going in
and stamped UTC coming out, because PostgreSQL preserves offsets and
SQLite silently discards them. A platform that compares a naive
timestamp against an aware one crashes in development and, worse,
computes a wrong staleness in production.

## Technology

| Concern   | Choice                  | Why |
|-----------|-------------------------|-----|
| API       | FastAPI                 | Typed request validation, JSON-first, async-capable |
| ORM       | SQLAlchemy 2.0, sync    | Sync endpoints in a threadpool avoid async-DB foot-guns |
| Database  | PostgreSQL; SQLite dev  | `Numeric` money, real timestamptz, mature migrations |
| Migrations| Alembic, batch mode     | One migration file works on both engines |
| Passwords | Argon2id                | Memory-hard, current best practice |
| Frontend  | Jinja now, Next.js Ph.2 | Phase 1 pages are server state; a build step buys nothing yet |
| Quant     | pandas, NumPy           | Phase 2, for the feature engine |

### On the front end

The specification suggests Next.js and TypeScript. Phase 1 renders
server-side instead, deliberately: every figure on the Phase 1 pages is
server state, the API is already JSON-first, and a React toolchain adds
surface without adding capability until the scanner and charts exist.
Phase 2 introduces the Next.js terminal against the same endpoints, and
the templates are the only thing thrown away.
