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
        for column, ddl in (
            ("source", "source TEXT"),
            # Which shadow experiment made this call, or NULL for the live
            # strategy. Every reader of live results filters on NULL —
            # experiments share the journal so settlement works unchanged,
            # and must never leak into reports, calibration or the cooldown.
            ("experiment", "experiment TEXT"),
            # The platform's own id for a settled deal, on manual rows. The
            # in-memory dedup dies with the process; this is what lets a
            # restart refuse a deal the platform re-mentions afterwards.
            ("deal_id", "deal_id TEXT"),
        ):
            if column not in existing:
                log.info("adding journal column %s", column)
                self._connection.execute(f"ALTER TABLE signals ADD COLUMN {ddl}")
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_source ON signals(source)"
        )
        self._migrate_data()

    #: Journal data-migration version, tracked in SQLite's ``user_version``.
    #: Bumped when stored rows themselves have to change, as opposed to the
    #: schema gaining a column.
    DATA_VERSION = 2

    def _migrate_data(self) -> None:
        """Run each data correction this file has not yet had, oldest first.

        Called with the lock held, from ``_migrate``. Each step is a one-off
        repair of rows a specific defect wrote; ``user_version`` records how
        far this file has been brought, so a repair never reruns and never
        touches rows written after its defect was fixed.
        """
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version >= self.DATA_VERSION:
            return
        if version < 1:
            self._sweep_ghost_trades()
        if version < 2:
            self._sweep_duplicate_rows()
        self._connection.execute(f"PRAGMA user_version = {self.DATA_VERSION}")

    #: When the first build that stamps hand trades on this machine's clock
    #: was published. Every manual row written before it came from a build
    #: with the deal-clock defect, and its timestamp is the broker's zone
    #: rather than this machine's.
    CLOCK_FIX_SHIPPED = "2026-08-21T16:13:00+00:00"

    def _sweep_ghost_trades(self) -> None:
        """Remove hand trades the clock defect stamped into the wrong hour.

        Builds before the deal-clock fix filed every hand trade at the
        broker's own timestamp — an hour ahead in one session, two in the
        next — and could match none of them to a call, so each carries a zero
        score. The wrong stamps are not cosmetic: a row filed at 17:45 by an
        afternoon session lands inside any *evening* session's window, so the
        same seventeen trades reappeared, dash for dash, in the next report's
        "TRADES YOU PLACED" as though they had been placed again.

        Deleted rather than re-stamped, because the offset varied by session
        and is not recoverable per row — and by the report's own words these
        rows teach the score bands nothing: a zero score is excluded from
        calibration by design, so their only remaining effect was haunting
        windows they were never in. Rows written after the fix shipped are
        untouched, including genuinely unattributable ones — their stamps are
        real, so they stay in the sessions they belong to.

        Runs once per journal file, tracked in ``user_version``.
        """
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(signals)")
        }
        # A journal old enough to predate these columns predates hand-trade
        # recording itself, so it holds no ghosts — nothing to sweep, and a
        # DELETE naming columns it does not have would stop it opening at all.
        if not {"notes", "direction_confidence", "timestamp"} <= columns:
            return
        removed = self._connection.execute(
            "DELETE FROM signals WHERE notes = 'manual' "
            "AND direction_confidence = 0 AND timestamp < ?",
            (self.CLOCK_FIX_SHIPPED,),
        ).rowcount
        if removed:
            log.info(
                "removed %d hand trade(s) the clock defect had stamped into "
                "the wrong hour; they carried no score and were reappearing "
                "in later sessions' reports",
                removed,
            )

    #: How far apart two rows may sit and still be the same event written by
    #: two processes. Both copies ran on one machine — one clock — so their
    #: stamps differ only by polling phase, a second or two; hand trades carry
    #: the broker's own stamp and land identical. A real re-arm of the same
    #: setup is a new read bars later, and prints a new entry price besides.
    DUPLICATE_WINDOW_SECONDS = 10.0

    def _sweep_duplicate_rows(self) -> None:
        """Remove the doubles written by two app copies sharing this journal.

        Before the single-instance lock existed, nothing stopped a second
        GateKeeper from opening the same journal, and on 2026-08-21 one did:
        every call and every hand trade of the evening session was recorded
        twice — 48 listed calls that were 24 reads, 20 hand trades that were
        10, every rate in the report computed at double weight, and the
        calibration record counting each real trade twice.

        Two rows are the same event when they agree on everything a call *is*
        — pair, direction, score, entry price, expiry, chart timeframe, data
        source, and provenance — and were stamped within a few seconds of
        each other. The outcome is deliberately not part of that identity:
        the two copies settled independently, and on a borderline expiry they
        could disagree (one FLAT, one LOSS); such a pair is still one event,
        and the earlier row is the one kept. The id is a per-process UUID and
        the payload embeds per-evaluation detail, so neither can serve as the
        identity — the columns above are it.

        Runs once per journal file; the instance lock keeps it from being
        needed again.
        """
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(signals)")
        }
        needed = {
            "timestamp", "asset", "direction", "direction_confidence",
            "price", "trade_duration", "chart_timeframe", "source", "notes",
        }
        if not needed <= columns:
            return
        removed = self._connection.execute(
            "DELETE FROM signals WHERE rowid IN ("
            " SELECT later.rowid FROM signals AS later"
            " JOIN signals AS earlier ON earlier.rowid < later.rowid"
            "  AND earlier.asset = later.asset"
            "  AND earlier.direction = later.direction"
            "  AND earlier.direction_confidence = later.direction_confidence"
            "  AND COALESCE(earlier.price, -1) = COALESCE(later.price, -1)"
            "  AND earlier.trade_duration = later.trade_duration"
            "  AND earlier.chart_timeframe = later.chart_timeframe"
            "  AND COALESCE(earlier.source, '') = COALESCE(later.source, '')"
            "  AND COALESCE(earlier.notes, '') = COALESCE(later.notes, '')"
            "  AND ABS(julianday(later.timestamp) - julianday(earlier.timestamp))"
            "      * 86400.0 <= ?"
            ")",
            (self.DUPLICATE_WINDOW_SECONDS,),
        ).rowcount
        if removed:
            log.info(
                "removed %d duplicate row(s) written while two copies of the "
                "app shared this journal; each remaining row now counts its "
                "event once",
                removed,
            )

    def all_settled_calls(self, source: str | None = None) -> list[dict[str, Any]]:
        """Every settled call the tool ever made, for the pooled ledger.

        Tool calls only — manual rows are the user's trades — and scoped to
        one source when asked, because a demo run pooled into a live record
        is a record of nothing. Unbounded on purpose: the ledger is the one
        reader whose whole point is everything.
        """
        query = (
            "SELECT timestamp, asset, trade_duration, overall_confidence, "
            "direction, outcome, experiment FROM signals "
            "WHERE outcome IN ('win', 'loss') "
            "AND (notes IS NULL OR notes != 'manual')"
        )
        params: list[Any] = []
        if source:
            query += " AND source = ?"
            params.append(source)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def recent_deal_ids(self, within_hours: float = 24.0) -> set[str]:
        """Platform deal ids already filed, for seeding the restart dedup.

        The in-memory seen-set dies with the process, and the platform's
        re-send window does not: a reconnect shortly after a relaunch can
        replay settled deals the previous run already recorded. Bounded to a
        day because that is far past any observed re-send horizon, and an
        unbounded set would grow with the journal forever.
        """
        cutoff = (utcnow() - timedelta(hours=within_hours)).isoformat()
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT deal_id FROM signals WHERE deal_id IS NOT "
                "NULL AND timestamp > ?",
                (cutoff,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def last_loss_at(
        self,
        asset: str,
        source: str | None = None,
        within_minutes: float = 15.0,
    ) -> datetime | None:
        """When this pair's most recent losing call settled, if recently.

        Feeds the loss cooldown, so it is deliberately narrow: the tool's own
        calls only — a manual row is the user's trade, not this tool's read —
        and only losses this data source produced, within a short horizon so
        the query stays cheap and an old loss cannot haunt a new session.
        Pair-level on purpose, matching how the rule was measured: the same
        pair on another timeframe is the same market that just cost a trade.
        """
        cutoff = (utcnow() - timedelta(minutes=within_minutes)).isoformat()
        query = (
            "SELECT MAX(outcome_at) FROM signals WHERE asset = ? "
            "AND outcome = 'loss' AND outcome_at > ? "
            "AND (notes IS NULL OR notes != 'manual') "
            # A shadow experiment's loss is its own business: only the live
            # strategy's losses may stand the live strategy down.
            "AND experiment IS NULL"
        )
        params: list[Any] = [asset, cutoff]
        if source is not None:
            query += " AND source = ?"
            params.append(source)
        with self._lock:
            row = self._connection.execute(query, params).fetchone()
        stamp = row[0] if row else None
        if not stamp:
            return None
        try:
            return datetime.fromisoformat(stamp)
        except ValueError:
            return None

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------

    def record(
        self,
        signal: Signal,
        screenshot_path: str | None = None,
        source: str | None = None,
        experiment: str | None = None,
    ) -> str:
        """Persist a signal. Returns the signal id.

        ``source`` names the data source the price came from. It is what keeps
        a demo run's outcomes out of a live run's win rate. ``experiment``
        names the shadow strategy that made this call; the live strategy
        leaves it None, and every reader of live results filters on that.
        """
        row = _signal_to_row(signal, screenshot_path, source)
        row["experiment"] = experiment
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
        deal_id: str | None = None,
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
            "deal_id": str(deal_id) if deal_id is not None else None,
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
        price_read_at: datetime | None = None,
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

        ``price_read_at`` is the moment ``current_price`` was read, when that
        moment is not "now" — a chart the platform dropped hands over its
        last known price, read before it vanished. A price read *before* a
        row's expiry cannot decide that row: it measures the open-to-drop
        move, not the move that was bet on. Such rows stay pending while a
        true price could still arrive, and void once the grace passes.
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
                    # A fallback price older than the expiry measures the
                    # wrong interval — the move to the moment the chart was
                    # last seen, not the move that was bet on. Wait for a
                    # price that can answer; void once none can be expected.
                    if (
                        price_read_at is not None
                        and expiry is not None
                        and price_read_at < expiry
                    ):
                        grace = max(
                            SETTLEMENT_GRACE_FACTOR
                            * float(row["trade_duration"] or 0),
                            SETTLEMENT_GRACE_FLOOR_SECONDS,
                        )
                        if (now - expiry).total_seconds() <= grace:
                            continue  # a true price may still arrive
                        reason = (
                            "The chart was gone before this expired, and no "
                            "price from the expiry ever arrived."
                        )
                        settled_price = current_price
                    else:
                        settled_price = current_price
                        reason = _settlement_block(
                            row, current_price, now, source, asset
                        )
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

    def recent(
        self,
        limit: int = 50,
        asset: str | None = None,
        include_experiments: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM signals WHERE 1=1"
        params: list[Any] = []
        if asset:
            query += " AND asset = ?"
            params.append(asset)
        # Excluded by default, included only on request: every default
        # reader of this — the session report, the dashboard's journal
        # view — means "the tool's calls", and a busy shadow roster would
        # otherwise both fill the window and present deliberately inverted
        # experiment rows as calls the panel made.
        if not include_experiments:
            query += " AND experiment IS NULL"
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
            # The live strategy only. Shadow experiments share the journal so
            # settlement works unchanged; they must never share a tally.
            "WHERE direction IN ('CALL', 'PUT') AND experiment IS NULL"
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
            "AND direction_confidence > 0 "
            # And only trades somebody actually placed. Without this the query
            # swept up the tool's own settled calls — every journalled setup
            # with an outcome — and handed them back as "your settled trades".
            # Twenty-eight of its own notional calls on one chart were enough
            # to build a record marked from_real_trades, which is the one rank
            # the gates allow to veto live setups — a privilege the gate's own
            # comment reserves for trades that were placed precisely so the
            # tool cannot silence itself on the strength of its own opinion.
            "AND notes = 'manual'"
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
