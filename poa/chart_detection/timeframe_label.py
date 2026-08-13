"""Reading the chart's timeframe off the screen.

The timeframe is the second thing GateKeeper cannot infer from candles: a
1-minute chart and a 5-minute chart draw identical-looking bars. Getting it
wrong is not cosmetic — every duration recommendation is expressed in candles
of that length, so a chart read as M1 when it is really M5 recommends
expirations five times too short.

Platforms label it in a handful of styles (``M1``, ``5m``, ``H1``, ``S30``,
``1 min``), all of which reduce to a number of seconds.
"""

from __future__ import annotations

import re

from ..config import CHART_TIMEFRAMES
from .label_reader import LabelReader

# M1 / H4 / S30 / D1  — a unit letter followed by a count.
_PREFIXED = re.compile(r"^([SMHD])\s*(\d{1,3})$", re.IGNORECASE)
# 5m / 15min / 1h / 30s / "5 minutes"
_SUFFIXED = re.compile(
    r"^(\d{1,3})\s*(S|SEC|SECS|SECOND|SECONDS|M|MIN|MINS|MINUTE|MINUTES|H|HR|HRS|HOUR|HOURS|D|DAY|DAYS)$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "S": 1, "SEC": 1, "SECS": 1, "SECOND": 1, "SECONDS": 1,
    "M": 60, "MIN": 60, "MINS": 60, "MINUTE": 60, "MINUTES": 60,
    "H": 3600, "HR": 3600, "HRS": 3600, "HOUR": 3600, "HOURS": 3600,
    "D": 86400, "DAY": 86400, "DAYS": 86400,
}

# A badge run together with the countdown beside it: M1 00:18 arrives as
# M10018 once OCR drops the space and the colon. The clock is four digits
# (MMSS) or six (HHMMSS); anything else is not a clock.
_WITH_CLOCK = re.compile(r"^([SMHD])\s*([\d:.\s]{5,12})$", re.IGNORECASE)

# OCR confuses these reading small uppercase text. Only applied to the unit
# letter, never to the digits, where a substitution would change the value.
_UNIT_CONFUSIONS = {"0": "D", "5": "S", "1": "M", "8": "B"}

# A timeframe outside this range is a misread, not a chart setting.
_MIN_SECONDS = 1
_MAX_SECONDS = 86400


def parse_timeframe(text: str) -> int | None:
    """Turn a timeframe label into seconds, or None if it is not one."""
    cleaned = " ".join(text.split()).strip().upper()
    if not cleaned:
        return None
    # Strip decoration the platform draws around the badge.
    cleaned = cleaned.replace("▾", "").replace("▼", "").strip(" .,:|·")
    if not cleaned:
        return None

    match = _PREFIXED.match(cleaned)
    if match:
        unit, count = match.group(1).upper(), int(match.group(2))
        return _validate(count * _UNIT_SECONDS.get(unit, 0))

    match = _SUFFIXED.match(cleaned)
    if match:
        count, unit = int(match.group(1)), match.group(2).upper()
        return _validate(count * _UNIT_SECONDS.get(unit, 0))

    # "M1 00:18" — the interval followed by the countdown to the next candle.
    # OCR delivers this as M1 00:18, M100:18 or M10018 depending on whether it
    # kept the space and the colon, so the digits are recovered and the split
    # tried explicitly. A regex cannot do this on its own: it commits to one
    # split, and the greedy one reads M10018 as M100 with an 18-second clock.
    match = _WITH_CLOCK.match(cleaned)
    if match:
        unit = match.group(1).upper()
        digits = re.sub(r"\D", "", match.group(2))
        for take in (1, 2, 3):
            # The clock is mm:ss or hh:mm:ss — four digits or six, never three.
            if len(digits) - take not in (4, 6):
                continue
            seconds = _validate(int(digits[:take]) * _UNIT_SECONDS.get(unit, 0))
            if seconds is not None and snap_to_known(seconds) is not None:
                return seconds

    # A leading digit misread as a letter, e.g. "M1" seen as "MI".
    collapsed = cleaned.replace(" ", "")
    if len(collapsed) == 2 and collapsed[0].isalpha():
        digit = _UNIT_CONFUSIONS.get(collapsed[1])
        if collapsed[1].isalpha() and digit is None:
            return None
    return None


def _validate(seconds: int) -> int | None:
    if seconds < _MIN_SECONDS or seconds > _MAX_SECONDS:
        return None
    return seconds


def snap_to_known(seconds: int, tolerance: float = 0.2) -> int | None:
    """Snap a detected timeframe onto one the platform actually offers.

    Returns None when it snaps to nothing. That is the important case: an OCR
    slip can produce a number that is perfectly plausible in isolation — "M999"
    reads as just under 17 hours — and adopting it would silently reshape every
    duration recommendation. The set of timeframes a platform offers is short
    and known, so anything outside it is a misread, not a setting.
    """
    best = min(CHART_TIMEFRAMES, key=lambda candidate: abs(candidate - seconds))
    if abs(best - seconds) <= best * tolerance:
        return best
    return None


class TimeframeLabelReader(LabelReader[int]):
    """Reads the chart timeframe badge, e.g. ``M1``."""

    whitelist = "SMHDsmhd0123456789: "

    def __init__(self, region: dict[str, int] | None, confirmations: int = 2) -> None:
        super().__init__(
            region,
            parse=lambda raw: (
                snap_to_known(value) if (value := parse_timeframe(raw)) else None
            ),
            confirmations=confirmations,
            min_width=6,
        )
