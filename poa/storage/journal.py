"""The signal journal.

Every generated signal is recorded with the full analysis context that produced
it, so that a setup can be reviewed after the fact rather than remembered. The
journal also resolves outcomes: once a signal's expiration has elapsed, the
price at that moment is compared against the entry price and the row is marked
won/lost/flat.

Settlement is deliberately fussy about *which* price it settles against. A
binary option is decided by the price of one instrument at one moment, so a row
may only be settled by a price that could plausibly be that instrument at that
moment. Three things disqualify a price:

* it came from a different data source (a synthetic-feed signal cannot be
  settled by a screen-read price, or vice versa);
* it is on an incompatible scale (a mis-calibrated axis, or a different pair
  entirely, reads as a price that is not a small perturbation of the entry);
* it arrives far too late (the app was closed over the expiry, so the current
  price says nothing about where price sat when the option actually expired).

Those rows are marked ``void`` rather than guessed at. A wrong outcome is worse
than no outcome: it feeds a win rate the user then reads as real.

This records analysis only. No trade is ever placed from here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ..logging_setup import get_logger
from ..models import Direction, utcnow
from ..signals.engine import Signal

log = get_logger(__name__)

# How late a settlement price may arrive and still be treated as the price at
# expiry. Within a running session the engine settles within one poll of the
# expiration, so this only bites after the app has been closed or paused across
# an expiry — exactly the case where the "current" price is meaningless.
SETTLEMENT_GRACE_FACTOR = 2.0
SETTLEMENT_GRACE_FLOOR_SECONDS = 180.0

# How far a settlement price may sit from the entry price and still be believed
# to be the same instrument on the same scale. A real move over one expiry is a
# fraction of a percent; a factor of two is a different chart or a broken axis.
SCALE_TOLERANCE = 2.0

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
    -- which data source produced the price on this row. Outcomes may only be
    -- settled by a price from the same source.
    source              TEXT,
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
-- idx_signals_source is created after the migration, since an older journal
-- file has no source column at the point this script runs.

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
            self._migrate()
            self._connection.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a journal file was first written.

        Called with the lock held. A journal from an older build has no
        ``source`` column; its existing rows get NULL, which settlement treats
        as "source unknown" and therefore refuses to settle.
        """
        existing = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(signals)")
        }
        for column, ddl in (("source", "source TEXT"),):
            if column not in existing:
                log.info("adding journal column %s", column)
                self._connection.execute(f"ALTER TABLE signals ADD COLUMN {ddl}")
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_source ON signals(source)"
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------

    def record(
        self,
        signal: Signal,
        screenshot_path: str | None = None,
        source: str | None = None,
    ) -> str:
        """Persist a signal. Returns the signal id.

        ``source`` names the data source the price came from. It is what keeps
        a demo run's outcomes out of a live run's win rate.
        """
        row = _signal_to_row(signal, screenshot_path, source)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        with self._lock:
            self._connection.execute(
                f"INSERT OR REPLACE INTO signals ({columns}) VALUES ({placeholders})",
                row,
            )
            self._connection.commit()
        return signal.id

    def record_manual(
        self,
        *,
        asset: str,
        chart_timeframe: int,
        trade_duration: int,
        direction: str,
        direction_confidence: float,
        duration_confidence: float,
        won: bool,
        market_regime: str = "",
        source: str | None = None,
        price: float | None = None,
        timestamp: datetime | None = None,
    ) -> str:
        """File a trade the user took themselves, already settled.

        The record only ever learned from calls it made. A trade taken off the
        panel's reading but not on its say-so — which is most of them, since
        the verdict is usually WAIT — taught it nothing at all, and those are
        the trades that carry the information it is missing: what happens at
        scores it currently refuses.

        Written as a settled row so it is indistinguishable to the calibration
        from a signal that was called and followed. It is real money on a real
        chart either way; which of us pressed the button does not change what
        the market did next. ``notes`` marks where it came from, so the two can
        still be told apart by anything that needs to.
        """
        stamp = (timestamp or utcnow()).isoformat()
        row = {
            "id": f"manual-{uuid.uuid4()}",
            "timestamp": stamp,
            "asset": asset,
            "chart_timeframe": int(chart_timeframe),
            "trade_duration": int(trade_duration),
            "direction": direction,
            "state": "SETTLED",
            "direction_confidence": float(direction_confidence),
            "duration_confidence": float(duration_confidence),
            "overall_confidence": float(direction_confidence),
            "setup_quality": "MANUAL",
            "price": price,
            "market_regime": market_regime,
            "source": source,
            "outcome": "win" if won else "loss",
            "outcome_at": stamp,
            "reason": "Taken manually; outcome entered by hand.",
            "notes": "manual",
        }
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        with self._lock:
            self._connection.execute(
                f"INSERT INTO signals ({columns}) VALUES ({placeholders})", row
            )
            self._connection.commit()
        return row["id"]

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

    def resolve_outcomes(
        self,
        current_price: float,
        now: datetime | None = None,
        source: str | None = None,
        asset: str | None = None,
        price_at: Callable[[datetime], float | None] | None = None,
    ) -> int:
        """Settle any directional signals whose expiration has elapsed.

        A binary option settles on where price sits at expiry relative to entry,
        so that is exactly what is recorded. ``flat`` means the price was
        unchanged, which on a real platform is usually a refund or a loss
        depending on the broker — it is recorded honestly rather than counted
        as a win.

        ``current_price`` is only applied to rows it could legitimately settle.
        Rows from another source, another instrument, another price scale, or
        an expiry the app slept through are marked ``void`` and excluded from
        every statistic. Returns the number of rows that stopped being pending,
        voids included.

        ``price_at`` looks a price up in the chart's own history, and when it
        can answer it decides the row instead of ``current_price``. That is the
        difference between settling a trade at its expiry and settling it at
        the moment the app got round to looking: the second measures a longer
        move than the one that was bet on, and a thirty-second trade noticed
        two minutes late was being scored on a two-and-a-half-minute move and
        filed as a clean win or loss with nothing marking it.

        How late each settlement was is recorded either way, so a rate can be
        read back against how promptly it was measured.
        """
        now = now or utcnow()
        pending = self.pending_outcomes(now)
        if not pending:
            return 0

        resolved = 0
        voided = 0
        with self._lock:
            for row in pending:
                # Not this price's row. Left pending for the chart that can
                # actually decide it, rather than voided for not being here.
                if settles_a_different_chart(row, asset):
                    continue
                entry_price = row["price"]
                # The moment this row was actually betting on.
                expiry = _expiry_of(row)
                settled_price: float | None = None
                if price_at is not None and expiry is not None:
                    try:
                        settled_price = price_at(expiry)
                    except Exception:  # pragma: no cover - defensive
                        settled_price = None
                # How far past its own expiry this settlement is being made.
                # Zero when the history could answer, because then it does not
                # matter how long ago that was.
                late = 0.0 if settled_price is not None else _lateness(row, now)

                if settled_price is None:
                    settled_price = current_price
                    reason = _settlement_block(row, current_price, now, source, asset)
                else:
                    # A price read out of the chart's own history at the right
                    # moment cannot be too late to be used, so the only bars
                    # left are the ones about whose chart it belongs to.
                    reason = _settlement_block(
                        row, settled_price, now, source, asset, late=0.0
                    )

                if reason is not None:
                    outcome, change = "void", None
                    voided += 1
                elif entry_price is None:
                    outcome, change = "unknown", None
                else:
                    change = settled_price - entry_price
                    if abs(change) < 1e-12:
                        outcome = "flat"
                    elif row["direction"] == Direction.CALL.value:
                        outcome = "win" if change > 0 else "loss"
                    else:
                        outcome = "win" if change < 0 else "loss"
                self._connection.execute(
                    "UPDATE signals SET outcome = ?, outcome_price = ?, "
                    "outcome_at = ?, price_change = ?, "
                    "notes = COALESCE(notes, ?) WHERE id = ?",
                    (
                        outcome,
                        None if reason else float(settled_price),
                        now.isoformat(),
                        None if change is None else float(change),
                        reason,
                        row["id"],
                    ),
                )
                resolved += 1
            self._connection.commit()
        if resolved:
            log.debug(
                "resolved %d journal outcome(s), %d voided", resolved, voided
            )
        return resolved

    def pending_outcomes(self, now: datetime | None = None) -> list[sqlite3.Row]:
        """Directional signals whose duration window has elapsed but are unsettled."""
        now = now or utcnow()
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, timestamp, asset, direction, trade_duration, price, source "
                "FROM signals WHERE outcome IS NULL AND direction IN (?, ?)",
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

    def statistics(
        self,
        asset: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """Aggregate performance over settled signals.

        ``source`` scopes the numbers to one data source. Without it a run on
        the synthetic feed and a run on the live chart share a win rate, which
        makes the live win rate meaningless — they are not the same experiment.

        ``since`` scopes them to one stretch of time, which is what makes a
        *session* tally a session tally rather than the whole history of the
        file.
        """
        query = (
            "SELECT direction, trade_duration, chart_timeframe, setup_quality, "
            "overall_confidence, outcome, market_regime, timestamp FROM signals "
            "WHERE direction IN ('CALL', 'PUT')"
        )
        params: list[Any] = []
        if asset:
            query += " AND asset = ?"
            params.append(asset)
        if source:
            query += " AND source = ?"
            params.append(source)
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(since.isoformat())
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

    def calibration_records(
        self,
        asset: str | None = None,
        source: str | None = None,
        chart_timeframe: int | None = None,
        trade_duration: int | None = None,
        limit: int = 2000,
    ) -> list[Any]:
        """Settled real trades, reduced to what calibration needs from them.

        These outrank anything a replay produces. A replay is what the engine
        *would* have done on history it can see; these are what it actually did
        and how it actually turned out — on this account, at this broker, with
        the delay between the panel lighting up and the button being pressed
        already baked in. That last part cannot be reproduced in a backtest, so
        a hundred of these are worth more than a thousand replayed ones.

        Scoped hard: a record from another instrument, another timeframe,
        another expiry or a demo feed describes a different experiment, and
        pooling them yields a number about nothing in particular.
        """
        from ..backtesting.calibration import Record

        query = (
            "SELECT direction, direction_confidence, duration_confidence, "
            "market_regime, outcome, timestamp FROM signals "
            "WHERE outcome IN ('win', 'loss') "
            "AND direction IN ('CALL', 'PUT') "
            # A trade with no score is one nothing could attribute: the
            # platform settled it, but no call of ours was live on that
            # instrument when it opened. The outcome is real and stays in the
            # journal; what it must not do is teach the score bands, because
            # the score it would teach them with is a zero standing in for
            # "unknown" — and this record outranks the replay and holds a veto.
            "AND direction_confidence > 0"
        )
        params: list[Any] = []
        for column, value in (
            ("asset", asset),
            ("source", source),
            ("chart_timeframe", chart_timeframe),
            ("trade_duration", trade_duration),
        ):
            if value is not None:
                query += f" AND {column} = ?"
                params.append(value)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(int(limit))

        with self._lock:
            rows = self._connection.execute(query, params).fetchall()

        records: list[Any] = []
        for row in rows:
            hour: int | None = None
            stamp = row["timestamp"]
            if isinstance(stamp, str) and stamp:
                try:
                    hour = datetime.fromisoformat(stamp.replace("Z", "+00:00")).hour
                except ValueError:
                    hour = None
            records.append(
                Record(
                    score=float(row["direction_confidence"] or 0.0),
                    # Carried through, or the expiry table would be built
                    # entirely from zeros the moment real trades take over
                    # from the replay — and the expiry gate would then tune
                    # itself against a column that says nothing.
                    duration_score=float(row["duration_confidence"] or 0.0),
                    won=row["outcome"] == "win",
                    regime=str(row["market_regime"] or ""),
                    hour=hour,
                    direction=str(row["direction"] or ""),
                )
            )
        return records

    def purge(self) -> None:
        """Drop every row — used by the tests and by an explicit user reset."""
        with self._lock:
            self._connection.execute("DELETE FROM signals")
            self._connection.execute("DELETE FROM alerts")
            self._connection.commit()


# --------------------------------------------------------------------------


def settles_a_different_chart(row: sqlite3.Row, asset: str | None) -> bool:
    """Whether this price simply belongs to a different instrument.

    Distinct from being unsettleable, and the distinction is the whole point:
    a row this price cannot decide may still be decided by the right chart's
    price a moment later, so it stays pending rather than being destroyed.

    It used to be a void, which was harmless while only the open chart was
    ever journalled — nothing belonging to another chart was in the table to
    destroy. The moment setups on watched charts became calls, settling the
    open chart voided every pending row belonging to the other eight: sixteen
    calls in one session, ten voided, and the six survivors were only the ones
    whose chart happened to be open as their expiry came due.
    """
    row_asset = row["asset"] if "asset" in row.keys() else None
    return bool(asset and row_asset and row_asset != asset)


def _expiry_of(row: sqlite3.Row) -> datetime | None:
    """When this row's trade actually expired."""
    try:
        started = datetime.fromisoformat(row["timestamp"])
    except (TypeError, ValueError):  # pragma: no cover - corrupt row
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started + timedelta(seconds=float(row["trade_duration"] or 0))


def _lateness(row: sqlite3.Row, now: datetime) -> float:
    """Seconds between the row expiring and this settlement being attempted."""
    expiry = _expiry_of(row)
    return 0.0 if expiry is None else max(0.0, (now - expiry).total_seconds())


def _settlement_block(
    row: sqlite3.Row,
    current_price: float,
    now: datetime,
    source: str | None,
    asset: str | None,
    late: float | None = None,
) -> str | None:
    """Why ``current_price`` may not settle ``row`` — or None if it may.

    The question this answers is not "did the trade win" but "is this price
    entitled to decide". Everything below is a way of the price belonging to a
    different chart, a different scale, or a different moment.
    """
    row_source = row["source"] if "source" in row.keys() else None
    if source is not None and row_source is not None and row_source != source:
        return (
            f"Voided: recorded on the '{row_source}' data source, and the only "
            f"price available to settle it came from '{source}'."
        )
    if source is not None and row_source is None:
        return (
            "Voided: recorded by an older version that did not record which "
            "data source the price came from, so it cannot be settled safely."
        )

    # A different instrument is deliberately not handled here — see
    # ``settles_a_different_chart``. It is a reason to leave the row alone,
    # not a reason to destroy it.

    # Late settlement. The price at expiry is gone; today's price is not it.
    try:
        started = datetime.fromisoformat(row["timestamp"])
    except (TypeError, ValueError):  # pragma: no cover - corrupt row
        started = None
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        duration = float(row["trade_duration"] or 0)
        grace = max(SETTLEMENT_GRACE_FACTOR * duration, SETTLEMENT_GRACE_FLOOR_SECONDS)
        if late is None:
            late = (now - started).total_seconds() - duration
        if late > grace:
            return (
                f"Voided: the expiry passed {late / 60:.0f} minutes before a "
                "price was available, so the settlement price is not the price "
                "at expiry."
            )

    entry_price = row["price"]
    if entry_price is not None and entry_price > 0 and current_price > 0:
        ratio = current_price / entry_price
        if ratio > SCALE_TOLERANCE or ratio < 1.0 / SCALE_TOLERANCE:
            return (
                f"Voided: entry price {entry_price:g} and settlement price "
                f"{current_price:g} are not on the same scale — the price axis "
                "was read differently, or this is a different instrument."
            )
    return None


def _signal_to_row(
    signal: Signal, screenshot_path: str | None, source: str | None = None
) -> dict[str, Any]:
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
        "source": source,
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
