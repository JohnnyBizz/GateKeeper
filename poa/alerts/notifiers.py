"""Alert delivery channels.

Each notifier is best-effort and must never raise into the engine loop: a
missing notification tool is an inconvenience, not a reason to stop analysing.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Alert:
    """A single notification."""

    kind: str  # BUY_SIGNAL, SELL_SIGNAL, SETUP_INVALIDATED, ...
    title: str
    body: str
    emoji: str = ""
    confidence: float = 0.0
    signal_id: str | None = None
    timestamp: str | None = None
    severity: str = "info"  # info | warning | critical

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "emoji": self.emoji,
            "confidence": round(self.confidence, 1),
            "signal_id": self.signal_id,
            "timestamp": self.timestamp,
            "severity": self.severity,
        }


class Notifier(ABC):
    name = "notifier"

    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """Deliver the alert. Returns True on success."""


class LogNotifier(Notifier):
    """Always available; writes alerts to the application log."""

    name = "log"

    def send(self, alert: Alert) -> bool:
        log.info("ALERT %s %s — %s", alert.emoji, alert.title, alert.body)
        return True


class DesktopNotifier(Notifier):
    """Native desktop notifications, using whatever the platform offers."""

    name = "desktop"

    def __init__(self) -> None:
        self._method = self._detect()

    def _detect(self) -> str | None:
        system = platform.system()
        if system == "Linux" and shutil.which("notify-send"):
            return "notify-send"
        if system == "Darwin" and shutil.which("osascript"):
            return "osascript"
        if system == "Windows":
            try:  # pragma: no cover - Windows only
                import win10toast  # noqa: F401

                return "win10toast"
            except ImportError:
                return None
        return None

    @property
    def available(self) -> bool:
        return self._method is not None

    def send(self, alert: Alert) -> bool:
        if self._method is None:
            return False
        title = f"{alert.emoji} {alert.title}".strip()
        try:
            if self._method == "notify-send":
                urgency = {"critical": "critical", "warning": "normal"}.get(
                    alert.severity, "low"
                )
                subprocess.run(
                    ["notify-send", "-u", urgency, title, alert.body],
                    check=False,
                    timeout=5,
                )
                return True
            if self._method == "osascript":
                script = (
                    f'display notification {_applescript_quote(alert.body)} '
                    f'with title {_applescript_quote(title)}'
                )
                subprocess.run(["osascript", "-e", script], check=False, timeout=5)
                return True
            if self._method == "win10toast":  # pragma: no cover - Windows only
                import win10toast

                win10toast.ToastNotifier().show_toast(
                    title, alert.body, duration=6, threaded=True
                )
                return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("desktop notification failed: %s", exc)
        return False


class SoundNotifier(Notifier):
    """Plays a sound via a user-configured command."""

    name = "sound"

    def __init__(self, command: str = "") -> None:
        self.command = command.strip() or self._default_command()

    @staticmethod
    def _default_command() -> str:
        system = platform.system()
        if system == "Darwin":
            return "afplay /System/Library/Sounds/Ping.aiff"
        if system == "Linux":
            for player in ("paplay", "aplay"):
                if shutil.which(player):
                    return f"{player} /usr/share/sounds/freedesktop/stereo/message.oga"
        return ""

    @property
    def available(self) -> bool:
        return bool(self.command)

    def send(self, alert: Alert) -> bool:
        if not self.command:
            return False
        try:
            subprocess.Popen(
                self.command,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("sound alert failed: %s", exc)
            return False


@dataclass
class CallbackNotifier(Notifier):
    """Forwards alerts to an in-process callback — used to push over WebSocket."""

    callback: Callable[[Alert], None]
    name: str = "callback"

    def send(self, alert: Alert) -> bool:
        try:
            self.callback(alert)
            return True
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("callback notifier failed: %s", exc)
            return False


def _applescript_quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
