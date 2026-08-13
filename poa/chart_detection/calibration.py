"""Mapping pixel rows to prices.

Two ways to calibrate, in order of reliability:

1. **Manual** — the user reads two prices off the chart's axis and records the
   pixel rows they sit on. This is exact and never breaks when the platform
   changes its fonts.
2. **OCR** — read the price axis with Tesseract, fit a line through the labels
   it recognised. Convenient, but it fails quietly on antialiased text, so its
   confidence is reported and it is never silently trusted.

Without either, the engine can still measure *shape* (structure, Heikin Ashi,
momentum) but the absolute prices are unknown, so calibration status is part of
the data-quality report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

try:  # pragma: no cover - optional
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

try:  # pragma: no cover - optional
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None  # type: ignore[assignment]


_PRICE_PATTERN = re.compile(r"^\d{1,7}(?:[.,]\d{1,6})?$")


@dataclass
class PriceCalibration:
    """A linear pixel-row to price mapping."""

    top_pixel: float
    top_price: float
    bottom_pixel: float
    bottom_price: float
    confidence: float = 100.0
    method: str = "manual"

    @property
    def valid(self) -> bool:
        return (
            self.bottom_pixel != self.top_pixel
            and self.bottom_price != self.top_price
            and np.isfinite(self.top_price)
            and np.isfinite(self.bottom_price)
        )

    @property
    def price_per_pixel(self) -> float:
        if self.bottom_pixel == self.top_pixel:
            return 0.0
        return (self.bottom_price - self.top_price) / (
            self.bottom_pixel - self.top_pixel
        )

    def price_at_row(self, row: float) -> float:
        return self.top_price + (row - self.top_pixel) * self.price_per_pixel

    def row_at_price(self, price: float) -> float:
        slope = self.price_per_pixel
        if slope == 0:
            return self.top_pixel
        return self.top_pixel + (price - self.top_price) / slope

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "confidence": round(self.confidence, 1),
            "top_pixel": self.top_pixel,
            "top_price": self.top_price,
            "bottom_pixel": self.bottom_pixel,
            "bottom_price": self.bottom_price,
            "valid": self.valid,
        }

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> "PriceCalibration | None":
        if not raw or not raw.get("enabled"):
            return None
        try:
            calibration = cls(
                top_pixel=float(raw["top_pixel"]),
                top_price=float(raw["top_price"]),
                bottom_pixel=float(raw["bottom_pixel"]),
                bottom_price=float(raw["bottom_price"]),
                method="manual",
                confidence=100.0,
            )
        except (KeyError, TypeError, ValueError):
            return None
        return calibration if calibration.valid else None


def relative_calibration(height: int) -> PriceCalibration:
    """A placeholder mapping used when no real calibration exists.

    Rows map onto an arbitrary 0-100 scale. Shape-based analysis (structure,
    Heikin Ashi, RSI, MACD) is unaffected by the scale; only the printed price
    levels are meaningless, which is why this reports low confidence.
    """
    return PriceCalibration(
        top_pixel=0.0,
        top_price=100.0,
        bottom_pixel=float(max(height - 1, 1)),
        bottom_price=0.0,
        confidence=35.0,
        method="uncalibrated",
    )


def _parse_price(text: str) -> float | None:
    cleaned = text.strip().replace(" ", "")
    if not cleaned or not _PRICE_PATTERN.match(cleaned):
        return None
    cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if np.isfinite(value) else None


def calibrate_with_ocr(
    image: np.ndarray, axis_width_px: int = 70
) -> PriceCalibration | None:
    """Read the right-hand price axis and fit a pixel-to-price line.

    Returns ``None`` when OCR is unavailable or the labels it found do not form
    a consistent linear scale — a wrong calibration is far worse than none.
    """
    if pytesseract is None or cv2 is None:
        return None
    if image is None or image.size == 0:
        return None

    height, width = image.shape[:2]
    axis_width = min(max(int(axis_width_px), 20), width - 1)
    axis = image[:, width - axis_width :]

    grey = cv2.cvtColor(axis, cv2.COLOR_BGR2GRAY)
    # Upscale before thresholding: axis labels are small and Tesseract does
    # markedly better with more pixels per glyph.
    grey = cv2.resize(grey, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if binary.mean() < 127:  # light text on dark chart
        binary = cv2.bitwise_not(binary)

    try:
        data = pytesseract.image_to_data(
            binary,
            config="--psm 6 -c tessedit_char_whitelist=0123456789.,",
            output_type=pytesseract.Output.DICT,
        )
    except Exception:  # pragma: no cover - tesseract binary missing/broken
        return None

    samples: list[tuple[float, float]] = []
    for i, text in enumerate(data.get("text", [])):
        price = _parse_price(text)
        if price is None:
            continue
        try:
            confidence = float(data["conf"][i])
        except (KeyError, IndexError, ValueError):
            confidence = -1.0
        if confidence < 45:
            continue
        # Undo the 3x upscale to get back to original image rows.
        row = (float(data["top"][i]) + float(data["height"][i]) / 2.0) / 3.0
        samples.append((row, price))

    if len(samples) < 2:
        return None

    samples.sort(key=lambda s: s[0])
    rows = np.array([s[0] for s in samples], dtype=np.float64)
    prices = np.array([s[1] for s in samples], dtype=np.float64)

    # A price axis must be strictly decreasing in price as rows increase.
    if not np.all(np.diff(prices) < 0):
        return None

    slope, intercept = np.polyfit(rows, prices, 1)
    if not np.isfinite(slope) or slope >= 0:
        return None

    predicted = slope * rows + intercept
    spread = float(prices.max() - prices.min())
    if spread <= 0:
        return None
    residual = float(np.max(np.abs(predicted - prices)) / spread)
    if residual > 0.05:
        # The labels do not sit on a straight line, so at least one was misread.
        return None

    confidence = float(max(40.0, min(95.0, 95.0 - residual * 600.0)))
    if len(samples) >= 4:
        confidence = min(95.0, confidence + 5.0)

    return PriceCalibration(
        top_pixel=0.0,
        top_price=float(intercept),
        bottom_pixel=float(height - 1),
        bottom_price=float(slope * (height - 1) + intercept),
        confidence=confidence,
        method="ocr",
    )


def resolve_calibration(
    image: np.ndarray,
    manual: PriceCalibration | None,
    *,
    use_ocr: bool = True,
    axis_width_px: int = 70,
) -> PriceCalibration:
    """Pick the best available calibration, falling back to a relative scale."""
    if manual is not None and manual.valid:
        return manual
    if use_ocr:
        ocr = calibrate_with_ocr(image, axis_width_px)
        if ocr is not None and ocr.valid:
            return ocr
    height = image.shape[0] if image is not None and image.size else 100
    return relative_calibration(height)


def make_price_mapper(calibration: PriceCalibration) -> Callable[[float], float]:
    return calibration.price_at_row
