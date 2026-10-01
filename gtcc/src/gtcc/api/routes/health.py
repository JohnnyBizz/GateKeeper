"""Liveness and readiness.

Unauthenticated, because a monitor should not need a session, and
deliberately thin: it reports whether the platform can trade, never
anything about credentials or account values.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from gtcc.api.deps import AppContext, get_context
from gtcc.api.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthOut)
def health(context: AppContext = Depends(get_context)) -> HealthOut:
    runtime = context.runtime
    settings = context.settings

    try:
        broker_health = runtime.broker.health()
        broker_ok, broker_detail = broker_health.healthy, broker_health.detail
    except Exception as exc:
        broker_ok, broker_detail = False, f"broker adapter error: {exc}"

    try:
        data_health = runtime.data.health()
        data_ok, data_detail = data_health.healthy, data_health.detail
    except Exception as exc:
        data_ok, data_detail = False, f"data adapter error: {exc}"

    limits = runtime.limits
    ready = broker_ok and data_ok and not limits.is_example

    return HealthOut(
        status="ready" if ready else "degraded",
        mode=settings.mode,
        live_trading=settings.live_trading,
        automatic_execution=settings.automatic_execution,
        broker_healthy=broker_ok,
        data_healthy=data_ok,
        risk_limits_loaded=True,
        risk_limits_are_example=limits.is_example,
        detail="; ".join(part for part in (broker_detail, data_detail) if part),
    )
