"""Shadow strategies racing the live one over the same charts.

The bottleneck this project has, measured six sessions running, is data —
one configuration tested per sitting, twenty-plus settled calls needed
before a rate says anything, and most ideas never getting a turn. So every
watchlist sweep now reads each chart several more times, once per shadow
experiment: the same candles, a different rulebook. A shadow call is
journalled like any other call and settled by the same machinery against
the same prices; what it never does is reach the panel, raise an alert,
teach the calibration record, trip the cooldown, or appear among the
session's own calls. Experiments write to one column — ``experiment`` —
and every reader of live results filters on it being empty.

The shipped roster carries the four candidates the record itself
suggested:

* **fade-overheat** — the one genuinely contrarian idea the data keeps
  pointing at: calls shown at ninety and above lost to the band beneath
  them in every live session measured, so this experiment takes exactly
  those reads and records the *opposite* direction.
* **three-minute** — every brutal number so far is thirty-second expiries;
  this asks the same charts the three-minute question in parallel.
* **strict-85** — only the historically best-behaved band.
* **loose-70** — the under-observed middle of the scale, so the bands
  below eighty finally accumulate a record instead of a guess.

A cooldown is deliberately not part of any shadow: the rulebook is the
thing under test, and the live strategy's stand-downs are not part of it.
Rates for the race live in the report's ledger, under the same rule as
every other rate: below twenty settled calls, a count and no percentage.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any

from ..logging_setup import get_logger
from ..models import Direction
from .gates import GateSettings

log = get_logger(__name__)


@dataclass(frozen=True)
class Experiment:
    """One shadow rulebook."""

    label: str
    #: GateSettings fields to override on top of the live configuration.
    overrides: tuple[tuple[str, Any], ...] = ()
    #: Expiry override, seconds. None keeps the live trade duration.
    trade_duration: int | None = None
    #: Record the opposite of the direction the signal argued. The read is
    #: unchanged — the same score lands in the row — only the call flips.
    invert: bool = False

    def settings(self, base: GateSettings) -> GateSettings:
        return replace(base, **dict(self.overrides))


DEFAULT_ROSTER: tuple[Experiment, ...] = (
    Experiment(
        "fade-overheat",
        overrides=(
            ("overheat_ceiling", 0.0),
            ("min_shown_confidence", 90.0),
        ),
        invert=True,
    ),
    # The two horizons, both always in the race regardless of which one the
    # live strategy is driving. The fit gate would refuse a foreign expiry
    # on many charts — correctly, for the live strategy, and fatally for an
    # experiment whose whole question is how that horizon settles — so each
    # waives the fit for itself; nothing else. When one of these matches
    # the live expiry it doubles as a control group: its rate should track
    # the live rate, and a divergence is a bug report, not a finding.
    Experiment(
        "three-minute",
        overrides=(("min_duration_compatibility", 0.0),),
        trade_duration=180,
    ),
    Experiment(
        "thirty-second",
        overrides=(("min_duration_compatibility", 0.0),),
        trade_duration=30,
    ),
    Experiment("strict-85", overrides=(("min_shown_confidence", 85.0),)),
    Experiment(
        "loose-70",
        overrides=(
            ("min_shown_confidence", 70.0),
            ("overheat_ceiling", 85.0),
        ),
    ),
)

_VALID_FIELDS = {f.name for f in fields(GateSettings)}


def roster_from_config(section: dict[str, Any]) -> tuple[Experiment, ...]:
    """The experiments to race, from config.

    Absent means the shipped roster — racing costs the user nothing and the
    whole point is data, so it defaults on. An explicit empty list turns it
    off. A malformed entry is skipped with a note rather than taking the
    sweep down: a broken experiment is not worth a broken session.
    """
    raw = section.get("experiments")
    if raw is None:
        return DEFAULT_ROSTER
    if not isinstance(raw, list) or not raw:
        return ()
    roster: list[Experiment] = []
    for entry in raw:
        experiment = _parse_experiment(entry)
        if experiment is not None:
            roster.append(experiment)
    return tuple(roster)


def _parse_experiment(entry: Any) -> Experiment | None:
    """One config entry, or None with the reason logged.

    Everything a YAML typo can produce is handled here — a string where a
    number belongs, an unknown setting, a value the gates cannot compare —
    because the alternative was measured the hard way in review: a
    ``trade_duration: 60s`` typo raising out of the constructor and the
    overlay never opening at all.
    """
    if not isinstance(entry, dict) or not entry.get("label"):
        log.warning("skipping a shadow experiment with no label: %r", entry)
        return None
    label = str(entry["label"])

    overrides = entry.get("overrides") or {}
    if not isinstance(overrides, dict):
        log.warning("skipping shadow experiment %r: overrides is not a map", label)
        return None
    unknown = set(overrides) - _VALID_FIELDS
    if unknown:
        log.warning(
            "skipping shadow experiment %r: unknown setting(s) %s",
            label, ", ".join(sorted(unknown)),
        )
        return None
    cleaned: dict[str, Any] = {}
    for key, value in overrides.items():
        # Every raceable gate setting is a number; a string that happens to
        # hold one is a YAML quoting accident, and anything else would raise
        # deep inside evaluate where the defensive except would silently
        # bury the whole experiment for the session.
        try:
            cleaned[key] = float(value)
        except (TypeError, ValueError):
            log.warning(
                "skipping shadow experiment %r: %s=%r is not a number",
                label, key, value,
            )
            return None

    duration = entry.get("trade_duration")
    if duration not in (None, 0, ""):
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            log.warning(
                "skipping shadow experiment %r: trade_duration %r is not a "
                "number of seconds",
                label, duration,
            )
            return None
        if duration <= 0:
            log.warning(
                "skipping shadow experiment %r: trade_duration must be "
                "positive, got %d",
                label, duration,
            )
            return None
    else:
        duration = None

    return Experiment(
        label=label,
        overrides=tuple(sorted(cleaned.items())),
        trade_duration=duration,
        invert=bool(entry.get("invert", False)),
    )


_OPPOSITE = {Direction.CALL: Direction.PUT, Direction.PUT: Direction.CALL}


class ShadowBook:
    """Tracks each experiment's open calls, so one setup is one row.

    The same transition rule the live watchlist earned the hard way: a call
    is the moment a chart *becomes* actionable under a rulebook, not every
    sweep it stays that way. Without this, one standing setup became
    fifty-seven rows once before, and it would again, four rulebooks over.
    """

    def __init__(self, roster: tuple[Experiment, ...]) -> None:
        self.roster = roster
        self._open: set[tuple[str, str, int]] = set()

    def sweep_chart(
        self,
        engine: Any,
        asset: str,
        timeframe: int,
        series: Any,
        calibration: Any | None,
    ) -> None:
        """Read one chart under every shadow rulebook and journal transitions.

        Never allowed to break the live sweep: a failing experiment logs and
        moves on. The live read already paid for the candles; each shadow is
        one more evaluation over data in hand.
        """
        if not self.roster:
            return
        try:
            base = engine.gate_settings()
        except Exception:  # pragma: no cover - defensive
            return
        source = getattr(engine.source, "name", None)
        for experiment in self.roster:
            # An expiry shorter than the chart's own bars cannot be settled
            # honestly: the bar covering the expiry closes after the trade
            # ended, so its close is not the price the trade settled at. On
            # a one-minute chart the thirty-second experiment simply sits
            # out rather than filing rows a minute-bar cannot decide.
            if (
                experiment.trade_duration
                and experiment.trade_duration < int(timeframe)
            ):
                continue
            key = (experiment.label, asset, int(timeframe))
            try:
                signal = engine.evaluate_series(
                    series,
                    asset,
                    timeframe,
                    calibration,
                    trade_duration=experiment.trade_duration,
                    settings=experiment.settings(base),
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("shadow %s failed on %s: %s", experiment.label, asset, exc)
                continue
            actionable = bool(signal is not None and signal.actionable)
            was_open = key in self._open
            if actionable and not was_open:
                self._open.add(key)
                recorded = signal
                if experiment.invert and signal.direction in _OPPOSITE:
                    recorded = replace(signal, direction=_OPPOSITE[signal.direction])
                try:
                    engine.journal.record(
                        recorded, source=source, experiment=experiment.label
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    log.debug(
                        "could not journal shadow call %s on %s: %s",
                        experiment.label, asset, exc,
                    )
            elif not actionable and was_open:
                self._open.discard(key)
