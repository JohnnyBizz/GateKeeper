"""The dashboard — specification section 27.

Phase 1 renders the pages server-side with Jinja. The pages that have
real data behind them show it; the pages whose engines arrive in later
phases say which phase and show nothing else. A dashboard that fills
an empty panel with a plausible-looking number is the exact failure
mode section 42 forbids, so the empty state is explicit everywhere.

Why not Next.js yet: everything on these pages is server state, and a
React build step buys nothing until the scanner and charts arrive in
Phase 2. The API is already JSON-first, so moving the front end then
costs the templates and nothing else.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from gtcc.api.deps import AppContext, Principal, current_principal, get_context
from gtcc.domain.money import D

router = APIRouter(tags=["dashboard"])

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "web" / "templates"))

#: Page, route, and which phase delivers its content.
PAGES = [
    ("command", "/", "Command Center", 1),
    ("scanner", "/scanner", "Market Scanner", 2),
    ("terminal", "/terminal", "Trade Terminal", 2),
    ("agents", "/agents", "Agent Room", 3),
    ("positions", "/positions", "Positions", 1),
    ("journal", "/journal", "Journal", 4),
    ("analytics", "/analytics", "Analytics", 4),
    ("backtest", "/backtest", "Backtest Lab", 4),
    ("risk", "/risk", "Risk Center", 1),
    ("settings", "/settings", "Settings", 1),
]


def _base(request: Request, context: AppContext, active: str) -> dict:
    settings = context.settings
    return {
        "request": request,
        "pages": PAGES,
        "active": active,
        "mode": str(settings.mode),
        "live_trading": settings.live_trading,
        "automatic_execution": settings.automatic_execution,
        "app_name": settings.app_name,
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, context: AppContext = Depends(get_context)) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "login.html", {"request": request, "app_name": context.settings.app_name}
    )


@router.get("/", response_class=HTMLResponse)
def command_center(
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> HTMLResponse:
    runtime = context.runtime
    account = runtime.account()
    state = runtime.ensure_state()
    breakers = state.breakers()
    positions = list(runtime.broker.get_positions())

    data = _base(request, context, "command")
    data.update(
        {
            "account": account,
            "state": state,
            "breakers": breakers,
            "positions": positions,
            "limits": runtime.limits,
            "daily_pnl": state.realised_pnl_today,
            "daily_loss_pct": state.daily_loss_fraction() * D(100),
            "drawdown_pct": state.drawdown_fraction(account.equity) * D(100),
            # Nothing populates these until the scanner and regime engine
            # exist. They are rendered as "awaiting Phase 2", not as zero.
            "regime": None,
            "best_setups": None,
            "upcoming_events": None,
        }
    )
    return TEMPLATES.TemplateResponse(request, "command.html", data)


@router.get("/positions", response_class=HTMLResponse)
def positions_page(
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> HTMLResponse:
    data = _base(request, context, "positions")
    data["positions"] = list(context.runtime.broker.get_positions())
    data["orders"] = list(context.runtime.broker.get_orders())
    return TEMPLATES.TemplateResponse(request, "positions.html", data)


@router.get("/risk", response_class=HTMLResponse)
def risk_page(
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> HTMLResponse:
    runtime = context.runtime
    account = runtime.account()
    state = runtime.ensure_state()
    data = _base(request, context, "risk")

    try:
        broker_health = runtime.broker.health()
    except Exception as exc:  # pragma: no cover - adapter failure path
        broker_health = None
        data["broker_error"] = str(exc)

    data.update(
        {
            "account": account,
            "state": state,
            "breakers": state.breakers(),
            "limits": runtime.limits,
            "daily_loss_pct": state.daily_loss_fraction() * D(100),
            "weekly_loss_pct": state.weekly_loss_fraction() * D(100),
            "drawdown_pct": state.drawdown_fraction(account.equity) * D(100),
            "broker_health": broker_health,
            "reconciliation": runtime.last_reconciliation,
            "csrf_token": principal.csrf_token,
        }
    )
    return TEMPLATES.TemplateResponse(request, "risk.html", data)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> HTMLResponse:
    data = _base(request, context, "settings")
    data.update(
        {
            "settings_view": context.settings.redacted(),
            "limits": context.runtime.limits,
            "csrf_token": principal.csrf_token,
            "user": principal.user,
        }
    )
    return TEMPLATES.TemplateResponse(request, "settings.html", data)


@router.get("/scanner", response_class=HTMLResponse)
@router.get("/terminal", response_class=HTMLResponse)
@router.get("/agents", response_class=HTMLResponse)
@router.get("/journal", response_class=HTMLResponse)
@router.get("/analytics", response_class=HTMLResponse)
@router.get("/backtest", response_class=HTMLResponse)
def pending_page(
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> HTMLResponse:
    """Pages whose engines land in a later phase.

    They are routed and navigable now so the shell is complete, and
    they state what is missing instead of showing placeholder numbers
    that could be mistaken for data.
    """
    path = request.url.path
    page = next((entry for entry in PAGES if entry[1] == path), PAGES[0])
    data = _base(request, context, page[0])
    data.update({"title": page[2], "phase": page[3]})
    return TEMPLATES.TemplateResponse(request, "pending.html", data)
