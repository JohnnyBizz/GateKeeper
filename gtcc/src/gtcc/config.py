"""Deployment configuration.

Settings describe *this deployment*: where the database is, what this
server is permitted to offer, which keys exist. They do not describe
what the application is currently doing. Runtime state — the mode in
force, whether live execution is armed, whether a breaker has latched —
lives in :class:`gtcc.risk.safety.ExecutionState`, in memory, and
starts fresh on every process start.

Three properties are enforced here rather than documented:

**Settings are frozen.** ``settings.mode = TradingMode.LIVE`` raises.
Pydantic validates at construction, so a mutable settings object is a
validator that can be walked around, and an earlier version of this
file had exactly that hole: an API route assigned to ``settings.mode``.

**LIVE is not a configurable mode.** The startup mode may be BACKTEST
or PAPER. Live execution is reached only by a person arming it at
runtime with a confirmation phrase, so no environment can produce a
process that boots already trading.

**No credential is ever rendered.** :meth:`Settings.public_view` is an
allowlist: it returns the fields named safe, and connection URLs are
decomposed into scheme and host with the userinfo discarded. The
previous version dumped everything and redacted the obvious fields,
which left ``postgresql://user:password@host/db`` in plain sight.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gtcc.domain.enums import TradingMode

#: Re-exported for callers that already import it from here. The
#: definition lives with the state machine that checks it, so that the
#: phrase and the gate cannot drift apart.
from gtcc.risk.safety import LIVE_CONFIRMATION_PHRASE  # noqa: E402,F401

_INSECURE_SECRETS = {"", "change-me", "changeme", "secret", "dev", "test"}


class LogFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


def describe_url(raw: str) -> dict[str, object]:
    """Describe a connection URL without any part of its credentials.

    Returns scheme, host, port and path. Username and password are not
    masked, not shortened and not included: they are never read out of
    the parsed result at all.
    """
    if not raw:
        return {"configured": False}
    try:
        parts = urlsplit(raw)
    except ValueError:  # pragma: no cover - urlsplit is forgiving
        return {"configured": True, "parse_error": True}
    return {
        "configured": True,
        "scheme": parts.scheme or None,
        "host": parts.hostname or None,
        "port": parts.port,
        "path": parts.path or None,
        "credentials_present": bool(parts.username or parts.password),
    }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GTCC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Deployment configuration does not change while the process
        # runs. Freezing it means a validator cannot be bypassed by
        # assigning to a field after construction.
        frozen=True,
    )

    # -- identity and environment -----------------------------------------
    app_name: str = "Grok Trading Command Center"
    environment: str = "development"
    debug: bool = False

    # -- trading --------------------------------------------------------------
    #: The mode this process starts in. LIVE is rejected: a process must
    #: never boot into armed live execution.
    mode: TradingMode = TradingMode.PAPER
    #: Deployment permission. Means "this server may OFFER live mode".
    #: It arms nothing on its own and nothing reads it as armed state.
    allow_live_trading: bool = False
    #: Whether strategies may submit without a human pressing anything.
    #: Independent of live: automatic paper execution is a real mode.
    automatic_execution: bool = False

    # -- storage -----------------------------------------------------------
    #: SecretStr, not str. A connection URL carries a password in the
    #: middle of an innocuous-looking value, and pydantic masks
    #: SecretStr in repr, str and model_dump. Without that, any
    #: traceback or debug log that happened to include the settings
    #: object printed the database password. Read the value through
    #: :attr:`database_dsn`.
    database_url: SecretStr = SecretStr("sqlite:///./gtcc.db")
    redis_url: SecretStr = SecretStr("")

    # -- web security -------------------------------------------------------
    secret_key: SecretStr = SecretStr("")
    session_cookie_name: str = "gtcc_session"
    session_max_age_seconds: int = Field(default=60 * 60 * 8, gt=0, le=60 * 60 * 24 * 7)
    secure_cookies: bool = True
    login_rate_limit_per_minute: int = Field(default=5, gt=0, le=1000)

    # -- risk ----------------------------------------------------------------
    risk_config_path: Path = Path("config/risk.yaml")

    # -- data quality gates --------------------------------------------------
    max_quote_age_seconds: float = Field(default=5.0, gt=0, le=3600)
    max_bar_age_multiple: float = Field(default=2.0, gt=0, le=100)
    max_clock_skew_seconds: float = Field(default=2.0, ge=0, le=3600)

    # -- Grok / xAI -----------------------------------------------------------
    #: No default model name: model identifiers go stale, and a stale one
    #: fails at the worst moment. Set GTCC_GROK_MODEL explicitly.
    grok_api_key: SecretStr = SecretStr("")
    grok_base_url: str = "https://api.x.ai/v1"
    grok_model: str = ""
    grok_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    grok_max_retries: int = Field(default=2, ge=0, le=10)
    require_grok_for_trades: bool = False

    # -- logging ---------------------------------------------------------------
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- market data providers (keys only; adapters read these) ---------------
    provider_keys: dict[str, SecretStr] = Field(default_factory=dict)

    @field_validator("mode")
    @classmethod
    def _startup_mode_is_never_live(cls, value: TradingMode) -> TradingMode:
        if value is TradingMode.LIVE:
            raise ValueError(
                "GTCC_MODE=LIVE is refused. Live execution is not a startup "
                "mode: a person arms it at runtime with the confirmation "
                "phrase, so that a restart can never resume it by itself. "
                "Set GTCC_ALLOW_LIVE_TRADING=true to permit arming."
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @model_validator(mode="after")
    def _enforce_secret_strength(self) -> "Settings":
        """A weak signing key is tolerable only in development.

        The live condition that used to be here is gone: live is no
        longer reachable from configuration, so there is nothing about
        it to check at construction. A deployment that permits live
        trading still needs a real key, which is what is checked.
        """
        secret = self.secret_key.get_secret_value()
        weak = secret.strip().lower() in _INSECURE_SECRETS or len(secret) < 32
        if weak and (self.environment != "development" or self.allow_live_trading):
            raise ValueError(
                "GTCC_SECRET_KEY must be set to at least 32 random characters "
                "outside development, and in any deployment that permits live "
                "trading. Generate one with: "
                "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        return self

    # -- derived ---------------------------------------------------------------

    @property
    def database_dsn(self) -> str:
        """The connection string itself. Never log or render this."""
        return self.database_url.get_secret_value()

    @property
    def redis_dsn(self) -> str:
        """The Redis connection string. Never log or render this."""
        return self.redis_url.get_secret_value()

    @property
    def uses_sqlite(self) -> bool:
        return self.database_dsn.startswith("sqlite")

    #: Fields that may be shown to an authenticated owner. Anything not
    #: named here is not rendered, so adding a setting cannot leak it by
    #: default. Credential-bearing URLs are described, never echoed.
    PUBLIC_FIELDS: ClassVar[tuple[str, ...]] = (
        "app_name",
        "environment",
        "debug",
        "mode",
        "allow_live_trading",
        "automatic_execution",
        "session_cookie_name",
        "session_max_age_seconds",
        "secure_cookies",
        "login_rate_limit_per_minute",
        "risk_config_path",
        "max_quote_age_seconds",
        "max_bar_age_multiple",
        "max_clock_skew_seconds",
        "grok_base_url",
        "grok_model",
        "grok_timeout_seconds",
        "grok_max_retries",
        "require_grok_for_trades",
        "log_level",
        "log_format",
    )

    def public_view(self) -> dict[str, object]:
        """Everything safe to render, and nothing else.

        An allowlist rather than a denylist. A new setting is invisible
        here until somebody adds it to ``PUBLIC_FIELDS``, which is the
        right default for a file that holds credentials.
        """
        view: dict[str, object] = {}
        for name in self.PUBLIC_FIELDS:
            value = getattr(self, name)
            view[name] = str(value) if isinstance(value, (Path, StrEnum)) else value

        view["database"] = describe_url(self.database_dsn)
        view["redis"] = describe_url(self.redis_dsn)
        view["secret_key_set"] = bool(self.secret_key.get_secret_value())
        view["grok_api_key_set"] = bool(self.grok_api_key.get_secret_value())
        view["provider_keys_configured"] = sorted(self.provider_keys)
        return view


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings, read once."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings | None) -> None:
    """Replace the cached settings. Tests use this; nothing else should."""
    global _settings
    _settings = settings
