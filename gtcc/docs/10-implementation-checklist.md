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
- [x] 625 tests including the eight critical refusals
- [x] `gtcc risk`, which states every limit in money, names the ceiling
      that actually binds each market, and sizes one real trade through
      the engine rather than describing what it would do
- [x] `config/risk.yaml` tracked, so a change to a limit has the same
      history as a change to the engine
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
- [x] Execution latch persisted, so a crash during a safety event cannot
      be cleared by restarting, while live arming still never resumes

## Phase 2 — real data and analysis

- [x] Feature engine: EMA, SMA, VWAP, anchored VWAP, RSI, MACD, ATR, ADX,
      +DI/-DI, Bollinger, stochastic, ROC, OBV, volume averages, relative
      volume, historical volatility
- [x] Market structure: swings, HH/HL/LH/LL, BOS, CHoCH, ranges, breakouts
      and failed breakouts, equal highs and lows, sweeps, fair value gaps,
      support and resistance, each storing the rule and parameters that
      found it
- [x] Regime classifier with its thresholds recorded alongside each reading
- [x] Strategy framework with validation-status and regime gates, and the
      first strategy module
- [x] First live data adapter against a sandbox: OANDA v20 practice,
      with 59 tests and a read-only verifier the owner runs themselves
      **(awaiting one `oanda-check` run against a real token to confirm
      the response field names)**
- [x] Market scanner: a row per symbol **requested**, so an unreadable
      symbol is reported rather than dropped, with a request budget whose
      exhaustion is stated instead of looking like an empty market
- [x] Scanner and Journal pages on the server-rendered dashboard, each
      showing what it could not read as prominently as what it could
- [ ] Next.js terminal with charts, against the existing JSON API
- [ ] Order book and order-flow analysis where a feed genuinely exists
- [x] Journal writer: every considered setup, taken or refused, written by
      the single submission path, with the risk verdict and the analysis
      context attached. A read-only endpoint, and no way to edit a row
      afterwards

## Phase 3 — Grok and the agents

- [ ] xAI client with timeout, retry and strict validation
- [ ] Agents: structure, technical, order flow, derivatives, macro, news, quant
- [ ] Scalping, day and swing agents with their timeframe bands
- [ ] Risk Manager agent (advisory; the engine still decides)
- [ ] Confluence scoring with transparent configurable weights
- [ ] Agent Room page showing disagreement rather than hiding it

## Phase 4 — measurement

- [x] Event-driven backtester with **tests proving** it cannot see the
      future: a decision fills no earlier than the next bar, a prefix run
      gives the same trades as a full run, and an ambiguous bar resolves as
      a stop. All three verified by reintroducing the bug
- [x] Chronological in-sample / validation / out-of-sample splits and
      walk-forward windows, never shuffled, with a ledger counting how often
      held-out data has been looked at
- [ ] Journal writer, including rejected setups
- [x] Performance metrics that carry their own reliability: untrustworthy
      below 30 closed trades, caveat printed before the statistics, and
      nothing uncomputable reported as zero
- [ ] The remaining section 21 breakdowns (by regime, session, strategy)
- [ ] Analytics and Backtest Lab pages
- [x] Parameter sensitivity and fee/slippage stress testing, with the cost
      headroom reported as a multiple whether or not it trips a threshold
- [x] Anti-overfitting flags: out-of-sample degradation, dependence on one
      trade, profit confined to one period, fragility to costs and to
      parameters. A clean report states in words that it is not evidence the
      strategy works, and CI greps the module for endorsement language

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

1. **No strategy has been validated.** `trend_continuation` is UNTESTED
   and the framework refuses to let it propose anything in paper or
   live. That is correct, not a bug: its edge has never been measured.
   The backtester that could change that arrives in Phase 4.
2. **OANDA's response field names are unverified.** The API documentation
   was unreachable from the build container, so they are assumptions.
   They are parsed strictly, so a wrong one fails loudly naming the
   endpoint and field rather than substituting a default, and
   `gtcc oanda-check` confirms them against a real account. That run has
   not happened.
3. No margin model in the paper broker. Documented in `07-paper-trading.md`.
   Its `buying_power` therefore equals equity, which makes the
   `BUYING_POWER` check weaker than it reads for leveraged instruments.
4. Rate limiting is per-process. Redis is needed behind more than one instance.
5. Risk state persistence writes on change and reads on startup, so the
   loss tally and its breakers survive a restart. It is not guarded
   against two processes writing the same account concurrently; a single
   instance is fine, more than one needs row locking.
6. The execution latch is persisted and survives a restart; live arming
   is not and does not. See `docs/05-risk-engine.md` for why those two
   rules are deliberately opposite.
7. Journal rows carry no outcome yet: `realised_pnl`, `r_multiple`, MFE
   and MAE stay NULL because nothing closes a position and measures it.
   The row is written when the setup is considered; the result has to be
   filled in by the position tracking that arrives with Phase 4.
8. Under the configured limits, a forex position's face value is capped
   by the market exposure limit well before the per-trade risk budget is
   reached, so forex trades risk materially less than `max_risk_pct`
   suggests. This is a deliberate choice, not an oversight —
   `gtcc risk` prints the crossover — but it means the headline
   risk-per-trade number is a ceiling rarely reached on that market.
