# Recordings

CSV market data, one file per symbol and timeframe:
`<SYMBOL>_<timeframe>.csv`, for example `BTCUSDT_5m.csv`.

Required columns: `timestamp,open,high,low,close`
Optional: `volume,bid,ask,bid_size,ask_size`

Timestamps are ISO 8601 with an offset, or a Unix epoch in seconds or
milliseconds. They are the bar's **open** time.

Without `bid` and `ask` columns, `ReplayAdapter.get_quote` raises
`FeatureUnavailable`. That is deliberate: a bar close is not a bid, and a
spread inferred from one is a number nobody observed. Recordings are
gitignored because market data is often licensed.
