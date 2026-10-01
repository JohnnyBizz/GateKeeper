"""Risk state: what has already happened to this account today.

Separate from :mod:`gtcc.risk.limits` because limits are configuration
and this is a running tally. The engine reads both and compares.

Everything here is derived from *settled* results — realised profit and
loss on closed trades plus the account's reported equity. Nothing is
estimated from open positions, because an unrealised number moves on its
own and a circuit breaker that trips on a wick is a breaker that trips
at random.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from gtcc.domain.enums import Market
from gtcc.domain.market_data import utcnow
from gtcc.domain.money import ZERO, D


@dataclass(frozen=True, slots=True)
class BreakerStatus:
    """Why trading is restricted, if it is."""

    new_trades_blocked: bool = False
    live_execution_blocked: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def clear(self) -> bool:
        return not self.new_trades_blocked and not self.live_execution_blocked


@dataclass(frozen=True, slots=True)
class RiskState:
    """A point-in-time view of the account's risk posture.

    Persisted after every settled trade so a restart cannot reset a
    tripped breaker — a crash must never look like a fresh trading day.
    """

    account_id: str
    #: Equity at the start of the current trading day and week.
    day_start_equity: Decimal
    week_start_equity: Decimal
    peak_equity: Decimal
    realised_pnl_today: Decimal = ZERO
    realised_pnl_week: Decimal = ZERO
    consecutive_losses: int = 0
    trades_today: int = 0
    current_day: date = field(default_factory=lambda: utcnow().date())
    week_start: date = field(default_factory=lambda: utcnow().date())

    # -- operator switches (specification section 37) -----------------------
    kill_switch: bool = False
    trading_paused: bool = False
    disabled_symbols: frozenset[str] = frozenset()
    disabled_strategies: frozenset[str] = frozenset()
    disabled_markets: frozenset[Market] = frozenset()

    #: Set by the engine when a breaker trips; cleared only by an operator
    #: or by the daily/weekly roll.
    daily_breaker_tripped: bool = False
    weekly_breaker_tripped: bool = False
    drawdown_breaker_tripped: bool = False
    updated_at: datetime = field(default_factory=utcnow)

    # -- derived ------------------------------------------------------------

    def daily_loss_fraction(self) -> Decimal:
        """Today's realised loss as a fraction of the day's opening equity.

        Zero when the day is flat or up. Always non-negative.
        """
        if self.day_start_equity <= ZERO:
            return ZERO
        return max(ZERO, -self.realised_pnl_today) / self.day_start_equity

    def weekly_loss_fraction(self) -> Decimal:
        if self.week_start_equity <= ZERO:
            return ZERO
        return max(ZERO, -self.realised_pnl_week) / self.week_start_equity

    def drawdown_fraction(self, current_equity: Decimal) -> Decimal:
        peak = max(self.peak_equity, current_equity)
        if peak <= ZERO:
            return ZERO
        return max(ZERO, (peak - current_equity) / peak)

    def breakers(self) -> BreakerStatus:
        reasons: list[str] = []
        new_blocked = False
        live_blocked = False
        if self.kill_switch:
            reasons.append("emergency kill switch engaged")
            new_blocked = True
            live_blocked = True
        if self.trading_paused:
            reasons.append("trading paused by operator")
            new_blocked = True
        if self.daily_breaker_tripped:
            reasons.append("daily loss circuit breaker tripped")
            new_blocked = True
        if self.weekly_breaker_tripped:
            reasons.append("weekly loss circuit breaker tripped")
            new_blocked = True
        if self.drawdown_breaker_tripped:
            reasons.append("maximum drawdown reached; live execution disabled")
            new_blocked = True
            live_blocked = True
        return BreakerStatus(
            new_trades_blocked=new_blocked,
            live_execution_blocked=live_blocked,
            reasons=tuple(reasons),
        )

    # -- transitions ----------------------------------------------------------

    def record_settled_trade(self, realised_pnl: Decimal, *, now: datetime | None = None) -> "RiskState":
        """Fold one closed trade into the tally."""
        now = now or utcnow()
        pnl = D(realised_pnl)
        losses = self.consecutive_losses + 1 if pnl < ZERO else 0
        return replace(
            self,
            realised_pnl_today=self.realised_pnl_today + pnl,
            realised_pnl_week=self.realised_pnl_week + pnl,
            consecutive_losses=losses,
            trades_today=self.trades_today + 1,
            updated_at=now,
        )

    def mark_equity(self, equity: Decimal) -> "RiskState":
        """Update the high-water mark. It only ever rises."""
        return replace(self, peak_equity=max(self.peak_equity, D(equity)))

    def roll_day(self, equity: Decimal, *, today: date | None = None) -> "RiskState":
        """Start a new trading day.

        The daily breaker clears. The weekly and drawdown breakers do
        not: a new day is not a new week, and a drawdown is not undone
        by the clock.
        """
        today = today or utcnow().date()
        new_week = today.isocalendar().week != self.week_start.isocalendar().week
        return replace(
            self,
            current_day=today,
            day_start_equity=D(equity),
            realised_pnl_today=ZERO,
            trades_today=0,
            daily_breaker_tripped=False,
            week_start=today if new_week else self.week_start,
            week_start_equity=D(equity) if new_week else self.week_start_equity,
            realised_pnl_week=ZERO if new_week else self.realised_pnl_week,
            weekly_breaker_tripped=False if new_week else self.weekly_breaker_tripped,
            updated_at=utcnow(),
        )

    def trip(
        self,
        *,
        daily: bool = False,
        weekly: bool = False,
        drawdown: bool = False,
    ) -> "RiskState":
        return replace(
            self,
            daily_breaker_tripped=self.daily_breaker_tripped or daily,
            weekly_breaker_tripped=self.weekly_breaker_tripped or weekly,
            drawdown_breaker_tripped=self.drawdown_breaker_tripped or drawdown,
            updated_at=utcnow(),
        )

    def with_kill_switch(self, engaged: bool) -> "RiskState":
        return replace(self, kill_switch=engaged, updated_at=utcnow())

    def paused(self, paused: bool) -> "RiskState":
        return replace(self, trading_paused=paused, updated_at=utcnow())

    def disable_symbol(self, symbol: str) -> "RiskState":
        return replace(self, disabled_symbols=self.disabled_symbols | {symbol})

    def enable_symbol(self, symbol: str) -> "RiskState":
        return replace(self, disabled_symbols=self.disabled_symbols - {symbol})

    def disable_strategy(self, strategy: str) -> "RiskState":
        return replace(self, disabled_strategies=self.disabled_strategies | {strategy})

    def disable_market(self, market: Market) -> "RiskState":
        return replace(self, disabled_markets=self.disabled_markets | {market})


def fresh_state(account_id: str, equity: Decimal, *, now: datetime | None = None) -> RiskState:
    """A state for an account with no history. Used on first run only."""
    now = now or utcnow()
    today = now.date()
    return RiskState(
        account_id=account_id,
        day_start_equity=D(equity),
        week_start_equity=D(equity),
        peak_equity=D(equity),
        current_day=today,
        week_start=today - timedelta(days=today.weekday()),
        updated_at=now,
    )
