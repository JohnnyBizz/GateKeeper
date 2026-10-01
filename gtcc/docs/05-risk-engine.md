# The deterministic risk engine

Specification section 16. This component has final authority. It is
ordinary Python, not a prompt.

## Shape

`RiskEngine.evaluate(request, context) -> RiskVerdict`

Pure. No I/O, no network, no clock, no model call, no globals. Every
input arrives in the `RiskContext` the caller assembled.

`RiskVerdict` carries the action (`ALLOW` / `REDUCE` / `REJECT`), the
approved quantity, the sizing result, the reward-to-risk breakdown, and
**every check that was evaluated, passed or failed**, each with its limit
and the observed value. Section 14 asks the platform to show exactly why
a setup scored what it did; the same applies more strongly to a refusal.

## The 33 checks

**System gates** — `LIVE_MODE_PERMITTED`, `KILL_SWITCH`,
`TRADING_NOT_PAUSED`, `SYMBOL_ENABLED`, `STRATEGY_ENABLED`,
`MARKET_ENABLED`, `BROKER_HEALTHY`, `ACCOUNT_RECONCILED`,
`EXPOSURE_KNOWN`.

**Market conditions** — `DATA_QUALITY`, `QUOTE_AVAILABLE`,
`SPREAD_WITHIN_LIMIT`, `SLIPPAGE_WITHIN_LIMIT`, `EVENT_BLACKOUT`.

**Trade structure** — `STOP_PRESENT`, `STOP_ON_CORRECT_SIDE`,
`STOP_DISTANCE`, `TARGET_PRESENT`, `REWARD_RISK`.

**Account breakers** — `DAILY_LOSS_LIMIT`, `WEEKLY_LOSS_LIMIT`,
`MAX_DRAWDOWN`, `CONSECUTIVE_LOSSES`, `MAX_OPEN_POSITIONS`.

**Size** — `POSITION_SIZEABLE`, `RISK_PER_TRADE`, `POSITION_NOTIONAL`,
`LEVERAGE`, `ASSET_EXPOSURE`, `CORRELATED_EXPOSURE`, `SECTOR_EXPOSURE`,
`MARKET_EXPOSURE`, `BUYING_POWER`.

A check can also be `SKIP`, and a skip is never silently a pass. The
clearest case: with no economic calendar connected, `EVENT_BLACKOUT`
reports SKIP with the detail "this is not evidence that no event is
scheduled".

## Size ceilings do not compound

Each notional ceiling is converted independently into the largest
quantity that fits under it, and the smallest of those wins. Scaling the
running quantity through each cap in turn would shrink the position once
per cap and undersize every trade. There is a test for this, because the
first implementation had the bug and the symptom was quiet: a plausible
number, half what it should have been.

## Per-market overrides

A single notional ceiling cannot cover cash equities and listed futures.
One ES contract is a quarter of a million dollars of face value against
a few thousand of margin, so a 25% cap makes the instrument untradeable.
`MarketOverride` lets a market carry its own notional, leverage and
per-asset exposure limits. Raising notional without raising concentration
would be incoherent rather than conservative, so a market that overrides
one and not the other gets the larger of the two.

## Reward to risk

Costs are charged against reward **and** added to risk:

```
net_reward = gross_reward - (fees + spread + slippage)
net_risk   = gross_risk   + (fees + spread + slippage)
ratio      = net_reward / net_risk
```

The **nearest** target is scored, not the furthest. Section 15 forbids
inventing a distant target to clear the minimum ratio, and scoring the
nearest one removes the incentive to try.

## Breakers

| Condition | Effect |
|---|---|
| Daily realised loss >= limit | New trades blocked for the rest of the day |
| Weekly realised loss >= limit | New trades blocked |
| Drawdown from high-water mark >= limit | **Live execution disabled** |
| Consecutive losses >= limit | New trades blocked |
| Kill switch | Everything stops, including live execution |

Breakers read **settled** results only. An unrealised number moves on its
own, and a breaker that trips on a wick trips at random.

Rolling the day clears the daily breaker. It does not clear the weekly or
the drawdown breaker: a new day is not a new week, and a drawdown is not
undone by the clock. The state is persisted, so a restart cannot look
like a fresh trading day.

## Position sizing

Risk capital is equity times the configured fraction. The size that risks
exactly that depends on the instrument, so the arithmetic lives on
`InstrumentSpec` and dispatches on asset class:

- **Linear** (equity, ETF, crypto spot and perpetual): one unit moves by
  the price difference, times contract size.
- **Tick-valued** (listed futures): one contract moves by a fixed cash
  amount per tick. A price difference alone is meaningless.
- **Quoted** (forex): the difference is in the quote currency and must be
  converted with a real rate. There is no default guess.

Rounding is always downward, and projected risk is recomputed from the
rounded quantity, because a size rounded up onto a lot grid can exceed
the limit the sizing was meant to respect.

## What Grok cannot do

It cannot change a limit, cannot be consulted by the engine, and cannot
place an order. `RiskEngine` does not import anything from `ai/`.
