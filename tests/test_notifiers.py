"""The delivery channels — above all, the two that never worked.

Every Windows install ran with no notification path at all: the desktop
notifier imported win10toast, which was never in the requirements, the build
spec or CI, so it detected nothing and silently sent nothing; and the sound
notifier had no default player on Windows, so an empty command meant silence
however the settings were set. The user of the 2026-08-23 session watched one
chart for 91 minutes and never received a single pop-up — because in eight
releases, none had ever been sent.

Both paths now use what Windows itself ships — PowerShell for the toast,
``winsound`` for the chime — and these tests pin that they need nothing else.
"""

from __future__ import annotations

import base64
import sys
import types

from poa.alerts import notifiers
from poa.alerts.notifiers import (
    Alert,
    DesktopNotifier,
    SoundNotifier,
    WINDOWS_CHIME,
    powershell_toast_command,
)


def _alert(**kw) -> Alert:
    fields = {
        "kind": "WATCHLIST",
        "title": "GBP/USD 5 SEC — PUT",
        "body": "PUT 88/100 on GBP/USD 5 SEC. You're on EUR/USD 1 MIN.",
    }
    fields.update(kw)
    return Alert(**fields)


def _decoded_script(argv: list[str]) -> str:
    """The PowerShell that would run, back out of the base64."""
    encoded = argv[argv.index("-EncodedCommand") + 1]
    return base64.b64decode(encoded).decode("utf-16-le")


class TestWindowsFinallyHasAPopup:
    def _windows(self, monkeypatch, has_powershell=True):
        monkeypatch.setattr(notifiers.platform, "system", lambda: "Windows")
        monkeypatch.setattr(
            notifiers.shutil,
            "which",
            lambda name: (
                "C:\\Windows\\powershell.exe"
                if has_powershell and name == "powershell"
                else None
            ),
        )

    def test_windows_detects_powershell_and_is_available(self, monkeypatch):
        self._windows(monkeypatch)
        assert DesktopNotifier().available

    def test_nothing_extra_is_ever_imported(self, monkeypatch):
        """The old path needed win10toast, which no build ever carried."""
        self._windows(monkeypatch)
        notifier = DesktopNotifier()
        assert notifier._method == "powershell"
        assert "win10toast" not in sys.modules

    def test_a_machine_without_powershell_reports_unavailable(self, monkeypatch):
        self._windows(monkeypatch, has_powershell=False)
        assert not DesktopNotifier().available

    def test_the_toast_carries_the_title_and_the_body(self):
        script = _decoded_script(
            powershell_toast_command("GBP/USD 5 SEC — PUT", "Switch it to 5 SEC.")
        )
        assert "GBP/USD 5 SEC — PUT" in script
        assert "Switch it to 5 SEC." in script
        assert "ToastNotificationManager" in script

    def test_markup_in_the_text_cannot_break_out_of_the_toast(self):
        """A pair name is platform data; the script must survive anything."""
        script = _decoded_script(
            powershell_toast_command("<evil>&'break'", 'body with "quotes"')
        )
        assert "<evil>" not in script
        assert "&lt;evil&gt;" in script
        assert "&apos;break&apos;" in script
        assert "&quot;quotes&quot;" in script

    def test_send_launches_powershell_without_waiting_for_it(self, monkeypatch):
        """PowerShell takes a second or two to start, and send() is called
        from the thread that repaints the panel. Fire and forget."""
        self._windows(monkeypatch)
        launched: list[list[str]] = []
        monkeypatch.setattr(
            notifiers.subprocess,
            "Popen",
            lambda argv, **kw: launched.append(argv),
        )
        assert DesktopNotifier().send(_alert())
        (argv,) = launched
        assert argv[0] == "powershell"
        assert "-EncodedCommand" in argv
        assert "PUT 88/100 on GBP/USD 5 SEC." in _decoded_script(argv)

    def test_a_failed_launch_is_an_inconvenience_not_a_crash(self, monkeypatch):
        self._windows(monkeypatch)

        def refuse(*a, **kw):
            raise OSError("powershell went missing mid-session")

        monkeypatch.setattr(notifiers.subprocess, "Popen", refuse)
        assert DesktopNotifier().send(_alert()) is False

    def test_linux_still_uses_notify_send(self, monkeypatch):
        monkeypatch.setattr(notifiers.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            notifiers.shutil,
            "which",
            lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
        )
        ran: list[list[str]] = []
        monkeypatch.setattr(
            notifiers.subprocess,
            "run",
            lambda argv, **kw: ran.append(argv),
        )
        assert DesktopNotifier().send(_alert())
        assert ran and ran[0][0] == "notify-send"


class TestWindowsFinallyHasASound:
    def test_the_default_on_windows_is_the_built_in_chime(self, monkeypatch):
        monkeypatch.setattr(notifiers.platform, "system", lambda: "Windows")
        notifier = SoundNotifier()
        assert notifier.command == WINDOWS_CHIME
        assert notifier.available

    def test_the_chime_goes_through_winsound(self, monkeypatch):
        beeped: list[int] = []
        fake = types.ModuleType("winsound")
        fake.MB_ICONASTERISK = 64
        fake.MessageBeep = lambda kind: beeped.append(kind)
        monkeypatch.setitem(sys.modules, "winsound", fake)

        assert SoundNotifier(WINDOWS_CHIME).send(_alert())
        assert beeped == [64]

    def test_a_refused_beep_returns_false_rather_than_raising(self, monkeypatch):
        fake = types.ModuleType("winsound")
        fake.MB_ICONASTERISK = 64

        def refuse(kind):
            raise RuntimeError("no sound device")

        fake.MessageBeep = refuse
        monkeypatch.setitem(sys.modules, "winsound", fake)
        assert SoundNotifier(WINDOWS_CHIME).send(_alert()) is False

    def test_a_users_own_command_still_runs_through_the_shell(self, monkeypatch):
        launched: list[str] = []
        monkeypatch.setattr(
            notifiers.subprocess,
            "Popen",
            lambda command, **kw: launched.append(command),
        )
        assert SoundNotifier("play /tmp/ding.wav").send(_alert())
        assert launched == ["play /tmp/ding.wav"]

    def test_no_player_anywhere_stays_quietly_unavailable(self, monkeypatch):
        monkeypatch.setattr(notifiers.platform, "system", lambda: "Linux")
        monkeypatch.setattr(notifiers.shutil, "which", lambda name: None)
        notifier = SoundNotifier()
        assert not notifier.available
        assert notifier.send(_alert()) is False
