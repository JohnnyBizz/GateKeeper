"""Structured logging with credential redaction.

Specification section 31 wants every trade reconstructable afterwards,
and section 29 wants secrets to never reach a log line. Both are handled
here: records are emitted as single-line JSON with a correlation id, and
a filter scrubs anything that looks like a credential on the way out.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

#: Set per request / per decision so related lines can be stitched together.
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

_SENSITIVE_KEYS = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|private[_-]?key|authorization|cookie|"
    r"session|credential|signature|passphrase)",
    re.IGNORECASE,
)

#: Long opaque strings that look like keys even without a telling name.
_KEY_SHAPED = re.compile(r"\b(?:xai-|sk-|pk_|Bearer\s+)[A-Za-z0-9_\-\.]{12,}")

#: Credentials inside a connection URL. No key name gives these away:
#: a field called "database_url" looks harmless and carries the
#: password in the middle of its value. The userinfo segment between
#: "://" and "@" is replaced wholesale.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<userinfo>[^/@\s]*@)")

REDACTED = "***redacted***"


def scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively remove anything that looks like a credential."""
    if _depth > 6:
        return value
    if isinstance(value, dict):
        return {
            key: (REDACTED if _SENSITIVE_KEYS.search(str(key)) else scrub(val, _depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item, _depth + 1) for item in value]
    if isinstance(value, str):
        cleaned = _URL_CREDENTIALS.sub(rf"\g<scheme>{REDACTED}@", value)
        return _KEY_SHAPED.sub(REDACTED, cleaned)
    return value


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.msg)
        if isinstance(getattr(record, "context", None), dict):
            record.context = scrub(record.context)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        cid = correlation_id.get()
        if cid:
            payload["correlation_id"] = cid
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(scrub(context))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install handlers. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s  %(message)s")
        )
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:16]
    correlation_id.set(cid)
    return cid


def log_event(logger: logging.Logger, level: int, message: str, **context: Any) -> None:
    """Emit a structured event. Keyword arguments become JSON fields."""
    logger.log(level, message, extra={"context": context})
