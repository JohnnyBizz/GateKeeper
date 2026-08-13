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
    axis_width: int = 80
    draw_axis: bool = True
    axis_labels: int = 6
    # Roughly 11px of glyph height, matching what a platform actually draws on
    # its price axis. Drawing it smaller made the fixture harder than reality
    # and sent the OCR chasing a problem it does not have.
    axis_font_scale: float = 0.45


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
                style.axis_font_scale,
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
class ScreenStyle:
    """Chrome drawn around the chart, to test layout detection against.

    Deliberately includes the things that fool a naive "find the green pixels"
    detector: a large green BUY button, a large red SELL button, a green
    account balance, coloured sidebar icons and a scattering of gridlines.
    """

    width: int = 1600
    height: int = 900
    # Blue-violet, like the platform's own theme. Deliberately not a hue the
    # candle masks match — the point of the fixture is the *shapes* around the
    # chart, and a hostile-hue variant is a separate test.
    background: tuple[int, int, int] = (110, 45, 60)
    chrome: tuple[int, int, int] = (135, 62, 78)
    text: tuple[int, int, int] = (225, 225, 235)
    sidebar_width: int = 90
    header_height: int = 64
    panel_width: int = 230
    footer_height: int = 40
    asset_label: str = "CAD/JPY OTC"
    timeframe_label: str = "H3"
    draw_buttons: bool = True
    # Menu items down the left rail. These are the decoys: read as text and
    # glued together, "Profile" becomes the instrument PROF/ILE.
    sidebar_labels: tuple[str, ...] = (
        "Trading", "Finance", "Profile", "Market", "Signals", "Help",
    )
    # Some platforms only show the interval inside the plot, beside the
    # countdown to the next candle.
    inside_timeframe: bool = False
    # A bare interval-looking badge elsewhere on screen. On Pocket Option
    # this is the visible-range selector, not the candle interval.
    range_badge: str = ""


def render_platform_screen(
    series: Series,
    style: ScreenStyle | None = None,
    chart_style: RenderStyle | None = None,
) -> tuple[np.ndarray, dict[str, tuple[int, int, int, int]]]:
    """Draw a whole trading-platform screen around a chart.

    Returns the image plus the ground-truth boxes for the chart plot, the pair
    label and the timeframe badge, so a detector can be scored against them.
    """
    if cv2 is None:  # pragma: no cover
        raise RuntimeError("rendering requires OpenCV")

    style = style or ScreenStyle()
    image = np.full((style.height, style.width, 3), style.background, dtype=np.uint8)

    # Sidebar, header and the right-hand trade panel.
    cv2.rectangle(image, (0, 0), (style.sidebar_width, style.height), style.chrome, -1)
    cv2.rectangle(image, (0, 0), (style.width, style.header_height), style.chrome, -1)
    cv2.rectangle(
        image,
        (style.width - style.panel_width, style.header_height),
        (style.width, style.height),
        style.chrome,
        -1,
    )

    if style.draw_buttons:
        # The two blocks of saturated colour that a naive detector eats.
        cv2.rectangle(
            image,
            (style.width - style.panel_width + 20, 150),
            (style.width - 20, 210),
            (110, 200, 80),
            -1,
        )
        cv2.rectangle(
            image,
            (style.width - style.panel_width + 20, 230),
            (style.width - 20, 290),
            (70, 70, 225),
            -1,
        )
        # A green balance in the header, and a couple of coloured sidebar icons.
        cv2.putText(
            image, "58,810.52", (style.width - 210, 40),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (110, 200, 80), 2, cv2.LINE_AA,
        )
        for i, y in enumerate((140, 210, 280)):
            cv2.circle(image, (style.sidebar_width // 2, y), 12, (110, 200, 80), -1)

    # Sidebar menu items — the text a symbol search must not mistake for one.
    for i, label in enumerate(style.sidebar_labels):
        cv2.putText(
            image, label, (6, 130 + i * 70),
            cv2.FONT_HERSHEY_SIMPLEX, 0.4, style.text, 1, cv2.LINE_AA,
        )

    # The chart itself, rendered into its own image and pasted in.
    plot_left = style.sidebar_width + 30
    plot_top = style.header_height + 70
    plot_right = style.width - style.panel_width - 20
    plot_bottom = style.height - style.footer_height - 20
    plot_height = plot_bottom - plot_top

    chart_style = chart_style or RenderStyle()
    chart, _ = render_series(series, height=plot_height, style=chart_style)
    chart_width = min(chart.shape[1], plot_right - plot_left)
    # Keep the right-hand edge — that is where the price axis is drawn.
    chart = chart[:, chart.shape[1] - chart_width :]
    image[plot_top : plot_top + plot_height, plot_left : plot_left + chart_width] = chart

    # Gridlines over the plot, in the platform's own tint — a shade of the
    # background rather than a candle colour. A candle-coloured overlay is a
    # real hazard, but it is the SuperTrend case and gets its own fixture.
    for i in range(1, 5):
        y = plot_top + plot_height * i // 5
        cv2.line(image, (plot_left, y), (plot_left + chart_width, y), (146, 74, 92), 1)

    # The pair name above the plot, and the timeframe badge below it.
    asset_origin = (plot_left + 10, style.header_height + 44)
    cv2.putText(
        image, style.asset_label, asset_origin,
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, style.text, 2, cv2.LINE_AA,
    )
    (asset_w, asset_h), _ = cv2.getTextSize(
        style.asset_label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2
    )

    if style.range_badge:
        # Bottom-left corner, where the range selector sits.
        cv2.putText(
            image, style.range_badge, (plot_left + 4, style.height - 12),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, style.text, 2, cv2.LINE_AA,
        )

    if style.inside_timeframe:
        # Beside the countdown to the next candle, inside the plot.
        tf_text = f"{style.timeframe_label} 00:18"
        tf_origin = (plot_left + 12, plot_top + 26)
    else:
        tf_text = style.timeframe_label
        tf_origin = (plot_left + 6, plot_bottom + 28)
    cv2.putText(
        image, tf_text, tf_origin,
        cv2.FONT_HERSHEY_SIMPLEX, 0.6, style.text, 2, cv2.LINE_AA,
    )
    (tf_w, tf_h), _ = cv2.getTextSize(tf_text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)

    truth = {
        "chart": (plot_left, plot_top, plot_left + chart_width, plot_bottom),
        "asset": (
            asset_origin[0], asset_origin[1] - asset_h,
            asset_origin[0] + asset_w, asset_origin[1],
        ),
        "timeframe": (
            tf_origin[0], tf_origin[1] - tf_h, tf_origin[0] + tf_w, tf_origin[1],
        ),
    }
    return image, truth


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
