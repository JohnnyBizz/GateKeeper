"""Drawn graphics for the overlay.

Tkinter's own widgets are why the panel looked its age: square corners, flat
fills, aliased lines, and no way to draw a curve that is not a staircase. None
of that is a limit of Tkinter so much as of its *widgets* — an image is a
widget it will happily display, and Pillow can draw anything.

So everything with a shape to it is rendered here as an image and handed to the
panel to blit. Pillow is already in the bundle (pytesseract depends on it), so
this costs nothing at install time.

Three things make the results look drawn rather than plotted:

*Supersampling.* Every shape is drawn at :data:`SCALE` times its final size and
reduced with a Lanczos filter, which is what turns a stepped diagonal into a
clean edge. Pillow has no anti-aliased primitives; this is the standard way
around that, and at these sizes the cost is microseconds.

*One palette, with depth.* Surfaces are separated by luminance rather than by
outline, so the panel reads as layers instead of as boxes inside boxes. Since
the 2026-09 redraw every surface also carries the two cues a flat render
cannot: a drop shadow beneath it and a hairline of light along its top edge,
which is what makes a card sit *on* the backdrop rather than be printed on it.

*Light.* Colour that bleeds past its shape — the halo under a live card, the
glow under the gauge's arc, the lit tip of a ring — is what reads as alive.
Every halo is a blurred copy of the shape it belongs to, so it survives being
animated and never has to be drawn twice.

This module imports no GUI toolkit and returns plain images, so every shape in
the overlay can be rendered and inspected in a test with no display attached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter

# How much larger everything is drawn before being reduced. Four is the point
# where a diagonal stops looking stepped; eight costs four times the pixels to
# fix something nobody can see.
SCALE = 4

# How far a halo bleeds past the shape that casts it, and how far a drop
# shadow does. Exported so the panel can offset a padded image by exactly the
# amount the renderer padded it — one number, not two guesses.
GLOW_PAD = 12
SHADOW_PAD = 10
DOT_PAD = 8


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


def lighten(color: str, amount: float) -> str:
    """``color`` moved toward white."""
    return mix(color, "#ffffff", amount)


def darken(color: str, amount: float) -> str:
    """``color`` moved toward black."""
    return mix(color, "#000000", amount)


def _canvas(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (width * SCALE, height * SCALE), (0, 0, 0, 0))
    return image, ImageDraw.Draw(image)


def _reduce(image: Image.Image, width: int, height: int) -> Image.Image:
    return image.resize((width, height), Image.LANCZOS)


def _blurred(layer: Image.Image, radius: float, alpha: float = 1.0) -> Image.Image:
    """A soft copy of a layer — the one primitive behind every halo and shadow."""
    soft = layer.filter(ImageFilter.GaussianBlur(radius))
    if alpha < 1.0:
        scale = max(0.0, alpha)
        soft.putalpha(soft.getchannel("A").point(lambda v: int(v * scale)))
    return soft


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


def _horizontal_gradient(
    width: int, height: int, left: str, right: str
) -> Image.Image:
    """The same ramp, run across the box instead of down it."""
    ramp = Image.new("RGBA", (max(2, width), 1))
    start, end = _hex(left), _hex(right)
    for x in range(ramp.width):
        t = x / (ramp.width - 1)
        ramp.putpixel(
            (x, 0),
            tuple(int(round(start[i] + (end[i] - start[i]) * t)) for i in range(4)),
        )
    return ramp.resize((width, height), Image.BILINEAR)


# ---------------------------------------------------------------------------
# surfaces


def card_padding(glow: str | None, shadow: float = 0.0) -> int:
    """How far a card's image extends past the card itself, on every side.

    A halo and a shadow both need room outside the shape, so the image is
    larger than the card and the panel places it offset by this much. Kept
    here so the renderer and the panel can never disagree about it.
    """
    pad = GLOW_PAD if glow else 0
    if shadow > 0:
        pad = max(pad, SHADOW_PAD)
    return pad


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
    shadow: float = 0.0,
    highlight: bool = False,
    across: bool = False,
) -> Image.Image:
    """A rounded panel surface, optionally lit from within — and, since the
    redraw, optionally resting on the backdrop rather than printed on it.

    The glow is what a flat border cannot do and what makes a live setup read
    as *live*: colour bleeding past the edge of the card rather than stopping
    at it. Drawn as a blurred copy of the same rounded shape underneath, which
    is cheap and, unlike a hard outline, survives being animated.

    ``shadow`` is the blur radius of a drop shadow beneath the card; zero
    draws none, and the image stays exactly the card's size. ``highlight``
    adds a hairline of light along the top edge and a hairline of dark along
    the bottom — the glass rim that separates a raised surface from a flat
    one at a glance.
    """
    pad = card_padding(glow, shadow)
    full_w, full_h = width + pad * 2, height + pad * 2
    base = Image.new("RGBA", (full_w, full_h), (0, 0, 0, 0))

    # The shadow and the halo are blurred, so they are drawn at final size:
    # supersampling a shape that is about to lose its edges is four times
    # the pixels for nothing, and this card is drawn nine times per breath.
    if shadow > 0:
        drop = Image.new("RGBA", (full_w, full_h), (0, 0, 0, 0))
        ImageDraw.Draw(drop).rounded_rectangle(
            [pad + 1, pad + 3, pad + width - 1, pad + height + 2],
            radius=radius, fill=(0, 0, 0, 165),
        )
        base.alpha_composite(_blurred(drop, shadow))

    if glow and glow_strength > 0:
        halo = Image.new("RGBA", (full_w, full_h), (0, 0, 0, 0))
        ImageDraw.Draw(halo).rounded_rectangle(
            [pad + 2, pad + 2, pad + width - 2, pad + height - 2],
            radius=radius,
            fill=_hex(glow, int(150 * max(0.0, min(1.0, glow_strength)))),
        )
        base.alpha_composite(halo.filter(ImageFilter.GaussianBlur(GLOW_PAD / 2.2)))

    shape, draw = _canvas(width, height)
    draw.rounded_rectangle(
        [0, 0, width * SCALE - 1, height * SCALE - 1],
        radius=radius * SCALE,
        fill=(255, 255, 255, 255),
    )
    mask = _reduce(shape, width, height).getchannel("A")

    if fill_to and across:
        body = _horizontal_gradient(width, height, fill, fill_to)
    elif fill_to:
        body = _vertical_gradient(width, height, fill, fill_to)
    else:
        body = Image.new("RGBA", (width, height), _hex(fill))
    # The fill keeps its own alpha inside the rounded mask — a translucent
    # colour makes a glass surface, and what lies under the card (the
    # backdrop's pools of light) shows through it.
    body.putalpha(ImageChops.multiply(body.getchannel("A"), mask))
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

    if highlight and width > radius * 2 + 2 and height > 4:
        rim = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(rim)
        draw.line(
            [(radius, 1), (width - radius - 1, 1)], fill=(255, 255, 255, 34), width=1
        )
        draw.line(
            [(radius, height - 2), (width - radius - 1, height - 2)],
            fill=(0, 0, 0, 80), width=1,
        )
        # Clipped to the card's own shape, so the rim never pokes past a
        # rounded corner.
        rim_mask = mask.point(lambda v: 255 if v > 128 else 0)
        rim.putalpha(ImageChops.multiply(rim.getchannel("A"), rim_mask))
        base.alpha_composite(rim, (pad, pad))

    return base


def backdrop(
    width: int,
    height: int,
    *,
    tint: str = "#6aa8ff",
    accent: str = "#6aa8ff",
    top: str = "#0c1328",
    bottom: str = "#05070e",
    phase: float = 0.0,
    violet: str = "#8b5cf6",
) -> Image.Image:
    """The panel's ground: a deep vertical ramp with two soft pools of light.

    A flat background makes every card a box on a wall. A ground with a
    little weather in it — one pool of the verdict's own colour high on the
    right, one of the accent low on the left, both blurred wide — makes the
    same cards read as objects lit from somewhere. The dot grid underneath
    is almost invisible on purpose: it gives the eye a scale to judge
    distance by, which is what makes the shadows work.

    Cheap to draw once and expensive to draw often, so the panel keeps one
    per height and tint and never redraws it per frame.
    """
    base = _vertical_gradient(width, height, top, bottom)

    # Three pools of light, drifting with ``phase`` (0..1 is one slow
    # orbit) so the ground is never quite still.
    turn = phase * 2.0 * math.pi
    pools = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(pools)

    r = int(width * 0.62)
    cx = width * 0.86 + width * 0.10 * math.sin(turn)
    cy = -r * 0.15 + r * 0.10 * math.cos(turn)
    draw.ellipse([cx - r * 0.65, cy - r * 0.65, cx + r * 0.65, cy + r * 0.65],
                 fill=_hex(tint, 110))

    r2 = int(width * 0.55)
    cx = width * 0.08 - width * 0.08 * math.sin(turn * 0.7)
    cy = height * 0.66 + r2 * 0.12 * math.cos(turn * 0.7 + 1.0)
    draw.ellipse([cx - r2 * 0.65, cy - r2 * 0.65, cx + r2 * 0.65, cy + r2 * 0.65],
                 fill=_hex(accent, 66))

    r3 = int(width * 0.42)
    cx = width * 0.55 + width * 0.12 * math.cos(turn * 0.5)
    cy = height * 0.36 + r3 * 0.16 * math.sin(turn * 0.5)
    draw.ellipse([cx - r3 * 0.6, cy - r3 * 0.6, cx + r3 * 0.6, cy + r3 * 0.6],
                 fill=_hex(violet, 46))

    base.alpha_composite(pools.filter(ImageFilter.GaussianBlur(max(8.0, width * 0.24))))

    grid = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(grid)
    step = 22
    for y in range(11, height, step):
        for x in range(11, width, step):
            draw.point((x, y), fill=(255, 255, 255, 13))
    base.alpha_composite(grid)
    return base


def button(
    width: int,
    height: int,
    *,
    color: str,
    color_to: str | None = None,
    radius: int = 11,
    hover: bool = False,
    glow: str | None = None,
    glow_strength: float = 0.0,
    border: str | None = None,
    shadow: float = 5.0,
    across: bool = False,
) -> Image.Image:
    """A pressable surface: a gradient card with a rim, a shadow, and a
    brighter face while the pointer is over it. ``across`` runs the
    gradient left to right — the brand's blue-to-violet sweep.

    Placed with :func:`card_padding` like any padded card.
    """
    other = color_to or darken(color, 0.18)
    if hover:
        color, other = lighten(color, 0.10), lighten(other, 0.10)
    return card(
        width, height, radius=radius, fill=color, fill_to=other,
        border=border or lighten(color, 0.22), glow=glow,
        glow_strength=glow_strength, shadow=shadow, highlight=True,
        across=across,
    )


def tab(
    width: int,
    height: int,
    *,
    color: str,
    active: bool,
    radius: int = 9,
    glow_strength: float = 0.0,
    hover: bool = False,
) -> Image.Image:
    """A watchlist tile: a small card with a stripe of its colour down the
    left edge, lit from within while it is worth a look.

    Placed with :func:`card_padding` — the stripe is inside the card.
    """
    fill, fill_to = ("#22304cbe", "#151f36a8") if active else ("#151d30a0", "#0f172890")
    if hover:
        # Lighter, but still glass: the alpha rides along.
        fill = lighten(fill[:7], 0.08) + fill[7:]
        fill_to = lighten(fill_to[:7], 0.08) + fill_to[7:]
    glow = color if glow_strength > 0 else None
    base = card(
        width, height, radius=radius, fill=fill, fill_to=fill_to,
        border=color if active else "#ffffff24", glow=glow,
        glow_strength=glow_strength, highlight=True,
    )
    pad = card_padding(glow)
    stripe, draw = _canvas(width, height)
    draw.rounded_rectangle(
        [3 * SCALE, 6 * SCALE, 6 * SCALE, (height - 6) * SCALE],
        radius=int(1.5 * SCALE),
        fill=_hex(color, 235 if active else 120),
    )
    base.alpha_composite(_reduce(stripe, width, height), (pad, pad))
    return base


def shield(size: int, *, color: str = "#6aa8ff", color_to: str = "#2fd06e") -> Image.Image:
    """The mark: a shield with a gate through it, in a gradient of the two
    colours the panel is made of."""
    image, draw = _canvas(size, size)
    s = size * SCALE
    outline = [
        (s * 0.50, s * 0.05), (s * 0.90, s * 0.20), (s * 0.86, s * 0.56),
        (s * 0.50, s * 0.95), (s * 0.14, s * 0.56), (s * 0.10, s * 0.20),
    ]
    draw.polygon(outline, fill=(255, 255, 255, 255))
    mask = image.getchannel("A")
    body = _vertical_gradient(s, s, color, color_to)
    body.putalpha(mask)
    cut = ImageDraw.Draw(body)
    ink = (8, 11, 18, 255)
    cut.rounded_rectangle(
        [s * 0.30, s * 0.46, s * 0.70, s * 0.58], radius=int(s * 0.05), fill=ink
    )
    cut.ellipse([s * 0.42, s * 0.27, s * 0.58, s * 0.43], fill=ink)
    return _reduce(body, size, size)


def glow_dot(size: int, color: str, *, strength: float = 1.0) -> Image.Image:
    """A status dot with a soft halo. The image is ``size + 2 * DOT_PAD``
    square, so the panel offsets it by :data:`DOT_PAD`."""
    full = size + DOT_PAD * 2
    image = Image.new("RGBA", (full, full), (0, 0, 0, 0))
    halo, draw = _canvas(full, full)
    reach = 3
    draw.ellipse(
        [
            (DOT_PAD - reach) * SCALE, (DOT_PAD - reach) * SCALE,
            (DOT_PAD + size + reach) * SCALE, (DOT_PAD + size + reach) * SCALE,
        ],
        fill=_hex(color, int(170 * max(0.0, min(1.0, strength)))),
    )
    image.alpha_composite(_blurred(_reduce(halo, full, full), 3.5))
    core, draw = _canvas(full, full)
    draw.ellipse(
        [DOT_PAD * SCALE, DOT_PAD * SCALE, (DOT_PAD + size) * SCALE, (DOT_PAD + size) * SCALE],
        fill=_hex(color),
    )
    draw.ellipse(
        [
            (DOT_PAD + size * 0.25) * SCALE, (DOT_PAD + size * 0.2) * SCALE,
            (DOT_PAD + size * 0.55) * SCALE, (DOT_PAD + size * 0.5) * SCALE,
        ],
        fill=(255, 255, 255, 110),
    )
    image.alpha_composite(_reduce(core, full, full))
    return image


# ---------------------------------------------------------------------------
# the score gauge


def _arc_point(size: int, inset: int, angle: float) -> tuple[float, float]:
    centre = size * SCALE / 2.0
    radius = centre - inset
    rad = math.radians(angle)
    return centre + radius * math.cos(rad), centre + radius * math.sin(rad)


def arc_gauge(
    size: int,
    value: float | None,
    *,
    color: str = "#22c55e",
    track: str = "#1e293b",
    thickness: int = 7,
    span: float = 260.0,
    glow: bool = True,
    ticks: bool = True,
) -> Image.Image:
    """A dial for the score, open at the bottom.

    A bar says "how full"; a dial says "where on the scale", which is the
    question actually being asked of a number out of a hundred. Open at the
    bottom so the gap reads as the scale's start and end rather than as a
    missing piece.

    The arc is a gradient — dim where the scale starts, the full colour along
    its length, lit at the tip — with a halo beneath it and a bright dot at
    its end, so the eye finds the reading before it finds the number.
    """
    image, draw = _canvas(size, size)
    inset = thickness * SCALE
    box = [inset, inset, size * SCALE - inset, size * SCALE - inset]
    start = 90.0 + (360.0 - span) / 2.0

    draw.arc(box, start, start + span, fill=_hex(track), width=thickness * SCALE)
    if ticks:
        # Drawn in the track's colour: they are part of the scale, not of
        # the reading, and a transparent track takes its ticks with it.
        for share in (0.0, 0.25, 0.5, 0.75, 1.0):
            angle = start + span * share
            outer = _arc_point(size, int(inset * 0.35), angle)
            inner = _arc_point(size, int(inset * 0.62), angle)
            draw.line([inner, outer], fill=_hex(track), width=max(1, SCALE))

    base = _reduce(image, size, size)
    if value is None:
        return base

    share = max(0.0, min(1.0, float(value) / 100.0))
    if share <= 0.005:
        return base

    end = start + span * share
    arc, sweep = _canvas(size, size)
    segments = max(6, int(span * share / 5.0))
    dim, lit = darken(color, 0.42), lighten(color, 0.30)
    for index in range(segments):
        t0, t1 = index / segments, (index + 1) / segments
        t = t1
        tone = mix(dim, color, min(1.0, t * 1.6)) if t < 0.62 else mix(color, lit, (t - 0.62) / 0.38)
        sweep.arc(
            box,
            start + span * share * t0,
            min(end, start + span * share * t1 + 1.2),
            fill=_hex(tone),
            width=thickness * SCALE,
        )
    reduced = _reduce(arc, size, size)
    if glow:
        base.alpha_composite(_blurred(reduced, thickness * 0.9, 0.62))
    base.alpha_composite(reduced)

    tip, draw = _canvas(size, size)
    x, y = _arc_point(size, inset, end)
    r = thickness * SCALE * 0.62
    draw.ellipse([x - r, y - r, x + r, y + r], fill=_hex(lighten(color, 0.55)))
    tip_reduced = _reduce(tip, size, size)
    if glow:
        base.alpha_composite(_blurred(tip_reduced, 2.5, 0.9))
    base.alpha_composite(tip_reduced)
    return base


def countdown_ring(
    size: int,
    remaining: float,
    *,
    color: str = "#60a5fa",
    track: str = "#1e293b",
    thickness: int = 4,
    tip: bool = False,
) -> Image.Image:
    """A full circle that drains clockwise from the top as a candle runs out.

    ``remaining`` is the share of the bar still to go, so a full ring is a bar
    that has just opened. Reading the time left off a shrinking arc takes no
    reading at all, which is the point when the number it replaces is changing
    every second. ``tip`` lights the leading end.
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
        if tip:
            x, y = _arc_point(size, inset, -90.0 + 360.0 * share)
            r = thickness * SCALE * 0.55
            draw.ellipse([x - r, y - r, x + r, y + r], fill=_hex(lighten(color, 0.5)))
    return _reduce(image, size, size)


