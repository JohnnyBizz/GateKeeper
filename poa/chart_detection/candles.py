"""Candle extraction from a chart image.

The approach is colour segmentation plus per-candle column scanning:

1. build bullish and bearish colour masks in HSV (the palette is configurable
   because platforms differ and users re-theme their charts);
2. find each candle as a contour — wick and body are the same colour and touch,
   so one contour is one candle, and its horizontal bounds are the body's;
3. separate body from wick *by width*: inside a candle's column band, rows
   covered edge to edge belong to the body, rows covered by only a pixel or two
   belong to the wick. This is what makes open/close recoverable at all;
4. estimate the candle pitch from the spacing of centres, which tells us
   whether we found a coherent set of candles or a mess;
5. map pixel rows to prices with the supplied calibration.

Everything here is best-effort and reports a confidence. Computer vision cannot
identify every chart element reliably, and the confidence is what the rest of
the application uses to decide whether to trust the result at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import numpy as np

from ..models import Candle, Series

try:  # pragma: no cover - exercised only when OpenCV is installed
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]


class CandleExtractionError(RuntimeError):
    """Raised when extraction cannot proceed at all."""


@dataclass
class ColorProfile:
    """HSV ranges for bullish and bearish candles.

    Defaults cover the green/red palettes common on Pocket Option and most
    TradingView-style charts, including the teal-ish greens some themes use.
    """

    bullish: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = field(
        default_factory=lambda: [
            ((35, 60, 60), (90, 255, 255)),  # green through teal
        ]
    )
    bearish: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = field(
        default_factory=lambda: [
            ((0, 60, 60), (12, 255, 255)),  # red (low hue wrap)
            ((165, 60, 60), (180, 255, 255)),  # red (high hue wrap)
        ]
    )

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> "ColorProfile":
        if not raw:
            return cls()
        profile = cls()
        for key in ("bullish", "bearish"):
            ranges = raw.get(key)
            if not ranges:
                continue
            parsed: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = []
            for entry in ranges:
                low = tuple(int(v) for v in entry["low"])
                high = tuple(int(v) for v in entry["high"])
                parsed.append((low, high))  # type: ignore[arg-type]
            setattr(profile, key, parsed)
        return profile


@dataclass
class PixelCandle:
    """A candle still expressed in pixel coordinates."""

    x_center: int
    body_top: int
    body_bottom: int
    wick_top: int
    wick_bottom: int
    bullish: bool
    width: int


@dataclass
class ExtractionResult:
    candles: list[PixelCandle]
    confidence: float  # 0..100
    issues: list[str]
    pitch: float  # median horizontal distance between candles, in pixels
    plot_bounds: tuple[int, int, int, int]  # x0, y0, x1, y1

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.candles),
            "confidence": round(self.confidence, 1),
            "issues": list(self.issues),
            "pitch": round(self.pitch, 2),
            "plot_bounds": list(self.plot_bounds),
        }


def _require_cv2():
    if cv2 is None:
        raise CandleExtractionError(
            "OpenCV is not installed. Install it with 'pip install opencv-python' "
            "to use screen-capture chart recognition."
        )


def build_masks(
    image: np.ndarray, profile: ColorProfile
) -> tuple[np.ndarray, np.ndarray]:
    """Return (bullish_mask, bearish_mask) as uint8 0/255 arrays."""
    _require_cv2()
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    height, width = hsv.shape[:2]

    def combine(ranges) -> np.ndarray:
        mask = np.zeros((height, width), dtype=np.uint8)
        for low, high in ranges:
            mask |= cv2.inRange(hsv, np.array(low, np.uint8), np.array(high, np.uint8))
        return mask

    bull = combine(profile.bullish)
    bear = combine(profile.bearish)

    # A 1px vertical close joins a wick to its body without merging neighbours.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 3))
    bull = cv2.morphologyEx(bull, cv2.MORPH_CLOSE, kernel)
    bear = cv2.morphologyEx(bear, cv2.MORPH_CLOSE, kernel)
    return bull, bear


def detect_plot_area(mask: np.ndarray) -> tuple[int, int, int, int]:
    """Bound the region that actually contains candles.

    Trims the price axis and any surrounding chrome by keeping only the columns
    and rows where candle-coloured pixels appear.
    """
    columns = mask.sum(axis=0)
    rows = mask.sum(axis=1)
    active_columns = np.nonzero(columns > 0)[0]
    active_rows = np.nonzero(rows > 0)[0]
    if active_columns.size == 0 or active_rows.size == 0:
        height, width = mask.shape[:2]
        return 0, 0, width, height
    return (
        int(active_columns[0]),
        int(active_rows[0]),
        int(active_columns[-1]) + 1,
        int(active_rows[-1]) + 1,
    )


def extract_pixel_candles(
    image: np.ndarray,
    profile: ColorProfile | None = None,
    min_body_width: int = 2,
    strip_lines: bool = True,
) -> ExtractionResult:
    """Locate candles in a chart image, in pixel space.

    ``strip_lines`` removes indicator overlays drawn in candle colours
    (SuperTrend, moving averages, entry markers) before the candles are read.
    Real charts almost always carry some of these, so it is on by default.
    """
    _require_cv2()
    if image is None or image.size == 0:
        raise CandleExtractionError("empty image")
    if image.ndim != 3 or image.shape[2] < 3:
        raise CandleExtractionError("expected a colour (BGR) image")

    profile = profile or ColorProfile()
    bull_mask, bear_mask = build_masks(image, profile)
    if strip_lines:
        bull_mask = strip_horizontal_lines(bull_mask)
        bear_mask = strip_horizontal_lines(bear_mask)
    combined = bull_mask | bear_mask

    issues: list[str] = []
    plot_bounds = detect_plot_area(combined)
    x0, y0, x1, y1 = plot_bounds

    total_pixels = int(combined.sum() / 255)
    if total_pixels < 200:
        return ExtractionResult(
            candles=[],
            confidence=0.0,
            issues=[
                "No candle-coloured pixels found. Check the selected region and "
                "the configured candle colours."
            ],
            pitch=0.0,
            plot_bounds=plot_bounds,
        )

    # --- find candles ------------------------------------------------------
    # Wick and body share a colour and touch, so each contour is one candle.
    # The contour's horizontal bounds are the body's bounds (the wick is a
    # single column inside them); its vertical bounds are the wick's extremes.
    raw: list[tuple[int, int, int, int, bool]] = []  # x, y, w, h, bullish
    for mask, is_bull in ((bull_mask, True), (bear_mask, False)):
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if h < 1 or w < 1:
                continue
            raw.append((x, y, w, h, is_bull))

    if not raw:
        return ExtractionResult(
            candles=[],
            confidence=0.0,
            issues=["Candle shapes could not be separated from the background."],
            pitch=0.0,
            plot_bounds=plot_bounds,
        )

    raw.sort(key=lambda r: r[0] + r[2] / 2.0)
    body_widths = np.array([r[2] for r in raw], dtype=np.float64)
    typical_width = float(np.median(body_widths))
    # Blobs far narrower than a body are stray wick fragments or gridline
    # artefacts; they get folded into their neighbour during the merge below.
    min_width = max(min_body_width, 1)
    # The diagonal risers of a staircase indicator (SuperTrend and friends)
    # survive horizontal-line removal because they are vertical. They are far
    # narrower than a candle body, so width separates them cleanly. Only apply
    # this once there are enough contours for the median to mean something.
    narrow_cutoff = typical_width * 0.45 if len(raw) >= 8 else 0.0

    candles: list[PixelCandle] = []
    for x, y, w, h, is_bull in raw:
        if w < min_width and typical_width >= min_width * 2:
            continue
        if narrow_cutoff and w < narrow_cutoff:
            continue
        body_top, body_bottom = _body_extent(combined, x, w, y, h)
        candles.append(
            PixelCandle(
                x_center=x + w // 2,
                body_top=body_top,
                body_bottom=body_bottom,
                wick_top=y,
                wick_bottom=y + h - 1,
                bullish=is_bull,
                width=w,
            )
        )

    if not candles:
        return ExtractionResult(
            candles=[],
            confidence=0.0,
            issues=["No candle bodies survived filtering."],
            pitch=0.0,
            plot_bounds=plot_bounds,
        )

    # Merge blobs that belong to the same candle (a body split by a gridline).
    merged = _merge_same_candle(candles, max(1, int(typical_width * 0.6)))

    # --- pitch and coherence ----------------------------------------------
    centers = np.array([b.x_center for b in merged], dtype=np.float64)
    if centers.size >= 2:
        spacings = np.diff(centers)
        spacings = spacings[spacings > 0]
        pitch = float(np.median(spacings)) if spacings.size else 0.0
        regularity = (
            float(np.median(np.abs(spacings - pitch)) / pitch) if pitch > 0 else 1.0
        )
    else:
        pitch = 0.0
        regularity = 1.0

    # --- confidence --------------------------------------------------------
    confidence = 100.0
    if len(merged) < 20:
        confidence -= (20 - len(merged)) * 3.0
        issues.append(f"Only {len(merged)} candles were recognised in the region.")
    if regularity > 0.35:
        confidence -= 30.0
        issues.append(
            "Candle spacing is irregular — some candles may have been missed or merged."
        )
    elif regularity > 0.18:
        confidence -= 12.0
        issues.append("Candle spacing is somewhat irregular.")

    width_spread = float(np.std([b.width for b in merged]))
    if typical_width > 0 and width_spread / typical_width > 0.5:
        confidence -= 15.0
        issues.append(
            f"Candle widths vary widely ({typical_width:.0f}px typical, "
            f"{width_spread:.0f}px spread) — the selected area probably "
            "includes buttons, the sidebar or the time axis as well as candles."
        )

    # Candles pinned to the very top or bottom of the region are almost
    # certainly clipped, which corrupts their high or low.
    clipped = sum(
        1 for b in merged if b.wick_top <= y0 + 1 or b.wick_bottom >= y1 - 2
    )
    if clipped > len(merged) * 0.1:
        confidence -= 20.0
        issues.append(
            f"{clipped} candles touch the edge of the selected region and may be clipped."
        )

    coverage = (x1 - x0) / max(image.shape[1], 1)
    if coverage < 0.4:
        confidence -= 10.0
        issues.append("Candles occupy only a small part of the selected region.")

    # Zoom the chart in far enough and neighbouring bodies touch. They then
    # arrive as one blob spanning several candles, and what comes out is not a
    # slightly worse reading of the chart — it is a different chart, with a
    # third of the candles and each one's open and close taken from whichever
    # candle happened to start and end the run. Silence here would be the worst
    # outcome, because every number downstream still looks perfectly ordinary.
    # Measured straight off the mask rather than inferred from the blobs: on a
    # separated chart the gaps between candles leave a fifth to a third of the
    # columns empty, and when the candles touch there are no empty columns at
    # all. Comparing blob widths instead does not work, because once they fuse
    # the blobs and the spacing between them both grow together.
    band = combined[y0:y1, x0:x1]
    if band.size:
        column_fill = float((band.any(axis=0)).mean())
        if column_fill >= 0.94:
            confidence = min(confidence, 40.0)
            issues.append(
                "Candles are touching each other, so they cannot be told "
                "apart — what is read back has fewer, wider candles than the "
                "chart really has. Zoom the chart out a step until there is a "
                "visible gap between candles."
            )

    confidence = float(max(0.0, min(100.0, confidence)))

    return ExtractionResult(
        candles=merged,
        confidence=confidence,
        issues=issues,
        pitch=pitch,
        plot_bounds=plot_bounds,
    )


def strip_horizontal_lines(mask: np.ndarray, min_run: int = 31) -> np.ndarray:
    """Erase long horizontal runs — indicator lines — from a candle mask.

    Overlays like SuperTrend, moving averages, entry markers and price lines
    are drawn in the same greens and reds as the candles, so colour cannot
    separate them. Worse, where a line *touches* a candle the two merge into a
    single contour and the candle's measured extent is wrong.

    The removal is deliberately surgical rather than a blanket vertical
    opening: an opening erodes every candle a little, which measurably costs
    accuracy on charts that carry no overlays at all. Instead, only pixels
    belonging to a horizontal run at least ``min_run`` wide are deleted. No
    candle is that wide — bodies are a handful of pixels and neighbouring
    candles are separated by gaps — so clean charts come through untouched.

    Where a removed line crossed a candle it leaves a one-pixel notch, so the
    mask is closed vertically afterwards to heal the candle back together.
    """
    if cv2 is None:  # pragma: no cover - guarded by callers
        return mask

    horizontal = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, min_run), 1))
    lines = cv2.morphologyEx(mask, cv2.MORPH_OPEN, horizontal)

    # If "lines" account for most of the coloured pixels, the assumption has
    # failed (a very zoomed-in chart with enormous bodies, say). Removing them
    # would destroy the candles, so leave the mask alone and let the ordinary
    # confidence scoring report whatever mess results.
    total = float((mask > 0).sum())
    if total > 0 and float((lines > 0).sum()) / total > 0.35:
        return mask

    cleaned = cv2.subtract(mask, lines)
    heal = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5))
    return cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, heal)


def _body_extent(
    mask: np.ndarray, x: int, w: int, y: int, h: int
) -> tuple[int, int]:
    """Find a candle's body rows inside its bounding box.

    Body rows are covered across most of the candle's width; wick rows are a
    pixel or two. When no row qualifies — a doji drawn as a bare line — the
    body collapses to the widest row available, which is the right answer.
    """
    band = mask[y : y + h, x : x + w]
    if band.size == 0:
        return y, y + h - 1
    coverage = (band > 0).sum(axis=1)
    if coverage.size == 0:
        return y, y + h - 1

    threshold = max(2.0, w * 0.6)
    body_rows = np.nonzero(coverage >= threshold)[0]
    if body_rows.size == 0:
        # No full-width row: fall back to the widest rows present so that a
        # thin doji still yields a body rather than the whole wick range.
        widest = coverage.max()
        if widest <= 0:
            return y, y + h - 1
        body_rows = np.nonzero(coverage >= widest)[0]

    return int(y + body_rows[0]), int(y + body_rows[-1])


def _merge_same_candle(
    bodies: list[PixelCandle], tolerance: int
) -> list[PixelCandle]:
    """Fold blobs whose centres are within ``tolerance`` px into one candle."""
    merged: list[PixelCandle] = []
    for body in bodies:
        if merged and abs(body.x_center - merged[-1].x_center) <= tolerance:
            previous = merged[-1]
            previous.body_top = min(previous.body_top, body.body_top)
            previous.body_bottom = max(previous.body_bottom, body.body_bottom)
            previous.wick_top = min(previous.wick_top, body.wick_top)
            previous.wick_bottom = max(previous.wick_bottom, body.wick_bottom)
            previous.width = max(previous.width, body.width)
            # Keep the colour of the taller blob; that is the real body.
            if (body.body_bottom - body.body_top) > (
                previous.body_bottom - previous.body_top
            ):
                previous.bullish = body.bullish
        else:
            merged.append(body)
    return merged


def pixel_candles_to_series(
    pixel_candles: Sequence[PixelCandle],
    price_at_row,
    *,
    timeframe_seconds: int,
    symbol: str = "UNKNOWN",
    end_time: datetime | None = None,
    mark_last_incomplete: bool = True,
) -> Series:
    """Convert pixel candles into a priced ``Series``.

    ``price_at_row`` maps a pixel row (y) to a price — see ``calibration.py``.
    Timestamps are reconstructed backwards from ``end_time`` because a chart
    image carries no reliable per-candle timestamps; the time axis is regular,
    so this is accurate as long as the timeframe is right.
    """
    if not pixel_candles:
        return Series((), timeframe_seconds, symbol)

    end_time = end_time or datetime.now(timezone.utc).replace(microsecond=0)
    count = len(pixel_candles)
    candles: list[Candle] = []

    for index, pixel in enumerate(pixel_candles):
        # Pixel rows increase downward, so the top row is the higher price.
        high = price_at_row(pixel.wick_top)
        low = price_at_row(pixel.wick_bottom)
        body_high = price_at_row(pixel.body_top)
        body_low = price_at_row(pixel.body_bottom)

        if pixel.bullish:
            open_price, close_price = body_low, body_high
        else:
            open_price, close_price = body_high, body_low

        high = max(high, open_price, close_price)
        low = min(low, open_price, close_price)

        offset = (count - 1 - index) * timeframe_seconds
        candles.append(
            Candle(
                timestamp=end_time - timedelta(seconds=offset),
                open=float(open_price),
                high=float(high),
                low=float(low),
                close=float(close_price),
                volume=None,
                complete=not (mark_last_incomplete and index == count - 1),
            )
        )

    return Series(candles, timeframe_seconds, symbol)
