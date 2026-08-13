"""Render a candle series as a chart image.

This is test and demo infrastructure, not part of the live path. It draws
candles the way a trading platform does — dark background, green/red bodies,
thin wicks, a price axis on the right — so the extraction pipeline can be
tested end to end without a screen: render a known series, extract it, and
check the recovered candles match.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..models import Series

try:  # pragma: no cover - optional
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]


@dataclass
class RenderStyle:
    background: tuple[int, int, int] = (24, 26, 32)  # BGR
    bullish: tuple[int, int, int] = (110, 200, 80)
    bearish: tuple[int, int, int] = (70, 70, 225)
    axis_text: tuple[int, int, int] = (200, 200, 200)
    candle_width: int = 7
    candle_gap: int = 4
    padding: int = 24
    axis_width: int = 70
    draw_axis: bool = True
    axis_labels: int = 6


def render_series(
    series: Series,
    height: int = 480,
    style: RenderStyle | None = None,
) -> tuple[np.ndarray, "PriceMapping"]:
    """Draw ``series`` and return the image plus its true pixel-to-price map."""
    if cv2 is None:  # pragma: no cover
        raise RuntimeError("rendering requires OpenCV")
    if len(series) == 0:
        raise ValueError("cannot render an empty series")

    style = style or RenderStyle()
    count = len(series)
    pitch = style.candle_width + style.candle_gap
    plot_width = count * pitch + style.padding * 2
    width = plot_width + (style.axis_width if style.draw_axis else 0)

    image = np.full((height, width, 3), style.background, dtype=np.uint8)

    high = float(series.high.max())
    low = float(series.low.min())
    span = high - low
    if span <= 0:
        span = max(abs(high), 1.0) * 0.01
        high += span / 2
        low -= span / 2

    top_row = style.padding
    bottom_row = height - style.padding - 1

    def row_for(price: float) -> int:
        ratio = (high - price) / (high - low)
        return int(round(top_row + ratio * (bottom_row - top_row)))

    for index, candle in enumerate(series):
        x_center = style.padding + index * pitch + style.candle_width // 2
        colour = style.bullish if candle.close >= candle.open else style.bearish

        # Wick first, then the body over the top of it.
        cv2.line(
            image,
            (x_center, row_for(candle.high)),
            (x_center, row_for(candle.low)),
            colour,
            1,
        )
        body_top = row_for(max(candle.open, candle.close))
        body_bottom = row_for(min(candle.open, candle.close))
        if body_bottom - body_top < 1:
            body_bottom = body_top + 1  # keep doji bodies visible
        cv2.rectangle(
            image,
            (x_center - style.candle_width // 2, body_top),
            (x_center + style.candle_width // 2, body_bottom),
            colour,
            -1,
        )

    if style.draw_axis:
        for i in range(style.axis_labels):
            price = high - (high - low) * i / max(style.axis_labels - 1, 1)
            row = row_for(price)
            cv2.putText(
                image,
                f"{price:.5f}",
                (plot_width + 4, min(height - 4, max(12, row + 4))),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                style.axis_text,
                1,
                cv2.LINE_AA,
            )

    mapping = PriceMapping(
        top_row=float(top_row),
        top_price=high,
        bottom_row=float(bottom_row),
        bottom_price=low,
    )
    return image, mapping


@dataclass
class PriceMapping:
    """The exact mapping used when rendering — the ground truth for tests."""

    top_row: float
    top_price: float
    bottom_row: float
    bottom_price: float

    def price_at_row(self, row: float) -> float:
        ratio = (row - self.top_row) / (self.bottom_row - self.top_row)
        return self.top_price + ratio * (self.bottom_price - self.top_price)

    def to_calibration(self):
        from .calibration import PriceCalibration

        return PriceCalibration(
            top_pixel=self.top_row,
            top_price=self.top_price,
            bottom_pixel=self.bottom_row,
            bottom_price=self.bottom_price,
            confidence=100.0,
            method="rendered",
        )
