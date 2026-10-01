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

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from gtcc.domain.enums import Market
from gtcc.domain.money import D
from gtcc.risk.safety import Trip, TripReason
from gtcc.risk.state import RiskState, fresh_state
from gtcc.journal.entry import JournalEntry
from gtcc.storage.models import (
    ExecutionTrip,
    RiskStateRow,
    TradeJournalEntry,
    TradingAccount,
)


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


class ExecutionLatchRepository:
    """Persists the latched safety trips, and only those.

    Deliberately asymmetric with the rest of the execution state. Live
    arming is never written here: it is a decision a person made about
    a running process, and a restart must invalidate it. Trips are
    always written: the process may have died *because* of the
    condition that tripped it, and coming back clear would hide that.
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def open_trips(self, account_external_id: str) -> tuple[Trip, ...]:
        """Every trip that has not been explicitly cleared."""
        with self._session_factory() as session:
            account = session.scalar(
                select(TradingAccount).where(
                    TradingAccount.external_id == account_external_id
                )
            )
            if account is None:
                return ()
            rows = session.scalars(
                select(ExecutionTrip)
                .where(ExecutionTrip.account_id == account.id)
                .where(ExecutionTrip.cleared_at.is_(None))
                .order_by(ExecutionTrip.occurred_at)
            ).all()
            return tuple(
                Trip(
                    reason=TripReason(row.reason),
                    detail=row.detail,
                    occurred_at=row.occurred_at,
                )
                for row in rows
            )

    def record(self, account_external_id: str, trip: Trip, equity: Decimal) -> None:
        """Write a trip. Idempotent per open reason.

        A condition that keeps failing on every poll must not grow an
        unbounded table, and the first occurrence is the one that says
        when the problem actually started.
        """
        with self._session_factory() as session:
            account = _ensure_account(session, account_external_id, equity)
            already = session.scalar(
                select(ExecutionTrip)
                .where(ExecutionTrip.account_id == account.id)
                .where(ExecutionTrip.reason == str(trip.reason))
                .where(ExecutionTrip.cleared_at.is_(None))
            )
            if already is not None:
                return
            session.add(
                ExecutionTrip(
                    account_id=account.id,
                    reason=str(trip.reason),
                    detail=trip.detail[:1024],
                    occurred_at=trip.occurred_at,
                )
            )

    def clear(
        self, account_external_id: str, *, actor: str, now: datetime, equity: Decimal
    ) -> int:
        """Close every open trip. Returns how many were cleared."""
        with self._session_factory() as session:
            account = _ensure_account(session, account_external_id, equity)
            rows = session.scalars(
                select(ExecutionTrip)
                .where(ExecutionTrip.account_id == account.id)
                .where(ExecutionTrip.cleared_at.is_(None))
            ).all()
            for row in rows:
                row.cleared_at = now
                row.cleared_by = actor
            return len(rows)

    def history(self, account_external_id: str, limit: int = 50) -> list[ExecutionTrip]:
        with self._session_factory() as session:
            account = session.scalar(
                select(TradingAccount).where(
                    TradingAccount.external_id == account_external_id
                )
            )
            if account is None:
                return []
            return list(
                session.scalars(
                    select(ExecutionTrip)
                    .where(ExecutionTrip.account_id == account.id)
                    .order_by(ExecutionTrip.occurred_at.desc())
                    .limit(limit)
                ).all()
            )


def _ensure_account(session: DbSession, external_id: str, equity: Decimal) -> TradingAccount:
    account = session.scalar(
        select(TradingAccount).where(TradingAccount.external_id == external_id)
    )
    if account is None:
        account = TradingAccount(
            external_id=external_id, broker="paper", mode="PAPER",
            currency="USD", starting_equity=D(equity),
        )
        session.add(account)
        session.flush()
    return account


class TradeJournalRepository:
    """Writes journal rows, including the setups that were refused.

    Section 24 keeps every considered setup. The reason is that a journal
    of taken trades can only answer "were my trades any good", and the
    more useful question — "were my refusals right" — needs the refusal
    written down with the verdict that caused it. Filtering to taken
    trades would make the limits unfalsifiable.
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def record(self, entry: JournalEntry, *, equity: Decimal) -> int:
        """Write one row and return its id.

        Raises on failure. Swallowing the error here would leave the
        caller believing the trade was recorded; the caller decides what
        an unrecorded trade means, and for a placed order it means
        something serious.
        """
        with self._session_factory() as session:
            account = _ensure_account(session, entry.account_external_id, equity)
            row = TradeJournalEntry(
                trade_id=entry.trade_id,
                account_id=account.id,
                considered_at=entry.considered_at,
                symbol=entry.symbol,
                market=entry.market,
                strategy=entry.strategy,
                direction=entry.direction,
                timeframe=entry.timeframe,
                mode=entry.mode,
                outcome=entry.outcome,
                planned_entry=entry.planned_entry,
                planned_stop=entry.planned_stop,
                planned_targets=entry.planned_targets,
                planned_size=entry.planned_size,
                planned_risk=entry.planned_risk,
                reward_risk=entry.reward_risk,
                actual_entry=entry.actual_entry,
                actual_size=entry.actual_size,
                regime=entry.regime,
                session=entry.session,
                data_quality=entry.data_quality,
                risk_verdict=entry.risk_verdict,
                market_structure=entry.market_structure,
                indicators=entry.indicators,
                agent_outputs=entry.agent_outputs,
                ai_decision=entry.ai_decision,
                news_context=entry.news_context,
                macro_context=entry.macro_context,
                order_ids=entry.order_ids,
                notes=entry.notes,
                opened_at=entry.opened_at,
            )
            if entry.fees is not None:
                row.fees = entry.fees
            session.add(row)
            session.commit()
            return row.id

    def recent(
        self, account_external_id: str, *, limit: int = 50, outcome: str | None = None
    ) -> list[TradeJournalEntry]:
        with self._session_factory() as session:
            account = session.scalar(
                select(TradingAccount).where(
                    TradingAccount.external_id == account_external_id
                )
            )
            if account is None:
                return []
            query = (
                select(TradeJournalEntry)
                .where(TradeJournalEntry.account_id == account.id)
                .order_by(TradeJournalEntry.considered_at.desc())
                .limit(limit)
            )
            if outcome is not None:
                query = query.where(TradeJournalEntry.outcome == outcome)
            return list(session.scalars(query).all())

    def count(self, account_external_id: str) -> dict[str, int]:
        """Rows per outcome, so a caller can see refusals are being kept."""
        with self._session_factory() as session:
            account = session.scalar(
                select(TradingAccount).where(
                    TradingAccount.external_id == account_external_id
                )
            )
            if account is None:
                return {}
            rows = session.scalars(
                select(TradeJournalEntry).where(
                    TradeJournalEntry.account_id == account.id
                )
            ).all()
            counts: dict[str, int] = {}
            for row in rows:
                counts[row.outcome] = counts.get(row.outcome, 0) + 1
            return counts
