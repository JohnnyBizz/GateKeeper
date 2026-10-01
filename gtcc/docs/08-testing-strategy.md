# Testing strategy

491 tests, all passing. `cd gtcc && pytest`.

## The seven that matter most

Specification section 41 names seven conditions that must refuse an
order. They live in `TestTheSevenRefusals`, one per method, named so a
failure says what broke:

| Condition | Test |
|---|---|
| Oversized position | `test_an_oversized_position_is_cut_or_refused` |
| Daily loss limit hit | `test_the_daily_loss_limit_blocks_new_orders` |
| Stale price | `test_a_stale_price_is_refused` |
| Broker disconnected | `test_a_broker_disconnect_is_refused` |
| Invalid stop | `test_an_invalid_stop_is_refused`, `test_a_missing_stop_is_refused` |
| Live mode unarmed | `test_a_live_order_is_impossible_while_live_is_unarmed` |
| Safety breaker latched | `test_a_latched_breaker_refuses_every_order` |
| Malformed AI response | `TestMalformedAnswersAreRejected` (ten shapes) |

If one of these ever goes green by accident the platform can lose money
in a way no other test catches.

## Files

| File | Covers |
|---|---|
| `test_risk_engine.py` | The seven refusals, the happy path, reward:risk, operator switches, breakers, concentration, spread and slippage, event blackout, sizing across four asset classes |
| `test_paper_execution.py` | Fill friction, the ledger, the order state machine, reconciliation |
| `test_data_and_instruments.py` | Quote and bar validation, instrument arithmetic, sizing edges, the replay adapter's refusal to invent a quote |
| `test_ai_contract.py` | Schema acceptance, ten malformed shapes, hallucination rejection, snapshot construction |
| `test_api_and_runtime.py` | Auth, CSRF per endpoint and per identity, rate limiting, security headers, production cookie flags, session fixation, live arming, the latched breaker, dashboard rendering, configuration gates, secret rendering |
| `test_money_and_pricing.py` | Non-finite rejection at every boundary, tick rounding direction for limits, stops and targets, and that a paper fill cannot violate its limit |
| `test_adapter_failures.py` | Stale data, crossed quotes, provider exceptions, rate limiting, broker disconnect, broker rejection, unvaluable positions, reconciliation failure, strict-fixture enforcement |
| `test_migrations.py` | Alembic upgrade from empty to head, drift check, downgrade, and the application running against the migrated schema |
| `test_features.py` | Indicator values against hand arithmetic and an independently written reference, series alignment, and warm-up behaviour |
| `test_structure.py` | Swings, sequence, breaks, gaps, sweeps, ranges, and the look-ahead guards |
| `test_regime_and_strategies.py` | Regime classification, the validation and regime gates, proposal coherence, and the registry |
| `test_oanda.py` | The OANDA adapter against a strict stub transport: instrument translation, quotes, candles, order placement, every failure mode, the live-host gate, and that the token never reaches any output |

## Principles

**No hidden clocks.** Every time-dependent test passes an explicit `now`.
A risk verdict that depends on when the suite ran is a verdict nobody can
trust.

**One variable per test.** The `context` fixture approves a reasonable
order; each rejection test changes exactly one thing, so a failure names
its own cause.

**Fixtures do not inherit the example config.** The test limits are
written out in `conftest.py` rather than loaded from
`risk.example.yaml`, so editing the example cannot silently stop a test
from testing what it says.

**Fakes are strict.** `StrictDataAdapter` answers only for symbols it
was given and raises `FixtureMisuse` otherwise, distinguishing that
from a symbol the venue genuinely does not list, which raises
`FeatureUnavailable` as production would. `FailingBroker` raises if
asked to place an order the test did not arrange. A double that answers
every question can make a broken test pass, and one did: the original
instrument source returned the same crypto contract for every symbol,
so an AAPL order was sized against Bitcoin and nothing noticed.

**Authentication is not bundled into convenience.** There are five
clients — anonymous, owner, owner without CSRF, owner with a bad CSRF
token, and a non-owner — and every dangerous endpoint is parametrised
across them. A single signed-in fixture makes the happy path short and
the denial paths easy to never write.

**Migrations are run, not assumed.** `create_all` builds the schema
from ORM metadata and proves nothing about production. The migration
tests shell out to the real Alembic CLI against an empty database.

**The OANDA fixtures are labelled as assumptions.** They were written
from the v20 API as documented elsewhere, not captured from live
traffic, because the build environment could not reach OANDA. They
prove the adapter handles the shape it expects and they do not prove
that shape is right. `python -m gtcc oanda-check` is what closes that,
and one test asserts the verifier's checklist has not drifted away from
the fields the adapter actually reads.

**Tests that would catch a quiet bug.** Five came from real defects found
while building, and each now has a test naming the symptom:

1. Size ceilings compounding against each other, which halved every
   position with a plausible-looking number.
2. Reward:risk computed before the caps shrank the size, so the reported
   cost base belonged to an order that was never sent.
3. Naive timestamps from SQLite, which crashed every session lookup in
   development while working in PostgreSQL.
4. An unknown symbol escaping as a 500 instead of a refusal.
5. Two feeds the Grok schema allowed a model to cite that the snapshot
   had no field for, so a legitimate citation would have been rejected as
   a hallucination.

## Not yet covered

Backtest look-ahead, agent outputs, live broker adapters, the scanner.
Those arrive with the phases that introduce them.
The backtest engine in particular needs a test that **proves** it cannot
see the future, not just a comment saying it does not.

## Running

```
cd gtcc
pip install -r requirements.txt
pytest                      # all 313
pytest -k TheSevenRefusals  # the critical risk tests
```
