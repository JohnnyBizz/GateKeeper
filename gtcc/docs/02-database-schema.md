# Database schema

Twenty tables, created by one Alembic migration. Money is `Numeric(28,10)`;
timestamps are UTC-enforced via `UtcDateTime`.

## Identity and audit

| Table | Purpose | Notes |
|---|---|---|
| `users` | Accounts | Argon2id hash, failed-login counter, lockout timestamp |
| `sessions` | Server-side sessions | Opaque id, CSRF token, revocable; the cookie carries only a signed id |
| `audit_log` | Who did what | Append-only by convention; correlation id ties rows to log lines. Records `control.live_armed`, `control.live_arm_refused`, `control.breaker_reset`, `control.breaker_reset_refused`, `control.kill_switch`, `control.mode_switch` and `order.submit`, each with the acting user |

## Accounts and venues

| Table | Purpose | Notes |
|---|---|---|
| `accounts` | Trading accounts | Mode and currency per account |
| `broker_connections` | Configured venues | **Stores no credential.** `credential_ref` names the secret; `withdrawal_permission` records what the venue says the key can do |
| `instruments` | Contract specifications | Tick size, lot step, contract size, tick value, pip size, fees, session |

## Orders and positions

| Table | Purpose | Notes |
|---|---|---|
| `orders` | Order lifecycle | `risk_verdict` JSON holds every check that was evaluated |
| `fills` | Confirmed executions | Unique on the venue's own fill id, so a replayed webhook cannot double-count |
| `positions` | Net positions | Signed quantity; positive long, negative short |

## The journal

`trades` is specification section 24: **every considered setup, not only
the taken ones.** A refused setup is a row with `outcome='REJECTED_BY_RISK'`
and the verdict attached, which is what makes it possible to ask later
whether the refusals were right. Columns cover the plan (entry, stop,
targets, size, risk, R:R), the outcome (fills, fees, slippage, P&L, R
multiple, MFE, MAE, exit reason), and the context (regime, session, data
quality, agent outputs, AI decision, risk verdict, indicators, structure,
news, macro).

## Risk

| Table | Purpose | Notes |
|---|---|---|
| `risk_events` | Breaker trips, kill-switch flips | Severity and acknowledgement |
| `risk_state` | Persisted tally | **A restart must not clear a tripped breaker.** A crash is not a fresh trading day |

## Research tables

`candles`, `strategies`, `agent_analyses`, `ai_decisions`, `news_items`,
`economic_events`, `backtests`, `performance_metrics`. Created now so the
migration history stays linear and Phase 2 onward has somewhere to write
without a schema change.

Two worth calling out:

- `strategies.validation_status` starts `UNTESTED`. Section 25 forbids
  enabling anything that has not passed out-of-sample testing.
- `backtests.fingerprint` hashes code and data, so a result can be tied
  to what produced it. A metric with no fingerprint is an anecdote.
- `news_items` keeps source, URL and timestamp on every row. A summary
  without a link back to what was summarised is not usable evidence.

## Migrations

```
GTCC_DATABASE_URL=postgresql+psycopg://... alembic upgrade head
```

`alembic check` reports no drift between the models and the migration.
`render_as_batch` is set for SQLite so one migration file serves both
engines.
