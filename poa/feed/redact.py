"""Stripping secrets out of captured WebSocket frames.

A trading platform's socket carries the session that authenticates it. Frames
recorded for debugging therefore contain, somewhere in them, everything needed
to act as the user — and the whole point of recording frames is to send them to
somebody else to look at.

So redaction runs before anything is written to disk, not before it is shared.
A capture file that never contained a token cannot leak one, however it is
handled afterwards. This is deliberately blunt: it would rather destroy a field
that turns out to be a candle price than preserve one that turns out to be a
session id.
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "[redacted]"

# Field names whose values are never wanted, whatever they contain.
_SECRET_KEYS = {
    "token", "auth", "authorization", "session", "sessionid", "session_id",
    "password", "passwd", "secret", "key", "apikey", "api_key", "cookie",
    "cookies", "jwt", "bearer", "signature", "sign", "hash", "uid", "userid",
    "user_id", "id", "email", "phone", "balance", "demo_balance",
    "live_balance", "amount", "profile", "account", "accountid", "account_id",
}

# Values that look like credentials regardless of the field they arrived in:
# long hex strings, JWTs, and anything base64-shaped and lengthy.
_HEXISH = re.compile(r"\b[0-9a-f]{24,}\b", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")


def redact_text(text: str) -> str:
    """Blank out anything credential-shaped in a raw string."""
    for pattern in (_JWT, _EMAIL, _HEXISH, _LONG_TOKEN):
        text = pattern.sub(PLACEHOLDER, text)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact a decoded frame.

    Keys are matched case-insensitively and by substring, so ``authToken`` and
    ``user_session_id`` both go, without needing to know the platform's naming.
    """
    if _depth > 12:  # pragma: no cover - absurdly nested input
        return PLACEHOLDER

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).lower().replace("-", "").replace("_", "")
            if any(secret.replace("_", "") in name for secret in _SECRET_KEYS):
                cleaned[key] = PLACEHOLDER
            else:
                cleaned[key] = redact(item, _depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
