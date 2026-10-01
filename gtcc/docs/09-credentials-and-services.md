# Credentials, external services and what must be verified

Specification section 44, items 12 to 14.

## Nothing is required to run Phase 1

The platform starts with no credential at all: paper broker, recorded
data, no model calls. `GTCC_SECRET_KEY` is the only value you must set,
and only outside development.

## Environment variables

| Variable | Needed for | Notes |
|---|---|---|
| `GTCC_SECRET_KEY` | Sessions | 32+ random characters. `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `GTCC_DATABASE_URL` | Storage | Defaults to SQLite. PostgreSQL in production |
| `GTCC_MODE` | Mode | `BACKTEST` or `PAPER`. **`LIVE` is refused**: live is armed at runtime, never configured |
| `GTCC_ALLOW_LIVE_TRADING` | Live | Deployment permission only. Lets the server OFFER live mode; arms nothing |
| `GTCC_RISK_CONFIG_PATH` | Risk | Defaults to `config/risk.yaml` |
| `GTCC_GROK_API_KEY` | Phase 3 | xAI key |
| `GTCC_GROK_MODEL` | Phase 3 | **No default.** Set it from current xAI documentation |
| `GTCC_REDIS_URL` | Scale | Rate limiting across more than one instance |

Secrets are read from the environment or a secret store, never from a
committed file. `Settings.public_view()` is the only shape that reaches
a response or a page, and it is an **allowlist**: a new setting is
invisible until somebody adds it to `PUBLIC_FIELDS`. Connection URLs are
described by scheme, host, port and path, with the userinfo segment
never read out of the parsed result, so a password inside
`postgresql://user:pass@host/db` cannot be rendered. The log filter
strips the same userinfo segment from any string it emits.

There is deliberately **no** environment variable carrying the live
confirmation phrase. One used to exist, and it meant a stale deploy
environment satisfied the human-confirmation gate at startup.

## Services to evaluate

| Service | For | Sandbox | Cost shape |
|---|---|---|---|
| Alpaca | US equities and crypto, data + execution | Paper API | Free tier; paid data tiers |
| Interactive Brokers | Equities, futures, forex, options | Paper TWS | Commission per trade; market data billed separately per subscription |
| OANDA | Forex | Practice account | Spread-based |
| Binance | Crypto spot and futures | Testnet | Maker/taker fees |
| Coinbase Advanced | Crypto spot | Sandbox | Maker/taker fees |
| Polygon | Equities and options data | Trial | Monthly, by tier |
| Databento | Futures and equities, historical | Trial | Per dataset |
| Trading Economics / FMP | Economic calendar | Trial | Monthly |
| xAI | Grok | No free sandbox | Per token |

## What must be verified, not assumed

Section 44 item 14, and these are questions rather than answers:

1. **Account eligibility by jurisdiction.** Crypto derivatives are
   unavailable to retail accounts in several jurisdictions. Whether this
   account can trade them at all is a question for the venue.
2. **Pattern day trader rules** on margin US equity accounts below
   $25,000, which constrain intraday round trips in a way no strategy
   parameter can work around.
3. **Options approval levels.** Spreads need a level most new accounts do
   not have. Section 2 defers options anyway.
4. **Data licensing.** Whether a provider's terms permit storing bars,
   and whether redistribution rules affect anything the dashboard shows.
5. **API key permissions.** Section 29 asks for keys with trade
   permission and **without** withdrawal permission wherever the venue
   supports that split. Which venues support it, and how, must be read
   per venue. `broker_connections.withdrawal_permission` records what the
   venue reports.
6. **Rate limits and weight systems.** Binance weights requests rather
   than counting them; a naive poller gets banned.
7. **Futures contract rolls and expiries**, which change the symbol a
   strategy is trading underneath it.
8. **Pocket Option.** Section 3: no official execution API is assumed to
   exist, and the integration stays analysis and signal only. No private
   endpoints, no credential interception, no anti-bot circumvention.
   Automated execution waits for a documented, authorised integration
   that can actually be verified.

## Secret rotation

Sessions are server-side rows, so rotating `GTCC_SECRET_KEY` invalidates
every cookie and nothing else. Venue keys live in the environment and are
named by `broker_connections.credential_ref`, so rotating one is a
secret-store change plus a restart, with no database migration.
