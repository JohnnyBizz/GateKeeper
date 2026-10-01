"""Runtime execution state: live arming and the latched safety breaker.

This module exists because of a specific failure the first version
allowed. Live activation was a configuration field,
``GTCC_LIVE_CONFIRMATION``, validated at startup. A process whose
environment still carried the phrase booted straight into a state where
live orders were permitted, with no human present. Configuration that
survives a restart is deployment *permission*; it is not a person
deciding, now, that this process should trade real money.

So the two are separated and never conflated:

**Deployment permission** is :attr:`Settings.allow_live_trading`. It
means "this deployment is allowed to offer live mode" and nothing more.
It is static, it survives restarts, and on its own it arms nothing.

**Runtime arming** is :class:`ExecutionState` in this module. It lives
in memory, it starts disarmed on every single process start, and
reaching the armed state requires an authenticated owner to pass the
exact confirmation phrase through
:meth:`ExecutionState.arm_live`. There is no code path that arms it
from configuration, and that is asserted in CI.

The second thing here is the **latched breaker**. A critical safety
event trips it, and recovery of the underlying dependency does not
untrip it. The breaker is cleared only by an authorised person calling
:meth:`ExecutionState.reset_breaker`, and clearing it leaves the system
*disarmed*: arming is a separate deliberate act. Two actions to resume
live trading after a safety event is the intended cost.

Nothing in :mod:`gtcc.ai` can reach any of this. The arm, trip and
reset methods take a human actor's identity, and the risk engine reads
the resulting state without being able to change it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

from gtcc.domain.enums import TradingMode
from gtcc.domain.market_data import utcnow

#: Typed by hand, exactly, to arm live trading. Deliberately tedious.
#: It is compared against a value supplied in a request, never against
#: anything read from the environment.
LIVE_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"


class TripReason(StrEnum):
    """Why execution was latched off. Stable codes for alerts and audit."""

    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    INVALID_MARKET_DATA = "INVALID_MARKET_DATA"
    BROKER_UNHEALTHY = "BROKER_UNHEALTHY"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    UNKNOWN_ORDER_STATE = "UNKNOWN_ORDER_STATE"
    ACCOUNT_STATE_UNKNOWN = "ACCOUNT_STATE_UNKNOWN"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    KILL_SWITCH = "KILL_SWITCH"
    OPERATOR = "OPERATOR"


class LiveArmingError(RuntimeError):
    """Live arming was refused. The message says which gate failed."""


@dataclass(frozen=True, slots=True)
class Trip:
    """One latched safety event."""

    reason: TripReason
    detail: str
    occurred_at: datetime

    def describe(self) -> str:
        return f"{self.reason} at {self.occurred_at.isoformat()}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ExecutionState:
    """What this process is currently permitted to do.

    Immutable: every transition returns a new instance, so a state
    cannot be edited in place by a caller that happens to hold a
    reference to it.
    """

    #: The mode this process is operating in right now. Starts at the
    #: configured default, which can never be LIVE.
    mode: TradingMode = TradingMode.PAPER
    #: True only after a human armed it during THIS process's lifetime.
    live_armed: bool = False
    armed_at: datetime | None = None
    armed_by: str | None = None
    #: Latched safety events. Non-empty means execution is off.
    trips: tuple[Trip, ...] = ()
    last_reset_at: datetime | None = None
    last_reset_by: str | None = None

    # -- queries ---------------------------------------------------------

    @property
    def tripped(self) -> bool:
        return bool(self.trips)

    @property
    def trip_reasons(self) -> tuple[TripReason, ...]:
        return tuple(trip.reason for trip in self.trips)

    @property
    def live_permitted(self) -> bool:
        """May a live order be sent right now?

        Three things must hold at once, and no two of them imply the
        third: the process is in live mode, a human armed it during this
        process's life, and nothing has tripped since.
        """
        return self.mode is TradingMode.LIVE and self.live_armed and not self.tripped

    @property
    def new_trades_blocked(self) -> bool:
        """A latched trip stops new orders in every mode, paper included.

        Paper results are the evidence base for whether this platform
        works. Continuing to record them while the data feed is stale or
        the account cannot be reconciled would poison that record, so
        the breaker is not live-only.
        """
        return self.tripped

    def describe(self) -> str:
        if self.tripped:
            return "TRIPPED — " + "; ".join(trip.describe() for trip in self.trips)
        if self.live_permitted:
            return f"LIVE, armed by {self.armed_by} at {self.armed_at}"
        return f"{self.mode}, live not armed"

    # -- transitions -------------------------------------------------------

    def arm_live(
        self,
        *,
        actor: str,
        confirmation: str,
        deployment_allows_live: bool,
        now: datetime | None = None,
    ) -> "ExecutionState":
        """Arm live execution, or raise saying which gate refused.

        Four independent conditions, checked in the order a person would
        hit them. None of them is implied by any other, and none of them
        can be satisfied by configuration alone.
        """
        if not deployment_allows_live:
            raise LiveArmingError(
                "this deployment does not permit live trading. Set "
                "GTCC_ALLOW_LIVE_TRADING=true in the server environment and "
                "restart. That permits arming; it does not arm anything."
            )
        if self.tripped:
            raise LiveArmingError(
                "a safety breaker is latched and must be reset by an "
                "authorised person first: "
                + "; ".join(trip.describe() for trip in self.trips)
            )
        if confirmation != LIVE_CONFIRMATION_PHRASE:
            raise LiveArmingError(
                f"the confirmation phrase must be exactly "
                f"{LIVE_CONFIRMATION_PHRASE!r}"
            )
        if not actor:
            raise LiveArmingError("arming must record who did it")

        now = now or utcnow()
        return replace(
            self, mode=TradingMode.LIVE, live_armed=True, armed_at=now, armed_by=actor
        )

    def disarm_live(self, *, actor: str, now: datetime | None = None) -> "ExecutionState":
        """Stand down from live without tripping a breaker."""
        return replace(
            self,
            mode=TradingMode.PAPER,
            live_armed=False,
            armed_at=None,
            armed_by=None,
            last_reset_at=now or utcnow(),
            last_reset_by=actor,
        )

    def trip(
        self, reason: TripReason, detail: str, *, now: datetime | None = None
    ) -> "ExecutionState":
        """Latch execution off.

        Disarms live in the same step, so recovering the dependency
        cannot leave the system quietly armed again. Re-arming needs a
        reset and then a fresh confirmation.

        Tripping twice for the same reason does not stack: the first
        occurrence is the one that matters and keeping it preserves when
        the problem actually started.
        """
        if reason in self.trip_reasons:
            return replace(self, live_armed=False)
        trip = Trip(reason=reason, detail=detail, occurred_at=now or utcnow())
        return replace(self, trips=self.trips + (trip,), live_armed=False)

    def reset_breaker(
        self,
        *,
        actor: str,
        healthy: bool,
        unhealthy_detail: str = "",
        now: datetime | None = None,
    ) -> "ExecutionState":
        """Clear the latch. Requires an actor and a healthy system.

        Clearing leaves the process **disarmed**. Resuming live trading
        then needs :meth:`arm_live` with the confirmation phrase again,
        so recovering from a safety event always costs two deliberate
        human actions rather than one.
        """
        if not actor:
            raise LiveArmingError("a breaker reset must record who performed it")
        if not self.tripped:
            return self
        if not healthy:
            raise LiveArmingError(
                "the condition that tripped the breaker has not cleared: "
                + (unhealthy_detail or "system still reports unhealthy")
            )
        now = now or utcnow()
        return replace(
            self,
            trips=(),
            live_armed=False,
            armed_at=None,
            armed_by=None,
            mode=TradingMode.PAPER if self.mode is TradingMode.LIVE else self.mode,
            last_reset_at=now,
            last_reset_by=actor,
        )

    def set_mode(self, mode: TradingMode, *, actor: str) -> "ExecutionState":
        """Switch between the non-live modes.

        LIVE is deliberately unreachable here. It is reached only
        through :meth:`arm_live`, so there is exactly one door and it
        has the phrase on it.
        """
        if mode is TradingMode.LIVE:
            raise LiveArmingError(
                "LIVE is not a mode that can be switched into. Arm live "
                "trading with the confirmation phrase instead."
            )
        return replace(self, mode=mode, live_armed=False, armed_at=None, armed_by=None)


def initial_state(default_mode: TradingMode) -> ExecutionState:
    """The state every process starts in.

    Disarmed, untripped, and never LIVE, whatever the environment says.
    """
    if default_mode is TradingMode.LIVE:
        raise LiveArmingError(
            "LIVE cannot be a startup mode. A process must never boot into "
            "armed live execution; arming is a runtime action by a person."
        )
    return ExecutionState(mode=default_mode)