def spinner(
    size: int,
    phase: float,
    *,
    color: str = "#6aa8ff",
    track: str = "#1e293b",
    thickness: int = 5,
) -> Image.Image:
    """A sweeping arc for the scanning state — a tail that fades behind a
    lit head, turning with ``phase`` (0..1 is one revolution)."""
    image, draw = _canvas(size, size)
    inset = thickness * SCALE
    box = [inset, inset, size * SCALE - inset, size * SCALE - inset]
    draw.ellipse(box, outline=_hex(track), width=thickness * SCALE)
    head = (phase % 1.0) * 360.0 - 90.0
    sweep = 240.0
    pieces = 16
    for index in range(pieces):
        t = index / (pieces - 1)
        a0 = head - sweep + sweep * index / pieces
        a1 = head - sweep + sweep * (index + 1) / pieces + 1.5
        draw.arc(box, a0, a1, fill=_hex(color, int(20 + 235 * t)), width=thickness * SCALE)
    x, y = _arc_point(size, inset, head)
    r = thickness * SCALE * 0.6
    draw.ellipse([x - r, y - r, x + r, y + r], fill=_hex(lighten(color, 0.5)))
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
    """The recent closes as a lit line over a fading fill.

    The panel scored a market it never showed. A number saying 78 and an arrow
    saying up are a claim; the shape of the last hour is the thing a trader
    reads in one glance to decide whether the claim is plausible.
    """
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    points = [float(c) for c in closes if c == c]
    if len(points) < 2:
        return image

    low, high = _bounds(points)
    step = (width * SCALE) / (len(points) - 1)

    def place(index: int, value: float) -> tuple[float, float]:
        y = (value - low) / (high - low)
        return index * step, (height * SCALE) * (1.0 - y)

    line = [place(i, value) for i, value in enumerate(points)]

    if fill:
        under, draw = _canvas(width, height)
        draw.polygon(
            line + [(width * SCALE, height * SCALE), (0, height * SCALE)],
            fill=(255, 255, 255, 255),
        )
        under_mask = _reduce(under, width, height).getchannel("A")
        fade = _vertical_gradient(width, height, color + "6e", color + "00")
        fade.putalpha(ImageChops.multiply(fade.getchannel("A"), under_mask))
        image.alpha_composite(fade)

    stroke, draw = _canvas(width, height)
    draw.line(line, fill=_hex(color), width=2 * SCALE, joint="curve")
    stroke_reduced = _reduce(stroke, width, height)
    image.alpha_composite(_blurred(stroke_reduced, 2.2, 0.7))
    image.alpha_composite(stroke_reduced)

    # The live end, marked. Which end is "now" is not obvious on a line that
    # has no axis.
    dot, draw = _canvas(width, height)
    x, y = line[-1]
    r = 3 * SCALE
    draw.ellipse([x - r, y - r, x + r, y + r], fill=_hex(lighten(color, 0.35)))
    dot_reduced = _reduce(dot, width, height)
    image.alpha_composite(_blurred(dot_reduced, 3.0, 0.9))
    image.alpha_composite(dot_reduced)
    return image


