"""Authentication, sessions, CSRF and rate limiting — section 29.

Passwords are hashed with Argon2id and never stored, logged or
returned. Sessions live server-side in the database; the cookie carries
only an opaque, signed identifier, so revoking a session is a row
update rather than a hope that a token expires.

The rate limiter here is per-process and in-memory, which is correct
for a single instance and wrong behind more than one. The production
path is Redis, and the limiter is written so swapping the backing store
touches one class.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from gtcc.storage.models import AuditLog, Session as SessionRow, User

_hasher = PasswordHasher()

SESSION_ID_BYTES = 32
CSRF_BYTES = 32
#: Consecutive failures before an account is locked for a cooling period.
MAX_FAILED_LOGINS = 10
LOCKOUT_MINUTES = 15


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses outdated parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@dataclass
class RateLimiter:
    """Fixed-window counter, keyed by whatever the caller chooses."""

    limit: int
    window_seconds: float = 60.0
    _hits: dict[str, list[float]] = field(default_factory=dict)

    def check(self, key: str, *, now: float | None = None) -> bool:
        """True when the call is allowed; records the attempt either way."""
        now = now if now is not None else time.monotonic()
        window_start = now - self.window_seconds
        hits = [stamp for stamp in self._hits.get(key, []) if stamp > window_start]
        allowed = len(hits) < self.limit
        hits.append(now)
        self._hits[key] = hits
        return allowed

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


class CookieSigner:
    """Signs the session identifier that travels in the cookie."""

    def __init__(self, secret_key: str) -> None:
        if len(secret_key) < 32:
            raise ValueError("session signing key must be at least 32 characters")
        self._serializer = URLSafeSerializer(secret_key, salt="gtcc-session")

    def sign(self, session_id: str) -> str:
        return self._serializer.dumps(session_id)

    def unsign(self, value: str) -> str | None:
        try:
            return str(self._serializer.loads(value))
        except BadSignature:
            return None


def create_session(
    db: DbSession,
    user: User,
    *,
    max_age_seconds: int,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> SessionRow:
    row = SessionRow(
        id=secrets.token_urlsafe(SESSION_ID_BYTES),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(CSRF_BYTES),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=max_age_seconds),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:512] or None,
    )
    db.add(row)
    user.last_login_at = datetime.now(timezone.utc)
    user.failed_login_count = 0
    db.flush()
    return row


def load_session(db: DbSession, session_id: str) -> SessionRow | None:
    row = db.get(SessionRow, session_id)
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at <= datetime.now(timezone.utc):
        return None
    return row


def revoke_session(db: DbSession, session_id: str) -> None:
    row = db.get(SessionRow, session_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        db.flush()


def authenticate(db: DbSession, email: str, password: str) -> User | None:
    """Verify credentials, applying lockout. Returns None on any failure.

    The failure reason is deliberately not returned: "no such user" and
    "wrong password" are the same answer to a caller, and a different
    one would enumerate accounts.
    """
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        # Spend comparable time so a missing account is not detectably
        # faster than a wrong password.
        _hasher.hash("timing-equalisation-placeholder")
        return None
    if not user.is_active:
        return None
    now = datetime.now(timezone.utc)
    if user.locked_until is not None and user.locked_until > now:
        return None

    if not verify_password(user.password_hash, password):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
        db.flush()
        return None

    if needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    user.locked_until = None
    return user


def csrf_valid(expected: str, provided: str | None) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)


def audit(
    db: DbSession,
    action: str,
    *,
    user_id: int | None = None,
    detail: dict | None = None,
    ip_address: str | None = None,
    correlation_id: str | None = None,
) -> None:
    db.add(
        AuditLog(
            action=action,
            user_id=user_id,
            detail=detail or {},
            ip_address=ip_address,
            correlation_id=correlation_id,
        )
    )
