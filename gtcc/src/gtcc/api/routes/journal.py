"""Reading the trade journal.

Read-only. The journal is written by `TradingRuntime.submit` and nowhere
else, so there is no endpoint here that creates or edits a row: a journal
somebody can edit after the fact is not evidence.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from gtcc.api.deps import AppContext, Principal, current_principal, get_context
from gtcc.api.schemas import JournalOut, JournalRowOut

router = APIRouter(prefix="/api", tags=["journal"])


@router.get("/journal", response_model=JournalOut)
def read_journal(
    limit: int = Query(default=50, ge=1, le=500),
    outcome: str | None = Query(default=None),
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> JournalOut:
    runtime = context.runtime
    store = runtime.journal_store
    if store is None:
        # Not an empty journal — no journal. Saying "no trades" here would
        # be a claim about trading rather than about configuration.
        return JournalOut(counts={"_no_journal_configured": 1}, rows=[])

    account_id = runtime.account().account_id
    rows = store.recent(account_id, limit=limit, outcome=outcome)
    return JournalOut(
        counts=store.count(account_id),
        rows=[
            JournalRowOut(
                trade_id=row.trade_id,
                considered_at=row.considered_at,
                symbol=row.symbol,
                market=row.market,
                strategy=row.strategy,
                direction=row.direction,
                timeframe=row.timeframe,
                mode=row.mode,
                outcome=row.outcome,
                planned_entry=row.planned_entry,
                planned_stop=row.planned_stop,
                planned_targets=[str(target) for target in (row.planned_targets or [])],
                planned_size=row.planned_size,
                planned_risk=row.planned_risk,
                reward_risk=row.reward_risk,
                actual_entry=row.actual_entry,
                actual_size=row.actual_size,
                realised_pnl=row.realised_pnl,
                regime=row.regime,
                data_quality=row.data_quality,
                failures=[
                    f"{failure.get('code')}: {failure.get('detail')}"
                    for failure in (row.risk_verdict or {}).get("failures", [])
                ],
                notes=row.notes,
            )
            for row in rows
        ],
    )
