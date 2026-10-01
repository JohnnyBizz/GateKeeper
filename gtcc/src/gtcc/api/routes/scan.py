"""The scanner endpoint. Read-only by construction.

This route builds a scanner from the runtime's data adapter and the
strategy registry. It never touches `submit`, and the scanner it builds
holds no broker, so there is no path from here to an order.

The response always carries `summary`, `truncated` and `not_analysed`.
A caller that renders only `rows` and finds three of them must still be
able to tell "three symbols look interesting" from "thirty-seven symbols
could not be read".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from gtcc.api.deps import AppContext, Principal, current_principal, get_context
from gtcc.api.schemas import (
    ScanProposalOut,
    ScanRequestIn,
    ScanResultOut,
    ScanRowOut,
)
from gtcc.domain.enums import TradingMode
from gtcc.scanner import ScanResult, ScanRow, ScanSettings, SortKey

router = APIRouter(prefix="/api", tags=["scanner"])


def _row_out(row: ScanRow) -> ScanRowOut:
    signal = row.signal
    return ScanRowOut(
        symbol=row.symbol,
        status=str(row.status),
        detail=row.detail,
        market=str(row.market) if row.market is not None else None,
        price=row.price,
        change_pct=row.change_pct,
        atr=row.atr,
        atr_pct=row.atr_pct,
        relative_volume=row.relative_volume,
        spread_bps=row.spread_bps,
        trend=str(row.trend) if row.trend is not None else None,
        regime=str(row.regime) if row.regime is not None else None,
        structure_summary=row.structure_summary,
        data_quality=str(row.data_quality) if row.data_quality is not None else None,
        bars_seen=row.bars_seen,
        signal=(
            ScanProposalOut(
                strategy=signal.strategy,
                decision=str(signal.decision),
                rationale=signal.rationale,
                entry=signal.entry,
                stop=signal.stop,
                targets=list(signal.targets),
                conviction=signal.conviction,
            )
            if signal is not None
            else None
        ),
        silent_because=list(row.silent_because),
    )


def _result_out(result: ScanResult, sort_by: SortKey) -> ScanResultOut:
    return ScanResultOut(
        summary=result.summary(),
        requested=result.requested,
        requests_made=result.requests_made,
        request_budget=result.request_budget,
        truncated=result.truncated,
        started_at=result.started_at,
        finished_at=result.finished_at,
        rows=[_row_out(row) for row in result.ranked(sort_by)],
        not_analysed=[_row_out(row) for row in result.not_analysed],
    )


@router.post("/scan", response_model=ScanResultOut)
def scan(
    body: ScanRequestIn,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> ScanResultOut:
    try:
        sort_by = SortKey(body.sort_by)
    except ValueError:
        sort_by = SortKey.SIGNAL

    runtime = context.runtime
    scanner = runtime.scanner(
        ScanSettings(
            timeframe=body.timeframe,
            min_history=body.min_history,
            max_requests=body.max_requests,
        )
    )
    mode = runtime.ensure_execution().mode
    result = scanner.scan(
        [symbol.strip() for symbol in body.symbols if symbol.strip()],
        mode=mode if isinstance(mode, TradingMode) else TradingMode.PAPER,
        now=runtime.clock(),
    )
    return _result_out(result, sort_by)
