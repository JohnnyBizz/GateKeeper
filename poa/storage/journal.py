"""The signal journal.

Every generated signal is recorded with the full analysis context that produced
it, so that a setup can be reviewed after the fact rather than remembered. The
journal also resolves outcomes: once a signal's expiration has elapsed, the
price at that moment is compared against the entry price and the row is marked
won/lost/flat.

This records analysis only. No trade is ever placed from here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..logging_setup import get_logger
from ..models import Direction, utcnow
from ..signals.engine import Signal

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id                  TEXT PRIMARY KEY,
    timestamp           TEXT NOT NULL,
    asset               TEXT NOT NULL,
    chart_timeframe     INTEGER NOT NULL,
    trade_duration      INTEGER NOT NULL,
    direction           TEXT NOT NULL,
    state               TEXT NOT NULL,
    direction_confidence REAL NOT NULL,
    duration_confidence REAL NOT NULL,
    overall_confidence  REAL NOT NULL,
    setup_quality       TEXT NOT NULL,
    price               REAL,
    market_regime       TEXT,
    heikin_ashi         TEXT,
    trend               TEXT,
    trend_strength      REAL,
    rsi                 REAL,
    macd                TEXT,
    ema_condition       TEXT,
    support             REAL,
    resistance          REAL,
    recommended_duration INTEGER,
    reason              TEXT,
    invalidation        TEXT,
    warnings            TEXT,
    screenshot          TEXT,
    payload             TEXT,
    -- outcome, filled in once the expiration has elapsed
    outcome             TEXT,
    outcome_price       REAL,
    outcome_at          TEXT,
    price_change        REAL,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_asset ON signals(asset);
CREATE INDEX IF NOT EXISTS idx_signals_outcome ON signals(outcome);
CREATE INDEX IF NOT EXISTS idx_signals_direction ON signals(direction);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    confidence  REAL,
    signal_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
"""


@dataclass
class JournalEntry:
    """A row from the journal, shaped for the dashboard."""

    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


