"""Configuration loading.

Settings come from a YAML file (``config.yaml`` by default, falling back to the
shipped ``config.example.yaml``) and may be overridden by environment variables
prefixed with ``POA_``, e.g. ``POA_SERVER__PORT=9000``.
"""

from __future__ import annotations

import copy
import logging
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The standard logger rather than the project's own: ``logging_setup`` reads
# its destination from here, so importing it back would be a cycle.
log = logging.getLogger(__name__)

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
    5, 10, 15, 30, 60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 10800, 14400, 86400,
)

# The timeframes the same pair is also read at, on top of whichever one the
# chart is open on. Seconds and minutes: the platform's S5 through M30. Higher
# ones are left out deliberately — an H4 candle takes four hours to settle,
# which is not a timeframe anyone is taking three-minute expiries against.
#
# Every one of these is built from candles already in hand, so scanning them
# costs a resample rather than a new subscription, and the answer is available
# immediately instead of hours from now.
SCAN_TIMEFRAMES: tuple[int, ...] = (
    5, 10, 15, 30, 60, 120, 180, 300, 600, 900, 1800,
)

# Expirations Pocket Option style platforms commonly offer, in seconds.
TRADE_DURATIONS: tuple[int, ...] = (
    30, 60, 120, 180, 300, 600, 900, 1800,
)


# Bumped whenever a stored setting has to change on installs that already
# exist. See ``_migrate``.
CONFIG_VERSION = 3

