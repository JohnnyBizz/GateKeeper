# The Grok contract

Specification sections 12 and 36. Grok is an advisory input that receives
structured data and must answer in a fixed shape.

## Input

`MarketSnapshot` carries symbol, market, timestamp, current price, spread,
data quality, per-timeframe summaries, market structure, indicators,
volume, volatility, order flow, funding, open interest, liquidations,
correlation, news, macro events, portfolio exposure and strategy signals.

Two deliberate properties:

- **No narrative.** The model reasons over numbers the platform computed,
  not over prose the platform wrote for it.
- **Absence is stated, not omitted.** `unavailable_feeds` lists what we
  could not obtain, and the field itself is sent as `null`. The model is
  told what it does not have.

## Output

```json
{
  "decision": "WAIT",
  "direction": "NONE",
  "confidence": 0.63,
  "setup": "LIQUIDITY_SWEEP_RECLAIM",
  "entry_zone": null,
  "invalidation": null,
  "targets": [],
  "reasoning_summary": "...",
  "supporting_factors": [],
  "conflicting_factors": [],
  "missing_data": [],
  "risk_flags": []
}
```

`additionalProperties: false`. Every field required.

## Rejection, not repair

`validate_response` raises `AIResponseRejected` with **every** reason, and
the platform discards the answer. Rejected for:

- Unparseable JSON, a missing field, an unexpected field.
- Confidence outside [0, 1] or non-numeric.
- Decision and direction disagreeing. LONG with direction NONE is not a
  near miss, it is incoherent.
- An actionable decision with no entry zone, no invalidation or no target.
- Levels on the wrong side: a LONG invalidation above the entry zone, a
  LONG target below it.
- **Citing a feed the snapshot marked unavailable.** This is the
  hallucination check. A model claiming order-flow support when we told it
  order flow was missing has confabulated, and the parts that look
  reasonable deserve no more trust than the part that does not.
- Naming a feed in `missing_data` that this platform does not have.

An extra field is rejected rather than ignored for the same reason: a
model that invented a field has not followed the schema.

## Confidence is not a probability

`AIDecision.confidence_is_calibrated` is `False` and stays False until a
calibration study has been run against settled outcomes. Nothing in the
risk engine reads confidence at all. Section 12 is explicit and the flag
is there so nobody downstream forgets.

Calibration, when it happens, is a measurement: bucket predictions by
stated confidence, compare to realised win rate in each bucket, over a
sample large enough to mean something. Until that exists, confidence is
an opinion with a decimal point.

## Failure handling

Timeout, malformed JSON, schema violation or hallucination all produce
the same outcome: the output is discarded and the platform continues
without it. `require_grok_for_trades` defaults to `False`, so the
infrastructure runs when the model is unavailable. It never falls back to
assuming the model approved.

## Model identifier

`GTCC_GROK_MODEL` has **no default**. Model names go stale and a stale one
fails at the worst possible moment. The platform refuses to guess.
