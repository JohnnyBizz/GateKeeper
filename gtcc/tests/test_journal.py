"""The journal keeps the refusals, and says when it could not.

Two properties carry most of the weight here.

A journal of taken trades can only answer "were my trades any good".
The question worth answering is "were my refusals right", and that is
unanswerable unless the refusal was written down at the time with the
verdict that caused it. So every exit path from `submit` writes a row.

And a journal write that fails must not be invisible. The handling is
asymmetric on purpose: a lost refusal costs analysis, while a lost record
of a PLACED order means there is a position at a venue the platform
cannot explain, which is the condition that must stop trading.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gtcc.domain.enums import Market, OrderType, RiskAction, Side, TradingMode
from gtcc.domain.money import D
from gtcc.domain.orders import OrderRequest
from gtcc.journal import TradeAnnotations
from gtcc.journal.entry import NOT_SUPPLIED, Outcome, entry_for
from gtcc.risk.safety import TripReason
from gtcc.storage.repositories import TradeJournalRepository


WHEN = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _order(symbol: str = "BTCUSDT", **overrides) -> OrderRequest:
    base = dict(
        symbol=symbol,
        market=Market.CRYPTO,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        protective_stop=D("59000"),
        targets=(D("62500"),),
        strategy="breakout",
    )
    base.update(overrides)
    return OrderRequest(**base)


class RecordingStore:
    """A journal store that keeps rows in memory, or refuses to."""

    def __init__(self, fail: bool = False) -> None:
        self.entries: list = []
        self.fail = fail

    def record(self, entry, *, equity):
        if self.fail:
            raise RuntimeError("the database is gone")
        self.entries.append(entry)
        return len(self.entries)


@pytest.fixture
def journal():
    return RecordingStore()


class TestEverySetupIsRecorded:
    def test_a_taken_trade_is_journalled(self, runtime, journal):
        runtime.journal_store = journal

        result = runtime.submit(_order())

        assert result.placed
        assert len(journal.entries) == 1
        entry = journal.entries[0]
        assert entry.outcome == Outcome.TAKEN
        assert entry.symbol == "BTCUSDT"
        assert entry.order_ids, "a taken trade carries its order ids"
        assert entry.opened_at is not None

    def test_a_risk_refusal_is_journalled_with_its_verdict(self, runtime, journal):
        runtime.journal_store = journal

        result = runtime.submit(_order(protective_stop=None))

        assert not result.placed
        assert len(journal.entries) == 1
        entry = journal.entries[0]
        assert entry.outcome == Outcome.REJECTED_BY_RISK
        assert entry.risk_verdict["action"] == "REJECT"
        assert any(
            "STOP_PRESENT" in failure["code"]
            for failure in entry.risk_verdict["failures"]
        )
        assert entry.planned_size is None, "nothing was approved, so no size"
        assert entry.opened_at is None

    def test_an_unknown_symbol_is_journalled_too(self, runtime, journal):
        """A setup refused by the plumbing is still a setup considered."""
        runtime.journal_store = journal

        runtime.submit(_order(symbol="NOSUCHTHING"))

        assert len(journal.entries) == 1
        assert journal.entries[0].outcome == Outcome.REJECTED_BY_RISK
        assert journal.entries[0].symbol == "NOSUCHTHING"

    def test_the_verdict_keeps_the_checks_that_passed(self, runtime, journal):
        """"Why was this allowed" is a question worth asking about a loss."""
        runtime.journal_store = journal

        runtime.submit(_order())

        checks = journal.entries[0].risk_verdict["checks"]
        assert len(checks) > 20
        assert any(check["outcome"] == "PASS" for check in checks)

    def test_a_kill_switch_refusal_is_recorded(self, runtime, journal):
        runtime.journal_store = journal
        runtime.state = runtime.ensure_state()
        runtime.engage_kill_switch(True)

        runtime.submit(_order())

        assert len(journal.entries) == 1
        assert journal.entries[0].outcome == Outcome.REJECTED_BY_RISK


class TestNothingIsInvented:
    def test_absent_context_is_marked_absent_not_empty(self, runtime, journal):
        """An empty structure dict would read as "looked, found nothing"."""
        runtime.journal_store = journal

        runtime.submit(_order())

        entry = journal.entries[0]
        assert entry.market_structure == NOT_SUPPLIED
        assert entry.indicators == NOT_SUPPLIED
        assert entry.ai_decision == NOT_SUPPLIED

    def test_supplied_context_is_stored_as_given(self, runtime, journal):
        runtime.journal_store = journal

        runtime.submit(
            _order(),
            annotations=TradeAnnotations(
                timeframe="15m",
                regime="TRENDING_UP",
                structure={"trend": "UP", "rule": "higher highs and higher lows"},
                indicators={"atr": "350.00"},
                notes="from the scanner",
            ),
        )

        entry = journal.entries[0]
        assert entry.market_structure["trend"] == "UP"
        assert entry.indicators["atr"] == "350.00"
        assert entry.regime == "TRENDING_UP"
        assert entry.timeframe == "15m"
        assert entry.notes == "from the scanner"

    def test_a_refusal_has_no_fees_rather_than_zero_fees(self, runtime, journal):
        """A zero would average into every later cost statistic."""
        runtime.journal_store = journal

        runtime.submit(_order(protective_stop=None))

        assert journal.entries[0].fees is None

    def test_planned_risk_is_the_loss_at_the_approved_size(self, runtime, journal):
        runtime.journal_store = journal

        result = runtime.submit(_order())

        entry = journal.entries[0]
        assert entry.planned_risk == result.verdict.approved_risk
        assert entry.planned_size == result.verdict.approved_quantity


class TestFailureIsNotSilent:
    def test_a_failed_write_on_a_refusal_does_not_stop_trading(self, runtime):
        """Nothing is at the venue, so the account is still accountable."""
        runtime.journal_store = RecordingStore(fail=True)

        result = runtime.submit(_order(protective_stop=None))

        assert not result.placed
        assert not runtime.ensure_execution().tripped

    def test_a_failed_write_on_a_placed_order_latches_the_breaker(self, runtime):
        """A position the platform cannot explain must stop trading."""
        runtime.journal_store = RecordingStore(fail=True)

        result = runtime.submit(_order())

        assert result.placed, "the order did reach the venue"
        execution = runtime.ensure_execution()
        assert execution.tripped
        assert TripReason.ACCOUNT_STATE_UNKNOWN in {
            trip.reason for trip in execution.trips
        }

    def test_the_next_order_is_refused_after_that_trip(self, runtime):
        runtime.journal_store = RecordingStore(fail=True)
        runtime.submit(_order())

        second = runtime.submit(_order())

        assert not second.placed
        assert second.verdict.action is RiskAction.REJECT

    def test_no_store_configured_is_not_an_error(self, runtime):
        runtime.journal_store = None

        result = runtime.submit(_order())

        assert result.placed
        assert not runtime.ensure_execution().tripped


@pytest.fixture
def journal_db(settings):
    """A real database, so the row's own column types are exercised."""
    from gtcc.storage import db

    db.configure(settings.database_dsn)
    db.create_all()
    return db.session_scope


