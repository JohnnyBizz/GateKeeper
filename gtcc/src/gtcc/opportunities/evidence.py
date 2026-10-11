"""Evidence for a trade idea, counted honestly.

The usual way trading software lies about evidence is by counting. RSI,
stochastic, MACD and rate of change are four different formulas applied to
the same closing prices, and when all four "agree" that is one observation
— momentum turned — reported four times. A dashboard showing "8 of 10
signals bullish" is usually showing three facts and a lot of repetition.

So evidence here is grouped into FAMILIES, and the threshold for calling
something a pick counts families, not items. Five momentum readings that
agree are one family in favour.

Even families are not independent in the statistical sense. Trend,
momentum, structure and location are all functions of one price series,
so they tend to agree with each other for the same underlying reason.
Grouping reduces the double-counting; it cannot remove it. The one family
that is genuinely different in kind is TRACK_RECORD — whether this setup
has made money before, measured on data the setup was not tuned on — and
it is ranked ahead of everything else for that reason.

There is no separate "regime" family, deliberately. The regime classifier
reads mostly off ADX and the directional lines — the same numbers the
trend-strength reading uses — so a regime that "agrees" with trend strength
is that one reading reported twice. Regime fit is filed under trend
strength, where it counts once.

Families split into two kinds:

DIRECTIONAL families are evidence that price will move the way the trade
needs. These are what the "plenty of evidence" threshold counts.

PRACTICAL families — reward to risk, costs, event risk, volatility — say
whether the trade is worth taking if the move happens. A good reward to
risk is not evidence that price will rise, so it never counts toward the
threshold. These families can only disqualify an idea or warn about it.

UNKNOWN is a first-class stance. When something could not be assessed —
no volume data, no economic calendar, no measured track record — the
dossier says so in words. It never counts for or against, and it is never
silently omitted, because an omitted check reads as a passed one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable


class Family(StrEnum):
    # -- directional: is the move likely? ---------------------------------
    STRUCTURE = "STRUCTURE"
    TREND = "TREND"
    HIGHER_TIMEFRAME = "HIGHER_TIMEFRAME"
    MOMENTUM = "MOMENTUM"
    TREND_STRENGTH = "TREND_STRENGTH"
    VOLUME = "VOLUME"
    LOCATION = "LOCATION"
    TRACK_RECORD = "TRACK_RECORD"
    # -- practical: is it worth taking if it happens? ----------------------
    REWARD_RISK = "REWARD_RISK"
    COSTS = "COSTS"
    VOLATILITY = "VOLATILITY"
    EVENT_RISK = "EVENT_RISK"
    RISK_LIMITS = "RISK_LIMITS"


DIRECTIONAL: frozenset[Family] = frozenset(
    {
        Family.STRUCTURE,
        Family.TREND,
        Family.HIGHER_TIMEFRAME,
        Family.MOMENTUM,
        Family.TREND_STRENGTH,
        Family.VOLUME,
        Family.LOCATION,
        Family.TRACK_RECORD,
    }
)

#: Plain-language names for the page and the CLI.
FAMILY_LABEL: dict[Family, str] = {
    Family.STRUCTURE: "Setup trigger",
    Family.TREND: "Trend on this timeframe",
    Family.HIGHER_TIMEFRAME: "Higher-timeframe trend",
    Family.MOMENTUM: "Momentum",
    Family.TREND_STRENGTH: "Trend strength & regime",
    Family.VOLUME: "Participation (volume)",
    Family.LOCATION: "Location vs nearby levels",
    Family.TRACK_RECORD: "Measured track record",
    Family.REWARD_RISK: "Reward to risk",
    Family.COSTS: "Trading costs",
    Family.VOLATILITY: "Stop vs normal noise",
    Family.EVENT_RISK: "Scheduled news risk",
    Family.RISK_LIMITS: "Your risk limits",
}


class Stance(StrEnum):
    FOR = "FOR"
    AGAINST = "AGAINST"
    #: Assessed, and it decides nothing — ordinary volume, a regime this
    #: setup is neither built for nor against. Different from UNKNOWN: this
    #: was checked.
    NEUTRAL = "NEUTRAL"
    #: Could not be assessed. Stated, never counted, never omitted.
    UNKNOWN = "UNKNOWN"


class FamilyStance(StrEnum):
    FOR = "FOR"
    AGAINST = "AGAINST"
    #: The family's own readings disagree. Counted as neither.
    MIXED = "MIXED"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Evidence:
    family: Family
    stance: Stance
    #: What was observed, with the numbers, so a person can check it on the
    #: chart rather than take it on trust.
    statement: str
    #: Only meaningful for AGAINST. Disqualifying evidence removes the idea
    #: whatever else supports it.
    disqualifying: bool = False

    def __post_init__(self) -> None:
        if self.disqualifying and self.stance is not Stance.AGAINST:
            raise ValueError("only evidence AGAINST an idea can disqualify it")


def for_(family: Family, statement: str) -> Evidence:
    return Evidence(family, Stance.FOR, statement)


def against(family: Family, statement: str, *, disqualifying: bool = False) -> Evidence:
    return Evidence(family, Stance.AGAINST, statement, disqualifying)


def unknown(family: Family, statement: str) -> Evidence:
    return Evidence(family, Stance.UNKNOWN, statement)


def neutral(family: Family, statement: str) -> Evidence:
    return Evidence(family, Stance.NEUTRAL, statement)


@dataclass(frozen=True, slots=True)
class Dossier:
    items: tuple[Evidence, ...] = field(default_factory=tuple)

    @classmethod
    def of(cls, items: Iterable[Evidence]) -> "Dossier":
        return cls(items=tuple(items))

    def family_stance(self, family: Family) -> FamilyStance:
        present = {item.stance for item in self.items if item.family is family}
        stances = present - {Stance.UNKNOWN, Stance.NEUTRAL}
        if not stances:
            return (
                FamilyStance.NEUTRAL if Stance.NEUTRAL in present
                else FamilyStance.UNKNOWN
            )
        if stances == {Stance.FOR}:
            return FamilyStance.FOR
        if stances == {Stance.AGAINST}:
            return FamilyStance.AGAINST
        return FamilyStance.MIXED

    def families(self) -> tuple[Family, ...]:
        seen: list[Family] = []
        for item in self.items:
            if item.family not in seen:
                seen.append(item.family)
        return tuple(seen)

    def _directional(self, wanted: FamilyStance) -> tuple[Family, ...]:
        return tuple(
            family
            for family in self.families()
            if family in DIRECTIONAL and self.family_stance(family) is wanted
        )

    @property
    def supporting(self) -> tuple[Family, ...]:
        """Directional families in favour. What the threshold counts."""
        return self._directional(FamilyStance.FOR)

    @property
    def opposing(self) -> tuple[Family, ...]:
        return self._directional(FamilyStance.AGAINST)

    @property
    def mixed(self) -> tuple[Family, ...]:
        return self._directional(FamilyStance.MIXED)

    @property
    def disqualifiers(self) -> tuple[Evidence, ...]:
        return tuple(item for item in self.items if item.disqualifying)

    @property
    def unknowns(self) -> tuple[Evidence, ...]:
        return tuple(item for item in self.items if item.stance is Stance.UNKNOWN)

    def by_stance(self, stance: Stance | str) -> tuple[Evidence, ...]:
        # Coerced, because a template passes the name as a string and an
        # identity check against a str would quietly match nothing — an
        # empty "against" list that reads as "nothing against".
        wanted = Stance(stance)
        return tuple(item for item in self.items if item.stance is wanted)
