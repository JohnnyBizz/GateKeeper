"""Alert delivery channels.

Each notifier is best-effort and must never raise into the engine loop: a
missing notification tool is an inconvenience, not a reason to stop analysing.

Windows is the platform most installs run on, and for the first eight releases
it was the one platform where no channel worked at all: the desktop path
required a package (win10toast) that was never in the requirements, never in
the build spec and never in CI, and the sound path had no default player. The
user of the 2026-08-23 session watched one chart for 91 minutes and never
received a single pop-up — because none had ever been sent. Both paths now use
what Windows itself ships: a toast raised through PowerShell, and the system
chime through the standard library's ``winsound``. Nothing to install.
"""

from __future__ import annotations

import base64
import platform
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable
from xml.sax.saxutils import escape as _xml_escape

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


# PowerShell's own registered AppUserModelID. A toast has to be raised under
# an application Windows knows, and an unpackaged Python process is not one —
# but PowerShell is, on every machine, with no registration step. The same
# trick BurntToast and every dependency-free toast script uses.
_POWERSHELL_APP_ID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\"
    "powershell.exe"
)

_XML_QUOTES = {"'": "&apos;", '"': "&quot;"}


def _toast_script(title: str, body: str) -> str:
    """The PowerShell that shows one toast, with the text already escaped.

    Windows PowerShell 5.1 syntax on purpose — the WinRT bracket-loading it
    relies on does not exist in PowerShell 7, and ``powershell.exe`` is 5.1
    on every Windows 10 and 11 machine. The text is XML-escaped and then sits
    inside single-quoted PowerShell strings it can no longer close, so a pair
    name or an alert body can never break out of the script.
    """
    clean_title = _xml_escape(" ".join(str(title).split()), _XML_QUOTES)
    clean_body = _xml_escape(" ".join(str(body).split()), _XML_QUOTES)
    xml = (
        '<toast duration="short"><visual><binding template="ToastText02">'
        f'<text id="1">{clean_title}</text><text id="2">{clean_body}</text>'
        "</binding></visual></toast>"
    )
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "$null = [Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime]\n"
        "$null = [Windows.UI.Notifications.ToastNotification, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime]\n"
        "$null = [Windows.Data.Xml.Dom.XmlDocument, "
        "Windows.Data.Xml.Dom, ContentType = WindowsRuntime]\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$xml.LoadXml('{xml}')\n"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification "
        "-ArgumentList $xml\n"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{_POWERSHELL_APP_ID}').Show($toast)\n"
    )


def powershell_toast_command(title: str, body: str) -> list[str]:
    """The argv that raises one Windows toast.

    ``-EncodedCommand`` rather than ``-Command``: the script is carried as
    base64 over UTF-16, so no quoting rule of cmd, PowerShell or the C
    runtime ever sees the alert text.
    """
    encoded = base64.b64encode(
        _toast_script(title, body).encode("utf-16-le")
    ).decode("ascii")
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-EncodedCommand",
        encoded,
    ]


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
        # PowerShell rather than a pip package: the old path imported
        # win10toast, which was never in the requirements, the build spec or
        # CI — so every Windows install detected nothing and no Windows user
        # ever saw a notification. PowerShell is on every Windows machine.
        if system == "Windows" and shutil.which("powershell"):
            return "powershell"
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
            if self._method == "powershell":
                # Popen, not run: PowerShell takes a second or two to start,
                # and this is called from the thread that repaints the panel.
                # A toast is fire-and-forget; blocking the panel to await one
                # would trade the whole UI for a delivery receipt.
                subprocess.Popen(
                    powershell_toast_command(title, alert.body),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("desktop notification failed: %s", exc)
        return False


# Not a shell command: the sentinel that means "use the standard library's
# winsound". Windows has no bundled command-line player to shell out to, which
# is why its default here was an empty string — and an empty string meant every
# Windows install was silent however the settings were set.
WINDOWS_CHIME = "winsound"


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
        if system == "Windows":
            return WINDOWS_CHIME
        return ""

    @property
    def available(self) -> bool:
        return bool(self.command)

    def send(self, alert: Alert) -> bool:
        if not self.command:
            return False
        if self.command == WINDOWS_CHIME:
            try:
                import winsound

                # MessageBeep posts to the sound driver and returns at once,
                # so the panel thread never waits on audio.
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                return True
            except (ImportError, RuntimeError, OSError) as exc:
                log.debug("sound alert failed: %s", exc)
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
