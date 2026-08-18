"""Drawn graphics for the overlay.

Tkinter's own widgets are why the panel looked its age: square corners, flat
fills, aliased lines, and no way to draw a curve that is not a staircase. None
of that is a limit of Tkinter so much as of its *widgets* — an image is a
widget it will happily display, and Pillow can draw anything.

So everything with a shape to it is rendered here as an image and handed to the
panel to blit. Pillow is already in the bundle (pytesseract depends on it), so
this costs nothing at install time.

Two things make the results look drawn rather than plotted:

*Supersampling.* Every shape is drawn at :data:`SCALE` times its final size and
reduced with a Lanczos filter, which is what turns a stepped diagonal into a
clean edge. Pillow has no anti-aliased primitives; this is the standard way
around that, and at these sizes the cost is microseconds.

*One palette, with depth.* Surfaces are separated by luminance rather than by
outline, so the panel reads as layers instead of as boxes inside boxes.

This module imports no GUI toolkit and returns plain images, so every shape in
the overlay can be rendered and inspected in a test with no display attached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFilter

# How much larger everything is drawn before being reduced. Four is the point
# where a diagonal stops looking stepped; eight costs four times the pixels to
# fix something nobody can see.
SCALE = 4


def _hex(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """``#rrggbb`` to RGBA. Also accepts ``#rrggbbaa``."""
    text = color.lstrip("#")
    if len(text) == 8:
        r, g, b, a = (int(text[i : i + 2], 16) for i in (0, 2, 4, 6))
        return r, g, b, a
    r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    return r, g, b, alpha


def mix(first: str, second: str, amount: float) -> str:
    """Blend two ``#rrggbb`` colours, ``amount`` of the way to the second."""
    amount = max(0.0, min(1.0, amount))
    a, b = _hex(first), _hex(second)
    return "#{:02x}{:02x}{:02x}".format(
        *(int(round(a[i] + (b[i] - a[i]) * amount)) for i in range(3))
    )


def _canvas(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (width * SCALE, height * SCALE), (0, 0, 0, 0))
    return image, ImageDraw.Draw(image)


def _reduce(image: Image.Image, width: int, height: int) -> Image.Image:
    return image.resize((width, height), Image.LANCZOS)


def _vertical_gradient(
    width: int, height: int, top: str, bottom: str
) -> Image.Image:
    """A one-pixel-wide ramp stretched across the box.

    Drawing a gradient line by line at supersampled size is thousands of
    operations for something the resize does better: build the ramp at its
    natural resolution, then let the filter interpolate it.
    """
    ramp = Image.new("RGBA", (1, max(2, height)))
    start, end = _hex(top), _hex(bottom)
    for y in range(ramp.height):
        t = y / (ramp.height - 1)
        ramp.putpixel(
            (0, y),
            tuple(int(round(start[i] + (end[i] - start[i]) * t)) for i in range(4)),
        )
    return ramp.resize((width, height), Image.BILINEAR)


# ---------------------------------------------------------------------------
# surfaces


def card(
    width: int,
    height: int,
    *,
    radius: int = 10,
    fill: str = "#141c2b",
    fill_to: str | None = None,
    border: str | None = None,
    border_width: int = 1,
    glow: str | None = None,
    glow_strength: float = 1.0,
) -> Image.Image:
    """A rounded panel surface, optionally lit from within.

    The glow is what a flat border cannot do and what makes a live setup read
    as *live*: colour bleeding past the edge of the card rather than stopping
    at it. Drawn as a blurred copy of the same rounded shape underneath, which
    is cheap and, unlike a hard outline, survives being animated.
    """
    pad = 12 if glow else 0
    full_w, full_h = width + pad * 2, height + pad * 2
    base = Image.new("RGBA", (full_w, full_h), (0, 0, 0, 0))

    if glow and glow_strength > 0:
        halo, draw = _canvas(full_w, full_h)
        draw.rounded_rectangle(
            [
                (pad + 2) * SCALE,
                (pad + 2) * SCALE,
                (pad + width - 2) * SCALE,
                (pad + height - 2) * SCALE,
            ],
            radius=radius * SCALE,
            fill=_hex(glow, int(150 * max(0.0, min(1.0, glow_strength)))),
        )
        halo = _reduce(halo, full_w, full_h).filter(ImageFilter.GaussianBlur(pad / 2.2))
        base.alpha_composite(halo)

    shape, draw = _canvas(width, height)
    draw.rounded_rectangle(
        [0, 0, width * SCALE - 1, height * SCALE - 1],
        radius=radius * SCALE,
        fill=(255, 255, 255, 255),
    )
    mask = _reduce(shape, width, height).getchannel("A")

    body = (
        _vertical_gradient(width, height, fill, fill_to)
        if fill_to
        else Image.new("RGBA", (width, height), _hex(fill))
    )
    body.putalpha(mask)
    base.alpha_composite(body, (pad, pad))

    if border:
        edge, draw = _canvas(width, height)
        draw.rounded_rectangle(
            [0, 0, width * SCALE - 1, height * SCALE - 1],
            radius=radius * SCALE,
            outline=_hex(border),
            width=max(1, border_width * SCALE),
        )
        base.alpha_composite(_reduce(edge, width, height), (pad, pad))

    return base


