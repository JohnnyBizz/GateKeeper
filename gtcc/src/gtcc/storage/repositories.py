"""Persistence for the risk state.

A tripped circuit breaker that a restart clears is not a circuit
breaker. The process can die for reasons that correlate with a bad day —
an exception storm, a venue outage, somebody pulling the plug — and
coming back up with a clean daily tally is the worst possible moment to
forget that trading was supposed to be stopped.

So the state is written after every change that matters and read back on
startup. The in-memory object remains the thing the engine sees; this
module only moves it to and from a row.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from gtcc.domain.enums import Market
from gtcc.domain.money import D
from gtcc.risk.state import RiskState, fresh_state
from gtcc.storage.models import RiskStateRow, TradingAccount


def _to_state(row: RiskStateRow, account_external_id: str) -> RiskState:
    return RiskState(
        account_id=account_external_id,
        day_start_equity=D(row.day_start_equity),
        week_start_equity=D(row.week_start_equity),
        peak_equity=D(row.peak_equity),
        realised_pnl_today=D(row.realised_pnl_today),
        realised_pnl_week=D(row.realised_pnl_week),
        consecutive_losses=row.consecutive_losses,
        trades_today=row.trades_today,
        current_day=date.fromisoformat(row.current_day),
        week_start=date.fromisoformat(row.week_start),
        kill_switch=row.kill_switch,
        trading_paused=row.trading_paused,
        disabled_symbols=frozenset(row.disabled_symbols or ()),
        disabled_strategies=frozenset(row.disabled_strategies or ()),
        disabled_markets=frozenset(Market(name) for name in (row.disabled_markets or ())),
        daily_breaker_tripped=row.daily_breaker_tripped,
        weekly_breaker_tripped=row.weekly_breaker_tripped,
        drawdown_breaker_tripped=row.drawdown_breaker_tripped,
        updated_at=row.updated_at,
    )


def _apply(row: RiskStateRow, state: RiskState) -> None:
    row.day_start_equity = state.day_start_equity
    row.week_start_equity = state.week_start_equity
    row.peak_equity = state.peak_equity
    row.realised_pnl_today = state.realised_pnl_today
    row.realised_pnl_week = state.realised_pnl_week
    row.consecutive_losses = state.consecutive_losses
    row.trades_today = state.trades_today
    row.current_day = state.current_day.isoformat()
    row.week_start = state.week_start.isoformat()
    row.kill_switch = state.kill_switch
    row.trading_paused = state.trading_paused
    row.disabled_symbols = sorted(state.disabled_symbols)
    row.disabled_strategies = sorted(state.disabled_strategies)
    row.disabled_markets = sorted(str(market) for market in state.disabled_markets)
    row.daily_breaker_tripped = state.daily_breaker_tripped
    row.weekly_breaker_tripped = state.weekly_breaker_tripped
    row.drawdown_breaker_tripped = state.drawdown_breaker_tripped


class RiskStateRepository:
    """Loads and saves :class:`RiskState` against the ``risk_state`` table."""

    def __init__(self, session_factory) -> None:
        #: A callable returning a context-managed session, so the
        #: repository never holds one open between calls.
        self._session_factory = session_factory

    def load(self, account_external_id: str, equity: Decimal) -> RiskState:
        """Read the stored state, or create one for a new account."""
        with self._session_factory() as session:
            account = self._account(session, account_external_id, equity)
            row = session.scalar(
                select(RiskStateRow).where(RiskStateRow.account_id == account.id)
            )
            if row is None:
                state = fresh_state(account_external_id, equity)
                row = RiskStateRow(
                    account_id=account.id,
                    day_start_equity=state.day_start_equity,
                    week_start_equity=state.week_start_equity,
                    peak_equity=state.peak_equity,
                    current_day=state.current_day.isoformat(),
                    week_start=state.week_start.isoformat(),
                )
                _apply(row, state)
                session.add(row)
                return state
            return _to_state(row, account_external_id)

    def save(self, state: RiskState, equity: Decimal) -> None:
        with self._session_factory() as session:
            account = self._account(session, state.account_id, equity)
            row = session.scalar(
                select(RiskStateRow).where(RiskStateRow.account_id == account.id)
            )
            if row is None:
                row = RiskStateRow(
                    account_id=account.id,
                    day_start_equity=state.day_start_equity,
                    week_start_equity=state.week_start_equity,
                    peak_equity=state.peak_equity,
                    current_day=state.current_day.isoformat(),
                    week_start=state.week_start.isoformat(),
                )
                session.add(row)
            _apply(row, state)

    def _account(
        self, session: DbSession, external_id: str, equity: Decimal
    ) -> TradingAccount:
        account = session.scalar(
            select(TradingAccount).where(TradingAccount.external_id == external_id)
        )
        if account is None:
            account = TradingAccount(
                external_id=external_id,
                broker="paper",
                mode="PAPER",
                currency="USD",
                starting_equity=D(equity),
            )
            session.add(account)
            session.flush()
        return account
