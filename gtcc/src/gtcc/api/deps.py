"""FastAPI dependencies: the application container and the current user."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session as DbSession

from gtcc.api.security import CookieSigner, RateLimiter, csrf_valid, load_session
from gtcc.config import Settings
from gtcc.runtime import TradingRuntime
from gtcc.storage.db import session_scope
from gtcc.storage.models import User

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass
class AppContext:
    """Everything the routes need, assembled once at startup."""

    settings: Settings
    runtime: TradingRuntime
    signer: CookieSigner
    login_limiter: RateLimiter


def get_context(request: Request) -> AppContext:
    context = getattr(request.app.state, "context", None)
    if context is None:  # pragma: no cover - misconfiguration
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="application context is not configured",
        )
    return context


def get_db() -> Iterator[DbSession]:
    with session_scope() as session:
        yield session


@dataclass
class Principal:
    user: User
    session_id: str
    csrf_token: str


def current_principal(
    request: Request,
    context: AppContext = Depends(get_context),
    db: DbSession = Depends(get_db),
) -> Principal:
    """Resolve the signed-in user, or refuse.

    CSRF is enforced here rather than in a middleware so that it cannot
    be forgotten on a new route: every authenticated unsafe request
    passes through this function.
    """
    raw_cookie = request.cookies.get(context.settings.session_cookie_name)
    if not raw_cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not signed in")

    session_id = context.signer.unsign(raw_cookie)
    if session_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session cookie is not valid"
        )

    row = load_session(db, session_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired or revoked"
        )

    if request.method in UNSAFE_METHODS:
        provided = request.headers.get("x-csrf-token")
        if not csrf_valid(row.csrf_token, provided):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="missing or invalid CSRF token",
            )

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account inactive")

    return Principal(user=user, session_id=row.id, csrf_token=row.csrf_token)


def require_owner(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.user.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this action is reserved to the account owner",
        )
    return principal
