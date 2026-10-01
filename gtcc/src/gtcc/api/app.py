"""The FastAPI application.

Assembled by :func:`create_app` so tests can build an instance with
their own settings and adapters rather than reaching for globals.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from gtcc.api.deps import AppContext
from gtcc.api.routes import auth as auth_routes
from gtcc.api.routes import dashboard as dashboard_routes
from gtcc.api.routes import health as health_routes
from gtcc.api.routes import scan as scan_routes
from gtcc.api.routes import trading as trading_routes
from gtcc.api.security import CookieSigner, RateLimiter
from gtcc.config import Settings, get_settings
from gtcc.logging_setup import configure_logging, log_event, new_correlation_id
from gtcc.runtime import TradingRuntime

logger = logging.getLogger("gtcc.api")

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

#: Sent on every response. The dashboard uses no third-party scripts,
#: so the policy can be strict without breaking the page.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}


def create_app(
    *, settings: Settings | None = None, runtime: TradingRuntime | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if runtime is None:  # pragma: no cover - production wiring
        from gtcc.bootstrap import build_runtime

        runtime = build_runtime(settings)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "Multi-market trading research and execution platform. "
            "Paper trading by default; live execution is disabled until "
            "explicitly enabled by the account owner."
        ),
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
    )

    app.state.context = AppContext(
        settings=settings,
        runtime=runtime,
        signer=CookieSigner(settings.secret_key.get_secret_value()),
        login_limiter=RateLimiter(limit=settings.login_rate_limit_per_minute),
    )

    @app.middleware("http")
    async def observability(request: Request, call_next):
        correlation = new_correlation_id()
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log_event(
                logger, logging.ERROR, "unhandled request error",
                path=request.url.path, method=request.method,
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        response.headers["X-Correlation-Id"] = correlation
        log_event(
            logger, logging.INFO, "request",
            method=request.method, path=request.url.path,
            status=response.status_code, duration_ms=round(elapsed_ms, 2),
        )
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Never leak an internal error to the client.

        The detail goes to the log with a correlation id; the caller
        gets that id and nothing else.
        """
        log_event(
            logger, logging.ERROR, "unhandled exception",
            path=request.url.path, error=str(exc), error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "internal error", "correlation_id": new_correlation_id()},
        )

    app.include_router(health_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(trading_routes.router)
    app.include_router(scan_routes.router)
    app.include_router(dashboard_routes.router)

    static_dir = WEB_ROOT / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    log_event(
        logger, logging.INFO, "application started",
        mode=str(runtime.ensure_execution().mode),
        deployment_allows_live=settings.allow_live_trading,
        live_armed=runtime.ensure_execution().live_armed,
        automatic_execution=settings.automatic_execution,
    )
    return app