def candles(
    width: int,
    height: int,
    bars: Sequence[Bar],
    *,
    up: str = "#22c55e",
    down: str = "#f43f5e",
    max_bars: int = 34,
) -> Image.Image:
    """Real candles, wicks and all, over a faint ruled ground.

    A sparkline says where price went; candles say how it got there, which is
    what every pattern the engine names is actually about. Showing the shapes
    the analysis is reading is the difference between being told a verdict and
    being able to check it.
    """
    image, draw = _canvas(width, height)
    window = list(bars)[-max_bars:]
    if not window:
        return _reduce(image, width, height)

    for share in (0.25, 0.5, 0.75):
        y = height * SCALE * share
        draw.line([(0, y), (width * SCALE, y)], fill=(255, 255, 255, 22), width=SCALE)

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
        draw.rounded_rectangle(
            [max(0, end - height * SCALE * 1.5), 0, end, height * SCALE - 1],
            radius=radius, fill=_hex(lighten(color, 0.3)),
        )
    return _reduce(image, width, height)


def direction_glyph(
    size: int, direction: str, color: str, *, glow: bool = False
) -> Image.Image:
    """The verdict's arrow, drawn rather than typed.

    A font's ``▲`` is whatever the font decides, differs between machines, and
    cannot be given a weight or a soft corner. This one is the same everywhere.
    With ``glow`` it casts a little of its colour around itself.
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
    shape = _reduce(image, size, size)
    if not glow:
        return shape
    lit = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    lit.alpha_composite(_blurred(shape, 2.6, 0.75))
    lit.alpha_composite(shape)
    return lit


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