# ---------------------------------------------------------------------------
# the score gauge


def arc_gauge(
    size: int,
    value: float | None,
    *,
    color: str = "#22c55e",
    track: str = "#1e293b",
    thickness: int = 7,
    span: float = 260.0,
) -> Image.Image:
    """A dial for the score, open at the bottom.

    A bar says "how full"; a dial says "where on the scale", which is the
    question actually being asked of a number out of a hundred. Open at the
    bottom so the gap reads as the scale's start and end rather than as a
    missing piece.
    """
    image, draw = _canvas(size, size)
    inset = thickness * SCALE
    box = [inset, inset, size * SCALE - inset, size * SCALE - inset]
    start = 90.0 + (360.0 - span) / 2.0

    draw.arc(box, start, start + span, fill=_hex(track), width=thickness * SCALE)
    if value is not None:
        share = max(0.0, min(1.0, float(value) / 100.0))
        if share > 0.005:
            draw.arc(
                box,
                start,
                start + span * share,
                fill=_hex(color),
                width=thickness * SCALE,
            )
    return _reduce(image, size, size)


def countdown_ring(
    size: int,
    remaining: float,
    *,
    color: str = "#60a5fa",
    track: str = "#1e293b",
    thickness: int = 4,
) -> Image.Image:
    """A full circle that drains clockwise from the top as a candle runs out.

    ``remaining`` is the share of the bar still to go, so a full ring is a bar
    that has just opened. Reading the time left off a shrinking arc takes no
    reading at all, which is the point when the number it replaces is changing
    every second.
    """
    image, draw = _canvas(size, size)
    inset = thickness * SCALE
    box = [inset, inset, size * SCALE - inset, size * SCALE - inset]
    draw.ellipse(box, outline=_hex(track), width=thickness * SCALE)
    share = max(0.0, min(1.0, float(remaining)))
    if share > 0.005:
        draw.arc(
            box, -90.0, -90.0 + 360.0 * share, fill=_hex(color),
            width=thickness * SCALE,
        )
    return _reduce(image, size, size)


# ---------------------------------------------------------------------------
# the chart


@dataclass(frozen=True)
class Bar:
    """One candle, reduced to what drawing it needs."""

    open: float
    high: float
    low: float
    close: float


def _bounds(values: Iterable[float]) -> tuple[float, float]:
    lows = list(values)
    low, high = min(lows), max(lows)
    if high - low < 1e-12:
        # A flat window still has to occupy the box rather than divide by zero.
        return low - 0.0005, high + 0.0005
    pad = (high - low) * 0.12
    return low - pad, high + pad


def sparkline(
    width: int,
    height: int,
    closes: Sequence[float],
    *,
    color: str = "#22c55e",
    fill: bool = True,
) -> Image.Image:
    """The recent closes as a filled line.

    The panel scored a market it never showed. A number saying 78 and an arrow
    saying up are a claim; the shape of the last hour is the thing a trader
    reads in one glance to decide whether the claim is plausible.
    """
    image, draw = _canvas(width, height)
    points = [float(c) for c in closes if c == c]
    if len(points) < 2:
        return _reduce(image, width, height)

    low, high = _bounds(points)
    step = (width * SCALE) / (len(points) - 1)

    def place(index: int, value: float) -> tuple[float, float]:
        y = (value - low) / (high - low)
        return index * step, (height * SCALE) * (1.0 - y)

    line = [place(i, value) for i, value in enumerate(points)]

    if fill:
        under = line + [(width * SCALE, height * SCALE), (0, height * SCALE)]
        draw.polygon(under, fill=_hex(color, 46))

    draw.line(line, fill=_hex(color), width=2 * SCALE, joint="curve")
    # The live end, marked. Which end is "now" is not obvious on a line that
    # has no axis.
    x, y = line[-1]
    r = 3 * SCALE
    draw.ellipse([x - r, y - r, x + r, y + r], fill=_hex(color))
    return _reduce(image, width, height)


