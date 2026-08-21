"""One GateKeeper per journal.

Two copies of the app pointed at the same data directory both record every
call and every settled hand trade, so the journal fills with byte-identical
rows: every win counted twice, every loss counted twice, and the calibration
record — the thing the gates are tuned from — polluted at double weight. That
is not hypothetical; it is exactly what a session on 2026-08-21 produced, 48
listed calls that were 24, and 20 hand trades that were 10.

The guard is a held file lock. The first instance opens a lock file inside the
data directory and locks it; the operating system releases the lock the moment
that process ends, however it ends — crash, kill, power cut — so there is no
stale state to reason about and nothing to clean up. A second instance fails
to take the lock while the first is alive and is told so plainly.

What this deliberately is **not**:

* Not a PID file. A stored PID has to be probed to learn whether its process
  is still alive, and the obvious probe — ``os.kill(pid, 0)`` — does not ask
  on Windows, it *terminates*: signal 0 is not special there, so checking
  whether GateKeeper is running would kill it. A lock held by the kernel
  needs no probe at all.
* Not an unlink-on-exit scheme. Deleting the lock file on release races: one
  process can lock an inode another has already unlinked, after which two
  "locks" on two different inodes both hold. The file stays; it is a few
  bytes in the app's own data directory.

The PID written into the file is a diagnostic for a human reading the
directory, never an input to any decision.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO

from .logging_setup import get_logger

log = get_logger(__name__)

LOCK_FILENAME = "gatekeeper.lock"


class AnotherInstanceRunning(RuntimeError):
    """A second GateKeeper tried to open a journal that is already open."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        super().__init__(
            "GateKeeper is already running.\n\n"
            "Two copies sharing one journal record every call and every "
            "trade twice, which corrupts the record the gates are tuned "
            "from. Use the copy that is already open — or close it first "
            "if you cannot find it."
        )


class InstanceLock:
    """A held lock on one data directory. Release by calling close()."""

    def __init__(self, path: Path, handle: IO[bytes]) -> None:
        self.path = path
        self._handle: IO[bytes] | None = handle

    @property
    def held(self) -> bool:
        return self._handle is not None

    def close(self) -> None:
        """Release the lock. The file itself stays — see the module docstring."""
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock(handle)
        except OSError:  # pragma: no cover - release is best-effort
            pass
        finally:
            handle.close()

    # The lock rides the process down if close() is never reached — the OS
    # drops it when the handle dies with us. These just make the tidy paths
    # tidy.
    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown
        self.close()


def acquire(data_dir: Path) -> InstanceLock:
    """Take the single-instance lock for ``data_dir``.

    Returns the held lock, which must stay referenced for the life of the
    process. Raises :class:`AnotherInstanceRunning` if a live process already
    holds it.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / LOCK_FILENAME

    # "a+b" creates the file without truncating it: truncation is reserved
    # for after the lock is won, so a losing opener cannot blank the
    # holder's PID note.
    handle: IO[bytes] = open(lock_path, "a+b")
    try:
        _lock(handle)
    except OSError:
        handle.close()
        raise AnotherInstanceRunning(lock_path) from None

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.flush()
    except OSError:  # pragma: no cover - the note is optional, the lock is not
        pass
    log.info("single-instance lock held: %s", lock_path)
    return InstanceLock(lock_path, handle)


if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
    import msvcrt

    def _lock(handle: IO[bytes]) -> None:
        # msvcrt locks a byte range from the current position, so pin it.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
