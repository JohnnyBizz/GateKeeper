"""The Grok contract — specification sections 12 and 36.

Grok is an advisory input. It receives a structured snapshot of what the
deterministic engines already computed, and it must answer in a fixed
shape. Anything else is discarded.

The validator here is deliberately suspicious. Beyond checking that the
JSON parses and the fields exist, it checks that the answer is
*consistent with what we actually sent*: a model that cites order-flow
support when the snapshot said order flow was unavailable has
hallucinated, and section 36 says the right response to that is to
reject the output, not to use the parts that look reasonable.

One thing this module never does is treat ``confidence`` as a
probability. It is a number the model emitted. Until a calibration
study has been run against settled outcomes, it is an opinion with a
decimal point, and :attr:`AIDecision.confidence_is_calibrated` stays
False to keep anyone downstream from forgetting that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from gtcc.domain.enums import Decision, Market, Timeframe
from gtcc.domain.money import D

#: Feeds the snapshot can describe. A model may only cite these.
KNOWN_FEEDS = frozenset(
    {
        "market_structure",
        "indicators",
        "volume",
        "volatility",
        "order_flow",
        "funding",
        "open_interest",
        "liquidations",
        "news",
        "macro_events",
        "portfolio_exposure",
        "strategy_signals",
        "correlation",
    }
)

REQUIRED_FIELDS = frozenset(
    {
        "decision",
        "direction",
        "confidence",
        "setup",
        "entry_zone",
        "invalidation",
        "targets",
        "reasoning_summary",
        "supporting_factors",
        "conflicting_factors",
        "missing_data",
        "risk_flags",
    }
)

ALLOWED_DIRECTIONS = frozenset({"LONG", "SHORT", "NONE"})

MAX_REASONING_CHARS = 2000
MAX_FACTORS = 20


class AIResponseRejected(ValueError):
    """The model's answer cannot be used. Carries every reason."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(reasons))


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """The structured input Grok receives.

    Free text is kept out of this deliberately: the model is asked to
    reason over numbers the platform computed, not over a narrative the
    platform wrote for it.
    """

    symbol: str
    market: Market
    timestamp: datetime
    current_price: Decimal
    spread_bps: Decimal
    data_quality: str
    timeframes: Mapping[str, Any] = field(default_factory=dict)
    market_structure: Mapping[str, Any] | None = None
    indicators: Mapping[str, Any] | None = None
    volume: Mapping[str, Any] | None = None
    volatility: Mapping[str, Any] | None = None
    order_flow: Mapping[str, Any] | None = None
    funding: Mapping[str, Any] | None = None
    open_interest: Mapping[str, Any] | None = None
    liquidations: Mapping[str, Any] | None = None
    correlation: Mapping[str, Any] | None = None
    news: Sequence[Mapping[str, Any]] = ()
    macro_events: Sequence[Mapping[str, Any]] = ()
    portfolio_exposure: Mapping[str, Any] = field(default_factory=dict)
    strategy_signals: Sequence[Mapping[str, Any]] = ()
    #: Feeds the platform could not obtain. The model is told explicitly
    #: and is forbidden from reasoning as though they were present.
    unavailable_feeds: tuple[str, ...] = ()

    def available_feeds(self) -> frozenset[str]:
        present = set()
        for feed in KNOWN_FEEDS:
            value = getattr(self, feed, None)
            if value:
                present.add(feed)
        return frozenset(present - set(self.unavailable_feeds))

    def to_payload(self) -> dict[str, Any]:
        """The JSON actually sent. Nothing is omitted silently."""
        return {
            "symbol": self.symbol,
            "market": str(self.market),
            "timestamp": self.timestamp.isoformat(),
            "current_price": str(self.current_price),
            "spread_bps": str(self.spread_bps),
            "data_quality": self.data_quality,
            "timeframes": dict(self.timeframes),
            "market_structure": self.market_structure,
            "indicators": self.indicators,
            "volume": self.volume,
            "volatility": self.volatility,
            "order_flow": self.order_flow,
            "funding": self.funding,
            "open_interest": self.open_interest,
            "liquidations": self.liquidations,
            "correlation": self.correlation,
            "news": list(self.news),
            "macro_events": list(self.macro_events),
            "portfolio_exposure": dict(self.portfolio_exposure),
            "strategy_signals": list(self.strategy_signals),
            "unavailable_feeds": list(self.unavailable_feeds),
        }