def candles(
    width: int,
    height: int,
    bars: Sequence[Bar],
    *,
    up: str = "#22c55e",
    down: str = "#f43f5e",
    max_bars: int = 34,
) -> Image.Image:
    """Real candles, wicks and all.

    A sparkline says where price went; candles say how it got there, which is
    what every pattern the engine names is actually about. Showing the shapes
    the analysis is reading is the difference between being told a verdict and
    being able to check it.
    """
    image, draw = _canvas(width, height)
    window = list(bars)[-max_bars:]
    if not window:
        return _reduce(image, width, height)

    low, high = _bounds([v for bar in window for v in (bar.high, bar.low)])
    slot = (width * SCALE) / len(window)
    body_w = max(SCALE, slot * 0.62)

    def y_of(value: float) -> float:
        return (height * SCALE) * (1.0 - (value - low) / (high - low))

    for index, bar in enumerate(window):
        centre = slot * (index + 0.5)
        rising = bar.close >= bar.open
        color = _hex(up if rising else down)
        draw.line(
            [(centre, y_of(bar.high)), (centre, y_of(bar.low))],
            fill=color,
            width=max(SCALE, int(SCALE * 1.2)),
        )
        top, bottom = y_of(max(bar.open, bar.close)), y_of(min(bar.open, bar.close))
        if bottom - top < SCALE:  # a doji still needs a visible body
            middle = (top + bottom) / 2
            top, bottom = middle - SCALE / 2, middle + SCALE / 2
        draw.rounded_rectangle(
            [centre - body_w / 2, top, centre + body_w / 2, bottom],
            radius=max(1, int(SCALE * 0.6)),
            fill=color,
        )
    return _reduce(image, width, height)


# ---------------------------------------------------------------------------
# small ornaments


def pill(
    text_width: int,
    height: int,
    *,
    color: str = "#22c55e",
    opacity: int = 38,
) -> Image.Image:
    """The tinted lozenge behind a badge. Colour without shouting."""
    image, draw = _canvas(text_width, height)
    draw.rounded_rectangle(
        [0, 0, text_width * SCALE - 1, height * SCALE - 1],
        radius=(height * SCALE) // 2,
        fill=_hex(color, opacity),
        outline=_hex(color, 90),
        width=SCALE,
    )
    return _reduce(image, text_width, height)


def bar_meter(
    width: int,
    height: int,
    value: float,
    *,
    color: str = "#22c55e",
    track: str = "#1e293b",
) -> Image.Image:
    """A rounded progress bar, for the things a dial would over-dignify."""
    image, draw = _canvas(width, height)
    radius = (height * SCALE) // 2
    draw.rounded_rectangle(
        [0, 0, width * SCALE - 1, height * SCALE - 1], radius=radius, fill=_hex(track)
    )
    share = max(0.0, min(1.0, float(value)))
    if share > 0:
        end = max(height * SCALE, width * SCALE * share)
        draw.rounded_rectangle([0, 0, end, height * SCALE - 1], radius=radius,
                               fill=_hex(color))
    return _reduce(image, width, height)


def direction_glyph(size: int, direction: str, color: str) -> Image.Image:
    """The verdict's arrow, drawn rather than typed.

    A font's ``▲`` is whatever the font decides, differs between machines, and
    cannot be given a weight or a soft corner. This one is the same everywhere.
    """
    image, draw = _canvas(size, size)
    s = size * SCALE
    if direction in ("CALL", "PUT"):
        rising = direction == "CALL"
        tip, base = (s * 0.16, s * 0.74) if rising else (s * 0.84, s * 0.26)
        draw.polygon(
            [(s * 0.5, tip), (s * 0.88, base), (s * 0.12, base)],
            fill=_hex(color),
        )
    elif direction == "NO_TRADE":
        for a, b in (((0.22, 0.22), (0.78, 0.78)), ((0.78, 0.22), (0.22, 0.78))):
            draw.line([(s * a[0], s * a[1]), (s * b[0], s * b[1])],
                      fill=_hex(color), width=int(s * 0.11))
    else:
        # A disc for "no side taken". A flat bar reads as a stray hyphen next
        # to a word the size of the verdict.
        draw.ellipse([s * 0.32, s * 0.32, s * 0.68, s * 0.68], fill=_hex(color))
    return _reduce(image, size, size)


def shimmer(width: int, height: int, phase: float, *, color: str = "#60a5fa") -> Image.Image:
    """A band of light travelling across a bar, for the scanning state.

    Three cycling dots said "busy" the way a 2005 dialog did. A sweep says the
    same thing without asking to be decoded.
    """
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    band = max(8, width // 4)
    centre = (phase % 1.0) * (width + band * 2) - band
    for x in range(width):
        distance = abs(x - centre) / band
        if distance >= 1.0:
            continue
        alpha = int(200 * math.cos(distance * math.pi / 2) ** 2)
        draw.line([(x, 0), (x, height)], fill=_hex(color, alpha))
    return image
