"""Save a venue's candles as recordings a backtest can replay.

This is the bridge between having a broker connection and being able to
measure anything: the backtester replays CSV, the venue serves candles,
and nothing joined the two.

Three rules, and the first one is why this file is longer than it looks.

**Only closed bars are written.** A forming candle's close is not a close;
it is wherever price happened to be when the request landed. Recording it
would put a value into a file that the next request would contradict, and
every backtest run afterwards would be reading a price that never existed
at that timestamp.

**An existing recording is extended, never silently rewritten.** Bars are
merged by timestamp, with the ones already on disk kept. A venue that
revises history is a real thing, and a recorder that overwrote on every
run would mean two backtests of the same period could disagree with no
record of why. Changed bars are reported as conflicts and left alone
unless the caller asks for them.

**The file says what made it, and carries the contract specification.**
Symbol, timeframe, venue and the moment of recording go in a sidecar, so a
recording whose provenance is unknown can be told apart from one that was
downloaded deliberately. The instrument's tick size, lot step and the rest
go with it, which makes a recording self-contained: a backtest can size
positions without a live connection, and it sizes them against the
specification that was true when the data was recorded rather than
whatever the venue reports today.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from gtcc.adapters.base import MarketDataAdapter
from gtcc.domain.enums import Timeframe
from gtcc.domain.instruments import InstrumentSpec
from gtcc.domain.market_data import Bar, utcnow

COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


@dataclass(frozen=True, slots=True)
class RecordingReport:
    path: Path
    symbol: str
    timeframe: Timeframe
    #: Bars received from the venue, before any filtering.
    received: int
    #: Forming candles dropped. A forming bar's close is not a close.
    unclosed_dropped: int
    #: Bars already on disk with the same timestamp and identical values.
    already_present: int
    #: Bars already on disk whose values DIFFER from the venue's. Left as
    #: they were. A venue revising history is worth knowing about.
    conflicts: tuple[datetime, ...]
    added: int
    total_on_disk: int
    first_at: datetime | None
    last_at: datetime | None

    def describe(self) -> list[str]:
        lines = [
            f"{self.symbol} {self.timeframe} -> {self.path}",
            f"  received {self.received} bar(s) from the venue",
        ]
        if self.unclosed_dropped:
            lines.append(
                f"  dropped {self.unclosed_dropped} forming candle(s): a bar that "
                "has not closed has no close"
            )
        if self.already_present:
            lines.append(f"  {self.already_present} already on disk, unchanged")
        if self.conflicts:
            lines.append(
                f"  {len(self.conflicts)} bar(s) ON DISK DIFFER from what the venue "
                "now reports, and were left alone:"
            )
            for moment in self.conflicts[:5]:
                lines.append(f"      {moment.isoformat()}")
            if len(self.conflicts) > 5:
                lines.append(f"      ... and {len(self.conflicts) - 5} more")
            lines.append(
                "    Pass overwrite=True to take the venue's version. Until then "
                "the recording is unchanged, so earlier backtests remain "
                "reproducible."
            )
        lines.append(f"  added {self.added}, now {self.total_on_disk} on disk")
        if self.first_at and self.last_at:
            lines.append(
                f"  covering {self.first_at.isoformat()} to {self.last_at.isoformat()}"
            )
        return lines


def _read_existing(path: Path, symbol: str, timeframe: Timeframe) -> dict[datetime, Bar]:
    if not path.exists():
        return {}
    out: dict[datetime, Bar] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            from gtcc.adapters.replay import _parse_timestamp
            from gtcc.domain.money import D

            moment = _parse_timestamp(row["timestamp"])
            out[moment] = Bar(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=moment,
                open=D(row["open"]),
                high=D(row["high"]),
                low=D(row["low"]),
                close=D(row["close"]),
                volume=D(row.get("volume") or 0),
            )
    return out


#: Spec fields written to the sidecar. Listed rather than taken from the
#: dataclass so that adding a field to InstrumentSpec cannot silently start
#: or stop being recorded.
SPEC_FIELDS = (
    "symbol", "market", "asset_class", "quote_currency", "base_currency",
    "tick_size", "lot_step", "min_qty", "max_qty", "min_notional",
    "contract_size", "tick_value", "pip_size", "max_leverage",
    "allows_fractional", "maker_fee_bps", "taker_fee_bps", "session",
)


def spec_to_dict(spec: "InstrumentSpec") -> dict[str, object]:
    out: dict[str, object] = {}
    for name in SPEC_FIELDS:
        value = getattr(spec, name)
        out[name] = None if value is None else str(value)
    out["allows_fractional"] = bool(spec.allows_fractional)
    return out


def spec_from_dict(raw: dict) -> "InstrumentSpec":
    """Rebuild a spec from a sidecar.

    Missing keys raise rather than defaulting. A default tick size here
    would mis-size every position in the backtest, quietly.
    """
    from gtcc.domain.enums import AssetClass, Market
    from gtcc.domain.instruments import InstrumentSpec
    from gtcc.domain.money import D

    def need(name: str) -> str:
        if name not in raw or raw[name] is None:
            raise ValueError(
                f"the recording's sidecar has no {name!r}; it was written before "
                "specs were recorded, or by something else. Re-record it rather "
                "than backtesting against a guessed contract specification"
            )
        return str(raw[name])

    def optional(name: str):
        value = raw.get(name)
        return None if value in (None, "") else D(str(value))

    return InstrumentSpec(
        symbol=need("symbol"),
        market=Market(need("market")),
        asset_class=AssetClass(need("asset_class")),
        quote_currency=need("quote_currency"),
        base_currency=str(raw.get("base_currency") or ""),
        tick_size=D(need("tick_size")),
        lot_step=D(need("lot_step")),
        min_qty=D(need("min_qty")),
        max_qty=optional("max_qty"),
        min_notional=D(str(raw.get("min_notional") or 0)),
        contract_size=D(str(raw.get("contract_size") or 1)),
        tick_value=optional("tick_value"),
        pip_size=optional("pip_size"),
        max_leverage=D(str(raw.get("max_leverage") or 1)),
        allows_fractional=bool(raw.get("allows_fractional")),
        maker_fee_bps=D(str(raw.get("maker_fee_bps") or 0)),
        taker_fee_bps=D(str(raw.get("taker_fee_bps") or 0)),
        session=str(raw.get("session") or "24x7"),
    )


def read_sidecar(directory: Path, symbol: str, timeframe: Timeframe) -> dict:
    path = Path(directory) / f"{symbol}_{timeframe.value}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no sidecar at {path}. A recording without one has unknown "
            "provenance and no contract specification"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _same(left: Bar, right: Bar) -> bool:
    return (
        left.open == right.open
        and left.high == right.high
        and left.low == right.low
        and left.close == right.close
        and left.volume == right.volume
    )


def write_recording(
    directory: Path,
    symbol: str,
    timeframe: Timeframe,
    bars: Sequence[Bar],
    *,
    venue: str = "unknown",
    spec: "InstrumentSpec | None" = None,
    overwrite: bool = False,
    now: datetime | None = None,
) -> RecordingReport:
    """Merge *bars* into the recording for this symbol and timeframe."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{symbol}_{timeframe.value}.csv"

    received = len(bars)
    closed = [bar for bar in bars if bar.closed]
    dropped = received - len(closed)

    existing = _read_existing(path, symbol, timeframe)
    unchanged = 0
    conflicts: list[datetime] = []
    added = 0

    merged = dict(existing)
    for bar in closed:
        current = merged.get(bar.timestamp)
        if current is None:
            merged[bar.timestamp] = bar
            added += 1
        elif _same(current, bar):
            unchanged += 1
        else:
            conflicts.append(bar.timestamp)
            if overwrite:
                merged[bar.timestamp] = bar

    ordered = [merged[key] for key in sorted(merged)]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for bar in ordered:
            writer.writerow(
                [
                    bar.timestamp.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                ]
            )

    sidecar = path.with_suffix(".json")
    provenance = {
        "symbol": symbol,
        "timeframe": timeframe.value,
        "venue": venue,
        "recorded_at": (now or utcnow()).isoformat(),
        "bars": len(ordered),
        "first_at": ordered[0].timestamp.isoformat() if ordered else None,
        "last_at": ordered[-1].timestamp.isoformat() if ordered else None,
        "note": (
            "Closed bars only. A recording with no sidecar came from somewhere "
            "this recorder did not write, and its provenance is unknown."
        ),
    }
    if spec is not None:
        provenance["instrument"] = spec_to_dict(spec)
    elif sidecar.exists():
        # Keep a spec recorded earlier rather than dropping it: a later run
        # without a live connection should not strip what an earlier one knew.
        try:
            previous = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if "instrument" in previous:
            provenance["instrument"] = previous["instrument"]
    sidecar.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")

    return RecordingReport(
        path=path,
        symbol=symbol,
        timeframe=timeframe,
        received=received,
        unclosed_dropped=dropped,
        already_present=unchanged,
        conflicts=tuple(sorted(conflicts)),
        added=added,
        total_on_disk=len(ordered),
        first_at=ordered[0].timestamp if ordered else None,
        last_at=ordered[-1].timestamp if ordered else None,
    )


