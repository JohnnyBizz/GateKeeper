# Implementation checklist

`[x]` shipped and tested. `[ ]` not started. Phase in brackets.

## Phase 1 — foundation (complete)

- [x] Project structure, packaging, pinned dependencies
- [x] Domain model: enums, Decimal money, instrument specs per asset class
- [x] Settings with PAPER / live-off / automatic-off defaults and live gates
- [x] Structured JSON logging with credential redaction and correlation ids
- [x] Data validation gate: GOOD / DEGRADED / INVALID
- [x] Risk limits loader that refuses the shipped example file
- [x] Risk state with circuit breakers persisted across restarts
- [x] Position sizing: linear, tick-valued, quoted
- [x] **Deterministic risk engine, 36 checks, full audit output**
- [x] Per-market limit overrides for leveraged products
- [x] Broker and market-data adapter interfaces with capability negotiation
- [x] Replay data adapter that will not invent a quote
- [x] Paper broker with a real ledger
- [x] Paper fill engine: spread, slippage, depth, partials, fees, latency, sessions
- [x] Order management state machine and broker reconciliation
- [x] Grok input and output schemas with hallucination rejection
- [x] Database: 20 tables, Alembic migration, UTC-enforced timestamps
- [x] API: Argon2id auth, server-side sessions, CSRF, rate limiting, audit log
- [x] Security headers and a strict content security policy
- [x] Dashboard shell: ten routed pages, honest empty states
- [x] Operator controls: kill switch, pause, close position, reconcile, mode switch
- [x] CLI: serve, init-db, create-user, check
- [x] 313 tests including the eight critical refusals
- [x] Architecture, schema, adapter, pipeline, risk, AI, paper, testing docs

## Safety repair pass (2026-10-01)

Findings from an external review of Phase 1, all closed:

- [x] Live activation separated into deployment permission and runtime
      arming; the confirmation phrase is no longer a configuration field
- [x] Settings frozen, so a validator cannot be bypassed by assignment
- [x] `GTCC_MODE=LIVE` refused: a process cannot boot into live
- [x] Latched safety breaker that recovery does not clear
- [x] Non-finite Decimals refused in `D()` and at every domain boundary
- [x] Tick rounding made directional per order role; paper fills clamp
      to the limit after rounding rather than before
- [x] `quantize_down` refuses negatives, with `quantize_floor` for a
      true floor
- [x] Settings output changed from a denylist to an allowlist; URL
      credentials are never rendered, and the log filter strips them too
- [x] Strict fixtures: symbol-specific instruments, a broker that
      refuses unarranged calls, and a symbol-consistency check in the
      engine
- [x] Alembic migrations tested from an empty database, including drift
      and a persistence flow against the migrated schema
- [x] Auth, CSRF and role denial tested per endpoint per identity
- [x] Production cookie flags, session fixation and logout invalidation
- [x] Failure-injection adapters for every venue failure mode
- [x] Numeric configuration validated at startup
- [x] Dead validator branch removed

## Phase 2 — real data and analysis

- [ ] First live data adapter against a sandbox, with its own test suite
- [ ] Feature engine: EMA, SMA, VWAP, anchored VWAP, RSI, MACD, ATR, ADX,
      Bollinger, stochastic, ROC, OBV, relative volume, historical volatility
- [ ] Market structure: HH/HL/LH/LL, BOS, CHoCH, ranges, S/R, supply/demand,
      sweeps, fair value gaps, each storing the rule that found it
- [ ] Regime classifier, stored with every trade
- [ ] Market scanner and its columns
- [ ] Next.js terminal with charts, against the existing JSON API
- [ ] Strategy framework and the first strategy module
- [ ] Order book and order-flow analysis where a feed genuinely exists

## Phase 3 — Grok and the agents

- [ ] xAI client with timeout, retry and strict validation
- [ ] Agents: structure, technical, order flow, derivatives, macro, news, quant
- [ ] Scalping, day and swing agents with their timeframe bands
- [ ] Risk Manager agent (advisory; the engine still decides)
- [ ] Confluence scoring with transparent configurable weights
- [ ] Agent Room page showing disagreement rather than hiding it

## Phase 4 — measurement

- [ ] Event-driven backtester with a **test proving** it cannot see the future
- [ ] In-sample / validation / out-of-sample splits and walk-forward
- [ ] Journal writer, including rejected setups
- [ ] Performance metrics and the breakdowns in section 21
- [ ] Analytics and Backtest Lab pages
- [ ] Parameter sensitivity and fee/slippage stress testing
- [ ] Anti-overfitting flags for edges that vanish under small changes

## Phase 5 — paper forward

- [ ] Position manager: trailing stops, partial exits, bracket orders
- [ ] Notifications
- [ ] Daily report
- [ ] Reconciliation on a schedule, with alerts
- [ ] Metrics endpoint and dashboards for the risk engine's own behaviour

## Phase 6 — sandbox brokers

- [ ] Alpaca paper adapter
- [ ] OANDA practice adapter
- [ ] Crypto testnet adapter
- [ ] Reconciliation against each, proven under induced failures

## Phase 7 — forward testing

- [ ] Extended paper run with a pre-registered evaluation
- [ ] Calibration study before any confidence number is called a probability
- [ ] Live readiness review: every item in section 30 demonstrated

Live infrastructure is enabled only after that review and an explicit
authorisation from the account owner.

## Known limitations today

1. No margin model in the paper broker. Documented in `07-paper-trading.md`.
2. Rate limiting is per-process. Redis is needed behind more than one instance.
3. Risk state persistence writes on change and reads on startup, so the
   loss tally and its breakers survive a restart. It is not guarded
   against two processes writing the same account concurrently; a single
   instance is fine, more than one needs row locking.
4. **The execution latch is in-process only.** A restart clears it. That
   is deliberate for live arming, which must never resume by itself, but
   it means a latched breaker does not currently survive a restart
   either. The safe direction would be to persist the latch as well, so
   a crash during a safety event cannot be cleared by restarting; that
   is not yet implemented and is the first item of the next pass.
5. No real market data adapter, so the platform cannot currently trade
   anything. This is the correct state for Phase 1 and the first Phase 2 item.
6. Journal rows are not yet written by the runtime; the table and schema exist.
7. The paper broker's `buying_power` equals equity, because no margin
   model exists. For leveraged instruments the `BUYING_POWER` check is
   therefore weaker than it reads.
