"""The tick archive: the input the analysis has been throwing away.

The feed delivers the market several times a second — symbol, moment, exact
price — and everything downstream reads only the candles those ticks get
bucketed into. The bucketing is lossy in exactly the dimension a one-minute
trade cares about: how price moved *within* the bar. Velocity into an entry,
direction runs in the final seconds, whether a level broke in one push or
seven — all of it arrives on the socket and, until this file, died there.

Nothing here changes a call or a gate. This is pure collection, so that when
the current experiment queue settles there is a tick record to measure the
next ideas against, instead of another month of sessions to book first. The
FINDINGS file has ranked tick microstructure the highest-value untouched
input since the ledger shipped; an archive is the precondition to ever
measuring it.

Design constraints, in order:

* **The feed must never notice it.** Every write is batched, every failure is
  swallowed after one log line, and a broken archive degrades to a no-op.
  Losing telemetry is an inconvenience; stalling the socket reader is not.
* **Bounded.** Ticks accumulate at tens of rows a second across the watched
  pairs. Rows older than the retention window are pruned every time the
  archive opens, so the file grows with the window, not with the install's
  age.
* **Separate from the journal.** The journal is the record of decisions and
  outcomes, copied around and read by every report; bulk telemetry stapled to
  it would make every one of those reads heavier. The archive is a sidecar
  database in the same storage directory, under the same single-instance
  lock, joinable to the journal by symbol and time when the analysis wants
  it.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from ..logging_setup import get_logger

log = get_logger(__name__)

#: Buffered rows that force a write. Small enough that a crash loses a couple
#: of seconds of telemetry, large enough that the feed thread commits a few
#: times a minute instead of hundreds.
FLUSH_ROWS = 400

#: Seconds a buffered row may wait before the next tick triggers a write
#: anyway, so a quiet stream still lands on disk while it is quiet.
FLUSH_SECONDS = 5.0

#: Days of ticks kept. Two weeks is several times the gap between sessions
#: being analysed, and roughly fifty megabytes at the observed tick rates —
#: a bounded cost for an unbounded-feeling record.
RETENTION_DAYS = 14.0


class TickArchive:
    """Append-only store for the feed's raw ticks, batched and bounded.

    Used from the feed's own thread; the connection is opened lazily there.
    ``close`` may be called from another thread during shutdown, which SQLite
    permits because the connection is opened with cross-thread checks off and
    the feed thread is joined before shutdown closes anything.
    """

    def __init__(
        self,
        path: str | Path,
        retention_days: float = RETENTION_DAYS,
        flush_rows: int = FLUSH_ROWS,
        flush_seconds: float = FLUSH_SECONDS,
        clock: Any = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self.retention_days = float(retention_days)
        self.flush_rows = max(1, int(flush_rows))
        self.flush_seconds = float(flush_seconds)
        self._clock = clock
        self._pending: list[tuple[str, float, float]] = []
        self._conn: sqlite3.Connection | None = None
        self._last_flush = float(clock())
        self._failed = False
        self._archived = 0

    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection | None:
        if self._conn is not None or self._failed:
            return self._conn
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ticks ("
                "symbol TEXT NOT NULL, at REAL NOT NULL, price REAL NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticks_symbol_at "
                "ON ticks(symbol, at)"
            )
            if self.retention_days > 0:
                horizon = time.time() - self.retention_days * 86400.0
                conn.execute("DELETE FROM ticks WHERE at < ?", (horizon,))
            conn.commit()
            self._conn = conn
        except (sqlite3.Error, OSError) as exc:
            # OSError too: a path whose parent turns out to be a file, a
            # read-only disk — the promise is no failure mode raises into
            # the feed, not just the SQLite-shaped ones.
            self._give_up(exc)
        return self._conn

    def _give_up(self, exc: Exception) -> None:
        # Said once, then silent: this runs on the thread that reads the
        # market, and a log line per tick about a full disk would itself be
        # the problem it is reporting.
        if not self._failed:
            log.warning("tick archive disabled: %s", exc)
        self._failed = True
        self._pending.clear()

    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return not self._failed

    @property
    def archived(self) -> int:
        """Ticks written this run — buffered ones included, so the number
        shown at shutdown matches what close() will have persisted."""
        return self._archived + len(self._pending)

    def add(self, symbol: str, timestamp: float, price: float) -> None:
        if self._failed:
            return
        try:
            self._pending.append((str(symbol), float(timestamp), float(price)))
        except (TypeError, ValueError):
            return
        now = float(self._clock())
        if (
            len(self._pending) >= self.flush_rows
            or now - self._last_flush >= self.flush_seconds
        ):
            self.flush()

    def extend(self, ticks: Iterable[Any]) -> None:
        """Ticks as the feed parses them — anything with symbol/timestamp/price."""
        for tick in ticks:
            self.add(
                getattr(tick, "symbol", ""),
                getattr(tick, "timestamp", 0.0),
                getattr(tick, "price", 0.0),
            )

    def flush(self) -> None:
        self._last_flush = float(self._clock())
        if not self._pending or self._failed:
            return
        conn = self._connect()
        if conn is None:
            return
        rows, self._pending = self._pending, []
        try:
            conn.executemany(
                "INSERT INTO ticks (symbol, at, price) VALUES (?, ?, ?)", rows
            )
            conn.commit()
            self._archived += len(rows)
        except sqlite3.Error as exc:
            self._give_up(exc)

    def close(self) -> None:
        self.flush()
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
            self._conn = None
        if self._archived:
            log.info(
                "tick archive: %d ticks kept this run (%s)",
                self._archived, self.path,
            )