def record(
    adapter: MarketDataAdapter,
    directory: Path,
    symbol: str,
    timeframe: Timeframe,
    *,
    limit: int = 5000,
    overwrite: bool = False,
    now: datetime | None = None,
) -> RecordingReport:
    """Fetch candles from *adapter* and merge them into a recording.

    Read-only against the venue. Any adapter error propagates: a recorder
    that swallowed a failure would leave a short recording that looks like
    a quiet market.
    """
    bars = adapter.get_bars(symbol, timeframe, limit=limit)
    # The specification is recorded with the data so a backtest needs no live
    # connection, and sizes against what was true when the bars were taken.
    try:
        spec = adapter.get_instrument(symbol)
    except Exception:  # noqa: BLE001 - an adapter that cannot say is not fatal
        spec = None
    return write_recording(
        directory,
        symbol,
        timeframe,
        bars,
        venue=adapter.name,
        spec=spec,
        overwrite=overwrite,
        now=now,
    )


@dataclass(frozen=True, slots=True)
class Available:
    """One recording on disk, as the UI lists it."""

    symbol: str
    timeframe: str
    bars: int | None
    first_at: str | None
    last_at: str | None
    venue: str | None
    recorded_at: str | None
    has_spec: bool

    @property
    def backtestable(self) -> bool:
        """A recording with no contract specification cannot be sized.

        Listed anyway, with this False, so the owner sees it exists and why
        it cannot be used — rather than wondering where their file went.
        """
        return self.has_spec


def list_recordings(directory: Path) -> list[Available]:
    """Every recording in *directory*, whether or not it is usable.

    A file with no sidecar is listed with its counts unknown rather than
    omitted: it exists, the owner put it there, and silence about it would
    be the least helpful possible response.
    """
    directory = Path(directory)
    if not directory.exists():
        return []
    out: list[Available] = []
    for path in sorted(directory.glob("*.csv")):
        stem = path.stem
        symbol, _, timeframe = stem.rpartition("_")
        if not symbol:
            symbol, timeframe = stem, "?"
        sidecar = path.with_suffix(".json")
        meta: dict = {}
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        out.append(
            Available(
                symbol=symbol,
                timeframe=timeframe,
                bars=meta.get("bars"),
                first_at=meta.get("first_at"),
                last_at=meta.get("last_at"),
                venue=meta.get("venue"),
                recorded_at=meta.get("recorded_at"),
                has_spec="instrument" in meta,
            )
        )
    return out
