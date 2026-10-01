"""The Grok contract — specification sections 12 and 36.

The platform must keep working when the model is down, slow, or wrong,
and must never act on an answer it cannot verify.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gtcc.ai.schema import (
    KNOWN_FEEDS,
    RESPONSE_SCHEMA,
    AIResponseRejected,
    MarketSnapshot,
    validate_response,
)
from gtcc.domain.enums import Decision, Market
from gtcc.domain.money import D

GOOD = {
    "decision": "LONG",
    "direction": "LONG",
    "confidence": 0.63,
    "setup": "LIQUIDITY_SWEEP_RECLAIM",
    "entry_zone": [59900, 60100],
    "invalidation": 59000,
    "targets": [62000, 63500],
    "reasoning_summary": "Indicators and structure align on the entry timeframe.",
    "supporting_factors": ["indicators momentum positive"],
    "conflicting_factors": ["volume below average"],
    "missing_data": ["order_flow"],
    "risk_flags": [],
}


@pytest.fixture
def snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        market=Market.CRYPTO,
        timestamp=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
        current_price=D("60000"),
        spread_bps=D("1"),
        data_quality="GOOD",
        indicators={"rsi": 61},
        market_structure={"trend": "up"},
        unavailable_feeds=("order_flow", "funding"),
    )


class TestAValidAnswerIsAccepted:
    def test_a_well_formed_answer_parses(self, snapshot):
        decision = validate_response(json.dumps(GOOD), snapshot)

        assert decision.decision is Decision.LONG
        assert decision.confidence == 0.63
        assert decision.targets == (D("62000"), D("63500"))

    def test_a_fenced_code_block_is_tolerated(self, snapshot):
        decision = validate_response("```json\n" + json.dumps(GOOD) + "\n```", snapshot)
        assert decision.decision is Decision.LONG

    def test_wait_is_a_valid_answer_without_levels(self, snapshot):
        body = {
            **GOOD, "decision": "WAIT", "direction": "NONE",
            "entry_zone": None, "invalidation": None, "targets": [],
        }
        assert validate_response(json.dumps(body), snapshot).decision is Decision.WAIT

    def test_confidence_is_never_marked_calibrated(self, snapshot):
        """Section 12: a model's confidence is not a probability until a
        calibration study says it is."""
        assert validate_response(json.dumps(GOOD), snapshot).confidence_is_calibrated is False


class TestMalformedAnswersAreRejected:
    """Section 41's fourth critical test, in its several shapes."""

    @pytest.mark.parametrize(
        "mutation,label",
        [
            ({"confidence": 1.5}, "confidence above one"),
            ({"confidence": -0.2}, "confidence below zero"),
            ({"confidence": "high"}, "confidence not numeric"),
            ({"decision": "MAYBE"}, "unknown decision"),
            ({"direction": "SIDEWAYS"}, "unknown direction"),
            ({"decision": "SHORT"}, "decision and direction disagree"),
            ({"invalidation": 61000}, "long invalidated above the entry"),
            ({"targets": [58000]}, "long target below the entry"),
            ({"entry_zone": [59900]}, "entry zone of one number"),
            ({"targets": "soon"}, "targets not an array"),
        ],
    )
    def test_incoherent_answers_are_refused(self, snapshot, mutation, label):
        with pytest.raises(AIResponseRejected):
            validate_response(json.dumps({**GOOD, **mutation}), snapshot)

    def test_unparseable_json_is_refused(self, snapshot):
        with pytest.raises(AIResponseRejected, match="not valid JSON"):
            validate_response("{the model started explaining itself", snapshot)

    def test_a_missing_field_is_refused(self, snapshot):
        body = {key: value for key, value in GOOD.items() if key != "targets"}
        with pytest.raises(AIResponseRejected, match="missing required"):
            validate_response(json.dumps(body), snapshot)

    def test_an_extra_field_is_refused(self, snapshot):
        """A model that invents a field has not followed the schema, and
        the fields it did fill in deserve no more trust than that one."""
        with pytest.raises(AIResponseRejected, match="unexpected field"):
            validate_response(json.dumps({**GOOD, "position_size": 3}), snapshot)

    def test_an_empty_response_is_refused(self, snapshot):
        with pytest.raises(AIResponseRejected):
            validate_response("", snapshot)

    def test_every_reason_is_reported_not_only_the_first(self, snapshot):
        body = {**GOOD, "confidence": 2.0, "targets": [1]}
        with pytest.raises(AIResponseRejected) as caught:
            validate_response(json.dumps(body), snapshot)

        assert len(caught.value.reasons) >= 2


class TestHallucinationIsRejected:
    """Section 36: an answer that references data we did not send is not
    partially usable, it is evidence the model is confabulating."""

    def test_citing_a_feed_marked_unavailable_is_refused(self, snapshot):
        body = {**GOOD, "supporting_factors": ["order flow shows heavy bid absorption"]}

        with pytest.raises(AIResponseRejected, match="unavailable"):
            validate_response(json.dumps(body), snapshot)

    def test_citing_a_feed_that_was_never_supplied_is_refused(self, snapshot):
        body = {**GOOD, "supporting_factors": ["liquidations cascade under the low"]}

        with pytest.raises(AIResponseRejected, match="not supplied"):
            validate_response(json.dumps(body), snapshot)

    def test_naming_a_feed_the_platform_does_not_have_is_refused(self, snapshot):
        body = {**GOOD, "missing_data": ["satellite_imagery"]}

        with pytest.raises(AIResponseRejected, match="not a feed"):
            validate_response(json.dumps(body), snapshot)

    def test_citing_a_supplied_feed_is_accepted(self, snapshot):
        body = {**GOOD, "supporting_factors": ["market structure made a higher low"]}

        assert validate_response(json.dumps(body), snapshot).decision is Decision.LONG


class TestTheSnapshotSentToTheModel:
    def test_unavailable_feeds_are_stated_rather_than_omitted(self, snapshot):
        payload = snapshot.to_payload()

        assert payload["unavailable_feeds"] == ["order_flow", "funding"]
        assert payload["order_flow"] is None

    def test_available_feeds_exclude_the_unavailable_ones(self, snapshot):
        available = snapshot.available_feeds()

        assert "indicators" in available
        assert "order_flow" not in available

    def test_the_schema_forbids_extra_properties(self):
        assert RESPONSE_SCHEMA["additionalProperties"] is False
        assert set(RESPONSE_SCHEMA["required"]) <= set(RESPONSE_SCHEMA["properties"])

    def test_every_known_feed_is_a_snapshot_concept(self, snapshot):
        for feed in KNOWN_FEEDS:
            assert hasattr(snapshot, feed), f"{feed} is citable but has no snapshot field"
