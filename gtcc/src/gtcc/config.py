"""Application settings.

Three rules the specification sets, enforced here rather than documented:

* Section 43 — the platform starts in PAPER, with live trading and
  automatic execution off. Those are the defaults below, and nothing
  turns them on implicitly.
* Section 29 — secrets come from the environment or a secret store. No
  credential has a default value, and none is ever logged.
* Section 43 again — risk numbers are the owner's to choose. There is no
  default risk-per-trade here. ``config/risk.example.yaml`` is clearly
  labelled an example, and the platform refuses to arm trading until the
  owner has supplied a file of their own.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gtcc.domain.enums import TradingMode

#: Typed into the UI, by hand, to move from PAPER to LIVE. Deliberately
#: tedious; see specification section 30.
LIVE_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"

_INSECURE_SECRETS = {"", "change-me", "changeme", "secret", "dev", "test"}


class LogFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GTCC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- identity and environment -----------------------------------------
    app_name: str = "Grok Trading Command Center"
    environment: str = "development"
    debug: bool = False

    # -- trading mode: the safe defaults from section 43 -------------------
    mode: TradingMode = TradingMode.PAPER
    live_trading: bool = False
    automatic_execution: bool = False
    #: Must equal LIVE_CONFIRMATION_PHRASE before mode may be LIVE.
    live_confirmation: str = ""

    # -- storage -----------------------------------------------------------
    database_url: str = "sqlite:///./gtcc.db"
    redis_url: str = ""

    # -- web security -------------------------------------------------------
    secret_key: SecretStr = SecretStr("")
    session_cookie_name: str = "gtcc_session"
    session_max_age_seconds: int = 60 * 60 * 8
    secure_cookies: bool = True
    login_rate_limit_per_minute: int = 5

    # -- risk ----------------------------------------------------------------
    #: Path to the owner's risk limits. No numbers are assumed on their behalf.
    risk_config_path: Path = Path("config/risk.yaml")

    # -- data quality gates --------------------------------------------------
    max_quote_age_seconds: float = 5.0
    max_bar_age_multiple: float = 2.0
    max_clock_skew_seconds: float = 2.0

    # -- Grok / xAI -----------------------------------------------------------
    #: No default model name: model identifiers go stale, and a stale one
    #: fails at the worst moment. Set GTCC_GROK_MODEL explicitly.
    grok_api_key: SecretStr = SecretStr("")
    grok_base_url: str = "https://api.x.ai/v1"
    grok_model: str = ""
    grok_timeout_seconds: float = 30.0
    grok_max_retries: int = 2
    #: When Grok is unreachable or malformed, the platform keeps running
    #: without it. It never falls back to "assume the AI approved".
    require_grok_for_trades: bool = False

    # -- logging ---------------------------------------------------------------
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- market data providers (keys only; adapters read these) ---------------
    provider_keys: dict[str, SecretStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _enforce_live_gates(self) -> "Settings":
        if self.mode is TradingMode.LIVE:
            if not self.live_trading:
                raise ValueError(
                    "mode=LIVE requires GTCC_LIVE_TRADING=true. Live trading is "
                    "never enabled as a side effect of setting the mode."
                )
            if self.live_confirmation != LIVE_CONFIRMATION_PHRASE:
                raise ValueError(
                    "mode=LIVE requires GTCC_LIVE_CONFIRMATION to be exactly "
                    f"{LIVE_CONFIRMATION_PHRASE!r}."
                )
        if self.automatic_execution and not self.live_trading and self.mode is TradingMode.LIVE:
            raise ValueError("automatic execution in LIVE requires live_trading=true")
        return self

    @model_validator(mode="after")
    def _enforce_secret_strength(self) -> "Settings":
        secret = self.secret_key.get_secret_value()
        weak = secret.strip().lower() in _INSECURE_SECRETS or len(secret) < 32
        if weak and (self.environment != "development" or self.mode is TradingMode.LIVE):
            raise ValueError(
                "GTCC_SECRET_KEY must be set to at least 32 random characters "
                "outside development. Generate one with: "
                "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        return self

    # -- derived ---------------------------------------------------------------

    @property
    def is_live(self) -> bool:
        return self.mode is TradingMode.LIVE and self.live_trading

    @property
    def uses_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def redacted(self) -> dict:
        """A dict safe to log or return from the settings endpoint."""
        data = self.model_dump(mode="json")
        for key, value in list(data.items()):
            if isinstance(getattr(self, key, None), SecretStr):
                data[key] = "***set***" if getattr(self, key).get_secret_value() else "***unset***"
        data["provider_keys"] = {name: "***set***" for name in self.provider_keys}
        return data


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
