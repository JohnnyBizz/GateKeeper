# Broker and market-data adapters

Specification section 34. Two abstract classes in `adapters/base.py`;
everything above them speaks only platform types, so no vendor SDK is
imported outside this layer and a venue is swappable.

## MarketDataAdapter

| Method | Required | Notes |
|---|---|---|
| `get_instrument(symbol)` | yes | Venue-reported tick size, lot step, contract size, fees. An adapter that cannot supply this must not offer the symbol: sizing without a real tick size is guesswork |
| `get_quote(symbol)` | yes | Top of book |
| `get_bars(symbol, timeframe, limit, start, end)` | yes | OHLCV |
| `health()` | yes | Whether this connection may be traded on now |
| `list_symbols()` | no | |
| `get_order_book(symbol, depth)` | no | |
| `get_trades(symbol, limit)` | no | |
| `get_funding_rate(symbol)` | no | |
| `get_open_interest(symbol)` | no | |
| `stream_market_data(symbols, handler)` | no | |

## BrokerAdapter

`get_account`, `get_balance`, `get_positions`, `place_order`,
`cancel_order`, `get_order`, `get_orders`, `get_fills`, `close_position`,
`health`.

`place_order(request, *, quantity)` takes the approved quantity as a
separate argument from the request. That is deliberate: the risk engine
may have reduced the size, and passing it separately means an adapter
cannot accidentally use the unapproved number from the request body.

## Capability negotiation, not empty returns

An adapter declares a `frozenset[Capability]`. Asking for something
outside it raises `FeatureUnavailable`, which is **not an error
condition** — it is the correct answer when a venue does not publish a
feed. Section 7 requires the caller to mark that analysis unavailable
rather than substitute.

The failure mode this avoids: `get_order_book` returning an empty book,
which reads downstream as "no imbalance" rather than "no data". For the
same reason `OrderBook.imbalance` returns `None` when either side is
empty.

## Adapters today

| Adapter | Kind | Notes |
|---|---|---|
| `ReplayAdapter` | data | CSV recordings. **Refuses to produce a quote from a bar close** — a close is not a bid and a spread inferred from one is a number nobody observed. Serves quotes only when the file has `bid`/`ask` columns. A cursor hides bars after the replay position, which is what stops a backtest reading its own future |
| `PaperBroker` | broker | Real ledger: average entry, realised P&L on reduction, venue fees |
| `OandaDataAdapter` | data | OANDA v20 quotes, candles and instrument specifications. Every instrument detail comes from OANDA's own endpoint; nothing is hardcoded. `complete` maps to the platform's `closed`, so a forming candle cannot reach the indicators. An untradeable instrument raises rather than returning a stale price |
| `OandaBroker` | broker | Account, positions and orders. Direction is the sign of `units` and that translation happens in one place. The protective stop and first target are attached as OANDA's own `stopLossOnFill` and `takeProfitOnFill`, so they survive this process dying |

### A documented uncertainty in the OANDA adapter

It was written without access to OANDA's API documentation, which the
build environment could not reach. The endpoint paths come from
secondary sources and the response field names from the API as
understood at the time. Three things contain that risk:

* Every response is parsed strictly. A missing field raises
  `OandaSchemaError` naming the endpoint and the field, never a default.
* No instrument detail is hardcoded, so a wrong field name produces a
  loud error rather than a quietly wrong tick size.
* `python -m gtcc oanda-check` probes every endpoint with the owner's
  own token and reports which fields were actually present. It is
  read-only and must be run once before the adapter is trusted.

The live host is gated in code: constructing a live-pointing adapter
requires both deployment permission and a runtime arming. A practice
token against the live host fails harmlessly; a live token would not,
which is why the check is not left to configuration.

## Planned adapters and their sandboxes

| Venue | Kind | Sandbox | Verify before building |
|---|---|---|---|
| Alpaca | US equities, crypto | Yes, paper API | Whether the account's data entitlement covers the bars needed |
| Interactive Brokers | equities, futures, forex | Yes, paper TWS | Gateway session handling; market-data subscriptions are separate purchases |
| OANDA | forex | Yes, practice | Instrument financing and the exact pip conventions per pair |
| Binance / Coinbase | crypto | Testnet / sandbox | Jurisdiction eligibility; whether futures are available to the account at all |
| Polygon / Databento | market data | Trial tiers | Licence terms for storing and redistributing bars |

None of this is assumed. Section 44 item 14 asks that regulatory and
account limitations be verified rather than assumed, and each row above
is a question to answer with that venue's current documentation before a
line of adapter code is written.

## Writing a new adapter

1. Subclass, set `name` and `capabilities`.
2. Map venue symbols to `InstrumentSpec` from the venue's own metadata
   endpoint. Never hard-code a tick size.
3. Translate venue order states onto `OrderStatus`. If a state cannot be
   mapped, raise rather than guess: an unmappable state is exactly the
   condition section 42 says must stop live trading.
4. Make `health()` honest. It gates every order.
5. Credentials come from settings, never from a constructor default.