class Journal:
    """SQLite-backed signal journal. Safe to use from the engine thread."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=10.0
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------

    def record(self, signal: Signal, screenshot_path: str | None = None) -> str:
        """Persist a signal. Returns the signal id."""
        row = _signal_to_row(signal, screenshot_path)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        with self._lock:
            self._connection.execute(
                f"INSERT OR REPLACE INTO signals ({columns}) VALUES ({placeholders})",
                row,
            )
            self._connection.commit()
        return signal.id

    def record_alert(
        self,
        kind: str,
        title: str,
        body: str,
        confidence: float,
        signal_id: str | None,
        timestamp: datetime | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO alerts (timestamp, kind, title, body, confidence, signal_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (timestamp or utcnow()).isoformat(),
                    kind,
                    title,
                    body,
                    float(confidence),
                    signal_id,
                ),
            )
            self._connection.commit()

    # ------------------------------------------------------------------

    def resolve_outcomes(self, current_price: float, now: datetime | None = None) -> int:
        """Settle any directional signals whose expiration has elapsed.

        A binary option settles on where price sits at expiry relative to entry,
        so that is exactly what is recorded. ``flat`` means the price was
        unchanged, which on a real platform is usually a refund or a loss
        depending on the broker — it is recorded honestly rather than counted
        as a win.
        """
        now = now or utcnow()
        pending = self.pending_outcomes(now)
        if not pending:
            return 0

        resolved = 0
        with self._lock:
            for row in pending:
                entry_price = row["price"]
                if entry_price is None:
                    outcome = "unknown"
                    change = None
                else:
                    change = current_price - entry_price
                    if abs(change) < 1e-12:
                        outcome = "flat"
                    elif row["direction"] == Direction.CALL.value:
                        outcome = "win" if change > 0 else "loss"
                    else:
                        outcome = "win" if change < 0 else "loss"
                self._connection.execute(
                    "UPDATE signals SET outcome = ?, outcome_price = ?, "
                    "outcome_at = ?, price_change = ? WHERE id = ?",
                    (
                        outcome,
                        float(current_price),
                        now.isoformat(),
                        None if change is None else float(change),
                        row["id"],
                    ),
                )
                resolved += 1
            self._connection.commit()
        if resolved:
            log.debug("resolved %d journal outcome(s)", resolved)
        return resolved

    def pending_outcomes(self, now: datetime | None = None) -> list[sqlite3.Row]:
        """Directional signals whose duration window has elapsed but are unsettled."""
        now = now or utcnow()
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, timestamp, direction, trade_duration, price FROM signals "
                "WHERE outcome IS NULL AND direction IN (?, ?)",
                (Direction.CALL.value, Direction.PUT.value),
            ).fetchall()
        due: list[sqlite3.Row] = []
        for row in rows:
            try:
                started = datetime.fromisoformat(row["timestamp"])
            except ValueError:  # pragma: no cover - corrupt row
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if now >= started + timedelta(seconds=int(row["trade_duration"])):
                due.append(row)
        return due

    # ------------------------------------------------------------------

    def recent(self, limit: int = 50, asset: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM signals"
        params: list[Any] = []
        if asset:
            query += " WHERE asset = ?"
            params.append(asset)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get(self, signal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM signals WHERE id = ?", (signal_id,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def annotate(self, signal_id: str, notes: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE signals SET notes = ? WHERE id = ?", (notes, signal_id)
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM signals").fetchone()
        return int(row["n"])

    def statistics(self, asset: str | None = None) -> dict[str, Any]:
        """Aggregate performance over settled signals."""
        query = (
            "SELECT direction, trade_duration, chart_timeframe, setup_quality, "
            "overall_confidence, outcome, market_regime, timestamp FROM signals "
            "WHERE direction IN ('CALL', 'PUT')"
        )
        params: list[Any] = []
        if asset:
            query += " AND asset = ?"
            params.append(asset)
        query += " ORDER BY timestamp ASC"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()

        from ..backtesting.stats import summarise_outcomes

        return summarise_outcomes(
            [
                {
                    "direction": r["direction"],
                    "trade_duration": r["trade_duration"],
                    "chart_timeframe": r["chart_timeframe"],
                    "setup_quality": r["setup_quality"],
                    "confidence": r["overall_confidence"],
                    "outcome": r["outcome"],
                    "regime": r["market_regime"],
                    "timestamp": r["timestamp"],
                }
                for r in rows
            ]
        )

    def purge(self) -> None:
        """Drop every row — used by the tests and by an explicit user reset."""
        with self._lock:
            self._connection.execute("DELETE FROM signals")
            self._connection.execute("DELETE FROM alerts")
            self._connection.commit()


# --------------------------------------------------------------------------


def _signal_to_row(signal: Signal, screenshot_path: str | None) -> dict[str, Any]:
    mtf = signal.mtf
    current = mtf.current if mtf else None
    indicators = current.indicators if current else None
    levels = current.levels if current else None

    return {
        "id": signal.id,
        "timestamp": signal.timestamp.isoformat(),
        "asset": signal.asset,
        "chart_timeframe": signal.chart_timeframe,
        "trade_duration": signal.trade_duration,
        "direction": signal.direction.value,
        "state": signal.state.value,
        "direction_confidence": float(signal.direction_confidence),
        "duration_confidence": float(signal.duration_confidence),
        "overall_confidence": float(signal.overall_confidence),
        "setup_quality": signal.setup_quality.value,
        "price": signal.price,
        "market_regime": current.regime.regime.value if current else None,
        "heikin_ashi": current.heikin_ashi.pattern if current else None,
        "trend": current.trend_bias.value if current else None,
        "trend_strength": current.trend_strength if current else None,
        "rsi": _finite(indicators.rsi) if indicators else None,
        "macd": indicators.macd_bias.value if indicators else None,
        "ema_condition": indicators.ema_alignment.value if indicators else None,
        "support": (
            levels.nearest_support.price
            if levels and levels.nearest_support
            else None
        ),
        "resistance": (
            levels.nearest_resistance.price
            if levels and levels.nearest_resistance
            else None
        ),
        "recommended_duration": (
            signal.duration.recommended_seconds if signal.duration else None
        ),
        "reason": signal.reason,
        "invalidation": signal.invalidation,
        "warnings": json.dumps(signal.warnings),
        "screenshot": screenshot_path,
        "payload": json.dumps(signal.to_dict(include_mtf=False)),
        "outcome": None,
        "outcome_price": None,
        "outcome_at": None,
        "price_change": None,
        "notes": None,
    }


def _finite(value: float | None) -> float | None:
    import math

    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("warnings", "payload"):
        raw = data.get(key)
        if raw:
            try:
                data[key] = json.loads(raw)
            except (TypeError, ValueError):
                data[key] = None
    return data
