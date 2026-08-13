"""Configuration loading.

Settings come from a YAML file (``config.yaml`` by default, falling back to the
shipped ``config.example.yaml``) and may be overridden by environment variables
prefixed with ``POA_``, e.g. ``POA_SERVER__PORT=9000``.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.yaml"

# Timeframes the UI offers, in seconds.
CHART_TIMEFRAMES: tuple[int, ...] = (
    5, 15, 30, 60, 120, 180, 300, 600, 900, 1800, 3600, 14400,
)

# Expirations Pocket Option style platforms commonly offer, in seconds.
TRADE_DURATIONS: tuple[int, ...] = (
    30, 60, 120, 180, 300, 600, 900, 1800,
)


DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": False,
    },
    "capture": {
        # "synthetic" | "csv" | "screen"
        "source": "synthetic",
        "poll_seconds": 2.0,
        # Screen capture region, in pixels. Populate with tools/select_region.py.
        "region": {"left": 0, "top": 0, "width": 0, "height": 0},
        "monitor": 1,
        # Price calibration for the screen source. Two reference points read off
        # the chart's price axis let us convert pixel rows into prices without
        # relying on OCR. OCR is attempted first when enabled.
        "calibration": {
            "enabled": False,
            "top_pixel": 0,
            "top_price": 0.0,
            "bottom_pixel": 0,
            "bottom_price": 0.0,
        },
        "ocr": {
            "enabled": True,
            "axis_width_px": 70,
        },
        "csv_path": "data/sample_eurusd_m1.csv",
        "csv_replay_speed": 0.0,  # 0 = advance one candle per poll
        "save_screenshots": True,
    },
    "market": {
        "asset": "EUR/USD",
        "chart_timeframe": 60,
        "trade_duration": 180,
        # Multipliers applied to the chart timeframe to build the higher and
        # middle timeframe views. 1 means "the chart timeframe itself".
        "higher_timeframe_multiple": 5,
        "entry_timeframe_multiple": 1,
        "min_candles": 60,
        "max_candles": 600,
        # Broker payout on a win, as a fraction. This sets the break-even win
        # rate, so it is not cosmetic: 0.92 means 52.1% wins is break-even.
        "payout": 0.92,
    },
    "risk": {
        "balance": 1000.0,
        "risk_percent": 2.0,
    },
    "overlay": {
        "x": 40,
        "y": 80,
        "opacity": 0.96,
        # How long the scanning state is held before the verdict is revealed.
        "scan_seconds": 2.4,
    },
    "signals": {
        "min_confidence": 75,
        "min_duration_compatibility": 65,
        "min_data_confidence": 70,
        # Structural gates. Every one of these must pass before a direction is
        # emitted; failing any of them yields WAIT.
        "require_multi_timeframe_agreement": True,
        "require_heikin_ashi_confirmation": True,
        "min_component_agreement": 0.55,
        "max_atr_percentile": 92,
        "resistance_proximity_atr": 0.75,
        "cooldown_seconds": 60,
        "weakening_drop": 15,
        "invalidation_drop": 25,
    },
    "alerts": {
        "enabled": True,
        "desktop_notifications": True,
        "sound": False,
        "sound_command": "",
        "cooldown_seconds": 120,
        "min_confidence": 75,
        "notify_on": [
            "BUY_SIGNAL",
            "SELL_SIGNAL",
            "SETUP_INVALIDATED",
            "TREND_REVERSAL",
            "HIGH_VOLATILITY",
            "CONFLICTING_SIGNALS",
        ],
    },
    "storage": {
        "database": "storage/journal.db",
        "screenshot_dir": "storage/screenshots",
        "retain_screenshots": 500,
    },
    "logging": {
        "level": "INFO",
        "file": "storage/poa.log",
    },
}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", ""):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _env_overrides(env: dict[str, str]) -> dict[str, Any]:
    """Translate ``POA_SECTION__KEY=value`` variables into a nested dict."""
    overrides: dict[str, Any] = {}
    for raw_key, raw_value in env.items():
        if not raw_key.startswith("POA_"):
            continue
        path = raw_key[4:].lower().split("__")
        cursor = overrides
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):  # pragma: no cover - malformed env
                break
        else:
            cursor[path[-1]] = _coerce(raw_value)
    return overrides


@dataclass
class Config:
    """Dot/section access over the merged configuration dictionary."""

    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: Path | None = None

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    def get(self, dotted: str, default: Any = None) -> Any:
        cursor: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                return default
            cursor = cursor[part]
        return cursor

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cursor = self.data
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value

    def resolve_path(self, dotted: str) -> Path:
        """Resolve a configured path relative to the project root."""
        raw = str(self.get(dotted, ""))
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration, layering file settings and env vars over defaults."""
    chosen: Path | None
    if path is not None:
        chosen = Path(path)
    elif DEFAULT_CONFIG_PATH.exists():
        chosen = DEFAULT_CONFIG_PATH
    elif EXAMPLE_CONFIG_PATH.exists():
        chosen = EXAMPLE_CONFIG_PATH
    else:
        chosen = None

    file_data: dict[str, Any] = {}
    if chosen is not None and chosen.exists():
        with chosen.open("r", encoding="utf-8") as handle:
            file_data = yaml.safe_load(handle) or {}
        if not isinstance(file_data, dict):
            raise ValueError(f"config file {chosen} must contain a mapping")

    merged = _deep_merge(DEFAULTS, file_data)
    merged = _deep_merge(merged, _env_overrides(dict(os.environ)))
    return Config(data=merged, path=chosen)
