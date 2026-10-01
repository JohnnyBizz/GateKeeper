"""Sign in, sign out, and who am I."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session as DbSession

from gtcc.api.deps import AppContext, Principal, current_principal, get_context, get_db
from gtcc.api.schemas import LoginRequest, UserOut
from gtcc.api.security import audit, authenticate, create_session, revoke_session

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    context: AppContext = Depends(get_context),
    db: DbSession = Depends(get_db),
) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    if not context.login_limiter.check(client_ip):
        audit(db, "auth.rate_limited", detail={"email": body.email}, ip_address=client_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many sign-in attempts; wait a minute and try again",
        )

    user = authenticate(db, body.email, body.password)
    if user is None:
        # One message for every failure mode, so nothing is enumerable.
        audit(db, "auth.login_failed", detail={"email": body.email}, ip_address=client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="email or password is incorrect"
        )

    session = create_session(
        db,
        user,
        max_age_seconds=context.settings.session_max_age_seconds,
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent"),
    )
    audit(db, "auth.login", user_id=user.id, ip_address=client_ip)

    response.set_cookie(
        key=context.settings.session_cookie_name,
        value=context.signer.sign(session.id),
        max_age=context.settings.session_max_age_seconds,
        httponly=True,
        secure=context.settings.secure_cookies,
        samesite="strict",
        path="/",
    )
    context.login_limiter.reset(client_ip)
    return {
        "user": {"email": user.email, "role": user.role},
        # The client echoes this in X-CSRF-Token on unsafe requests.
        "csrf_token": session.csrf_token,
    }


@router.post("/logout")
def logout(
    response: Response,
    context: AppContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
    db: DbSession = Depends(get_db),
) -> dict:
    revoke_session(db, principal.session_id)
    audit(db, "auth.logout", user_id=principal.user.id)
    response.delete_cookie(context.settings.session_cookie_name, path="/")
    return {"signed_out": True}


@router.get("/me", response_model=UserOut)
def me(principal: Principal = Depends(current_principal)) -> UserOut:
    return UserOut(
        email=principal.user.email,
        role=principal.user.role,
        last_login_at=principal.user.last_login_at,
    )


@router.get("/csrf")
def csrf(principal: Principal = Depends(current_principal)) -> dict:
    """Re-read the CSRF token for the current session."""
    return {"csrf_token": principal.csrf_token}
