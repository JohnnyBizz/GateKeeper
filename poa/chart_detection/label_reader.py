"""Reading small text labels off the platform's interface.

The candles carry price and nothing else. Which instrument they belong to and
which timeframe they are drawn at are both *text* the platform renders, so OCR
is the only way to read them.

This is the shared machinery for that: crop a small region, OCR it, parse it
into something typed, and only accept a change once it has been read the same
way on consecutive frames. The hysteresis matters — a single frame can be
misread while the platform animates a transition, and a label that flickers
between two values is worse than one that lags by a second.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from ..logging_setup import get_logger

log = get_logger(__name__)

try:  # pragma: no cover - optional
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

try:  # pragma: no cover - optional
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None  # type: ignore[assignment]

T = TypeVar("T")

# Tesseract page-segmentation mode 7: "treat the image as a single text line",
# which is what every one of these labels is.
_SINGLE_LINE = "--psm 7"


@dataclass
class LabelReading(Generic[T]):
    """One attempt at reading a label."""

    value: T | None
    confidence: float
    raw: str = ""

    @property
    def ok(self) -> bool:
        """Whether this reading is worth acting on.

        Deliberately not gated on Tesseract's confidence. Under a character
        whitelist it routinely reports 0 for a perfectly correct read, so a
        confidence floor throws away exactly the reads it was meant to protect.
        The real gate is the parser — a string only becomes a value if it looks
        like a currency pair or a chart interval — backed by the hysteresis
        below, which will not adopt anything read only once.
        """
        return self.value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "confidence": round(self.confidence, 1),
            "raw": self.raw,
            "ok": self.ok,
        }


def ocr_text(image, whitelist: str) -> tuple[str, float]:
    """OCR a small image, returning (text, mean confidence)."""
    if pytesseract is None or cv2 is None or image is None or image.size == 0:
        return "", 0.0

    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # Upscale first: these labels are tiny, and Tesseract needs pixels per glyph.
    grey = cv2.resize(grey, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if binary.mean() < 127:  # light text on a dark chart
        binary = cv2.bitwise_not(binary)

    try:
        data = pytesseract.image_to_data(
            binary,
            config=f"{_SINGLE_LINE} -c tessedit_char_whitelist={whitelist}",
            output_type=pytesseract.Output.DICT,
        )
    except Exception as exc:  # pragma: no cover - tesseract missing/broken
        log.debug("label OCR failed: %s", exc)
        return "", 0.0

    words: list[str] = []
    confidences: list[float] = []
    for index, word in enumerate(data.get("text", [])):
        if not word.strip():
            continue
        try:
            confidence = float(data["conf"][index])
        except (KeyError, IndexError, ValueError):
            continue
        if confidence < 0:  # -1 marks a box Tesseract found no text in
            continue
        words.append(word.strip())
        confidences.append(confidence)

    if not words:
        return "", 0.0
    return " ".join(words), float(sum(confidences) / len(confidences))


# Text sizes worth considering, in screen pixels. Below the floor OCR is
# guessing; above the ceiling it is a heading, not a label or an axis price.
MIN_TEXT_HEIGHT = 9
MAX_TEXT_HEIGHT = 64


def find_text_boxes(image) -> list[tuple[int, int, int, int]]:
    """Locate word-shaped clusters of glyph strokes, without reading them.

    Returns (left, top, width, height) tuples in the image's own coordinates.

    Handing a whole strip of interface to Tesseract does not work: it picks one
    global threshold, and on a screen where the panel, the background and the
    text are three different brightnesses that threshold separates the panel
    from the background and loses the text entirely. Finding the words first
    and reading each one on its own turns an unsolved segmentation problem into
    a series of tight, high-contrast crops — which is the case OCR is good at.

    Glyphs are found by local contrast rather than brightness, so light text on
    a dark chart and dark text on a light toolbar are found the same way.
    """
    if cv2 is None or image is None or image.size == 0:
        return []

    grey = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # A glyph is a small, high-contrast disturbance in an otherwise smooth
    # background. Subtracting a blurred copy leaves the strokes and drops the
    # flat panels a global threshold used to lock on to.
    blurred = cv2.GaussianBlur(grey, (0, 0), 3.0)
    detail = cv2.absdiff(grey, blurred)
    if detail.max() < 12:  # nothing but flat colour
        return []
    _, strokes = cv2.threshold(detail, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)

    # Join letters into words: a horizontal close spanning roughly one
    # character gap, and a small vertical one to bridge dotted glyphs.
    strokes = cv2.morphologyEx(
        strokes, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3))
    )

    count, _, stats, _ = cv2.connectedComponentsWithStats(strokes, connectivity=8)
    boxes: list[tuple[int, int, int, int]] = []
    for index in range(1, count):
        left, top, width, height, area = (int(v) for v in stats[index])
        if not (MIN_TEXT_HEIGHT <= height <= MAX_TEXT_HEIGHT):
            continue
        if width < 8 or width > height * 30:
            continue
        if area < width * height * 0.05:  # a hollow box outline, not glyphs
            continue
        boxes.append((left, top, width, height))
    return boxes


def ocr_crop(
    image, box: tuple[int, int, int, int], whitelist: str
) -> tuple[str, float]:
    """Read one tight crop of ``image``. Returns (text, confidence)."""
    if pytesseract is None or cv2 is None or image is None or image.size == 0:
        return "", 0.0

    left, top, width, height = box
    x0 = max(0, left - 5)
    y0 = max(0, top - 5)
    x1 = min(image.shape[1], left + width + 5)
    y1 = min(image.shape[0], top + height + 5)
    if x1 <= x0 or y1 <= y0:
        return "", 0.0

    crop = image[y0:y1, x0:x1]
    grey = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # Aim for roughly 40px of glyph height, which is where Tesseract is happiest.
    scale = max(1.0, min(8.0, 40.0 / max(height, 1)))
    grey = cv2.resize(grey, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if binary.mean() < 127:  # light text on a dark background
        binary = cv2.bitwise_not(binary)

    try:
        data = pytesseract.image_to_data(
            binary,
            config=f"{_SINGLE_LINE} -c tessedit_char_whitelist={whitelist}",
            output_type=pytesseract.Output.DICT,
        )
    except Exception as exc:  # pragma: no cover - tesseract missing/broken
        log.debug("crop OCR failed: %s", exc)
        return "", 0.0

    words: list[str] = []
    confidences: list[float] = []
    for index, text in enumerate(data.get("text", [])):
        cleaned = text.strip()
        if not cleaned:
            continue
        try:
            confidence = float(data["conf"][index])
        except (KeyError, IndexError, ValueError):
            continue
        if confidence < 0:  # -1 marks a box Tesseract found no text in
            continue
        words.append(cleaned)
        confidences.append(confidence)

    if not words:
        return "", 0.0
    return " ".join(words), float(sum(confidences) / len(confidences))


class LabelReader(Generic[T]):
    """Reads one label from a fixed screen region, with change hysteresis."""

    #: Characters Tesseract is allowed to return. Narrowing this is the single
    #: most effective way to stop small text being misread.
    whitelist = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz/-0123456789 "
    )

    def __init__(
        self,
        region: dict[str, int] | None,
        parse: Callable[[str], T | None],
        *,
        confirmations: int = 2,
        min_width: int = 10,
    ) -> None:
        self.region = (
            region if region and int(region.get("width", 0)) > min_width else None
        )
        self.parse = parse
        self.confirmations = max(1, int(confirmations))
        self.current: T | None = None
        self._candidate: T | None = None
        self._streak = 0

    @property
    def enabled(self) -> bool:
        return self.region is not None and pytesseract is not None

    def read(self, grab: Callable[[dict[str, int]], Any]) -> LabelReading[T]:
        """``grab`` takes a region dict and returns a BGR image."""
        if self.region is None:
            return LabelReading(value=None, confidence=0.0)

        try:
            image = grab(self.region)
        except Exception as exc:  # pragma: no cover - capture failure
            log.debug("label capture failed: %s", exc)
            return LabelReading(value=None, confidence=0.0)

        raw, confidence = ocr_text(image, self.whitelist)
        value = self.parse(raw) if raw else None
        reading = LabelReading(value=value, confidence=confidence, raw=raw)
        if not reading.ok:
            return reading

        self._accept(reading.value)
        return reading

    def _accept(self, value: T | None) -> None:
        """Only change ``current`` once the same value repeats."""
        if value == self.current:
            self._candidate = None
            self._streak = 0
            return

        if value == self._candidate:
            self._streak += 1
        else:
            self._candidate = value
            self._streak = 1

        if self._streak >= self.confirmations:
            log.info("%s read as %s", type(self).__name__, value)
            self.current = value
            self._candidate = None
            self._streak = 0
