"""How far a move has already travelled — measured without saying which way.

Every other reading in this package answers a version of the same question:
which direction has price been going. Trend, structure, Heikin Ashi, momentum
and EMA alignment carry seventy of the score's hundred points between them, and
they agree with each other by construction, because they are five ways of
measuring one thing. A score built from them is not a committee. It is one
opinion counted five times, and its confidence is highest exactly when the five
have the least to disagree about — which is the middle of an obvious move.

Nothing in the score could say *"yes it is trending, and that is precisely why
this is a bad entry"*. These readings can, because they are unsigned: a market
stretched far above its own centre and one stretched far below it score the
same. That makes them independent of the direction everything else measures,
which is the only reason adding them can change an answer rather than restate
one.

Whether extension actually predicts anything over a trade horizon is a
question for ``tools/horizon.py`` and the journal, not for this file. Nothing
here is wired into the score, and nothing should be until it has earned a
weight against real outcomes in more than one session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..indicators.core import atr as atr_of
from ..indicators.core import sma
from ..models import Series

#: Bars of history the readings are measured against. Short enough to describe
#: the move in progress rather than the session around it.
LOOKBACK = 20

#: A run this long or longer is "extended" for the purposes of the note. Not a
#: threshold anything acts on — a label for a human reading the panel.
LONG_RUN = 5


@dataclass
class ExtensionReading:
    """How far, how long, and whether it is still accelerating."""

    #: Distance from the recent mean in ATR units, unsigned. The headline.
    stretch: float
    #: The same with a sign: positive when price is above its own centre.
    signed_stretch: float
    #: Consecutive closes in one direction, ending at the last closed bar.
    run_length: int
    #: Where that run sits among the runs this series has produced, 0..100.
    run_percentile: float
    #: Net move over the lookback in ATR units, unsigned.
    travelled: float
    #: True when the latest bars cover less ground than the run's average —
    #: a move still going but no longer going as hard.
    decelerating: bool
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stretch": round(self.stretch, 3),
            "signed_stretch": round(self.signed_stretch, 3),
            "run_length": self.run_length,
            "run_percentile": round(self.run_percentile, 1),
            "travelled": round(self.travelled, 3),
            "decelerating": self.decelerating,
            "notes": list(self.notes),
        }


def _runs(closes: np.ndarray) -> list[int]:
    """Every completed run of same-direction closes in the series."""
    lengths: list[int] = []
    current = 0
    previous = 0
    for step in np.sign(np.diff(closes)):
        if step == 0:
            continue
        if step == previous:
            current += 1
        else:
            if current:
                lengths.append(current)
            current = 1
            previous = step
    if current:
        lengths.append(current)
    return lengths


def _current_run(closes: np.ndarray) -> int:
    """How many closes in a row have gone the same way, ending at the last."""
    steps = np.sign(np.diff(closes))
    steps = steps[steps != 0]
    if steps.size == 0:
        return 0
    last = steps[-1]
    count = 0
    for step in steps[::-1]:
        if step != last:
            break
        count += 1
    return int(count)


def analyze_extension(series: Series, lookback: int = LOOKBACK) -> ExtensionReading:
    """Measure how far the move has gone, saying nothing about which way.

    Returns zeros rather than raising on a series too short to read. A reading
    that cannot be taken is not a reading of zero extension, but the callers
    here treat "no information" and "nothing unusual" the same way, and an
    exception in the analysis path stops a panel that could still show a price.
    """
    empty = ExtensionReading(0.0, 0.0, 0, 0.0, 0.0, False, ["not enough history"])
    if series is None or len(series) < max(lookback, 3):
        return empty

    closes = np.asarray(series.close, dtype=np.float64)
    average_range = float(
        atr_of(series.high, series.low, series.close, period=min(14, len(series) - 1))[-1]
    )
    if not np.isfinite(average_range) or average_range <= 0:
        return empty

    centre = float(sma(closes, lookback)[-1])
    if not np.isfinite(centre):
        return empty

    signed = (float(closes[-1]) - centre) / average_range
    net = (float(closes[-1]) - float(closes[-1 - lookback])) / average_range

    run = _current_run(closes)
    history = _runs(closes)
    if history:
        at_or_below = sum(1 for length in history if length <= run)
        percentile = at_or_below / len(history) * 100.0
    else:
        percentile = 0.0

    ranges = np.asarray(series.high, dtype=np.float64) - np.asarray(
        series.low, dtype=np.float64
    )
    recent = float(np.mean(ranges[-3:])) if ranges.size >= 3 else float("nan")
    earlier = float(np.mean(ranges[-lookback:-3])) if ranges.size > lookback else float("nan")
    decelerating = bool(
        np.isfinite(recent) and np.isfinite(earlier) and earlier > 0 and recent < earlier
    )

    notes: list[str] = []
    if abs(signed) >= 2.0:
        notes.append(
            f"price is {abs(signed):.1f} ATR from its {lookback}-bar centre"
        )
    if run >= LONG_RUN:
        notes.append(f"{run} closes in a row the same way")
    if decelerating and run >= 3:
        notes.append("still moving, covering less ground doing it")

    return ExtensionReading(
        stretch=abs(signed),
        signed_stretch=signed,
        run_length=run,
        run_percentile=percentile,
        travelled=abs(net),
        decelerating=decelerating,
        notes=notes,
    )
