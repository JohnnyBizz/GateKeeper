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

from .label_reader import find_text_boxes, ocr_crop

try:  # pragma: no cover - optional
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

try:  # pragma: no cover - optional
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None  # type: ignore[assignment]


_PRICE_PATTERN = re.compile(r"^\d{1,7}(?:[.,]\d{1,6})?$")

# What the last calibration attempt actually saw. Purely diagnostic, and it
# exists because the failures that matter are invisible from the outside:
# "price scale is not calibrated" is the same message whether no labels were
# found, four were found and disagreed, or they were read as the wrong
# numbers entirely. Knowing which turns a guess into a fix.
_LAST_READ: dict[str, Any] = {}


def last_read() -> dict[str, Any]:
    """A snapshot of the most recent calibration attempt."""
    return dict(_LAST_READ)


@dataclass
class PriceCalibration:
    """A linear pixel-row to price mapping."""

    top_pixel: float
    top_price: float
    bottom_pixel: float
    bottom_price: float
    confidence: float = 100.0
    method: str = "manual"
    # Set when something about this mapping deserves saying out loud — most
    # often that it disagrees with the axis currently on screen.
    note: str = ""

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
            "note": self.note,
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


def _consistent_format(
    reads: list[tuple[float, str, float]]
) -> list[tuple[float, str, float]]:
    """Keep only the labels that share the axis's dominant number format.

    Every label on one axis is drawn by the same code with the same precision,
    so "1.08641" and "08154" cannot both be right. Where they disagree the
    minority was misread, and a misread that still lands on a straight line is
    the dangerous kind — nothing further down the pipeline would catch it.
    """
    if len(reads) < 2:
        return reads

    def shape(text: str) -> tuple[bool, int]:
        head, _, tail = text.partition(".")
        return ("." in text, len(tail) if "." in text else len(head))

    counts: dict[tuple[bool, int], int] = {}
    for _row, text, _price in reads:
        counts[shape(text)] = counts.get(shape(text), 0) + 1
    dominant = max(counts, key=lambda key: (counts[key], key[0]))
    return [read for read in reads if shape(read[1]) == dominant]


def _price_labels_anywhere(image: np.ndarray) -> list[tuple[float, str, float]]:
    """Every price-shaped number on the chart, with the row it sits on."""
    reads: list[tuple[float, str, float]] = []
    for box in find_text_boxes(image):
        text, _confidence = ocr_crop(image, box, "0123456789.,")
        price = _parse_price(text)
        # A price carries decimals. Requiring them keeps out the clock on the
        # time axis and the countdown beside the current-price chip, which are
        # whole numbers and are not positioned at a price at all.
        if price is None or "." not in text.replace(",", "."):
            continue
        reads.append((box[1] + box[3] / 2.0, text.strip().replace(",", "."), price))
    return _consistent_format(reads)


def _fit_by_consensus(
    reads: list[tuple[float, str, float]]
) -> tuple[float, float, np.ndarray, np.ndarray] | None:
    """Fit the line that the most labels agree on, ignoring the rest.

    Labels found across the whole chart are not all positioned at their own
    price: a hovered candle's open/high/low/close readout sits in a corner
    reading four prices that have nothing to do with the row it is printed on.
    Fitting everything at once lets those bend the scale. Fitting each pair and
    keeping whichever line the most other labels fall on ignores them instead.
    """
    if len(reads) < 2:
        return None

    points = sorted({(row, price) for row, _text, price in reads})
    if len(points) < 2:
        return None

    best: tuple[int, float, float] | None = None
    for i, (row_a, price_a) in enumerate(points):
        for row_b, price_b in points[i + 1 :]:
            if row_b == row_a or price_b == price_a:
                continue
            slope = (price_b - price_a) / (row_b - row_a)
            if slope >= 0:  # price must fall as rows increase
                continue
            intercept = price_a - slope * row_a
            span = abs(price_b - price_a)
            tolerance = max(span * 0.02, 1e-9)
            inliers = sum(
                1
                for row, price in points
                if abs(price - (slope * row + intercept)) <= tolerance
            )
            if best is None or inliers > best[0]:
                best = (inliers, slope, intercept)

    if best is None or best[0] < 2:
        return None

    _count, slope, intercept = best
    tolerance = None
    kept = [
        (row, price)
        for row, price in points
        if abs(price - (slope * row + intercept))
        <= max(abs(slope) * 8.0, 1e-9)
    ]
    if len(kept) < 2:
        return None

    rows = np.array([k[0] for k in kept], dtype=np.float64)
    prices = np.array([k[1] for k in kept], dtype=np.float64)
    slope, intercept = np.polyfit(rows, prices, 1)
    return float(slope), float(intercept), rows, prices


