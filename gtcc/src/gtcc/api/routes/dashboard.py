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
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from gtcc.api.deps import AppContext, Principal, current_principal, get_context
from gtcc.domain.money import D

router = APIRouter(tags=["dashboard"])

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "web" / "templates"))

#: Page, route, label, the phase that delivers it, and whether it is BUILT.
#:
#: The last flag is what the navigation badge reads. Keying the badge off
#: the phase number alone meant a page kept advertising "P2" after its
#: engine landed, so a working page looked unbuilt.
PAGES = [
    ("command", "/", "Command Center", 1, True),
    ("scanner", "/scanner", "Market Scanner", 2, True),
    ("terminal", "/terminal", "Trade Terminal", 2, False),
    ("agents", "/agents", "Agent Room", 3, False),
    ("positions", "/positions", "Positions", 1, True),
    ("journal", "/journal", "Journal", 2, True),
    ("analytics", "/analytics", "Analytics", 4, True),
    ("backtest", "/backtest", "Backtest Lab", 4, False),
    ("risk", "/risk", "Risk Center", 1, True),
    ("settings", "/settings", "Settings", 1, True),
]


def _base(request: Request, context: AppContext, active: str) -> dict:
    settings = context.settings
    execution = context.runtime.ensure_execution()
    return {
        "request": request,
        "pages": PAGES,
        "active": active,
        "mode": str(execution.mode),
        "execution": execution,
        "deployment_allows_live": settings.allow_live_trading,
        "live_armed": execution.live_armed,
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
            "settings_view": context.settings.public_view(),
            "limits": context.runtime.limits,
            "csrf_token": principal.csrf_token,
            "user": principal.user,
        }
    )
    return TEMPLATES.TemplateResponse(request, "settings.html", data)


@router.get("/scanner", response_class=HTMLResponse)
def scanner_page(
    request: Request,
    symbols: str = "",
    timeframe: str = "15m",
    sort: str = "SIGNAL",
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> HTMLResponse:
    """Run a scan and show it, including what could not be read.

    The page renders `not_analysed` as its own panel saying in words that
    those rows are not findings. A scanner page that showed only the
    symbols it managed to read would be the most misleading screen in the
    platform: a short list looks like a quiet market.
    """
    from gtcc.domain.enums import Timeframe
    from gtcc.scanner import ScanSettings, SortKey

    data = _base(request, context, "scanner")
    requested = [part for part in symbols.replace(",", " ").split() if part]

    try:
        chosen_timeframe = Timeframe(timeframe)
    except ValueError:
        chosen_timeframe = Timeframe.M15
    try:
        chosen_sort = SortKey(sort)
    except ValueError:
        chosen_sort = SortKey.SIGNAL

    result = None
    rows: list = []
    silent: list[tuple[str, str]] = []
    if requested:
        scanner = context.runtime.scanner(ScanSettings(timeframe=chosen_timeframe))
        result = scanner.scan(
            requested,
            mode=context.runtime.ensure_execution().mode,
            now=context.runtime.clock(),
        )
        rows = list(result.ranked(chosen_sort))
        silent = [
            (row.symbol, reason)
            for row in rows
            if row.signal is None
            for reason in row.silent_because
        ]

    data.update(
        {
            "symbols_raw": symbols,
            "timeframe": str(chosen_timeframe),
            "sort": str(chosen_sort),
            "timeframes": [str(value) for value in Timeframe],
            "sort_keys": [str(value) for value in SortKey],
            "data_adapter": context.runtime.data.name,
            "result": result,
            "rows": rows,
            "silent": silent,
        }
    )
    return TEMPLATES.TemplateResponse(request, "scanner.html", data)


@router.get("/journal", response_class=HTMLResponse)
def journal_page(
    request: Request,
    outcome: str = "",
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> HTMLResponse:
    """The journal, with the refusals in it.

    When no database is configured the page says so rather than rendering
    an empty table, because an empty table reads as "nothing was traded".
    """
    from gtcc.journal.entry import Outcome

    data = _base(request, context, "journal")
    store = context.runtime.journal_store

    if store is None:
        data.update({"journal_configured": False, "rows": [], "counts": [], "outcome": "",
                     "filters": []})
        return TEMPLATES.TemplateResponse(request, "journal.html", data)

    account_id = context.runtime.account().account_id
    counts = store.count(account_id)
    raw = store.recent(account_id, limit=100, outcome=outcome or None)

    rows = []
    for row in raw:
        failures = (row.risk_verdict or {}).get("failures", [])
        rows.append(
            SimpleNamespace(
                considered_at=row.considered_at,
                symbol=row.symbol,
                strategy=row.strategy,
                direction=row.direction,
                outcome=row.outcome,
                planned_size=row.planned_size,
                planned_risk=row.planned_risk,
                reward_risk=row.reward_risk,
                why=(
                    "; ".join(
                        f"{failure.get('code')}" for failure in failures[:3]
                    )
                    or (row.notes or "")
                ),
            )
        )

    data.update(
        {
            "journal_configured": True,
            "counts": sorted(counts.items()),
            "rows": rows,
            "outcome": outcome,
            "filters": [
                ("", "All"),
                (Outcome.TAKEN, "Taken"),
                (Outcome.REJECTED_BY_RISK, "Refused by risk"),
                (Outcome.REJECTED_BY_VENUE, "Refused by venue"),
            ],
        }
    )
    return TEMPLATES.TemplateResponse(request, "journal.html", data)


@router.get("/terminal", response_class=HTMLResponse)
@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> HTMLResponse:
    """What the account actually did, measured like a backtest.

    Deliberately the same functions the backtester uses. Scoring paper
    trading one way and backtests another makes the only question worth
    asking of a backtest — did what it predicted happen — unanswerable,
    because any difference is then indistinguishable from a difference in
    measurement.
    """
    from gtcc.journal.analysis import summarise

    data = _base(request, context, "analytics")
    store = context.runtime.journal_store
    if store is None:
        data.update({"journal_configured": False, "summary": None})
        return TEMPLATES.TemplateResponse(request, "analytics.html", data)

    rows = store.recent(context.runtime.account().account_id, limit=1000)
    data.update(
        {
            "journal_configured": True,
            "summary": summarise(
                rows, starting_equity=context.runtime.account().equity
            ),
        }
    )
    return TEMPLATES.TemplateResponse(request, "analytics.html", data)


@router.get("/agents", response_class=HTMLResponse)
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