@dataclass(frozen=True, slots=True)
class AIDecision:
    """A validated model answer. Advisory only."""

    decision: Decision
    direction: str
    confidence: float
    setup: str
    entry_zone: tuple[Decimal, Decimal] | None
    invalidation: Decimal | None
    targets: tuple[Decimal, ...]
    reasoning_summary: str
    supporting_factors: tuple[str, ...]
    conflicting_factors: tuple[str, ...]
    missing_data: tuple[str, ...]
    risk_flags: tuple[str, ...]
    model: str = ""
    raw: str = ""

    #: Stays False until a calibration study exists. Nothing in the risk
    #: engine reads confidence at all, and nothing elsewhere should treat
    #: it as a probability while this is False.
    confidence_is_calibrated: bool = False

    @property
    def is_actionable(self) -> bool:
        return self.decision in (Decision.LONG, Decision.SHORT)


#: The JSON schema handed to the model, and the shape validated on return.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(REQUIRED_FIELDS),
    "properties": {
        "decision": {"type": "string", "enum": [d.value for d in Decision]},
        "direction": {"type": "string", "enum": sorted(ALLOWED_DIRECTIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "setup": {"type": "string"},
        "entry_zone": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 2,
            "maxItems": 2,
        },
        "invalidation": {"type": ["number", "null"]},
        "targets": {"type": "array", "items": {"type": "number"}},
        "reasoning_summary": {"type": "string", "maxLength": MAX_REASONING_CHARS},
        "supporting_factors": {"type": "array", "items": {"type": "string"}},
        "conflicting_factors": {"type": "array", "items": {"type": "string"}},
        "missing_data": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
}


def validate_response(
    raw: str | Mapping[str, Any],
    snapshot: MarketSnapshot | None = None,
    *,
    model: str = "",
) -> AIDecision:
    """Parse and check a model answer, or raise :class:`AIResponseRejected`.

    *snapshot* enables the consistency checks. Omitting it validates the
    shape only, which is enough for a unit test and not enough to trade.
    """
    reasons: list[str] = []

    if isinstance(raw, str):
        text = raw.strip()
        # Models often wrap JSON in a fenced block despite instructions.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIResponseRejected([f"response is not valid JSON: {exc}"]) from exc
    else:
        payload = dict(raw)

    if not isinstance(payload, dict):
        raise AIResponseRejected(["response is not a JSON object"])

    missing = REQUIRED_FIELDS - payload.keys()
    if missing:
        reasons.append(f"missing required field(s): {sorted(missing)}")
    extra = payload.keys() - REQUIRED_FIELDS
    if extra:
        reasons.append(f"unexpected field(s): {sorted(extra)}")
    if reasons:
        raise AIResponseRejected(reasons)

    try:
        decision = Decision(str(payload["decision"]).upper())
    except ValueError:
        raise AIResponseRejected(
            [f"decision {payload['decision']!r} is not one of {[d.value for d in Decision]}"]
        ) from None

    direction = str(payload["direction"]).upper()
    if direction not in ALLOWED_DIRECTIONS:
        reasons.append(f"direction {direction!r} is not one of {sorted(ALLOWED_DIRECTIONS)}")

    confidence = payload["confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        reasons.append("confidence is not a number")
        confidence = 0.0
    elif not 0.0 <= float(confidence) <= 1.0:
        reasons.append(f"confidence {confidence} is outside [0, 1]")

    # Direction and decision must agree. A LONG decision with direction
    # NONE is not a near miss, it is an incoherent answer.
    if decision is Decision.LONG and direction != "LONG":
        reasons.append("decision LONG with direction " + direction)
    if decision is Decision.SHORT and direction != "SHORT":
        reasons.append("decision SHORT with direction " + direction)
    if decision is Decision.WAIT and direction != "NONE":
        reasons.append("decision WAIT must carry direction NONE")

    targets = _decimal_list(payload["targets"], "targets", reasons)
    invalidation = _optional_decimal(payload["invalidation"], "invalidation", reasons)
    entry_zone = _entry_zone(payload["entry_zone"], reasons)

    if decision in (Decision.LONG, Decision.SHORT):
        if entry_zone is None:
            reasons.append(f"a {decision} decision must carry an entry_zone")
        if invalidation is None:
            reasons.append(f"a {decision} decision must carry an invalidation level")
        if not targets:
            reasons.append(f"a {decision} decision must carry at least one target")
        if entry_zone and invalidation is not None and targets:
            low, high = entry_zone
            midpoint = (low + high) / D(2)
            if decision is Decision.LONG:
                if invalidation >= low:
                    reasons.append("LONG invalidation must sit below the entry zone")
                if any(target <= midpoint for target in targets):
                    reasons.append("LONG targets must sit above the entry zone")
            else:
                if invalidation <= high:
                    reasons.append("SHORT invalidation must sit above the entry zone")
                if any(target >= midpoint for target in targets):
                    reasons.append("SHORT targets must sit below the entry zone")

    supporting = _string_list(payload["supporting_factors"], "supporting_factors", reasons)
    conflicting = _string_list(payload["conflicting_factors"], "conflicting_factors", reasons)
    missing_data = _string_list(payload["missing_data"], "missing_data", reasons)
    risk_flags = _string_list(payload["risk_flags"], "risk_flags", reasons)

    summary = str(payload["reasoning_summary"])
    if len(summary) > MAX_REASONING_CHARS:
        reasons.append(f"reasoning_summary is {len(summary)} characters, over the limit")

    if snapshot is not None:
        reasons.extend(_consistency_reasons(snapshot, supporting, missing_data))

    if reasons:
        raise AIResponseRejected(reasons)

    return AIDecision(
        decision=decision,
        direction=direction,
        confidence=float(confidence),
        setup=str(payload["setup"]),
        entry_zone=entry_zone,
        invalidation=invalidation,
        targets=targets,
        reasoning_summary=summary,
        supporting_factors=supporting,
        conflicting_factors=conflicting,
        missing_data=missing_data,
        risk_flags=risk_flags,
        model=model,
        raw=raw if isinstance(raw, str) else json.dumps(payload),
    )


def _consistency_reasons(
    snapshot: MarketSnapshot, supporting: Sequence[str], missing_data: Sequence[str]
) -> list[str]:
    """Catch answers that cite data the snapshot did not contain."""
    reasons: list[str] = []
    available = snapshot.available_feeds()
    unavailable = set(snapshot.unavailable_feeds)

    for factor in supporting:
        cited = _feed_mentioned(factor)
        if cited and cited in unavailable:
            reasons.append(
                f"supporting factor cites {cited}, which the snapshot marked unavailable: "
                f"{factor!r}"
            )
        elif cited and cited not in available:
            reasons.append(
                f"supporting factor cites {cited}, which was not supplied: {factor!r}"
            )

    for feed in missing_data:
        normalised = feed.strip().lower().replace(" ", "_")
        if normalised and normalised not in KNOWN_FEEDS:
            reasons.append(f"missing_data names {feed!r}, which is not a feed this platform has")

    return reasons


def _feed_mentioned(text: str) -> str | None:
    lowered = text.lower()
    for feed in KNOWN_FEEDS:
        if feed.replace("_", " ") in lowered or feed in lowered:
            return feed
    return None


def _decimal_list(value: Any, name: str, reasons: list[str]) -> tuple[Decimal, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        reasons.append(f"{name} must be an array")
        return ()
    if len(value) > MAX_FACTORS:
        reasons.append(f"{name} has {len(value)} entries, over the limit of {MAX_FACTORS}")
    out: list[Decimal] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            reasons.append(f"{name} contains a non-numeric entry: {item!r}")
            continue
        try:
            out.append(D(item))
        except ValueError:
            reasons.append(f"{name} contains an unreadable number: {item!r}")
    return tuple(out)


def _optional_decimal(value: Any, name: str, reasons: list[str]) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        reasons.append(f"{name} must be a number or null")
        return None
    try:
        return D(value)
    except ValueError:
        reasons.append(f"{name} is not a readable number: {value!r}")
        return None


def _entry_zone(value: Any, reasons: list[str]) -> tuple[Decimal, Decimal] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        reasons.append("entry_zone must be a two-element array or null")
        return None
    try:
        low, high = D(value[0]), D(value[1])
    except (ValueError, TypeError):
        reasons.append(f"entry_zone contains an unreadable number: {value!r}")
        return None
    if low > high:
        low, high = high, low
    return (low, high)


def _string_list(value: Any, name: str, reasons: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        reasons.append(f"{name} must be an array")
        return ()
    if len(value) > MAX_FACTORS:
        reasons.append(f"{name} has {len(value)} entries, over the limit of {MAX_FACTORS}")
    return tuple(str(item) for item in value)
