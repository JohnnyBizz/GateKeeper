"""Finding the chart, its axis and its labels without being told where they are.

Asking the user to drag three boxes was the wrong design. Every box is a chance
to include a button, clip the axis, or point at the wrong badge, and every one
of those mistakes surfaces later as a confident, wrong number. This module
removes the question: point at the screen and it works out where the chart is.

The whole thing rests on one property that candles have and interface chrome
does not — **rhythm**. Candles are drawn at a fixed pitch, dozens in a row, all
the same width. A BUY button, a coloured icon, a balance in green text and a
progress bar are all candle-coloured too, but none of them repeats forty times
at a constant spacing. So the detector does not look for "green things"; it
looks for the longest evenly-spaced run of green and red things, which is the
candle field and nothing else.

Once the candle field is known, everything else is positional: the price axis
is the numeric column immediately to its right, the pair name is the symbol-like
text above it, and the timeframe badge is the short M1/H3-like token nearest to
it. Each is confirmed by parsing, so a label is adopted only when it reads as
the thing it is supposed to be.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from ..logging_setup import get_logger
from .asset_label import normalise
from .candles import ColorProfile, build_masks
from .label_reader import find_text_boxes, ocr_crop
from .timeframe_label import parse_timeframe, snap_to_known

log = get_logger(__name__)

try:  # pragma: no cover - optional
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

try:  # pragma: no cover - optional
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None  # type: ignore[assignment]


# A candle field needs at least this many evenly-spaced members before it is
# believed. Well below the 60 candles the engine wants, so the detector still
# finds a sparse chart and the data-quality check does the complaining.
MIN_RUN = 18

# How far a gap may stray from a whole number of pitches and still belong to
# the run. A doji next to a wide-bodied candle shifts the centre a little.
PITCH_TOLERANCE = 0.35

# A gap may span this many pitches and still continue the run. Candles do go
# missing — a flat one is a single row of pixels that a gridline swallows — and
# ending the run at every one of those would chop a chart into fragments.
MAX_PITCH_SKIP = 4

# A candle is narrow. Anything wider than this fraction of the screen is a
# panel, a button or a filled background, whatever colour it is.
MAX_COMPONENT_WIDTH_FRACTION = 0.04

# How much of the screen a single candle-coloured blob may cover before it is
# obviously interface rather than a candle.
MAX_COMPONENT_AREA_FRACTION = 0.01

# Width of the price-axis strip kept to the right of the candles when OCR
# cannot measure it directly.
DEFAULT_AXIS_WIDTH = 96


def ocr_available() -> bool:
    """Whether text can be read from the screen at all.

    ``pytesseract`` is only a wrapper; the actual work is done by a separate
    Tesseract binary that may not be installed. Importing the module tells you
    nothing, so ask it to do something.
    """
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return False
    return True


@dataclass
class Box:
    """A screen rectangle, in the coordinate space of the image it came from."""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def offset(self, dx: int, dy: int) -> "Box":
        return Box(self.left + dx, self.top + dy, self.width, self.height)

    def clip(self, width: int, height: int) -> "Box":
        left = max(0, min(self.left, width - 1))
        top = max(0, min(self.top, height - 1))
        return Box(
            left,
            top,
            max(0, min(self.width, width - left)),
            max(0, min(self.height, height - top)),
        )

    def overlap(self, other: "Box") -> int:
        dx = min(self.right, other.right) - max(self.left, other.left)
        dy = min(self.bottom, other.bottom) - max(self.top, other.top)
        return dx * dy if dx > 0 and dy > 0 else 0

    def to_dict(self) -> dict[str, int]:
        return {
            "left": int(self.left),
            "top": int(self.top),
            "width": int(self.width),
            "height": int(self.height),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Box | None":
        if not raw:
            return None
        try:
            box = cls(
                int(raw.get("left", 0)),
                int(raw.get("top", 0)),
                int(raw.get("width", 0)),
                int(raw.get("height", 0)),
            )
        except (TypeError, ValueError):
            return None
        return box if box.width > 0 and box.height > 0 else None


@dataclass
class Layout:
    """Everything the detector found, and how sure it is."""

    chart: Box | None = None
    asset: Box | None = None
    timeframe: Box | None = None
    asset_name: str | None = None
    timeframe_seconds: int | None = None
    candles_found: int = 0
    pitch: float = 0.0
    confidence: float = 0.0
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.chart is not None and self.candles_found >= MIN_RUN

    def to_dict(self) -> dict[str, Any]:
        return {
            "chart": self.chart.to_dict() if self.chart else None,
            "asset": self.asset.to_dict() if self.asset else None,
            "timeframe": self.timeframe.to_dict() if self.timeframe else None,
            "asset_name": self.asset_name,
            "timeframe_seconds": self.timeframe_seconds,
            "candles_found": self.candles_found,
            "pitch": round(self.pitch, 2),
            "confidence": round(self.confidence, 1),
            "issues": list(self.issues),
            "ok": self.ok,
        }


# --------------------------------------------------------------------------
# Step 1: the candle field


@dataclass
class _Component:
    """One candle-coloured blob, reduced to what the rhythm test needs."""

    x: float
    left: int
    top: int
    right: int
    bottom: int
    width: int


def _strip_wide_runs(mask: np.ndarray, image_width: int) -> np.ndarray:
    """Erase every pixel that belongs to a long horizontal run.

    Two things on a trading screen are candle-coloured and horizontally
    continuous: gridlines and indicator overlays, which fuse neighbouring
    candles into one unusable blob, and the platform's BUY/SELL buttons, which
    are solid slabs of exactly the same green and red. One opening removes
    both, because neither a candle body nor a wick is anywhere near this wide.

    The chart pipeline's own stripper is not used here. It backs off when the
    runs it finds dominate the mask — a sensible guard on a cropped chart, and
    exactly wrong on a whole screen, where the buttons dominate by design and
    trip the guard every single time.
    """
    min_run = max(31, image_width // 40)
    horizontal = cv2.getStructuringElement(cv2.MORPH_RECT, (min_run, 1))
    cleaned = cv2.subtract(mask, cv2.morphologyEx(mask, cv2.MORPH_OPEN, horizontal))
    # A removed line leaves a one-pixel notch across every candle it crossed;
    # close vertically to heal them back into single blobs.
    heal = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5))
    return cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, heal)


def _drop_oversized(mask: np.ndarray, width: int, height: int) -> np.ndarray:
    """Blank out blobs far too big to be a candle — buttons, banners, fills."""
    max_width = max(4, int(width * MAX_COMPONENT_WIDTH_FRACTION))
    max_area = max(64, int(width * height * MAX_COMPONENT_AREA_FRACTION))

    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    cleaned = mask.copy()
    for index in range(1, count):
        blob_width, blob_height, area = (
            int(stats[index][2]),
            int(stats[index][3]),
            int(stats[index][4]),
        )
        # A gridline is wide but paper thin; it is the stripper's job, not this
        # one's, and removing it here would take the candles it crosses too.
        if blob_height <= 2:
            continue
        if blob_width > max_width or area > max_area:
            cleaned[labels == index] = 0
    return cleaned


def _candidate_components(
    mask: np.ndarray, image_width: int, image_height: int
) -> list[_Component]:
    """Blobs small enough to be a candle rather than a button or a panel."""
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    max_width = max(4, int(image_width * MAX_COMPONENT_WIDTH_FRACTION))
    max_area = max(64, int(image_width * image_height * MAX_COMPONENT_AREA_FRACTION))

    components: list[_Component] = []
    for index in range(1, count):  # 0 is the background
        left, top, width, height, area = (int(v) for v in stats[index])
        if width > max_width or area > max_area:
            continue
        if area < 3:
            continue
        # A candle is at least as tall as it is wide once its wick is attached.
        # A flat dash of the same width is a gridline fragment or an underline.
        if height < 2:
            continue
        components.append(
            _Component(
                x=float(centroids[index][0]),
                left=left,
                top=top,
                right=left + width,
                bottom=top + height,
                width=width,
            )
        )
    return components


def _merge_by_column(components: list[_Component], tolerance: float) -> list[_Component]:
    """Fuse blobs that share a column — a body and its wick, split by a gap."""
    if not components:
        return []
    components = sorted(components, key=lambda c: c.x)
    merged: list[_Component] = [components[0]]
    for item in components[1:]:
        last = merged[-1]
        if abs(item.x - last.x) <= tolerance:
            merged[-1] = _Component(
                x=(last.x + item.x) / 2.0,
                left=min(last.left, item.left),
                top=min(last.top, item.top),
                right=max(last.right, item.right),
                bottom=max(last.bottom, item.bottom),
                width=max(last.width, item.width),
            )
        else:
            merged.append(item)
    return merged


def _dominant_pitch(components: list[_Component]) -> float:
    """The spacing that repeats most often between neighbouring blobs.

    A plain median over all gaps is dragged upwards by the empty stretches
    between interface elements, so the estimate is taken from the lower part of
    the distribution, where a dense candle field lives, and then refined by
    voting so a handful of half-pitch gaps cannot bias it.
    """
    ordered = sorted(components, key=lambda c: c.x)
    gaps = [ordered[i + 1].x - ordered[i].x for i in range(len(ordered) - 1)]
    positive = sorted(g for g in gaps if g > 0.5)
    if not positive:
        return 0.0

    seed = float(np.median(positive[: max(1, len(positive) * 3 // 4)]))
    if seed <= 0:
        return 0.0
    # Refine: average every gap that is close to the seed, which sharpens a
    # median that landed between two integer pixel spacings.
    close = [g for g in positive if abs(g - seed) <= seed * 0.25]
    return float(np.mean(close)) if close else seed


def _longest_rhythmic_run(components: list[_Component]) -> list[_Component]:
    """The longest stretch of blobs sitting on one evenly-spaced lattice.

    This is the heart of the detector. Interface elements are coloured like
    candles but never *repeat* like candles, so requiring a consistent pitch
    over many members separates the chart from everything around it without
    knowing anything about the platform's layout.

    The test is made against a single global lattice rather than against each
    neighbour in turn. Neighbour-to-neighbour is what a first version does, and
    it snaps at the first irregular pair — a candle drawn as a bare wick sits a
    few pixels off its body's centre, which reads as a broken rhythm and chops
    the chart into fragments. A lattice absorbs that: the wick still lands in
    its own slot, and the field survives intact.
    """
    if len(components) < MIN_RUN:
        return []

    ordered = sorted(components, key=lambda c: c.x)
    pitch = _dominant_pitch(ordered)
    if pitch <= 0:
        return []

    # Phase: where the lattice sits. Each component votes with its offset
    # within one pitch, and the densest cluster of votes wins. A circular mean
    # is used because the offsets wrap around at the pitch boundary.
    offsets = np.array([c.x for c in ordered], dtype=np.float64) % pitch
    angles = offsets / pitch * 2.0 * np.pi
    phase = float(
        (np.arctan2(np.sin(angles).mean(), np.cos(angles).mean()) / (2.0 * np.pi))
        * pitch
    )

    # Bucket components into lattice slots, keeping the best fit per slot.
    slots: dict[int, _Component] = {}
    for component in ordered:
        index = int(round((component.x - phase) / pitch))
        expected = phase + index * pitch
        if abs(component.x - expected) > pitch * PITCH_TOLERANCE:
            continue
        held = slots.get(index)
        if held is None or abs(component.x - expected) < abs(held.x - expected):
            # Two blobs in one slot are a body and its wick: keep the union.
            slots[index] = component if held is None else _Component(
                x=(held.x + component.x) / 2.0,
                left=min(held.left, component.left),
                top=min(held.top, component.top),
                right=max(held.right, component.right),
                bottom=max(held.bottom, component.bottom),
                width=max(held.width, component.width),
            )
        elif held is not None:
            slots[index] = _Component(
                x=held.x,
                left=min(held.left, component.left),
                top=min(held.top, component.top),
                right=max(held.right, component.right),
                bottom=max(held.bottom, component.bottom),
                width=max(held.width, component.width),
            )

    if len(slots) < MIN_RUN:
        return []

    # The field is the longest contiguous stretch of occupied slots, tolerating
    # short gaps where a flat candle left no pixels of its own.
    indices = sorted(slots)
    best: list[int] = []
    current: list[int] = [indices[0]]
    for previous, index in zip(indices, indices[1:]):
        if index - previous <= MAX_PITCH_SKIP:
            current.append(index)
        else:
            if len(current) > len(best):
                best = current
            current = [index]
    if len(current) > len(best):
        best = current

    run = [slots[index] for index in best]
    return run if len(run) >= MIN_RUN else []


def find_candle_field(
    image: np.ndarray,
    profile: ColorProfile | None = None,
    exclude: Iterable[Box] = (),
) -> tuple[Box | None, int, float]:
    """Locate the candles on a full screenshot. Returns (box, count, pitch)."""
    if cv2 is None or image is None or image.size == 0:
        return None, 0, 0.0

    height, width = image.shape[:2]
    bull, bear = build_masks(image, profile or ColorProfile())
    mask = _strip_wide_runs(bull | bear, width)
    mask = _drop_oversized(mask, width, height)

    # Blank out anything we already know is not the platform — most importantly
    # GateKeeper's own window, which is full of candle-coloured buttons and
    # would otherwise be analysed as a chart.
    for box in exclude:
        clipped = box.clip(width, height)
        if clipped.area:
            mask[
                clipped.top : clipped.bottom, clipped.left : clipped.right
            ] = 0

    components = _candidate_components(mask, width, height)
    if len(components) < MIN_RUN:
        return None, len(components), 0.0

    typical_width = float(np.median([c.width for c in components]))
    components = _merge_by_column(components, tolerance=max(1.0, typical_width * 0.6))
    run = _longest_rhythmic_run(components)
    if not run:
        return None, 0, 0.0
    count = len(run)

    pitch = float(np.median([run[i + 1].x - run[i].x for i in range(len(run) - 1)]))

    # Antialiased text over a coloured background sheds specks that are candle
    # coloured, candle sized and occasionally land on the lattice. They are
    # nothing like as tall as a candle, and letting one into the bounds drags
    # the box up into the interface, so the run's own height sets the floor.
    median_height = float(np.median([c.bottom - c.top for c in run]))
    solid = [c for c in run if (c.bottom - c.top) >= max(3.0, median_height * 0.15)]
    if len(solid) >= MIN_RUN:
        run = solid

    left = min(c.left for c in run)
    right = max(c.right for c in run)
    top = min(c.top for c in run)
    bottom = max(c.bottom for c in run)

    # Breathe outwards: the extremes of a wick can be a pixel outside the blob,
    # and the engine reads better with a little empty space around the field.
    pad_x = int(max(2, pitch))
    pad_y = int(max(6, (bottom - top) * 0.06))
    box = Box(left - pad_x, top - pad_y, (right - left) + pad_x * 2, (bottom - top) + pad_y * 2)
    return box.clip(width, height), count, pitch


# --------------------------------------------------------------------------
# Step 2: the price axis


_NUMERIC = re.compile(r"^\d{1,7}([.,]\d{1,6})?$")


# A strip of interface can hold a lot of text; reading all of it would make a
# scan take minutes. The badges being looked for are short and near the chart.
MAX_TEXT_BOXES = 60


def _ocr_words(image: np.ndarray, whitelist: str) -> list[dict[str, Any]]:
    """Find and read every word in a region."""
    words: list[dict[str, Any]] = []
    for raw in find_text_boxes(image)[:MAX_TEXT_BOXES]:
        text, confidence = ocr_crop(image, raw, whitelist)
        if text:
            words.append(
                {"text": text, "confidence": confidence, "box": Box(*raw)}
            )
    return words


def measure_axis_width(image: np.ndarray, chart: Box) -> int:
    """How far right of the candles the price labels extend.

    Guessing this wrong is expensive in both directions: too narrow and the
    axis is cut off so no price can be read, too wide and the region swallows
    whatever sits beyond it.
    """
    height, width = image.shape[:2]
    available = width - chart.right
    if available <= 8:
        return 0

    strip_width = min(available, DEFAULT_AXIS_WIDTH * 2)
    strip = image[chart.top : chart.bottom, chart.right : chart.right + strip_width]
    words = [w for w in _ocr_words(strip, "0123456789.,") if _NUMERIC.match(w["text"])]
    if len(words) < 2:
        return min(available, DEFAULT_AXIS_WIDTH)

    furthest = max(w["box"].right for w in words)
    return int(min(available, furthest + 10))


# --------------------------------------------------------------------------
# Step 3 and 4: the labels


_LABEL_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz/-0123456789 "


def _search_strips(image: np.ndarray, chart: Box) -> list[Box]:
    """Where a platform plausibly puts the pair name and the timeframe badge.

    Ordered by how likely each is, because the first confident parse wins. The
    band directly above the chart comes first: every platform of this shape
    puts the instrument there.
    """
    height, width = image.shape[:2]
    strips = [
        # Directly above the plot, full width.
        Box(0, max(0, chart.top - 140), width, min(140, chart.top)),
        # The very top of the screen — browser chrome aside, the platform's
        # own toolbar.
        Box(0, 0, width, min(120, height)),
        # Just below the plot, where the timeframe badge often sits.
        Box(0, chart.bottom, width, min(90, max(0, height - chart.bottom))),
        # A left rail, for layouts that stack the instrument vertically.
        Box(0, 0, min(320, width), height),
    ]
    return [s for s in (strip.clip(width, height) for strip in strips) if s.area > 0]


def _find_asset(image: np.ndarray, chart: Box) -> tuple[Box | None, str | None]:
    """The instrument name: symbol-shaped text near the chart."""
    for strip in _search_strips(image, chart):
        crop = image[strip.top : strip.bottom, strip.left : strip.right]
        # Left to right: a pair split across boxes reads "EUR" then "/USD",
        # never the other way round.
        words = sorted(_ocr_words(crop, _LABEL_WHITELIST), key=lambda w: w["box"].left)
        best: tuple[float, Box, str] | None = None
        for index, word in enumerate(words):
            # A pair may arrive as one token ("EUR/USD") or as neighbouring
            # tokens the OCR split ("EUR", "/", "USD", "OTC"), so try growing
            # the phrase rightwards and keep the longest that still parses.
            phrase = ""
            box = word["box"]
            for extra in words[index : index + 4]:
                phrase = f"{phrase} {extra['text']}".strip()
                merged = Box(
                    min(box.left, extra["box"].left),
                    min(box.top, extra["box"].top),
                    0,
                    0,
                )
                merged.width = max(box.right, extra["box"].right) - merged.left
                merged.height = max(box.bottom, extra["box"].bottom) - merged.top
                # Tokens on different lines are not one label.
                if merged.height > word["box"].height * 2:
                    break
                name = normalise(phrase)
                if name and "/" in name:
                    score = word["confidence"] + len(phrase)
                    if best is None or score > best[0]:
                        best = (score, merged, name)
        if best is not None:
            _, box, name = best
            padded = Box(box.left - 6, box.top - 5, box.width + 12, box.height + 10)
            return padded.offset(strip.left, strip.top).clip(
                image.shape[1], image.shape[0]
            ), name
    return None, None


def _find_timeframe(image: np.ndarray, chart: Box) -> tuple[Box | None, int | None]:
    """The timeframe badge: a token that parses as a chart interval.

    Ties are broken by distance to the chart, because short tokens like "M1"
    and "H3" also occur inside unrelated text, and the badge is the one drawn
    beside the plot.
    """
    chart_cx = chart.left + chart.width / 2.0
    chart_cy = chart.top + chart.height / 2.0
    best: tuple[float, Box, int] | None = None

    for strip in _search_strips(image, chart):
        crop = image[strip.top : strip.bottom, strip.left : strip.right]
        for word in _ocr_words(crop, "SMHDsmhd0123456789 "):
            parsed = parse_timeframe(word["text"])
            if parsed is None:
                continue
            snapped = snap_to_known(parsed)
            if snapped is None:
                continue
            box = word["box"].offset(strip.left, strip.top)
            distance = np.hypot(
                box.left + box.width / 2.0 - chart_cx,
                box.top + box.height / 2.0 - chart_cy,
            )
            if best is None or distance < best[0]:
                best = (float(distance), box, snapped)

    if best is None:
        return None, None
    _, box, seconds = best
    padded = Box(box.left - 6, box.top - 5, box.width + 12, box.height + 10)
    return padded.clip(image.shape[1], image.shape[0]), seconds


# --------------------------------------------------------------------------
# The whole thing


def detect_layout(
    image: np.ndarray,
    *,
    profile: ColorProfile | None = None,
    exclude: Iterable[Box] = (),
    read_labels: bool = True,
) -> Layout:
    """Work out where the chart, its axis and its labels are on a screenshot."""
    layout = Layout()
    if cv2 is None:
        layout.issues.append(
            "OpenCV is not installed, so the screen cannot be analysed."
        )
        return layout
    if image is None or image.size == 0:
        layout.issues.append("The screen capture came back empty.")
        return layout

    height, width = image.shape[:2]
    field, count, pitch = find_candle_field(image, profile, exclude)
    layout.candles_found = count
    layout.pitch = pitch

    if field is None:
        layout.issues.append(
            "No candle chart was found on screen. Open the chart so it is fully "
            "visible and not covered by another window, then scan again."
        )
        return layout

    axis_width = measure_axis_width(image, field)
    chart = Box(
        field.left, field.top, field.width + axis_width, field.height
    ).clip(width, height)
    layout.chart = chart

    if read_labels and not ocr_available():
        # One clear cause beats three vague symptoms. Without OCR the pair, the
        # timeframe and the price axis are all unreadable, and saying so once
        # points at something the user can actually fix.
        layout.issues.append(
            "Text recognition (Tesseract) is not installed, so the pair name, "
            "the chart timeframe and the price axis cannot be read from the "
            "screen. Candle patterns and direction still work. Install "
            "Tesseract OCR to have the rest filled in automatically."
        )
        layout.confidence = 55.0
        return layout

    if read_labels:
        layout.asset, layout.asset_name = _find_asset(image, field)
        layout.timeframe, layout.timeframe_seconds = _find_timeframe(image, field)

    # Confidence: how much of the read is standing on its own feet. The candle
    # field is most of it; the labels are worth less individually but their
    # absence is what leaves the user renaming things by hand.
    confidence = 55.0
    confidence += min(25.0, (count - MIN_RUN) * 0.5)
    if layout.asset_name:
        confidence += 10.0
    else:
        layout.issues.append(
            "The pair's name could not be read from the screen, so it will not "
            "update by itself when you switch charts. Set it by hand in settings."
        )
    if layout.timeframe_seconds:
        confidence += 10.0
    else:
        layout.issues.append(
            "The chart's timeframe badge could not be read. Set the timeframe by "
            "hand in settings — duration suggestions depend on it."
        )
    if axis_width <= 0:
        layout.issues.append(
            "No price axis was found to the right of the candles, so prices will "
            "be relative rather than real. Scroll the chart so its axis is visible."
        )
    layout.confidence = float(min(95.0, confidence))
    return layout