class TestTheRepository:
    def test_rows_round_trip_including_a_refusal(self, runtime, journal_db):
        store = TradeJournalRepository(journal_db)
        runtime.journal_store = store

        runtime.submit(_order())
        runtime.submit(_order(protective_stop=None))

        account_id = runtime.account().account_id
        counts = store.count(account_id)
        assert counts.get(Outcome.TAKEN) == 1
        assert counts.get(Outcome.REJECTED_BY_RISK) == 1

        rows = store.recent(account_id)
        assert len(rows) == 2
        taken = next(row for row in rows if row.outcome == Outcome.TAKEN)
        assert taken.symbol == "BTCUSDT"
        assert taken.risk_verdict["action"] in ("ALLOW", "REDUCE")

    def test_refusals_can_be_read_back_on_their_own(self, runtime, journal_db):
        """The point of keeping them is being able to study them."""
        store = TradeJournalRepository(journal_db)
        runtime.journal_store = store

        runtime.submit(_order(protective_stop=None))
        runtime.submit(_order())

        refusals = store.recent(
            runtime.account().account_id, outcome=Outcome.REJECTED_BY_RISK
        )
        assert len(refusals) == 1
        assert refusals[0].planned_stop is None


class TestOutcomeAttribution:
    def test_a_venue_rejection_is_not_blamed_on_risk(self):
        """Mislabelling this would make a later study of the limits wrong."""
        from gtcc.risk.engine import Check, RiskVerdict

        approved = RiskVerdict(
            action=RiskAction.ALLOW, approved_quantity=D("1"), checks=()
        )
        entry = entry_for(
            _order(), approved,
            trade_id="t1", account_external_id="acct", now=WHEN, mode=TradingMode.PAPER,
            order=None, placed=False, venue_detail="venue said no",
        )

        assert entry.outcome == Outcome.REJECTED_BY_VENUE
        assert "venue said no" in (entry.notes or "")

        refused = RiskVerdict.refused(Check.STOP_PRESENT, "no stop")
        assert (
            entry_for(
                _order(), refused,
                trade_id="t2", account_external_id="acct", now=WHEN,
                mode=TradingMode.PAPER,
            ).outcome
            == Outcome.REJECTED_BY_RISK
        )


class TestTheReadEndpoint:
    def test_refusals_are_visible_through_the_api(self, owner_api, runtime, journal_db):
        runtime.journal_store = TradeJournalRepository(journal_db)
        runtime.submit(_order())
        runtime.submit(_order(protective_stop=None))

        payload = owner_api.get("/api/journal").json()

        assert payload["counts"][Outcome.TAKEN] == 1
        assert payload["counts"][Outcome.REJECTED_BY_RISK] == 1
        refusal = next(
            row for row in payload["rows"] if row["outcome"] == Outcome.REJECTED_BY_RISK
        )
        assert any("STOP_PRESENT" in failure for failure in refusal["failures"])

    def test_filtering_to_refusals_works(self, owner_api, runtime, journal_db):
        runtime.journal_store = TradeJournalRepository(journal_db)
        runtime.submit(_order())
        runtime.submit(_order(protective_stop=None))

        payload = owner_api.get(
            f"/api/journal?outcome={Outcome.REJECTED_BY_RISK}"
        ).json()

        assert len(payload["rows"]) == 1
        assert payload["rows"][0]["outcome"] == Outcome.REJECTED_BY_RISK

    def test_no_journal_configured_is_not_an_empty_journal(self, owner_api, runtime):
        """Reporting "no trades" here would be a claim about trading."""
        runtime.journal_store = None

        payload = owner_api.get("/api/journal").json()

        assert payload["rows"] == []
        assert "_no_journal_configured" in payload["counts"]

    def test_reading_the_journal_needs_a_session(self, anonymous_api):
        assert anonymous_api.get("/api/journal").status_code in (401, 403)

    def test_there_is_no_way_to_write_a_row_through_the_api(self, owner_api):
        """A journal somebody can edit afterwards is not evidence."""
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = owner_api.request(method, "/api/journal")
            assert response.status_code in (404, 405), method
