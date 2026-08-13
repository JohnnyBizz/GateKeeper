"""Reading the traded pair's name off the screen.

The candles tell GateKeeper everything about *price*, but nothing about which
instrument it is looking at — the pair is text drawn by the platform. Without
reading it, switching charts leaves the panel labelled with the previous pair
and the journal filing new signals under the wrong name.

The user points GateKeeper at the platform's pair label once (a small box
around "EUR/USD"), and it is OCR'd on each capture. Deliberately conservative:
a misread name is worse than no name, because it silently splits the journal,
so anything that does not look like an instrument symbol is rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

try:  # pragma: no cover - optional
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

try:  # pragma: no cover - optional
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None  # type: ignore[assignment]


# Instrument names on these platforms look like EUR/USD, EURUSD, AED/CNY OTC,
# BTC/USD, or an equity ticker. Anything else is a misread.
_SYMBOL = re.compile(
    r"^([A-Z]{3,6})\s*[/\-]?\s*([A-Z]{3,6})?(\s+OTC)?$", re.IGNORECASE
)

# OCR reliably confuses these when reading short uppercase text.
_CONFUSIONS = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B"})


@dataclass
class AssetReading:
    """What the label region was read as."""

    name: str | None
    confidence: float
    raw: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.name) and self.confidence >= 55.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "confidence": round(self.confidence, 1),
            "raw": self.raw,
            "ok": self.ok,
        }


def normalise(text: str) -> str | None:
    """Turn raw OCR output into a canonical pair name, or None if implausible."""
    cleaned = " ".join(text.split()).strip().upper()
    if not cleaned:
        return None

    # Strip decorations the platform draws next to the name.
    cleaned = cleaned.replace("▾", "").replace("▼", "").replace("|", "").strip(" .,:")
    # Currency codes contain no digits; correcting them beats rejecting them.
    cleaned = cleaned.translate(_CONFUSIONS)

    match = _SYMBOL.match(cleaned)
    if not match:
        return None

    base = match.group(1).upper()
    quote = (match.group(2) or "").upper()
    otc = " OTC" if match.group(3) else ""

    if quote:
        return f"{base}/{quote}{otc}"
    # A single 6-letter token is a pair written without a separator.
    if len(base) == 6:
        return f"{base[:3]}/{base[3:]}{otc}"
    return f"{base}{otc}"


def read_asset_label(image) -> AssetReading:
    """OCR an image of the platform's pair label."""
    if pytesseract is None or cv2 is None or image is None or image.size == 0:
        return AssetReading(name=None, confidence=0.0)

    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # Upscale: the label is small, and Tesseract needs pixels per glyph.
    grey = cv2.resize(grey, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if binary.mean() < 127:  # light text on a dark chart
        binary = cv2.bitwise_not(binary)

    try:
        data = pytesseract.image_to_data(
            binary,
            config=(
                "--psm 7 -c tessedit_char_whitelist="
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz/-0123456789"
            ),
            output_type=pytesseract.Output.DICT,
        )
    except Exception as exc:  # pragma: no cover - tesseract missing/broken
        log.debug("asset OCR failed: %s", exc)
        return AssetReading(name=None, confidence=0.0)

    words: list[str] = []
    confidences: list[float] = []
    for index, word in enumerate(data.get("text", [])):
        if not word.strip():
            continue
        try:
            confidence = float(data["conf"][index])
        except (KeyError, IndexError, ValueError):
            continue
        if confidence < 30:
            continue
        words.append(word.strip())
        confidences.append(confidence)

    if not words:
        return AssetReading(name=None, confidence=0.0)

    raw = " ".join(words)
    name = normalise(raw)
    confidence = float(sum(confidences) / len(confidences))
    if name is None:
        # Report the confidence anyway so the UI can say "could not read it"
        # rather than silently doing nothing.
        return AssetReading(name=None, confidence=confidence, raw=raw)
    return AssetReading(name=name, confidence=confidence, raw=raw)


class AssetLabelReader:
    """Reads the pair name from a fixed screen region, with hysteresis.

    A single frame's OCR is not trusted: text can be misread while the platform
    animates a transition. The name only changes after the same new value has
    been read on consecutive frames, which costs a couple of seconds and avoids
    the panel flickering between two names.
    """

    def __init__(self, region: dict[str, int] | None, confirmations: int = 2) -> None:
        self.region = region if region and int(region.get("width", 0)) > 10 else None
        self.confirmations = max(1, int(confirmations))
        self.current: str | None = None
        self._candidate: str | None = None
        self._streak = 0

    @property
    def enabled(self) -> bool:
        return self.region is not None and pytesseract is not None

    def read(self, grab) -> AssetReading:
        """``grab`` takes a region dict and returns a BGR image."""
        if self.region is None:
            return AssetReading(name=None, confidence=0.0)
        try:
            image = grab(self.region)
        except Exception as exc:  # pragma: no cover - capture failure
            log.debug("asset label capture failed: %s", exc)
            return AssetReading(name=None, confidence=0.0)

        reading = read_asset_label(image)
        if not reading.ok:
            return reading

        if reading.name == self.current:
            self._candidate = None
            self._streak = 0
            return reading

        if reading.name == self._candidate:
            self._streak += 1
        else:
            self._candidate = reading.name
            self._streak = 1

        if self._streak >= self.confirmations:
            log.info("asset label read as %s", reading.name)
            self.current = reading.name
            self._candidate = None
            self._streak = 0
        return reading
