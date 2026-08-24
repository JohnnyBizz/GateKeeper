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

* **The feed must never notice it.** The caller buffers ticks outside the
  feed's own lock; every disk write is batched; a *transient* storage error
  (the database briefly locked by another process) re-queues the batch and
  tries again next flush, and only an unrecoverable error retires the
  archive — after one log line, never a stream of them.
* **Bounded.** Rows older than the retention window are pruned when the
  archive first touches its database *and* periodically while the process
  runs, because a dashboard left serving for two months would otherwise
  grow the file for two months. The prune walks an index on the timestamp
  column, not the whole table.
* **Safe against overlap.** An internal lock guards the buffer, so a
  shutdown path closing the archive while the feed thread is mid-flush is
  a serialization, not a race.
* **Separate from the journal.** The journal is the record of decisions and
  outcomes, copied around and read by every report; bulk telemetry stapled
  to it would make every one of those reads heavier. The archive is a
  sidecar database in the same storage directory, under the same
  single-instance lock, joinable to the journal by symbol and time when
  the analysis wants it.
"""

from __future__ import annotations

import sqlite3
import threading
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

#: How often the retention window is re-enforced while the process runs.
#: Pruning only at first touch made "bounded" false for exactly the
#: always-on deployment (the dashboard server): six hours keeps the file
#: honest without the prune becoming a routine cost.
PRUNE_EVERY_SECONDS = 6 * 3600.0

#: Re-queued rows the buffer will hold while the database is transiently
#: locked. Past this the oldest are dropped — bounded memory beats a
#: perfect record, and the drop is logged once per incident.
MAX_PENDING_ROWS = 50_000


class TickArchive:
    """Append-only store for the feed's raw ticks, batched and bounded.

    Fed from the feed source *outside* its message lock; the internal lock
    here makes concurrent add/flush/close a serialization rather than a
    race, so a shutdown overlapping a slow flush cannot corrupt the buffer
    or double-write a batch.
    """

    def __init__(
        self,
        path: str | Path,
        retention_days: float = RETENTION_DAYS,
        flush_rows: int = FLUSH_ROWS,
        flush_seconds: float = FLUSH_SECONDS,
        prune_every_seconds: float = PRUNE_EVERY_SECONDS,
        clock: Any = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self.retention_days = float(retention_days)
        self.flush_rows = max(1, int(flush_rows))
        self.flush_seconds = float(flush_seconds)
        self.prune_every_seconds = float(prune_every_seconds)
        self._clock = clock
        self._mutex = threading.Lock()
        self._pending: list[tuple[str, float, float]] = []
        self._conn: sqlite3.Connection | None = None
        self._last_flush = float(clock())
        self._last_prune: float | None = None
        self._failed = False
        self._archived = 0
        self._overflow_noted = False

    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection | None:
        """Lock held. Opens lazily, prunes on first touch."""
        if self._conn is not None or self._failed:
            return self._conn
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # The same generous busy timeout the journal uses for the same
            # shared-directory reality: a sibling process holding the write
            # lock for a moment is a wait, not a failure.
            conn = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=10.0
            )
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
            # The retention prune ranges on ``at`` alone; without its own
            # index that DELETE walks the entire table — measured as the
            # single most expensive thing this file could do.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticks_at ON ticks(at)"
            )
            self._conn = conn
            self._prune_locked()
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            # OSError too: a path whose parent turns out to be a file, a
            # read-only disk — the promise is no failure mode raises into
            # the feed, not just the SQLite-shaped ones.
            self._give_up(exc)
        return self._conn

    def _prune_locked(self) -> None:
        """Lock held, connection open. Re-enforce the retention window."""
        if self._conn is None or self.retention_days <= 0:
            return
        horizon = time.time() - self.retention_days * 86400.0
        self._conn.execute("DELETE FROM ticks WHERE at < ?", (horizon,))
        self._last_prune = float(self._clock())

    def _give_up(self, exc: Exception) -> None:
        # Said once, then silent: this is telemetry, and a log line per tick
        # about a full disk would itself be the problem it is reporting.
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
        with self._mutex:
            return self._archived + len(self._pending)

    def add(self, symbol: str, timestamp: float, price: float) -> None:
        if self._failed:
            return
        try:
            row = (str(symbol), float(timestamp), float(price))
        except (TypeError, ValueError):
            return
        with self._mutex:
            self._pending.append(row)
            due = (
                len(self._pending) >= self.flush_rows
                or float(self._clock()) - self._last_flush
                >= self.flush_seconds
            )
        if due:
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
        with self._mutex:
            self._flush_locked()

    def _flush_locked(self) -> None:
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
            if (
                self._last_prune is not None
                and float(self._clock()) - self._last_prune
                >= self.prune_every_seconds
            ):
                self._prune_locked()
            conn.commit()
            self._archived += len(rows)
        except sqlite3.OperationalError as exc:
            # Transient by nature — the database briefly locked by a sibling
            # process, a momentarily unavailable disk. The batch goes back
            # in the queue for the next flush; retiring the whole archive
            # over a collision would discard a session of the input this
            # file exists to keep.
            self._pending = rows + self._pending
            if len(self._pending) > MAX_PENDING_ROWS:
                dropped = len(self._pending) - MAX_PENDING_ROWS
                del self._pending[:dropped]
                if not self._overflow_noted:
                    log.warning(
                        "tick archive backlog full; dropped %d oldest "
                        "buffered ticks (%s)", dropped, exc,
                    )
                    self._overflow_noted = True
        except sqlite3.Error as exc:
            self._give_up(exc)

    def close(self) -> None:
        with self._mutex:
            self._flush_locked()
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