DEFAULTS: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
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
        # How often the open chart is re-read. Reading it off the feed costs a
        # few milliseconds, so two seconds was two seconds of a thirty-second
        # trade spent waiting for no reason.
        #
        # Safe to set this low even on a screen source, where a poll is a
        # screenshot and an OCR pass: the engine loop never schedules the next
        # read sooner than the last one took, so an expensive source paces
        # itself and a cheap one gets the interval it asks for.
        "poll_seconds": 0.5,
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
        # Which chart lengths to also read the same pair at, on top of the one
        # the chart is open on. true = every length below, false = none, or a
        # list to watch only some.
        #
        # Defaulted to the two pairings this is actually used for — a 5 SEC
        # chart taken at 30 seconds, and a 1 MIN chart taken at 3 minutes.
        # Watching M15 as well is not more information, it is more rows to
        # read past, and one more chance to act on a verdict about a chart
        # nobody is trading. Set true to watch everything again.
        "scan_timeframes": [5, 60],
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
        # Per-asset payouts, because the platform pays differently for
        # each and changes them through the session. This is not cosmetic:
        # break-even is 52.1% at 92% and 55.6% at 80%, so a stale payout
        # moves the bar every measurement here is judged against.
        "payouts": {},
    },
    "risk": {
        # The brakes. Both off at zero. A losing run is when position sizing
        # stops being arithmetic and starts being a decision made badly, which
        # is exactly when a limit set in advance is worth having.
        "max_losses_in_a_row": 4,
        "max_daily_loss_percent": 10.0,
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
        # Open the reports folder when the app closes and a report was
        # written. It lands under AppData, which Windows hides by default,
        # so without this the session's own scorecard is somewhere most
        # people would never look. Set false once you know the path.
        "reveal_report": True,
    },
    "signals": {
        # Under test. Measured across a rising and a falling recording, the
        # old 75 straddled break-even and 85 cleared it — on a threshold
        # chosen by looking at the same data, so it is a hypothesis rather
        # than a settled number. data/recorded/README.md has the table.
        # The direction score gate. An internal number, and not the one on
        # the panel.
        "min_confidence": 75,
        # The gate on what the panel actually shows — direction and duration
        # combined, always the lower reading. This is the one under test.
        # Setting min_confidence to 85 gated the internals at 85 and still
        # displayed calls at 80.1, so "trade at 85" meant something the user
        # could not see and had not agreed to.
        #
        # 62, matching a competing tool that shows BUY at 62/100. Chosen for
        # rate rather than accuracy, deliberately, with the trade-off measured
        # first: on the recorded market it is a call about once a minute
        # instead of once every two, and it won 66.7% in one half hour and
        # 54.8% in another. Neither beat always-buy on the same entries, so
        # the honest reading is that 62 changes how often the tool speaks and
        # not how often it is right.
        #
        # Every session report prints the always-buy and always-sell rate
        # beside the win rate. That comparison is what tells a good week apart
        # from a trending one, and at this gate there will be plenty of both.
        "min_shown_confidence": 62,
        # Refuse calls whose direction score reaches this. Backwards until
        # measured; measured four times. In every live session so far —
        # every call followed to expiry against the platform's own prices —
        # the calls at ninety and above settled *below* the calls beneath
        # them: 14%, 31%, 42%, 21%, against 54%, 48%, 44%, 65% for the
        # band just under. The score is a trend detector, and it
        # maxes out when every component finally agrees — which is the
        # moment the move it is reading has already mostly run. Set 0 to
        # turn the ceiling off.
        "overheat_ceiling": 90,
        # Stand down on a pair for this many minutes after one of the tool's
        # own calls on it settles as a loss. Measured before shipping over
        # the three sessions with row-level records: on the session with the
        # chase pattern it removed five losses and one win (50.0% -> 60.0%)
        # and changed nothing on the others. The cascade variant (stand down
        # everywhere after clustered losses) was measured too, cost winners,
        # and did not ship. Set 0 to turn it off.
        "loss_cooldown_minutes": 3,
        # Refuse to call charts paying under this fraction. Arithmetic, not
        # a chart reading: at a 72% payout break-even is 58.1% and nothing
        # measured here has ever cleared that bar; at 92% it is 52.1%.
        # Charts whose payout the source does not know are never blocked.
        # Set 0 to turn it off.
        "min_payout": 0.80,
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
        # Refuse the market conditions this chart is measurably least often
        # right in. Measured per chart rather than fixed, because where the
        # reading works varies by instrument.
        "avoid_weak_regimes": True,
        # Let the measured record set min_confidence and
        # min_duration_compatibility for itself, rather than leaving them at
        # numbers somebody typed. Moves a few points at a time, within bounds,
        # and only on evidence the sample can carry. Set false to keep the
        # values above fixed.
        #
        # Off while min_confidence is being tested: a gate that moves itself
        # between 55 and 90 cannot also be the thing under measurement. Turn
        # it back on once the threshold is settled.
        "auto_tune": False,
        # Evaluate at the expiry the analysis prefers rather than the one
        # left in a settings box. The duration gate was rejecting sound
        # setups for a reason that had nothing to do with the market — a
        # stale number, not a market, saying no.
        "follow_recommended_expiry": True,
        "min_component_agreement": 0.55,
        "max_atr_percentile": 92,
        "resistance_proximity_atr": 0.75,
        # How long the same chart is silenced after a signal. Sixty seconds is
        # two whole trades at a thirty-second expiry, so a chart that set up
        # twice in a minute only ever offered the first one.
        "cooldown_seconds": 15,
        "weakening_drop": 15,
        "invalidation_drop": 25,
    },
    "alerts": {
        "enabled": True,
        "desktop_notifications": True,
        # On by default since v3. It was off while Windows had no default
        # player anyway; now the chime comes from the standard library and a
        # tool whose whole job is telling the user something must not
        # default to doing it silently.
        "sound": True,
        "sound_command": "",
        # Two minutes was four trades' worth of silence at a thirty-second
        # expiry. The watchlist already only announces on the transition into
        # being tradeable, so this is a second layer and does not need to be
        # the one doing the work.
        "cooldown_seconds": 20,
        # Must match the gate the panel shows calls at, or the tool displays a
        # call and says nothing about it. These drifted apart the moment the
        # shown gate moved to 62 and alerts stayed at 75: every call between
        # the two was silent, which on a thirty-second trade means missed.
        "min_confidence": 62,
        "notify_on": [
            "BUY_SIGNAL",
            "SELL_SIGNAL",
            # A setup on one of the other charts being watched. One chart
            # yields a handful of setups a day; the other seven are read at
            # the same bar and against the same gates, and saying nothing
            # about them was throwing most of the tool's output away.
            "WATCHLIST",
            "SETUP_INVALIDATED",
            "TREND_REVERSAL",
            "HIGH_VOLATILITY",
            "CONFLICTING_SIGNALS",
        ],
    },
    "storage": {
        "database": "storage/journal.db",
        "screenshot_dir": "storage/screenshots",
        # Where the end-of-session report is written. One plain-text file per
        # session, named for when it started so they sort into the order they
        # happened in.
        "report_dir": "storage/reports",
        "retain_screenshots": 500,
        # The feed's raw ticks, kept whole beside the candles they become.
        # The candles are these ticks bucketed, and the bucketing destroys
        # how price moved inside each bar — the one input never yet measured.
        # Bounded by the retention window, pruned at startup. Empty disables.
        "tick_archive": "storage/ticks.db",
        "tick_retention_days": 14,
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
        elif isinstance(out.get(key), dict):
            # A scalar where a whole section belongs — ``alerts: true`` is a
            # plausible hand-edit meaning "turn alerts on". Taking it would
            # erase every default under the section, crash the next ``set``
            # into it (the migration was the first to hit that, as a
            # TypeError at startup), and write the damage back on save. The
            # section's defaults stay; the stray value is noted and dropped.
            log.warning(
                "config: %r holds a whole section, not a single value; "
                "ignoring %r and keeping the defaults", key, value,
            )
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


def _set_aside(path: Path) -> Path | None:
    """Move a file that could not be read out of the way, and say where to.

    Kept rather than deleted: it is the user's settings, and whatever is wrong
    with it, the values in it are the ones they chose. A fresh one is written
    the next time anything is saved.
    """
    try:
        kept = path.with_suffix(path.suffix + ".unreadable")
        if kept.exists():
            kept.unlink()
        path.replace(kept)
        return kept
    except Exception:  # pragma: no cover - read-only disk, locked file
        return None


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
        # A settings file the app cannot read must not stop the app. This one
        # is written atomically, so a half-written file cannot come from us —
        # but the user is told to edit it by hand for the things the panel
        # does not expose, and one stray character in YAML would otherwise
        # mean a double-clicked GateKeeper dies on a parser stack trace, with
        # nothing on screen to say which line to fix. The unreadable file is
        # kept, under a name that says why, and the defaults are used instead.
        try:
            with chosen.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
            if loaded is not None and not isinstance(loaded, dict):
                raise ValueError("it does not hold a mapping of settings")
            file_data = loaded or {}
        except Exception as exc:
            file_data = {}
            kept = _set_aside(chosen)
            log.error(
                "could not read the settings in %s (%s). Starting from the "
                "defaults; the file has been kept as %s so nothing in it is "
                "lost.", chosen, exc, kept.name if kept else chosen.name,
            )
            chosen = None

    merged = _deep_merge(DEFAULTS, file_data)
    merged = _deep_merge(merged, _env_overrides(dict(os.environ)))
    config = Config(data=merged, path=chosen)
    _migrate(config, file_data)
    return config


def _stored_section(file_data: dict[str, Any], name: str) -> dict[str, Any]:
    """The file's own mapping for one section, or ``{}``.

    A scalar where a map belongs — ``alerts: true`` is a plausible hand-edit
    meaning "turn alerts on" — must not crash the migration with a
    ``TypeError`` on the ``in`` test. The loader itself survives a corrupt
    value and falls back to defaults; the migration cannot be the one step
    that strands the file.
    """
    section = file_data.get(name)
    return section if isinstance(section, dict) else {}


def _migrate(config: Config, file_data: dict[str, Any]) -> None:
    """Bring a settings file written by an older version up to date.

    Without this a changed default reaches nobody who has ever run the app.
    ``save()`` writes every key, so the file on disk already holds the old
    value and overlays the new one forever — a default is only a default on a
    machine that has never started GateKeeper.

    Applied once and stamped, so a value the user later chooses for themselves
    is not overwritten on the next start.
    """
    stored = int(file_data.get("config_version") or 0)
    if not file_data or stored >= CONFIG_VERSION:
        config.data["config_version"] = CONFIG_VERSION
        return

    changed: list[str] = []
    if stored < 2:
        # Measured on two real recordings, one rising and one falling, at the
        # chart-and-expiry pairings actually traded: at the old gate of 75 the
        # pooled win rate was 59.0%, interval 49.2–68.1, which straddles the
        # 52.1% a 92% payout needs. At 85 it was 73.6%, interval 60.4–83.6,
        # which clears it.
        #
        # 85 was chosen by looking at the table that scored it, so this is a
        # hypothesis under test rather than a settled number — see
        # data/recorded/README.md. Moved only where the install is still on
        # the old default; a number the user picked is left alone.
        # Written explicitly rather than left to the default. ``get`` reads
        # through to the defaults, so testing it against 85 is always true on
        # a file that has never heard of the key — the migration would decide
        # it had nothing to do and write nothing, and every later ``save``
        # would keep overlaying the old file.
        if "min_shown_confidence" not in _stored_section(file_data, "signals"):
            config.set("signals.min_shown_confidence", 62)
            changed.append("signals.min_shown_confidence -> 62")
        # 85 was the previous default and nobody chose it: it was fitted to
        # twenty-nine calls and did not survive the next batch. Move it on.
        # Any other number is one the user picked, and is left alone.
        elif float(config.get("signals.min_shown_confidence", 62)) == 85.0:
            config.set("signals.min_shown_confidence", 62)
            changed.append("signals.min_shown_confidence 85 -> 62")

        # The waiting. Every one of these was tuned when a call arrived every
        # few minutes; on a thirty-second trade off a five-second chart they
        # are each a slice of the trade spent doing nothing. Only values still
        # sitting on the old default are moved — a number the user chose is
        # theirs.
        for key, was, now in (
            ("capture.poll_seconds", 2.0, 0.5),
            ("overlay.scan_seconds", 2.4, 0.6),
            ("signals.cooldown_seconds", 60, 15),
            ("alerts.cooldown_seconds", 120, 20),
            # And this one is not about speed but about agreement: alerts
            # gated at 75 while the panel showed calls at 62 meant every call
            # between the two appeared in silence.
            ("alerts.min_confidence", 75, 62),
        ):
            try:
                current = float(config.get(key, now))
            except (TypeError, ValueError):  # pragma: no cover - corrupt value
                continue
            if current == float(was):
                config.set(key, now)
                changed.append(f"{key} {was} -> {now}")
        # An earlier version of this migration put the 85 on min_confidence,
        # which gates the direction score rather than the displayed number and
        # therefore did not do what it was set to do. Put it back.
        if float(config.get("signals.min_confidence", 75)) == 85.0:
            config.set("signals.min_confidence", 75)
            changed.append("signals.min_confidence 85 -> 75 (wrong gate)")
        # And pinned, because a gate under test cannot also be moving on its
        # own. Auto-tune travels between 55 and 90 in steps of five, which
        # would quietly answer a different question than the one being asked.
        if bool(config.get("signals.auto_tune", True)):
            config.set("signals.auto_tune", False)
            changed.append("signals.auto_tune on -> off")
        # And watch only the two chart lengths actually traded. Reading the
        # same pair at eleven lengths is not eleven times the information; it
        # is ten extra rows to scroll past, each one a verdict about a chart
        # nobody is looking at, and acting on one of those by mistake is a
        # trade the engine never scored.
        if config.get("market.scan_timeframes") is True:
            config.set("market.scan_timeframes", [5, 60])
            changed.append("market.scan_timeframes all -> [5, 60]")

    if stored < 3:
        # Sound was off by default for as long as Windows — the platform most
        # installs run on — had no way to make any: no bundled player, and a
        # desktop-notification path that needed a package the build never
        # carried. The 2026-08-23 session was watched for 91 minutes on one
        # chart while the other charts lit and faded with nothing but a tab
        # changing colour. Both channels now work with nothing installed
        # (PowerShell for the toast, winsound for the chime), so the old
        # silent default is moved on; silence chosen after this stays chosen.
        #
        # An explicit ``sound: false`` is only moved on Windows, where it
        # cannot have been an informed choice — there was no sound to
        # decline. On macOS and Linux the chime always worked, so a written
        # false there is a real preference and stays.
        if "sound" not in _stored_section(file_data, "alerts"):
            config.set("alerts.sound", True)
            changed.append("alerts.sound -> on")
        elif platform.system() == "Windows" and not bool(
            config.get("alerts.sound", True)
        ):
            config.set("alerts.sound", True)
            changed.append("alerts.sound off -> on")

    config.data["config_version"] = CONFIG_VERSION
    if not changed:
        return
    for line in changed:
        log.info("settings updated: %s", line)
    try:
        config.save()
    except Exception as exc:  # pragma: no cover - read-only install
        log.debug("could not save the updated settings: %s", exc)
