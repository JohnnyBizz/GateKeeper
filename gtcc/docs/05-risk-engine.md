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

## The 36 checks

**System gates** — `EXECUTION_NOT_TRIPPED`, `LIVE_MODE_PERMITTED`,
`SYMBOL_CONSISTENT`, `KILL_SWITCH`, `TRADING_NOT_PAUSED`,
`SYMBOL_ENABLED`, `STRATEGY_ENABLED`, `MARKET_ENABLED`,
`BROKER_HEALTHY`, `ACCOUNT_RECONCILED`, `EXPOSURE_KNOWN`,
`INSTRUMENT_KNOWN`.

`SYMBOL_CONSISTENT` requires the order, the contract specification and
the quote to name one symbol. An adapter mapping error would otherwise
size a position against the wrong instrument entirely, and a test
fixture that returned one specification for every symbol proved the
engine would not have noticed.

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

## Four ceilings cap face value, and only the smallest is real

`POSITION_NOTIONAL`, `ASSET_EXPOSURE`, `LEVERAGE` and `MARKET_EXPOSURE`
all limit the same quantity — the face value of one position — from
different directions. A configuration that raises one and leaves another
tight has not raised anything. Writing `max_position_notional_pct: 300`
for FOREX under a `max_market_exposure_pct: {FOREX: 40}` gives a 40%
ceiling and a file that claims 300.

`gtcc risk` reports the binding ceiling per market, by name, in money.
It is derived from `binding_face_value_cap()`, which takes the minimum of
the four, so the report cannot disagree with the engine about which one
applies.

## A cap and a risk budget are different ceilings

The per-trade risk limit answers "how much may I lose"; a face-value cap
answers "how much may I hold". The tighter one sizes the trade, and which
one that is depends on the stop distance: a tight stop needs a large
position to lose the full budget, so below some stop distance the cap
always wins and the trade risks **less** than `max_risk_pct`.

That crossover is `budget / face_cap` as a fraction of price. On a 0.5%
budget against a 25% cap it is 2% of the entry price: any stop tighter
than that is sized by the cap. `gtcc risk` prints it per market, because
an owner who does not know it will wonder why their losses are a fraction
of the number they signed off on.

## `approved_risk`, not `projected_risk`

`SizingResult.projected_risk` is the loss at the quantity the risk budget
asked for, before any ceiling applied. It is the correct input to the
`RISK_PER_TRADE` check and the wrong number to show a human: once a cap
shrinks the position, the loss shrinks with it.

`RiskVerdict.approved_risk` is the loss at the quantity actually
approved. Everything user-facing reads that — the API returns it as
`risk_at_stop`, and `gtcc risk` prints it. The API and the risk explainer
both reported the pre-cap figure at first, which overstated the loss on
every capped trade by however much the cap bit; there are tests at both
levels now.

A refusal that never reached sizing has `approved_risk is None`, not
zero. An unknown is not a zero.

Each ceiling that reduced the size records `reduced_to` on its
`CheckResult`, and `RiskVerdict.binding_limits` returns those in order,
tightest last. Callers name the binding limit from that field rather than
parsing the detail string.

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

## Two different breakers, and they are not the same thing

**Risk breakers** live in `RiskState`. They count settled results and
block new trades: daily loss, weekly loss, drawdown, consecutive
losses. Rolling the day clears the daily one.

**The execution latch** lives in `ExecutionState` and is a system-level
stop. It is what the README means by live mode disabling itself.

| Condition | Latches execution |
|---|---|
| Stale or invalid market data | yes |
| Broker reports unhealthy | yes |
| Order reconciliation finds a difference | yes |
| An open position cannot be valued | yes |
| Daily, weekly or drawdown loss breaker | yes |
| Kill switch | yes |
| Operator trip | yes |

Once latched:

* New submissions are refused in **every** mode, paper included. Paper
  results are the evidence base for whether any of this works, and
  recording them while the feed is stale would poison that record.
* **Recovery does not clear it.** The broker coming back on the next
  poll leaves the latch set, because whatever happened in between is
  unaccounted for.
* Clearing requires `reset_breaker` with an actor, and is refused while
  any dependency is still unhealthy.
* A reset leaves live **disarmed**. Resuming live trading after a
  safety event costs two deliberate human actions, not one.

Every trip and every reset is logged and written to the audit table
with the reason, the timestamp and, for a reset, who performed it.

### What survives a restart, and what must not

These two rules are deliberately opposite, and conflating them is how a
safety stop gets lost.

**A latched trip survives.** It is written to `execution_trips` as it
happens and read back on startup. The process may have died *because*
of whatever tripped it, and restarting is not a diagnosis. If the latch
cannot be read at startup the runtime latches defensively and says so,
because resuming on an unknown safety state is the one answer that is
definitely wrong.

**Live arming does not survive.** It is a decision a person made about
a running process and is never written anywhere. A process that came
back trading because a row said it was armed would be the original
configuration defect wearing a different hat.

A cleared trip is marked cleared, not deleted, so who cleared what and
when stays on the record. Repeating the same condition on every poll
updates nothing: the first occurrence is kept, because when the problem
started matters more than when it was noticed again.

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

## Live execution is armed, not configured

There is no setting that turns live trading on. `GTCC_ALLOW_LIVE_TRADING`
is deployment *permission*; the state that actually permits an order is
`ExecutionState.live_armed`, which is in memory, starts false on every
process start, and is set only by an authenticated owner passing the
exact phrase. The engine checks all three of mode, arming and latch,
and no two of them imply the third.

An earlier version made the phrase an environment-backed setting. A
process whose environment still carried it booted straight into a state
where live orders were permitted, with nobody present. That is the
defect this design exists to prevent, and
`test_an_environment_that_sets_the_old_variables_arms_nothing` sets
every one of those variables and asserts nothing is armed.

## What Grok cannot do

It cannot change a limit, cannot be consulted by the engine, cannot
place an order, and cannot arm or reset anything. `RiskEngine` does not
import anything from `ai/`, and a test greps the AI package for
`arm_live`, `reset_breaker` and any `gtcc.risk` import.