def calibrate_with_ocr(
    image: np.ndarray, axis_width_px: int = 70
) -> PriceCalibration | None:
    """Read the right-hand price axis and fit a pixel-to-price line.

    Returns ``None`` when OCR is unavailable or the labels it found do not form
    a consistent linear scale — a wrong calibration is far worse than none.
    """
    _LAST_READ.clear()
    _LAST_READ["outcome"] = "not attempted"
    if pytesseract is None or cv2 is None:
        _LAST_READ["outcome"] = "OCR unavailable"
        return None
    if image is None or image.size == 0:
        return None

    height, width = image.shape[:2]
    # Search a generous strip rather than a tight one. A strip narrower than
    # the labels slices the leading characters off every one of them — and
    # because it slices them all the same way, what is left is still perfectly
    # linear, so every downstream check passes and the chart reports prices
    # that are wrong by orders of magnitude. Widening is free: labels are found
    # as text shapes, and a candle is not text.
    axis_width = min(max(int(axis_width_px), 20, min(width // 3, 220)), width - 1)
    axis = image[:, width - axis_width :]

    # Find each label as a shape first, then read it on its own. Thresholding
    # the whole axis strip in one go fails whenever the chart's background and
    # the axis gutter differ in brightness — the split lands between those two
    # and the digits go with whichever side they sit on.
    reads: list[tuple[float, str, float]] = []
    for box in find_text_boxes(axis):
        text, _confidence = ocr_crop(axis, box, "0123456789.,")
        price = _parse_price(text)
        if price is None:
            continue
        reads.append((box[1] + box[3] / 2.0, text.strip().replace(",", "."), price))

    _LAST_READ["axis_strip_px"] = axis_width
    _LAST_READ["axis_labels"] = [text for _row, text, _price in reads]

    reads = _consistent_format(reads)
    axis_only = len(reads) >= 2

    if not axis_only:
        # Not every platform draws a price axis. Pocket Option's default layout
        # has no column of prices at all — just a chip on the current price and
        # one on each of the visible high and low. Those are prices at known
        # rows, which is all a calibration needs, so when the axis turns up
        # nothing the whole chart is searched instead.
        reads = _price_labels_anywhere(image)
        _LAST_READ["chart_labels"] = [text for _row, text, _price in reads]

    if len(reads) < 2:
        _LAST_READ["outcome"] = "too few price labels"
        return None

    if axis_only:
        samples = sorted((row, price) for row, _text, price in reads)
        rows = np.array([s[0] for s in samples], dtype=np.float64)
        prices = np.array([s[1] for s in samples], dtype=np.float64)
        # A price axis must be strictly decreasing in price as rows increase.
        if not np.all(np.diff(prices) < 0):
            return None
        slope, intercept = np.polyfit(rows, prices, 1)
        residual_limit = 0.05
    else:
        # Labels scattered over the chart include ones that are not positioned
        # at their own price — a hovered candle's OHLC readout sits in a corner.
        # Fit by consensus so those cannot drag the scale with them.
        fitted = _fit_by_consensus(reads)
        if fitted is None:
            return None
        slope, intercept, rows, prices = fitted
        residual_limit = 0.08

    if not np.isfinite(slope) or slope >= 0:
        _LAST_READ["outcome"] = "prices do not fall as rows increase"
        return None

    predicted = slope * rows + intercept
    spread = float(prices.max() - prices.min())
    if spread <= 0:
        return None
    residual = float(np.max(np.abs(predicted - prices)) / spread)
    if residual > residual_limit:
        # The labels do not sit on a straight line, so at least one was misread.
        _LAST_READ["outcome"] = f"labels not collinear (residual {residual:.2f})"
        return None

    confidence = float(max(40.0, min(95.0, 95.0 - residual * 600.0)))
    if len(samples) >= 4:
        confidence = min(95.0, confidence + 5.0)

    # A decimal point lost from every label is the one misread that leaves the
    # fit perfect and the prices wrong by a factor of a hundred thousand. It
    # cannot be ruled out from the numbers alone — an index really does read
    # 38500 — so it is not rejected, it is said out loud next to the price.
    note = ""
    if all("." not in text for _row, text, _price in reads) and min(
        len(text) for _row, text, _price in reads
    ) >= 5:
        confidence = min(confidence, 60.0)
        note = (
            "The price axis was read as whole numbers with no decimal point "
            f"(for example {reads[0][1]}). If the chart's prices have decimals, "
            "the levels shown are the wrong size — check the price against the "
            "chart, and set the scale by hand in settings if it disagrees."
        )

    _LAST_READ["outcome"] = "calibrated"
    return PriceCalibration(
        top_pixel=0.0,
        top_price=float(intercept),
        bottom_pixel=float(height - 1),
        bottom_price=float(slope * (height - 1) + intercept),
        confidence=confidence,
        method="ocr",
        note=note,
    )


# How far the manual scale may sit from the axis on screen before it is
# treated as describing a different chart. Expressed as a fraction of price:
# a real axis pan over a session moves the visible window, not the price by 0.5%.
CALIBRATION_DISAGREEMENT_TOLERANCE = 0.005

# Confidence a disputed manual scale is allowed to keep. Low enough to show in
# the status bar and raise an issue, high enough that shape-based analysis —
# which does not care about the absolute scale at all — still runs.
DISPUTED_CONFIDENCE = 55.0


def check_against_axis(
    image: np.ndarray,
    calibration: PriceCalibration,
    *,
    axis_width_px: int = 70,
) -> PriceCalibration:
    """Compare a manual scale against the axis on screen right now.

    A manual calibration is exact at the moment it is made and never expires by
    itself, which is the problem: the platform rescales its axis as price moves,
    the user may have switched pairs, or a digit may simply have been typed
    wrong. Any of those leaves the app printing precise, confident, wrong
    prices — and those prices go into the journal and settle outcomes.

    So the axis is re-read and the two are compared at the middle of the chart.
    Disagreement does not overrule the user; it lowers confidence and says so.
    """
    if not calibration.valid:
        return calibration
    ocr = calibrate_with_ocr(image, axis_width_px)
    if ocr is None or not ocr.valid:
        # Nothing to compare against. Silence here is correct: an unreadable
        # axis is not evidence the manual scale is wrong.
        return calibration

    mid_row = float(image.shape[0]) / 2.0
    mine = calibration.price_at_row(mid_row)
    theirs = ocr.price_at_row(mid_row)
    reference = max(abs(mine), abs(theirs), 1e-9)
    drift = abs(mine - theirs) / reference
    if drift <= CALIBRATION_DISAGREEMENT_TOLERANCE:
        return calibration

    return PriceCalibration(
        top_pixel=calibration.top_pixel,
        top_price=calibration.top_price,
        bottom_pixel=calibration.bottom_pixel,
        bottom_price=calibration.bottom_price,
        confidence=min(calibration.confidence, DISPUTED_CONFIDENCE),
        method="manual-disputed",
        note=(
            f"The saved price scale says {mine:g} at the middle of the chart, "
            f"but the axis on screen reads about {theirs:g}. Prices and levels "
            "shown may be wrong — re-run Select next to Chart area and click "
            "two prices again. Patterns and direction are unaffected."
        ),
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
        if use_ocr:
            return check_against_axis(image, manual, axis_width_px=axis_width_px)
        return manual
    if use_ocr:
        ocr = calibrate_with_ocr(image, axis_width_px)
        if ocr is not None and ocr.valid:
            return ocr
    height = image.shape[0] if image is not None and image.size else 100
    return relative_calibration(height)


def make_price_mapper(calibration: PriceCalibration) -> Callable[[float], float]:
    return calibration.price_at_row
