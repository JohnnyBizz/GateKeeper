"""One GateKeeper per journal.

The session of 2026-08-21 21:57 UTC proved what two copies sharing one
journal do: every call listed twice, every hand trade listed twice with the
same matched score, every rate in the report computed over rows counted at
double weight. The guard is a file lock the operating system itself releases
when the holding process dies, so there is no stale-lock state to reason
about — and deliberately not a PID probe, because ``os.kill(pid, 0)`` on
Windows does not ask whether a process is alive, it terminates it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from poa import single_instance
from poa.single_instance import AnotherInstanceRunning, acquire

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestOnlyOneGateKeeperPerJournal:
    def test_the_first_instance_gets_the_lock(self, tmp_path):
        with acquire(tmp_path) as lock:
            assert lock.held
            assert (tmp_path / single_instance.LOCK_FILENAME).exists()

    def test_the_pid_in_the_file_is_a_note_for_humans(self, tmp_path):
        import os

        with acquire(tmp_path):
            written = (tmp_path / single_instance.LOCK_FILENAME).read_text()
            assert written.strip() == str(os.getpid())

    def test_a_second_instance_is_refused_while_the_first_lives(self, tmp_path):
        with acquire(tmp_path):
            with pytest.raises(AnotherInstanceRunning):
                acquire(tmp_path)

    def test_the_refusal_says_what_is_wrong_and_what_to_do(self, tmp_path):
        with acquire(tmp_path):
            with pytest.raises(AnotherInstanceRunning) as caught:
                acquire(tmp_path)
        message = str(caught.value)
        assert "already running" in message
        assert "twice" in message  # the harm, not just the refusal

    def test_a_refused_acquire_does_not_blank_the_holder_note(self, tmp_path):
        import os

        with acquire(tmp_path):
            with pytest.raises(AnotherInstanceRunning):
                acquire(tmp_path)
            written = (tmp_path / single_instance.LOCK_FILENAME).read_text()
            assert written.strip() == str(os.getpid())

    def test_the_lock_dies_with_its_holder(self, tmp_path):
        acquire(tmp_path).close()
        # Same directory, immediately reusable: nothing stale survives.
        with acquire(tmp_path) as second:
            assert second.held

    def test_closing_twice_is_harmless(self, tmp_path):
        lock = acquire(tmp_path)
        lock.close()
        lock.close()
        assert not lock.held

    def test_the_lock_file_is_never_deleted(self, tmp_path):
        # Unlink-on-release races: a process can lock an inode another has
        # already unlinked, after which two locks on two inodes both "hold".
        # The file staying behind is the correct behaviour, not a leak.
        acquire(tmp_path).close()
        assert (tmp_path / single_instance.LOCK_FILENAME).exists()

    def test_two_different_journals_do_not_contend(self, tmp_path):
        # The lock guards a *journal*, not the machine: pointing two copies
        # at genuinely different databases is allowed on purpose.
        with acquire(tmp_path / "a"), acquire(tmp_path / "b"):
            pass

    def test_a_second_process_is_refused_too(self, tmp_path):
        # The in-process refusals above ride on the same-process semantics of
        # flock/msvcrt; this is the real case — another process entirely.
        probe = (
            "import sys\n"
            "from poa.single_instance import AnotherInstanceRunning, acquire\n"
            "try:\n"
            f"    acquire({str(tmp_path)!r})\n"
            "except AnotherInstanceRunning:\n"
            "    sys.exit(3)\n"
            "sys.exit(0)\n"
        )
        with acquire(tmp_path):
            held = subprocess.run(
                [sys.executable, "-c", probe], cwd=PROJECT_ROOT, timeout=60
            )
        released = subprocess.run(
            [sys.executable, "-c", probe], cwd=PROJECT_ROOT, timeout=60
        )
        assert held.returncode == 3  # refused while held...
        assert released.returncode == 0  # ...and free once the holder is gone


class TestTheLockUnderTheWorstConditions:
    """Not the polite case. Eight processes at once, and a holder that dies
    by SIGKILL mid-hold — no cleanup code runs, no atexit, nothing. The claim
    under test is the design itself: the kernel releases the lock when the
    process dies, however it dies, so exactly one instance ever holds and a
    crash can never wedge the journal shut.
    """

    PROBE = (
        "import sys, time\n"
        "from poa.single_instance import AnotherInstanceRunning, acquire\n"
        "try:\n"
        "    lock = acquire(sys.argv[1])\n"
        "except AnotherInstanceRunning:\n"
        "    sys.exit(3)\n"
        "print('WON', flush=True)\n"
        "time.sleep(120)\n"
    )

    def test_eight_at_once_one_wins_and_a_dead_winner_frees_the_rest(
        self, tmp_path
    ):
        import time

        procs = [
            subprocess.Popen(
                [sys.executable, "-c", self.PROBE, str(tmp_path)],
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
            )
            for _ in range(8)
        ]
        try:
            # Everyone but the winner is refused and exits on its own.
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                still_running = [p for p in procs if p.poll() is None]
                if len(still_running) == 1:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("the losers never finished being refused")

            losers = [p for p in procs if p.poll() is not None]
            assert len(losers) == 7
            assert all(p.returncode == 3 for p in losers)

            # The one still alive is the one that printed WON — and while it
            # lives, this process is refused like everyone else.
            (winner,) = still_running
            with pytest.raises(AnotherInstanceRunning):
                acquire(tmp_path)

            # Kill it the hard way. No release path runs.
            winner.kill()
            winner.wait(timeout=60)
            assert winner.stdout is not None and b"WON" in winner.stdout.read()

            # The kernel dropped the lock with the process: reacquire at once.
            acquire(tmp_path).close()
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=60)
                if p.stdout is not None:
                    p.stdout.close()


class TestTheOverlayRefusesToDoubleOpen:
    """The wiring: run() takes the lock before anything opens the journal."""

    def _config(self, tmp_path):
        from poa.config import Config

        return Config(
            data={"storage": {"database": str(tmp_path / "storage" / "journal.db")}}
        )

    def test_a_second_overlay_backs_off_and_says_so(self, tmp_path, monkeypatch):
        from poa.overlay import app as overlay_app

        config = self._config(tmp_path)
        monkeypatch.setattr(overlay_app, "load_config", lambda path=None: config)

        opened = []
        monkeypatch.setattr(
            overlay_app, "OverlayApp", lambda cfg: pytest.fail("second instance opened")
        )
        monkeypatch.setattr(
            overlay_app, "_show_already_running", lambda exc: opened.append(str(exc))
        )

        with acquire(tmp_path / "storage"):
            overlay_app.run()

        assert opened and "already running" in opened[0]

    def test_the_dashboard_takes_the_same_lock(self, tmp_path, monkeypatch):
        # The overlay guarding the journal while `python run.py` opened it
        # unguarded was the two-writers defect with a different front door.
        import sys
        from types import SimpleNamespace

        from poa import server as server_module
        from poa.config import Config

        config = Config(
            data={"storage": {"database": str(tmp_path / "storage" / "j.db")}}
        )
        monkeypatch.setattr(server_module, "load_config", lambda p=None: config)
        monkeypatch.setattr(server_module, "setup_logging", lambda **k: None)
        monkeypatch.setattr(
            server_module, "create_app", lambda cfg: object()
        )
        served = []
        monkeypatch.setitem(
            sys.modules, "uvicorn",
            SimpleNamespace(run=lambda *a, **k: served.append(True)),
        )

        with acquire(tmp_path / "storage"):
            server_module.run("ignored.yaml")
        assert served == []  # refused while the overlay holds the journal

        server_module.run("ignored.yaml")
        assert served == [True]  # and serves once the journal is free
        acquire(tmp_path / "storage").close()  # released after serving

    def test_the_first_overlay_runs_and_releases(self, tmp_path, monkeypatch):
        from poa.overlay import app as overlay_app

        config = self._config(tmp_path)
        monkeypatch.setattr(overlay_app, "load_config", lambda path=None: config)

        ran = []

        class FakeApp:
            def __init__(self, cfg):
                pass

            def run(self):
                ran.append(True)

        monkeypatch.setattr(overlay_app, "OverlayApp", FakeApp)
        overlay_app.run()

        assert ran
        # And the lock came off with it, so a restart is not locked out.
        acquire(tmp_path / "storage").close()
