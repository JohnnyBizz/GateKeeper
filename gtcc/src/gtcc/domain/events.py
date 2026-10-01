"""Scheduled economic events — specification section 9.

These come from a calendar provider. Nothing here generates an event;
an empty calendar means "we do not know of an event", which is not the
same as "there is no event", and the risk engine says so when it skips
the blackout check for want of data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from gtcc.domain.enums import Market


class EventImpact(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    event_id: str
    name: str
    scheduled_for: datetime
    impact: EventImpact
    #: Currencies the release moves, e.g. {"USD"}. Used to decide which
    #: symbols sit inside the blackout.
    currencies: frozenset[str] = frozenset()
    markets: frozenset[Market] = frozenset()
    source: str = ""
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    metadata: dict = field(default_factory=dict)

    def minutes_until(self, now: datetime) -> float:
        return (self.scheduled_for - now).total_seconds() / 60.0

    def affects(self, *, quote_currency: str, base_currency: str, market: Market) -> bool:
        if self.markets and market in self.markets:
            return True
        if not self.currencies:
            return False
        return quote_currency.upper() in self.currencies or base_currency.upper() in self.currencies
