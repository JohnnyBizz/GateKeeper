"""Configuration loading.

Settings come from a YAML file (``config.yaml`` by default, falling back to the
shipped ``config.example.yaml``) and may be overridden by environment variables
prefixed with ``POA_``, e.g. ``POA_SERVER__PORT=9000``.
"""

from __future__ import annotations

import copy
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def frozen() -> bool:
    """Whether this is running from a packaged one-file executable."""
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def bundle_root() -> Path:
    """Where the app's read-only files live.

    In a packaged build that is PyInstaller's unpack directory, which is
    created on launch and **deleted on exit**. Fine for assets that ship with
    the app; catastrophic for anything the user expects to keep.
    """
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else PROJECT_ROOT


def data_root() -> Path:
    """Where the user's own files go — settings, journal, logs, screenshots.

    This has to be separate from the bundle. A one-file executable unpacks
    itself into a temporary directory and deletes it on exit, so anything
    written relative to the running code is gone the moment the app closes:
    the selected chart region, the price calibration, the pair and timeframe
    boxes, the trading journal, and the log that would have explained any of
    it. Everything appears to work in the session and nothing survives the
    restart.

    Running from source keeps using the project directory, which is where a
    developer expects to find these files.
    """
    if not frozen():
        return PROJECT_ROOT

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"

    target = root / "GateKeeper"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - unwritable home directory
        return Path.home()
    return target


DEFAULT_CONFIG_PATH = data_root() / "config.yaml"
EXAMPLE_CONFIG_PATH = bundle_root() / "config.example.yaml"

# Timeframes the UI offers, in seconds. This list is also what the timeframe
# badge is snapped onto, so a timeframe missing from here cannot be read off
# the screen at all — H3 is in the list because the platform offers it.
CHART_TIMEFRAMES: tuple[int, ...] = (
    5, 15, 30, 60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 10800, 14400, 86400,
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
        # "feed" | "synthetic" | "csv" | "screen"
        #
        # The feed reads the platform's own messages: exact prices, the exact
        # instrument and the exact timeframe, with nothing to misread. Screen
        # capture is kept as a fallback for platforms whose traffic cannot be
        # reached, and falls back again to demo data so a first run always has
        # something to show.
        "source": "feed",
        "source_chosen": False,
        # Where the browser listens for the DevTools connection the feed source
        # reads. Only used by the "feed" source.
        "debug_port": 9222,
        "match": "pocketoption",
        # Start the debuggable browser if none is running. Opening
        # GateKeeper should be the only thing the user has to do.
        "auto_launch_browser": True,
        # Reload the platform's page when it has not said which chart is open.
        # Attaching to a tab that loaded its chart minutes ago means the
        # messages naming the instrument and carrying its history are long
        # gone; asking the page to load again recovers them without the user
        # having to touch anything.
        "refresh_chart": True,
        "poll_seconds": 2.0,
        # Screen capture region, in pixels. Populate with tools/select_region.py.
        "region": {"left": 0, "top": 0, "width": 0, "height": 0},
        "monitor": 1,
        # Optional small region over the platform's pair label. When set, the
        # asset name is read from the screen and follows chart switches.
        "asset_region": {"left": 0, "top": 0, "width": 0, "height": 0},
        # Optional small region over the platform's timeframe badge (M1, M5,
        # H1...). When set, the chart timeframe follows the platform.
        "timeframe_region": {"left": 0, "top": 0, "width": 0, "height": 0},
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
        # Every candle kept is a candle the replay can learn from, and the
        # measurement scales almost linearly with them: 600 bars yields a
        # few dozen settled trades, 2400 yields a couple of hundred — the
        # difference between a number that is barely readable and one that
        # can be broken down by regime and by hour. The old 600 was
        # throwing away most of what the platform sends.
        "max_candles": 5000,
        # How many of those the *live* read looks at. Its longest lookback
        # is the 200-period EMA, so more than this buys nothing but costs
        # real time on every poll — 5000 candles takes 110ms against 17ms
        # for 600, which the panel feels. The replay still gets all of
        # them; depth is for measuring, not for deciding.
        "analysis_candles": 600,
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
        # The win/loss tally is the user's own record. Set false to have it
        # filled from settled journal outcomes instead — but then two things
        # are writing to one column, and neither number means much.
        "session_manual": True,
        # Whether the risk block starts folded away. The panel remembers.
        "risk_collapsed": False,
    },
    "signals": {
        "min_confidence": 75,
        "min_duration_compatibility": 65,
        "min_data_confidence": 70,
        # Structural gates. Every one of these must pass before a direction is
        # emitted; failing any of them yields WAIT.
        "require_multi_timeframe_agreement": True,
        "require_heikin_ashi_confirmation": True,
        # Refuse a setup when the measured record for this chart says setups
        # like it have lost more often than the payout can carry. Silent until
        # there is a sample big enough to mean something, so it never stops a
        # record being built in the first place.
        "require_measured_edge": True,
        # Let the measured record set min_confidence and
        # min_duration_compatibility for itself, rather than leaving them at
        # numbers somebody typed. Moves a few points at a time, within bounds,
        # and only on evidence the sample can carry. Set false to keep the
        # values above fixed.
        "auto_tune": True,
        # Evaluate at the expiry the analysis prefers rather than the one
        # left in a settings box. The duration gate was rejecting sound
        # setups for a reason that had nothing to do with the market — a
        # stale number, not a market, saying no.
        "follow_recommended_expiry": True,
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
        """Resolve a configured path against the user's data directory.

        Relative paths in the config — ``storage/journal.db``, ``storage/
        poa.log`` — are the user's own files, so they resolve against
        ``data_root()`` and not against the code. In a packaged build those are
        different places, and resolving against the code would put the journal
        inside a temporary directory that is deleted on exit.
        """
        raw = str(self.get(dotted, ""))
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = data_root() / candidate
        return candidate

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def save(self, path: str | Path | None = None) -> Path:
        """Write the current settings back to disk.

        Settings changed in the app must survive a restart, otherwise the user
        is back to hand-editing YAML. Writes to ``config.yaml`` by default —
        never to ``config.example.yaml``, which is shipped documentation and
        must stay pristine even when it was the file we loaded from.
        """
        target = Path(path) if path is not None else self.path
        if target is None or target.name == EXAMPLE_CONFIG_PATH.name:
            target = DEFAULT_CONFIG_PATH
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temporary file and move it into place, so an interrupted
        # write cannot leave a half-written config that fails to parse.
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(self.data, handle, sort_keys=False, allow_unicode=True)
        temporary.replace(target)

        self.path = target
        return target


def ensure_config_file() -> Path | None:
    """Create ``config.yaml`` from the shipped example on first run.

    Without this the user has to copy a file by hand before they can change a
    setting — exactly the kind of chore the app should absorb.
    """
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    if not EXAMPLE_CONFIG_PATH.exists():  # pragma: no cover - broken install
        return None
    try:
        DEFAULT_CONFIG_PATH.write_text(
            EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - read-only install directory
        return None
    return DEFAULT_CONFIG_PATH


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
