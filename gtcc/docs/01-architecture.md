# Architecture

## The pipeline

Specification section 1 defines the decision path. This is how it maps
onto modules, and which parts exist today.

```
MARKET DATA            adapters/        MarketDataAdapter        Phase 1 replay, Phase 2 OANDA
      |
DATA VALIDATION        data/quality.py  DataQualityReport          Phase 1  DONE
      |
FEATURE ENGINE         features/        indicators, VWAP, ATR      Phase 2  DONE
      |
MARKET STRUCTURE       structure/       Detection + its rule       Phase 2  DONE
      |
STRATEGY ENGINES       strategies/      independent modules        Phase 2  DONE
      |
SCANNER                scanner/         ScanRow per symbol asked   Phase 2  DONE
      |
ANALYSIS AGENTS        agents/          structure, flow, macro     Phase 3
      |
GROK SYNTHESIS         ai/              MarketSnapshot -> AIDecision  schema Phase 1, client Phase 3
      |
RISK ENGINE            risk/engine.py   RiskEngine.evaluate        Phase 1  DONE
                       ^ the only path to a broker. The scanner sits above
                         this and holds no broker, so it cannot reach one.
      |
ORDER MANAGEMENT       execution/oms.py OrderManager               Phase 1  DONE
      |
BROKER / EXCHANGE      adapters/        BrokerAdapter              Phase 1 (paper)
      |
TRADE JOURNAL          journal/         every considered setup     Phase 2  DONE
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

## The scanner reports absence as loudly as presence

`scanner/` reads a universe of symbols and returns one `ScanRow` per
symbol **requested**, never one per symbol that happened to work. Six
statuses, and the distinctions between them are the point:

| Status | Means |
|---|---|
| `SCANNED` | analysed, data good |
| `DEGRADED` | analysed, data carried warnings |
| `UNAVAILABLE` | the venue does not list this symbol — a universe problem |
| `FAILED` | the venue did not answer — an operational problem |
| `REFUSED` | the data arrived and is not fit to analyse |
| `NOT_ATTEMPTED` | the request budget ran out before reaching it |

A scanner that drops what it could not read converts "I could not look at
these 28 symbols" into "there is nothing in these 28 symbols". Those are
opposite statements, and the second one is the one that makes somebody
stop watching a market. `ScanResult.summary()` therefore states the
counts, and says in words that a truncated scan is not a finding.

Every measurement on a row is optional, and `None` means "not computed".
None of them fall back to zero: a relative volume of 0.00 is a claim that
the market is dead, and a warm-up window is not that claim. The text
renderer prints a dash. The JSON serialises `null`.

`min_history` is enforced here as a hard refusal rather than passed to the
quality checker and ignored. The structure engine's own floor is much
lower — enough bars for the algorithm to run — and it will return a trend
of `UNCLEAR` from five bars. `UNCLEAR` reads as "looked, saw nothing
definite", which a five-bar series has not earned.

There is deliberately no composite score. Ranking is by one named key at a
time, and a row with no measurement sorts after every row that has one
rather than being treated as a zero. Blending relative volume, volatility
and conviction into a single number would produce an authoritative-looking
order whose weights nobody chose, which section 14 forbids for exactly
that reason.

The scanner holds a market-data adapter and the strategy registry. It has
no broker, no runtime and no risk engine, so there is no path from a scan
to an order; CI asserts all four.


## The journal keeps the refusals

`TradingRuntime.submit` writes a row on every exit path, and CI counts the
returns against the writes so a new path cannot skip it. A refusal is a
row with the verdict attached, including the checks that *passed* —
because "why was this allowed" is a question worth being able to answer
about a loss, and storing only failures would make rejections auditable
and approvals not.

A journal of taken trades can only answer "were my trades any good". The
question worth answering is "were my refusals right", and that one is
unanswerable unless the refusal was written down at the time. There is no
endpoint that creates or edits a row: a journal somebody can revise
afterwards is not evidence.

Three distinctions the row preserves:

**Refused by risk vs refused by the venue.** Risk approving an order that
the venue then rejects is not a risk refusal. Recording it as one would
blame the limits for a broker problem and corrupt any later study of them.

**Absent context vs empty context.** A caller that passed no structure
report leaves `{"_not_supplied": true}`, not `{}`. An empty dict reads as
"the structure engine looked and found nothing", which is a claim about
the market rather than about the caller.

**Unknown vs zero.** Fees on a setup that never reached a venue are NULL,
not 0.00 — a zero would average into every later cost statistic as a free
trade. Realised P&L stays NULL until something closes the position.

### A failed journal write is handled asymmetrically

A refusal that cannot be journalled logs an error and trading continues:
nothing is at the venue, so the account is still accountable and only the
analysis record is poorer.

A **placed** order that cannot be journalled latches the breaker. There
is now a position at a venue that the platform cannot explain, the next
reconciliation will find an order it has no row for, and an account state
that cannot be reliably determined is exactly the condition section 42
says must stop trading. Both halves have a test, and both were confirmed
by inverting the condition and watching the matching test fail.
