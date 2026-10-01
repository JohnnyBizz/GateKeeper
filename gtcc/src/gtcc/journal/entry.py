"""Build a journal row from a submission, including the refusals.

Section 24 says the journal records every considered setup. The reason is
measurement: a journal of taken trades can only ever answer "were the
trades I took any good". The interesting question is whether the
refusals were right, and that one is unanswerable unless the refusal was
written down at the time, with the verdict that caused it.

Two rules shape this module.

Nothing is invented. A field the platform does not know is None, which
reaches the database as NULL. Realised P&L on an order that has just been
placed is not zero, it is unknown, and a column of zeros would quietly
average into every later performance statistic.

Absent context is distinguishable from empty context. If nobody passed a
structure report, the row says the context was not supplied. An empty
``{}`` would read as "the structure engine looked and found nothing",
which is a claim about the market rather than about the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from gtcc.domain.enums import Side, TradingMode
from gtcc.domain.orders import Order, OrderRequest
from gtcc.risk.engine import RiskVerdict

#: Written into a context column when the caller supplied nothing, so the
#: row cannot be read as "analysed, found nothing".
NOT_SUPPLIED: dict[str, Any] = {"_not_supplied": True}


class Outcome:
    """The outcome values this writer produces."""

    TAKEN = "TAKEN"
    REJECTED_BY_RISK = "REJECTED_BY_RISK"
    #: Risk approved it and the venue would not take it.
    REJECTED_BY_VENUE = "REJECTED_BY_VENUE"


@dataclass(frozen=True, slots=True)
class TradeAnnotations:
    """The analysis context behind a setup, as the caller saw it.

    Optional in every part. A strategy that ran the structure engine
    passes its report; a manual order from the dashboard passes nothing,
    and the row says so rather than implying a flat market.
    """

    timeframe: str | None = None
    regime: str | None = None
    session: str | None = None
    structure: Mapping[str, Any] | None = None
    indicators: Mapping[str, Any] | None = None
    agent_outputs: Mapping[str, Any] | None = None
    ai_decision: Mapping[str, Any] | None = None
    news: Sequence[Any] | None = None
    macro: Sequence[Any] | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """A row, as plain data, before it touches a database."""

    trade_id: str
    #: The venue's account identifier, used to attach the row to an account.
    account_external_id: str
    considered_at: datetime
    symbol: str
    market: str
    strategy: str
    direction: str
    mode: str
    outcome: str
    timeframe: str | None = None
    planned_entry: Decimal | None = None
    planned_stop: Decimal | None = None
    planned_targets: list[str] = field(default_factory=list)
    planned_size: Decimal | None = None
    planned_risk: Decimal | None = None
    reward_risk: Decimal | None = None
    actual_entry: Decimal | None = None
    actual_size: Decimal | None = None
    fees: Decimal | None = None
    regime: str | None = None
    session: str | None = None
    data_quality: str | None = None
    risk_verdict: dict[str, Any] = field(default_factory=dict)
    market_structure: dict[str, Any] = field(default_factory=dict)
    indicators: dict[str, Any] = field(default_factory=dict)
    agent_outputs: dict[str, Any] = field(default_factory=dict)
    ai_decision: dict[str, Any] = field(default_factory=dict)
    news_context: list[Any] = field(default_factory=list)
    macro_context: list[Any] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)
    notes: str | None = None
    opened_at: datetime | None = None


def _verdict_json(verdict: RiskVerdict) -> dict[str, Any]:
    """The verdict, flattened, including the checks that passed.

    Storing only the failures would make a rejection auditable and an
    approval unauditable, and "why was this allowed" is a question worth
    being able to answer about a loss.
    """
    return {
        "action": str(verdict.action),
        "approved_quantity": str(verdict.approved_quantity),
        "requested_quantity": (
            str(verdict.requested_quantity)
            if verdict.requested_quantity is not None
            else None
        ),
        "risk_at_stop": (
            str(verdict.approved_risk) if verdict.approved_risk is not None else None
        ),
        "reward_risk": (
            str(verdict.reward_risk.ratio) if verdict.reward_risk is not None else None
        ),
        "binding_limits": [str(check.code) for check in verdict.binding_limits],
        "failures": [
            {"code": str(check.code), "detail": check.detail}
            for check in verdict.failures
        ],
        "checks": [
            {
                "code": str(check.code),
                "outcome": str(check.outcome),
                "detail": check.detail,
                "limit": check.limit,
                "observed": check.observed,
                "reduced_to": (
                    str(check.reduced_to) if check.reduced_to is not None else None
                ),
            }
            for check in verdict.checks
        ],
    }


def entry_for(
    request: OrderRequest,
    verdict: RiskVerdict,
    *,
    trade_id: str,
    account_external_id: str,
    now: datetime,
    mode: TradingMode,
    order: Order | None = None,
    placed: bool = False,
    data_quality: str | None = None,
    annotations: TradeAnnotations | None = None,
    venue_detail: str | None = None,
) -> JournalEntry:
    """One row for one considered setup, taken or not."""
    annotations = annotations or TradeAnnotations()

    if placed:
        outcome = Outcome.TAKEN
    elif verdict.allowed:
        # Risk said yes and it still did not reach the venue. Recording
        # this as REJECTED_BY_RISK would blame the risk engine for a
        # broker refusal and corrupt any later study of the limits.
        outcome = Outcome.REJECTED_BY_VENUE
    else:
        outcome = Outcome.REJECTED_BY_RISK

    notes = annotations.notes
    if venue_detail:
        notes = f"{notes}\n{venue_detail}" if notes else venue_detail

    return JournalEntry(
        trade_id=trade_id,
        account_external_id=account_external_id,
        considered_at=now,
        symbol=request.symbol,
        market=str(request.market),
        strategy=request.strategy or "unattributed",
        direction=str(request.side) if request.side is not None else str(Side.BUY),
        mode=str(mode),
        outcome=outcome,
        timeframe=annotations.timeframe,
        planned_entry=request.limit_price,
        planned_stop=request.protective_stop,
        planned_targets=[str(target) for target in request.targets],
        # The approved size, not the requested one: the requested number is
        # what somebody asked for and the approved number is what the
        # platform stood behind.
        planned_size=verdict.approved_quantity if verdict.allowed else None,
        planned_risk=verdict.approved_risk,
        reward_risk=verdict.reward_risk.ratio if verdict.reward_risk else None,
        actual_entry=order.average_fill_price if order is not None else None,
        actual_size=order.filled_quantity if order is not None else None,
        # Fees charged SO FAR on a placed order, which for an unfilled one
        # is a true zero. On a setup that never reached the venue it is
        # None: there is no trade to have charged anything, and a zero
        # would average into every later cost statistic as a free trade.
        fees=order.fees_paid if (order is not None and placed) else None,
        regime=annotations.regime,
        session=annotations.session,
        data_quality=data_quality,
        risk_verdict=_verdict_json(verdict),
        market_structure=(
            dict(annotations.structure)
            if annotations.structure is not None
            else dict(NOT_SUPPLIED)
        ),
        indicators=(
            dict(annotations.indicators)
            if annotations.indicators is not None
            else dict(NOT_SUPPLIED)
        ),
        agent_outputs=(
            dict(annotations.agent_outputs)
            if annotations.agent_outputs is not None
            else dict(NOT_SUPPLIED)
        ),
        ai_decision=(
            dict(annotations.ai_decision)
            if annotations.ai_decision is not None
            else dict(NOT_SUPPLIED)
        ),
        news_context=list(annotations.news) if annotations.news is not None else [],
        macro_context=list(annotations.macro) if annotations.macro is not None else [],
        order_ids=(
            [order.client_order_id]
            + ([order.broker_order_id] if order.broker_order_id else [])
            if order is not None
            else []
        ),
        notes=notes,
        opened_at=order.created_at if (order is not None and placed) else None,
    )
