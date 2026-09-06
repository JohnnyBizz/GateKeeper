"""The always-on-top overlay window.

Tkinter, on purpose: it ships with Python on Windows and macOS (and is one
`apt install python3-tk` away on Linux), so the overlay adds no pip dependency
to a tool meant to install and run in two commands.

Drawn on a single canvas rather than assembled out of widgets. Tk's widgets
are why this looked its age — square corners, flat fills, no curve that is not
a staircase — and none of that is a limit of Tk so much as of its *widgets*.
A canvas will happily show an image, and :mod:`graphics` can draw anything, so
every surface here is a rendered image with text placed over it. It also
retires a whole class of bug: there is no packing order to get wrong.

Since the 2026-09 redraw the panel is a lit scene rather than a stack of
boxes: a backdrop with a pool of the verdict's own colour in it, cards that
cast shadows onto it and carry a rim of light along their top edge, a gauge
whose arc glows, and motion that means something — a verdict that *arrives*
when it changes, a banner that slides in, a price that flashes when it ticks,
a tally that pops when it moves, a sweep while the chart is being searched.
Every frame of every animation is still one cached image: motion here is a
handful of appearances shown in sequence, never a fresh render per tick.

Only the fields that must accept typing are still real widgets, dropped onto
the canvas where they are needed.

This module only draws. Every decision about *what* to draw is made in
``viewmodel.py``, which is why the panel's behaviour can be tested without a
display.
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable

from ..logging_setup import get_logger
from . import graphics as gfx
from .viewmodel import COLORS, OverlayViewModel

log = get_logger(__name__)

PANEL_WIDTH = 340
PAD = 10           # the margin everything lines up against
INNER = PANEL_WIDTH - PAD * 2

# How many rendered images to keep. Every distinct size, colour and animation
# frame is drawn once and then reused; without a bound, a long session would
# accumulate one image per frame of every pulse.
MAX_CACHED_IMAGES = 240

# How finely the countdown ring is drawn. A thirty-pixel circle has nowhere to
# put more than this, and each step is an image kept for the session.
RING_STEPS = 24

# Where the draggable header ends. Everything below it is content, and a press
# there is a press rather than the start of a drag.
HEADER_BOTTOM = PAD + 44

# How many repaints the short animations take. At the overlay's repaint rate
# eight frames is well under a second — seen, not waited for.
ARRIVE_FRAMES = 8   # a changed verdict sliding into place
SLIDE_FRAMES = 8    # the switch banner sliding in
FLASH_FRAMES = 5    # a price tick lighting the price
POP_FRAMES = 3      # a tally number growing for a moment
# The scanning sweep turns in this many distinct frames, each a cached image.
SPINNER_STEPS = 12
# The backdrop is drawn once per height step and tint. Quantising the height
# means a two-line change in the evidence block does not redraw a 340px blur.
BACKDROP_STEP = 48

# Surface tones. The palette in ``viewmodel.COLORS`` names the meanings; these
# are the materials the cards are made of: glass. Each carries its own alpha,
# so the backdrop's pools of light show through every surface rather than
# only through the gaps between them.
SURFACE = "#16203696"
SURFACE_DEEP = "#0e152680"
SURFACE_HIGH = "#22304ab4"
SURFACE_TRACK = "#1c2740"
#: The rim of a glass edge — white, mostly transparent.
GLASS_LINE = "#ffffff24"
#: The one surface that is a real Tk widget rather than a drawn image: the
#: typing fields. Tk takes no alpha channel in a colour — "#22304ab4" is
#: "invalid color name" and the end of the first paint — so this is the
#: glass tone, opaque. Every colour handed to a Tk option must be six digits.
FIELD_BG = "#1b2740"
#: The brand's sweep, blue into violet: the mark, the SCAN button, the sweep.
BRAND = "#6aa8ff"
BRAND_TO = "#8b5cf6"
#: Where the verdict word starts. The fit test reads this rather than a
#: number of its own, so the room a label is measured against is the room it
#: actually has.
VERDICT_X = PAD + 176
#: The aurora drifts: this many cached frames, one per this many repaints.
AURORA_FRAMES = 8
AURORA_EVERY = 6


class OverlayPanel:
    """A compact, draggable, frameless panel that floats above other windows."""

    def __init__(
        self,
        view_model: OverlayViewModel,
        *,
        on_scan: Callable[[], None] | None = None,
        on_reset: Callable[[], None] | None = None,
        on_adjust: Callable[[int, int], None] | None = None,
        on_asset: Callable[..., None] | None = None,
        on_stake: Callable[[float | None], None] | None = None,
        on_balance: Callable[[float], None] | None = None,
        on_settings: Callable[[], None] | None = None,
        on_close: Callable[[], None] | None = None,
        on_toggle_risk: Callable[[], None] | None = None,
        on_toggle_details: Callable[[], None] | None = None,
        on_payout: Callable[[float], None] | None = None,
        on_record: Callable[[], None] | None = None,
        position: tuple[int, int] = (40, 80),
        opacity: float = 0.96,
        animate: bool = True,
    ) -> None:
        self.vm = view_model
        self.on_scan = on_scan or (lambda: None)
        self.on_reset = on_reset or (lambda: None)
        self.on_adjust = on_adjust or (lambda w, l: None)
        self.on_asset = on_asset or (lambda a, tf=None: None)
        self.on_stake = on_stake or (lambda s: None)
        self.on_balance = on_balance or (lambda b: None)
        self.on_settings = on_settings or (lambda: None)
        self.on_close = on_close or (lambda: None)
        self.on_toggle_risk = on_toggle_risk or (lambda: None)
        self.on_toggle_details = on_toggle_details or (lambda: None)
        self.on_payout = on_payout or (lambda p: None)
        self.on_record = on_record or (lambda: None)
        self.animate = bool(animate)

        self.root = tk.Tk()
        self.root.title("GateKeeper")
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry(f"{PANEL_WIDTH}x700+{position[0]}+{position[1]}")

        # Frameless and always on top. Both are best-effort: some window
        # managers refuse one or the other, and a panel with a title bar is
        # still perfectly usable, so a refusal must not be fatal.
        for attempt in (
            lambda: self.root.overrideredirect(True),
            lambda: self.root.attributes("-topmost", True),
            lambda: self.root.attributes("-alpha", opacity),
        ):
            try:
                attempt()
            except tk.TclError:
                pass

        self._collapsed = False
        self._drag_origin = (0, 0)
        self._window_origin = (0, 0)
        self._entries: dict[str, tk.Entry] = {}
        # The canvas items holding those fields, created once and moved after.
        self._entry_items: dict[str, int] = {}
        # Rendered images, kept alive: Tk holds only a weak claim on a
        # PhotoImage, so an unreferenced one is collected and the canvas draws
        # a blank where the picture was.
        self._images: dict[Any, Any] = {}
        # Images that follow the data rather than a fixed set of appearances,
        # each in a slot of its own so a new one replaces the last.
        self._slots: dict[str, tuple[Any, Any]] = {}
        # Whether the press that began this drag landed on the handle.
        self._dragging = False
        self._height = 700
        # Where the pulse is in its cycle, and how far the eased values have
        # travelled toward their targets.
        self._pulse = 0
        self._score_shown: float | None = None
        # Which tagged things the pointer is over. Read while drawing, so a
        # hovered button is drawn brighter on the next repaint.
        self._hover: set[str] = set()
        # The short animations: what each last showed, and how many frames
        # of its motion are left.
        self._verdict_key: tuple[Any, ...] | None = None
        self._arrive = 0
        self._notice_key: str | None = None
        self._slide = 0
        self._price_seen: str | None = None
        self._flash = 0
        self._tally_seen: dict[str, str] = {}
        self._pop: dict[str, int] = {}
        # Sections that have failed to draw, so a broken one is written to
        # the log once rather than twelve times a second.
        self._failed: set[str] = set()

        self._build_fonts()
        self._build()

    # -- fonts -------------------------------------------------------------

    def _build_fonts(self) -> None:
        def pick(candidates: list[str], size: int, weight: str = "normal") -> Any:
            try:
                available = set(tkfont.families())
            except tk.TclError:  # pragma: no cover - no display
                available = set()
            for name in candidates:
                if name in available:
                    return tkfont.Font(family=name, size=size, weight=weight)
            return tkfont.Font(size=size, weight=weight)

        # Windows 11's own display face first, then the best of what a
        # machine is likely to have. The captions use Bahnschrift where it
        # exists — a condensed grotesk that reads as a label rather than a
        # sentence — and fall back to the text face.
        sans = ["Segoe UI Variable Display", "Segoe UI Variable Text", "Inter",
                "Segoe UI", "SF Pro Display", "Helvetica Neue", "DejaVu Sans",
                "Arial"]
        caps = ["Bahnschrift SemiBold", "Bahnschrift", "Segoe UI Variable Small",
                "Segoe UI Semibold", "Segoe UI", "DejaVu Sans", "Arial"]
        mono = ["JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono",
                "Menlo", "DejaVu Sans Mono", "Courier"]

        # A type scale rather than nine independent guesses, and nothing below
        # eight point: this panel is meant to be glanced at from across a desk
        # rather than studied.
        self.f_brand = pick(sans, 13, "bold")
        self.f_caption = pick(caps, 8, "bold")
        self.f_label = pick(sans, 8)
        self.f_small = pick(sans, 9)
        self.f_body = pick(sans, 10)
        self.f_pair = pick(sans, 15, "bold")
        self.f_verdict = pick(sans, 30, "bold")
        self.f_score = pick(sans, 26, "bold")
        self.f_button = pick(caps, 11, "bold")
        self.f_mono = pick(mono, 10)
        self.f_price = pick(mono, 17, "bold")
        self.f_stat = pick(mono, 16, "bold")
        self.f_stat_pop = pick(mono, 19, "bold")

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        self.c = tk.Canvas(
            self.root, width=PANEL_WIDTH, height=self._height,
            bg=COLORS["bg"], highlightthickness=0, bd=0,
        )
        self.c.pack(fill="both", expand=True)
        self.c.bind("<Button-1>", self._drag_start)
        self.c.bind("<B1-Motion>", self._drag_move)

        for key, width, font, commit in (
            ("pair", 13, self.f_pair, lambda text: self.on_asset(text)),
            ("payout", 5, self.f_mono, self._commit_payout),
            ("balance", 9, self.f_mono, self._commit_balance),
            ("stake", 9, self.f_mono, self._commit_stake),
        ):
            self._entries[key] = self._text_field(
                width=width, font=font, on_commit=commit
            )

    def _text_field(
        self, *, width: int, font: Any, on_commit: Callable[[str], None]
    ) -> tk.Entry:
        """A themed Entry that commits on Enter or when focus leaves it."""
        entry = tk.Entry(
            self.c, font=font, width=width, justify="left",
            bg=FIELD_BG, fg=COLORS["text"],
            insertbackground=COLORS["accent"], relief="flat",
            highlightthickness=1, highlightbackground=COLORS["line"],
            highlightcolor=COLORS["accent"],
        )

        def commit(_event=None):
            on_commit(entry.get())
            # Give focus back to the window so the field stops swallowing keys.
            self.c.focus_set()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        return entry

    @staticmethod
    def _set_entry(entry: tk.Entry, text: str) -> None:
        """Update a field — but never while the user is typing in it."""
        try:
            if entry.focus_displayof() is entry:
                return
        except tk.TclError:  # pragma: no cover - window closing
            return
        if entry.get() != text:
            entry.delete(0, "end")
            entry.insert(0, text)

    # -- drawing helpers ----------------------------------------------------

    def _photo(self, key: Any, build: Callable[[], Any]) -> Any:
        """A cached ``PhotoImage``, drawn once per distinct appearance.

        Redrawing a blurred, supersampled card on every repaint would cost more
        than the whole rest of the frame. Almost everything here is one of a
        handful of appearances, so each is drawn once and shown thereafter.
        """
        found = self._images.get(key)
        if found is None:
            from PIL import ImageTk

            found = ImageTk.PhotoImage(build())
            if len(self._images) >= MAX_CACHED_IMAGES:
                self._images.pop(next(iter(self._images)), None)
            self._images[key] = found
        return found

    def _photo_slot(self, slot: str, key: Any, build: Callable[[], Any]) -> Any:
        """A cached image for something that follows the data.

        One slot, replaced rather than added to. The sparkline's appearance
        depends on the latest price, so caching it alongside the fixed shapes
        would add an entry per tick and evict everything else within a minute.
        """
        held = self._slots.get(slot)
        if held is None or held[0] != key:
            from PIL import ImageTk

            held = (key, ImageTk.PhotoImage(build()))
            self._slots[slot] = held
        return held[1]

    def _image(self, x: int, y: int, key: Any, build, *, tags: str = "") -> None:
        self.c.create_image(
            x, y, image=self._photo(key, build), anchor="nw",
            tags=tags or "frame",
        )

    def _image_slot(self, x: int, y: int, slot: str, key: Any, build) -> None:
        self.c.create_image(
            x, y, image=self._photo_slot(slot, key, build), anchor="nw",
            tags="frame",
        )

    def _place_entry(self, key: str, x: int, y: int, anchor: str = "w") -> None:
        """Put a typing field on the canvas, once, and move it thereafter.

        Deliberately untagged, so clearing the frame never takes it with it. A
        canvas window item destroyed and remade on every repaint unmaps and
        remaps its widget twelve times a second — which flickers, and takes the
        cursor out of the field the moment anyone tries to type in it.
        """
        item = self._entry_items.get(key)
        if item is None:
            self._entry_items[key] = self.c.create_window(
                x, y, window=self._entries[key], anchor=anchor
            )
            return
        self.c.coords(item, x, y)
        self.c.itemconfigure(item, state="normal", anchor=anchor)

    def _hide_entry(self, key: str) -> None:
        """Take a field off screen without destroying it."""
        item = self._entry_items.get(key)
        if item is not None:
            self.c.itemconfigure(item, state="hidden")

    def _card(
        self, x: int, y: int, w: int, h: int, *, radius: int = 12,
        fill: str = SURFACE, fill_to: str | None = None,
        border: str | None = None, glow: str | None = None,
        glow_strength: float = 0.0, shadow: float = 0.0,
        highlight: bool = True, tags: str = "",
    ) -> None:
        """A rounded surface. Padded ones are offset by their own halo."""
        key = ("card", w, h, radius, fill, fill_to, border, glow,
               round(glow_strength, 2), shadow, highlight)
        pad = gfx.card_padding(glow, shadow)
        self._image(
            x - pad, y - pad, key,
            lambda: gfx.card(
                w, h, radius=radius, fill=fill, fill_to=fill_to, border=border,
                glow=glow, glow_strength=glow_strength, shadow=shadow,
                highlight=highlight,
            ),
            tags=tags,
        )

    def _button(
        self, x: int, y: int, w: int, h: int, *, color: str,
        color_to: str | None = None, radius: int = 12, tag: str,
        glow: str | None = None, glow_strength: float = 0.0,
        border: str | None = None, across: bool = False,
    ) -> None:
        """A pressable surface, brighter while the pointer is over it."""
        hot = self._hot(tag)
        key = ("button", w, h, radius, color, color_to, hot, glow,
               round(glow_strength, 2), border, across)
        pad = gfx.card_padding(glow, 5.0)
        self._image(
            x - pad, y - pad, key,
            lambda: gfx.button(
                w, h, color=color, color_to=color_to, radius=radius, hover=hot,
                glow=glow, glow_strength=glow_strength, border=border,
                across=across,
            ),
            tags=f"frame {tag}",
        )
        self._hoverable(tag)

    def _text(
        self, x: int, y: int, text: str, font: Any, fill: str,
        anchor: str = "nw", tags: str = "",
    ) -> None:
        self.c.create_text(
            x, y, text=text, font=font, fill=fill, anchor=anchor,
            tags=tags or "frame",
        )

    def _clickable(self, tag: str, command: Callable[[], None]) -> None:
        self.c.tag_bind(tag, "<Button-1>", lambda _e: command())

    def _hoverable(self, tag: str) -> None:
        """Track the pointer over a tag; the next repaint draws it lit."""
        self.c.tag_bind(tag, "<Enter>", lambda _e, t=tag: self._hover.add(t))
        self.c.tag_bind(tag, "<Leave>", lambda _e, t=tag: self._hover.discard(t))

    def _hot(self, tag: str) -> bool:
        return tag in self._hover

    def _fitted(self, text: str, width: int, *fonts: Any) -> Any:
        """The largest of ``fonts`` this text fits inside ``width``.

        The verdict is one word for three of its four values and two for the
        fourth, and "NO TRADE" in the verdict face is wider than the panel: it
        ran off the right edge and was clipped mid-letter.

        This had already happened once, to "SCANNING", and was fixed there by
        hard-coding a smaller face for that one string — which fixed the
        instance and left the cause, so the next long word did it again.
        Measuring is the fix; the panel is a fixed 340 pixels and any label
        can be too long for it.
        """
        for font in fonts[:-1]:
            if self._width_of(text, font) <= width:
                return font
        return fonts[-1]

    @staticmethod
    def _width_of(text: str, font: Any) -> float:
        """How wide this string renders, asked of Tk where there is a display."""
        try:
            return float(font.measure(text))
        except Exception:
            # Headless, or a stubbed font. Estimate from the point size, and
            # lean high: bold capitals are the wide case, and the cost of
            # overestimating is one size smaller rather than a clipped word.
            try:
                size = float(font["size"])
            except Exception:
                size = 12.0
            return len(text) * size * 0.85

    # -- rendering ----------------------------------------------------------

    def refresh(self) -> None:
        """Paint the current view model onto the canvas."""
        self._pulse += 1
        data = self.vm.render()

        self.c.delete("frame")
        self._draw_backdrop(data)
        y = self._section(self._draw_header, data, PAD)
        if self._collapsed:
            # Parked out of the way. Everything below the header is drawn into
            # a window 56 pixels tall, which is work nobody can see.
            for key in self._entry_items:
                self._hide_entry(key)
            return
        for draw in (
            self._draw_notice, self._draw_market, self._draw_watchlist,
            self._draw_signal, self._draw_trend, self._draw_chart,
            self._draw_actions, self._draw_record, self._draw_session,
            self._draw_details, self._draw_risk, self._draw_footer,
        ):
            y = self._section(draw, data, y)
        self._fit(y + PAD)

    def _section(self, draw: Callable[..., int], data: dict[str, Any], y: int) -> int:
        """Draw one section, and never let one of them take the frame down.

        The first paint happens before the window's event loop starts, where
        an exception is not a logged callback failure but the end of the
        process. A section that cannot draw is skipped and written to the
        log; the sections below it still get drawn.
        """
        name = getattr(draw, "__name__", "section").lstrip("_")
        try:
            return int(draw(data, y))
        except Exception:
            if name not in self._failed:
                self._failed.add(name)
                log.exception("could not draw the %s section", name)
            return y

    # -- motion -------------------------------------------------------------

    @staticmethod
    def _ease(t: float) -> float:
        """Cubic ease-out: fast to leave, gentle to land."""
        t = max(0.0, min(1.0, t))
        return 1.0 - (1.0 - t) ** 3

    def _progress(self, left: int, total: int) -> float:
        """How far a short animation has come, 0..1, eased."""
        if not self.animate or total <= 0:
            return 1.0
        return self._ease(1.0 - left / total)

    def _quantised(self, strength: float) -> float:
        """A glow strength snapped to the pulse's own steps, so an animated
        halo costs no more cached images than a breathing one."""
        return round(max(0.0, min(1.0, strength)) * self.PULSE_STEPS) / self.PULSE_STEPS

    # -- sections -----------------------------------------------------------

    def _draw_backdrop(self, data: dict[str, Any]) -> None:
        """The ground everything sits on, lit by what the panel is saying —
        and never quite still: its pools of light drift one frame every few
        repaints, each frame a cached image."""
        verdict = data.get("verdict") or {}
        if data.get("risk", {}).get("paused"):
            tint = COLORS["no_trade"]
        elif verdict.get("actionable"):
            tint = str(verdict.get("color") or COLORS["accent"])
        else:
            tint = COLORS["accent"]
        height = int(math.ceil(max(self._height, 320) / BACKDROP_STEP)) * BACKDROP_STEP
        step = (self._pulse // AURORA_EVERY) % AURORA_FRAMES if self.animate else 0
        try:
            self._image(
                0, 0, ("backdrop", height, tint, step),
                lambda: gfx.backdrop(
                    PANEL_WIDTH, height, tint=tint, accent=COLORS["accent"],
                    phase=step / AURORA_FRAMES,
                ),
            )
        except Exception:  # pragma: no cover - the ground is never fatal
            log.exception("could not draw the backdrop")

    def _draw_header(self, data: dict[str, Any], y: int) -> int:
        header = data["header"]
        self._card(PAD, y, INNER, 44, radius=14, fill=SURFACE_HIGH,
                   fill_to=SURFACE, border=GLASS_LINE, shadow=8.0)
        self._image(PAD + 10, y + 9, ("shield", 26),
                    lambda: gfx.shield(26, color=BRAND, color_to=BRAND_TO))
        self._text(PAD + 44, y + 22, "GATEKEEPER", self.f_brand, COLORS["text"], "w")

        colour = header["status_color"]
        label = header["status"]
        width = max(56, len(label) * 6 + 26)
        left = PANEL_WIDTH - PAD - 64 - width
        self._image(left, y + 13, ("pill", width, colour),
                    lambda: gfx.pill(width, 18, color=colour))
        self._image(left + 8 - gfx.DOT_PAD, y + 18 - gfx.DOT_PAD, ("dot", 7, colour),
                    lambda: gfx.glow_dot(7, colour))
        self._text(left + 16, y + 22, label, self.f_caption, colour, "w")

        for index, (glyph, command, tag) in enumerate(
            (("⚙", self.on_settings, "gear"),
             ("–", self.toggle_collapse, "fold"),
             ("✕", self._close, "shut"))
        ):
            tone = COLORS["text"] if self._hot(tag) else COLORS["faint"]
            self._text(PANEL_WIDTH - PAD - 48 + index * 17, y + 22, glyph,
                       self.f_body, tone, "center", tags=f"frame {tag}")
            self._clickable(tag, command)
            self._hoverable(tag)
        return y + 44 + 12

    def _draw_notice(self, data: dict[str, Any], y: int) -> int:
        """The switch banner: a chart the user is not on just became tradeable.

        First thing under the header, above even the pair strip, because it is
        the one message on the panel with a deadline — a setup stands for a
        bar or two, and by the row of watchlist tabs it is one coloured tile
        among nine. It slides in when it appears, breathes like an actionable
        verdict, names the chart and the side, and the line under it says the
        move: switch this pair's timeframe, or open that pair, and what expiry
        to set. Clicking it does the panel's half of the switch, same as
        clicking the chart's tab.

        The view model decides whether it shows at all — expiry, the setup
        dying, the brake — so a banner on screen is always a banner still
        telling the truth.
        """
        notice = data.get("notice") or {}
        if not notice.get("show"):
            self._notice_key = None
            return y
        title = str(notice.get("title", ""))[:42]
        if title != self._notice_key:
            self._notice_key = title
            self._slide = SLIDE_FRAMES
        elif self._slide > 0:
            self._slide -= 1
        t = self._progress(self._slide, SLIDE_FRAMES)
        offset = int(round((1.0 - t) * 22))

        colour = notice.get("color") or COLORS["accent"]
        detail = str(notice.get("detail") or "")
        # The card holds the headline only, and the instruction flows beneath
        # it at whatever height it really renders. The first version sized
        # one card around both by estimating the wrap from the string width —
        # and Tk breaks at word boundaries, so a two-and-a-bit-line estimate
        # renders as four lines and the overflow lands on the market card
        # below. Estimating text height is how the verdict face got clipped
        # twice; measuring is the fix, and the panel's flowing-prose idiom
        # already cannot overlap.
        self._card(
            PAD + offset, y, INNER - offset, 34, radius=14, fill="#1f2c4cc0",
            fill_to="#141d33a8", border=colour, glow=colour,
            glow_strength=self._quantised(0.95 * self._pulse_phase() * t),
            shadow=5.0, tags="frame notice",
        )
        self._text(PAD + offset + 14, y + 18, title, self.f_button, colour, "w",
                   tags="frame notice")
        bottom = y + 34
        if detail:
            item = self.c.create_text(
                PAD + 2, y + 39, text=detail, font=self.f_label,
                fill=COLORS["dim"], anchor="nw", width=INNER - 4,
                tags="frame notice",
            )
            # Never below one line's worth: the headless stub answers bbox
            # with a fixed box, and a bottom edge that walked backwards would
            # draw the rest of the panel over the banner.
            bottom = y + 39 + 13
            try:
                bounds = self.c.bbox(item)
                if bounds:
                    bottom = max(bottom, bounds[3] + 3)
            except tk.TclError:  # pragma: no cover - window closing
                pass
        asset = str(notice.get("asset") or "")
        if asset:
            timeframe = int(notice.get("timeframe") or 0) or None
            self._clickable(
                "notice", lambda name=asset, tf=timeframe: self.on_asset(name, tf)
            )
            self._hoverable("notice")
        return bottom + 8

    def _draw_market(self, data: dict[str, Any], y: int) -> int:
        """The pair, the price, and the shape of the session so far."""
        tiles, chart = data["tiles"], data["chart"]
        height = 114 if chart["ready"] else 66
        self._card(PAD, y, INNER, height, radius=16, fill=SURFACE_HIGH,
                   fill_to=SURFACE_DEEP, border=GLASS_LINE, shadow=8.0)

        self._place_entry("pair", PAD + 14, y + 22)
        self._set_entry(self._entries["pair"], tiles["pair"])

        self._text(PAD + 14, y + 46,
                   f"{tiles['chart']} CHART   ·   {tiles['time']} EXPIRY",
                   self.f_caption, COLORS["faint"], "w")

        colour = (
            COLORS["call"] if chart.get("rising") else COLORS["put"]
        ) if chart["ready"] else COLORS["text"]

        # A tick lights the price for a few frames — the one cue that says
        # the feed is alive without a word of text.
        price = str(data["price"])
        if self._price_seen is not None and price != self._price_seen:
            self._flash = FLASH_FRAMES
        elif self._flash > 0:
            self._flash -= 1
        self._price_seen = price
        if self._flash > 0 and self.animate:
            strength = self._flash / FLASH_FRAMES
            width = max(64, len(price) * 11 + 18)
            self._image(PANEL_WIDTH - PAD - 14 - width + 9, y + 9,
                        ("flash", width, colour, round(strength, 1)),
                        lambda: gfx.pill(width, 26, color=colour,
                                         opacity=int(70 * strength)))
        self._text(PANEL_WIDTH - PAD - 14, y + 22, price, self.f_price, colour, "e")
        if chart["ready"]:
            self._text(PANEL_WIDTH - PAD - 14, y + 46, chart["change_label"],
                       self.f_caption, colour, "e")

            closes = chart["closes"][-140:]
            key = (colour, len(closes), round(closes[0], 6), round(closes[-1], 6))
            self._image_slot(
                PAD + 14, y + 58, "spark", key,
                lambda: gfx.sparkline(INNER - 28, 48, closes, color=colour),
            )
        return y + height + 12

    def _draw_watchlist(self, data: dict[str, Any], y: int) -> int:
        rows = data["watchlist"]
        if not rows:
            return y
        width = (INNER - 8) // 2
        for index, row in enumerate(rows):
            x = PAD + (index % 2) * (width + 8)
            top = y + (index // 2) * 36
            tag = f"watch{index}"
            active, colour = row["active"], row["color"]
            # A tab worth a look breathes with the verdict card; the rest sit
            # still. Quantised so a breathing tab is nine images, not ninety.
            strength = (
                self._quantised(0.85 * self._pulse_phase())
                if row.get("actionable") else 0.0
            )
            hot = self._hot(tag)
            pad = gfx.card_padding(colour if strength > 0 else None)
            self._image(
                x - pad, top - pad,
                ("tab", width, colour, active, strength, hot),
                lambda w=width, c=colour, a=active, s=strength, h=hot: gfx.tab(
                    w, 30, color=c, active=a, glow_strength=s, hover=h
                ),
                tags=f"frame {tag}",
            )
            self._text(x + 12, top + 12, row["label"], self.f_caption,
                       COLORS["text"] if active else COLORS["dim"], "w",
                       tags=f"frame {tag}")
            score = row["score"]
            if score is not None:
                self._text(x + width - 10, top + 12, f"{score:.0f}", self.f_mono,
                           colour, "e", tags=f"frame {tag}")
                # The score as a length, under the name: nine tiles compare
                # at a glance without reading nine numbers.
                self._image(
                    x + 12, top + 23, ("meter", width - 24, colour, int(score)),
                    lambda w=width - 24, c=colour, v=float(score): gfx.bar_meter(
                        w, 3, v / 100.0, color=c, track=SURFACE_TRACK
                    ),
                    tags=f"frame {tag}",
                )
            # The expiry this one was scored at, whenever that is not the
            # expiry the platform is set to. Without it a green tab invites a
            # trade at the wrong length — which is a different trade from the
            # one that passed, and often one the engine would have refused.
            needs = row.get("needs")
            if needs:
                self._text(x + width - 36, top + 12, f"▸{needs}", self.f_label,
                           COLORS["wait"], "e", tags=f"frame {tag}")
            self._clickable(
                tag,
                lambda name=row["asset"], tf=row.get("timeframe"): self.on_asset(
                    name, tf
                ),
            )
            self._hoverable(tag)
        return y + ((len(rows) + 1) // 2) * 36 + 6

    def _draw_signal(self, data: dict[str, Any], y: int) -> int:
        verdict, entry = data["verdict"], data["entry"]
        validity = data.get("validity") or {}
        scanning = data["scan"]["scanning"]
        colour = verdict["color"]

        # The entry inset grows with its explanation, measured on the canvas
        # rather than estimated: the STAND DOWN text ran five lines into a
        # fifty-four pixel box for two weeks before anyone rendered it.
        text_x = PAD + 66
        detail = str(entry.get("detail") or "")
        detail_h = self._measure_height(detail, PANEL_WIDTH - PAD - 26 - text_x)
        inset_h = max(58, detail_h + 36)
        height = 158 + inset_h + 20 + (34 if validity.get("show") else 0)

        # A changed verdict arrives: the arrow and the word slide into place
        # and the card's halo flares and settles. Seen once per change, so
        # the movement means "this is new" rather than "the app is running".
        key = (verdict["direction_label"], colour, scanning)
        if self._verdict_key is None:
            self._verdict_key = key
        elif key != self._verdict_key:
            self._verdict_key = key
            self._arrive = ARRIVE_FRAMES
        elif self._arrive > 0:
            self._arrive -= 1
        t = self._progress(self._arrive, ARRIVE_FRAMES)
        offset = int(round((1.0 - t) * 16))

        # The border breathes only while there is something to act on, so the
        # movement means "this one" rather than "the app is running".
        strength = 0.0
        if verdict["actionable"]:
            strength = self._quantised(0.95 * self._pulse_phase() + (1.0 - t) * 0.6)
        self._card(
            PAD, y, INNER, height, radius=20, fill="#15213ab8", fill_to="#0c142a9e",
            border=colour if verdict["actionable"] else GLASS_LINE,
            glow=colour if verdict["actionable"] else None,
            glow_strength=strength, shadow=10.0,
        )

        self._text(PAD + 18, y + 16, "SIGNAL", self.f_caption, COLORS["faint"], "w")
        if verdict["state"] not in ("ACTIVE", "IDLE"):
            self._text(PANEL_WIDTH - PAD - 18, y + 16, verdict["state"],
                       self.f_caption, COLORS["wait"], "e")
        elif verdict["actionable"]:
            self._text(PANEL_WIDTH - PAD - 18, y + 16, "GATE PASSED",
                       self.f_caption, colour, "e")

        # The ring holds the score and nothing else; the arrow and the word
        # sit beside it, so neither has to share the middle.
        score = verdict["score"]
        shown = self._ease_score(score)
        dial_x, dial_y, dial = PAD + 14, y + 34, 116
        if scanning:
            step = (self._pulse % SPINNER_STEPS) if self.animate else 0
            self._image(dial_x, dial_y, ("spin", dial, step),
                        lambda: gfx.spinner(dial, step / SPINNER_STEPS,
                                            color=BRAND, track=SURFACE_TRACK,
                                            thickness=8))
        else:
            self._image(
                dial_x, dial_y,
                ("gauge", dial, int(shown if shown is not None else -1), colour),
                lambda: gfx.arc_gauge(dial, shown, color=colour,
                                      track=SURFACE_TRACK, thickness=10, span=300),
            )
        centre = dial_x + dial // 2
        self._text(centre, dial_y + dial // 2 - 6,
                   "--" if score is None else f"{score:.0f}",
                   self.f_score, COLORS["text"], "center")
        self._text(centre, dial_y + dial // 2 + 18, "/100", self.f_label,
                   COLORS["faint"], "center")

        if scanning:
            # A narrower face than the verdict's: "SCANNING" is eight
            # characters where "BUY" is three, and at the verdict's size it
            # ran off the edge of the panel.
            self._text(PAD + 146, y + 66, "SCANNING", self.f_score,
                       COLORS["dim"], "w")
            phase = (self._pulse % 40) / 40.0 if self.animate else 0.5
            self._image(PAD + 146, y + 98, ("shimmer", round(phase, 2)),
                        lambda: gfx.shimmer(INNER - 160, 5, phase, color=BRAND))
        else:
            self._image(PAD + 142 + offset, y + 52,
                        ("glyph", verdict["direction"], colour),
                        lambda: gfx.direction_glyph(30, verdict["direction"],
                                                    colour, glow=True))
            label = verdict["direction_label"]
            room = PANEL_WIDTH - PAD - VERDICT_X - offset
            self._text(VERDICT_X + offset, y + 68, label,
                       self._fitted(label, room, self.f_verdict, self.f_score,
                                    self.f_button),
                       colour, "w")

            badge, badge_colour = verdict["badge"], verdict["badge_color"]
            if badge and badge != "--":
                width = max(96, len(badge) * 7 + 28)
                self._image(PAD + 144, y + 96, ("badge", width, badge_colour),
                            lambda: gfx.pill(width, 20, color=badge_colour))
                self._text(PAD + 144 + width // 2, y + 106, badge,
                           self.f_caption, badge_colour, "center")

            pattern = verdict["pattern"] or ""
            if pattern and pattern != "--":
                self._text(PAD + 144, y + 126, pattern[:34], self.f_label,
                           COLORS["dim"], "w")

        # When to get in, in its own inset. The ring is the countdown and
        # carries no number: a clock face inside a 32px ring is a number
        # fighting the shape drawn to replace it, and the ring loses.
        inset_y = y + 158
        self._card(PAD + 12, inset_y, INNER - 24, inset_h, radius=14,
                   fill=SURFACE_HIGH, fill_to=SURFACE, border=GLASS_LINE)
        ready, urgent = entry["ready"], entry["urgent"]
        tone = (
            self._pulse_toward(colour) if ready and urgent
            else colour if ready else COLORS["faint"]
        )
        left = max(0.0, min(1.0, 1.0 - entry["progress"]))
        left = round(left * RING_STEPS) / RING_STEPS
        self._image(PAD + 24, inset_y + 13, ("ring", left, tone),
                    lambda: gfx.countdown_ring(32, left, color=tone,
                                               track=SURFACE_TRACK, thickness=4,
                                               tip=bool(ready)))
        self._text(text_x, inset_y + 20, entry["text"], self.f_button, tone, "w")
        if entry["clock"]:
            self._text(PANEL_WIDTH - PAD - 26, inset_y + 20, entry["clock"],
                       self.f_mono, tone, "e")
        # Wrapped rather than cut: the line explains what the countdown is
        # counting toward, and half of that explains nothing.
        self.c.create_text(
            text_x, inset_y + 34, text=detail, font=self.f_label,
            fill=COLORS["dim"], anchor="nw",
            width=PANEL_WIDTH - PAD - 26 - text_x, tags="frame",
        )

        # How long this call has been alive, against the horizon it argued
        # about. The ring above answers "when does this read get re-derived";
        # this answers "how late into the call is an entry made now" — the
        # half of timing the score says nothing about. It drains over one
        # trade duration, because that is the window the call described, and
        # past it the ring sits empty rather than pretending otherwise.
        if validity.get("show"):
            vy = inset_y + inset_h + 6
            stale = validity.get("stale", False)
            v_tone = COLORS["wait"] if stale or validity.get("drift_against") else colour
            self._image(
                PAD + 24, vy + 4,
                ("fresh", round(validity["fraction"] * RING_STEPS) / RING_STEPS, v_tone),
                lambda: gfx.countdown_ring(
                    22, validity["fraction"], color=v_tone,
                    track=SURFACE_TRACK, thickness=3,
                ),
            )
            age_line = validity["age_text"] + ("  —  the window it called is over" if stale else "")
            self._text(PAD + 56, vy + 8, age_line, self.f_label, v_tone, "w")
            drift = validity.get("drift_text")
            if drift:
                self._text(
                    PAD + 56, vy + 22, drift, self.f_label,
                    COLORS["wait"] if validity.get("drift_against") else COLORS["dim"],
                    "w",
                )
        return y + height + 12

    def _measure_height(self, text: str, width: int) -> int:
        """How tall a wrapped block renders, asked of the canvas itself.

        A probe item is created, measured and deleted — never an estimate
        from the string's length, which is how the verdict face got clipped
        twice. Headless, the stub answers one line, which is the floor.
        """
        if not text:
            return 0
        probe = self.c.create_text(
            0, 0, text=text, font=self.f_label, anchor="nw", width=width,
            tags="frame probe",
        )
        height = 13
        try:
            bounds = self.c.bbox(probe)
            if bounds:
                height = int(bounds[3] - bounds[1])
        except tk.TclError:  # pragma: no cover - window closing
            pass
        self.c.delete("probe")
        return max(13, height)

    def _draw_trend(self, data: dict[str, Any], y: int) -> int:
        trend = data["trend"]
        if trend["blanked"]:
            return y
        self._card(PAD, y, INNER, 36, radius=12, fill=SURFACE,
                   fill_to=SURFACE_DEEP, border=GLASS_LINE, shadow=5.0)
        self._text(PAD + 14, y + 18, "MARKET", self.f_caption,
                   COLORS["faint"], "w")
        self._text(PAD + 72, y + 18, f"{trend['arrow']}  {trend['label']}",
                   self.f_small, trend["color"], "w")
        for index, view in enumerate(trend["views"][:3]):
            x = PANEL_WIDTH - PAD - 142 + index * 46
            self._text(x, y + 19, view["name"], self.f_label, COLORS["faint"], "w")
            self._text(x + 32, y + 18, view["arrow"], self.f_caption,
                       view["color"], "w")
        return y + 36 + 10

    def _draw_chart(self, data: dict[str, Any], y: int) -> int:
        """The candles the analysis is actually reading."""
        chart = data["chart"]
        bars = chart["bars"][-34:]
        if len(bars) < 4:
            return y
        self._card(PAD, y, INNER, 92, radius=16, fill=SURFACE,
                   fill_to=SURFACE_DEEP, border=GLASS_LINE, shadow=6.0)
        self._text(PAD + 14, y + 14, f"LAST {len(bars)} CANDLES", self.f_caption,
                   COLORS["faint"], "w")
        key = (len(bars), round(bars[0][0], 6), round(bars[-1][3], 6))
        self._image_slot(
            PAD + 14, y + 26, "candles", key,
            lambda: gfx.candles(
                INNER - 28, 58,
                [gfx.Bar(*bar) for bar in bars],
                up=COLORS["call"], down=COLORS["put"],
            ),
        )
        return y + 92 + 10

    def _draw_actions(self, data: dict[str, Any], y: int) -> int:
        scanning = data["scan"]["scanning"]
        wide = INNER - 122
        if scanning:
            self._button(PAD, y, wide, 42, color="#2a3650", color_to="#1c2740",
                         border=GLASS_LINE, tag="scan")
        else:
            hot = self._hot("scan")
            self._button(PAD, y, wide, 42, color=BRAND, color_to=BRAND_TO,
                         border="#b7d2ff66", tag="scan", across=True,
                         glow=BRAND if hot else None,
                         glow_strength=0.6 if hot else 0.0)
        self._text(PAD + wide // 2, y + 21, "SCANNING…" if scanning else "SCAN",
                   self.f_button, COLORS["dim"] if scanning else "#ffffff",
                   "center", tags="frame scan")
        self._clickable("scan", self._scan_clicked)

        self._button(PAD + wide + 10, y, 112, 42, color=SURFACE_HIGH,
                     color_to=SURFACE, border=GLASS_LINE, tag="reset")
        self._text(PAD + wide + 66, y + 21, "RESET", self.f_button,
                   COLORS["text"] if self._hot("reset") else COLORS["dim"],
                   "center", tags="frame reset")
        self._clickable("reset", self.on_reset)
        return y + 42 + 12

    def _draw_record(self, data: dict[str, Any], y: int) -> int:
        """Capture the live feed to a file, without a second download.

        Slim on purpose: it is not part of trading and must not compete with
        the verdict for attention. It is here rather than buried in settings
        because the recording is what the engine gets measured against, and a
        capability nobody can find is one nobody uses.
        """
        rec = data["recording"]
        colour = rec["color"]
        self._card(PAD, y, INNER, 28, radius=10, fill=SURFACE,
                   fill_to=SURFACE_DEEP, border=GLASS_LINE, tags="frame record")

        if rec["active"]:
            # The bar sits under the label rather than beside it: at this
            # height there is no room for both, and a half-hour wait wants to
            # show how far along it is more than it wants to be pretty.
            self._image_slot(
                PAD + 2, y + 20, "recbar", ("recbar", rec["progress"]),
                lambda: gfx.bar_meter(INNER - 4, 3, rec["progress"], color=colour),
            )
            self._image(PAD + 10 - gfx.DOT_PAD, y + 9 - gfx.DOT_PAD, ("dot", 7, colour),
                        lambda: gfx.glow_dot(7, colour), tags="frame record")
            self._text(PAD + 24, y + 12, rec["label"], self.f_caption, colour, "w",
                       tags="frame record")
            self._text(PANEL_WIDTH - PAD - 8, y + 12, f"{rec['frames']:,}",
                       self.f_label, COLORS["faint"], "e", tags="frame record")
        else:
            self._text(PANEL_WIDTH // 2, y + 14, rec["label"], self.f_caption,
                       COLORS["text"] if self._hot("record") else colour,
                       "center", tags="frame record")
        self._clickable("record", self.on_record)
        self._hoverable("record")
        y += 28 + 6

        if rec["message"]:
            y = self._wrapped(PAD + 2, y, rec["message"], COLORS["faint"])
            y += 2
        return y + 4

    def _draw_session(self, data: dict[str, Any], y: int) -> int:
        session, risk = data["session"], data["risk"]
        width = (INNER - 16) // 3
        cells = (
            ("WINS", str(session["wins"]), COLORS["call"], (1, 0), "win"),
            ("LOSSES", str(session["losses"]), COLORS["put"], (0, 1), "loss"),
            ("CALLED", str(session.get("calls", 0)), COLORS["accent"], None, ""),
        )
        for index, (label, value, colour, delta, tag) in enumerate(cells):
            x = PAD + index * (width + 8)
            self._card(x, y, width, 60, radius=14, fill=SURFACE,
                       fill_to=SURFACE_DEEP, border=colour + "55", shadow=6.0)
            self._text(x + width // 2, y + 13, label, self.f_caption,
                       COLORS["faint"], "center")
            # A number that just changed grows for a moment, so a win landing
            # is seen without being looked for.
            if self._tally_seen.get(label) not in (None, value):
                self._pop[label] = POP_FRAMES
            elif self._pop.get(label, 0) > 0:
                self._pop[label] -= 1
            self._tally_seen[label] = value
            popping = self.animate and self._pop.get(label, 0) > 0
            self._text(x + width // 2, y + 33, value,
                       self.f_stat_pop if popping else self.f_stat, colour, "center")
            if delta is None:
                continue
            wins, losses = delta
            for sign, symbol, offset in ((1, "+", -16), (-1, "−", 16)):
                mark = f"{tag}{'up' if sign > 0 else 'dn'}"
                cx = x + width // 2 + offset
                hot = self._hot(mark)
                self._image(cx - 9, y + 43, ("knob", colour, hot),
                            lambda c=colour, h=hot: gfx.pill(
                                18, 18, color=c, opacity=95 if h else 45
                            ),
                            tags=f"frame {mark}")
                self._text(cx, y + 52, symbol, self.f_caption, COLORS["text"],
                           "center", tags=f"frame {mark}")
                self._clickable(
                    mark,
                    lambda w=wins * sign, l=losses * sign: self.on_adjust(w, l),
                )
                self._hoverable(mark)
        y += 60 + 8

        if session["total"]:
            rate = f"{session['win_rate_display']} of {session['total']} trades"
            if not session["meaningful"]:
                rate += "  (too few to read)"
        else:
            rate = "No trades recorded yet"
        self._text(PAD + 2, y + 6, rate, self.f_label, COLORS["dim"], "w")
        if risk.get("paused"):
            self._text(PANEL_WIDTH - PAD - 2, y + 6, "⏸ PAUSED", self.f_caption,
                       COLORS["no_trade"], "e")
        return y + 20

    def _draw_details(self, data: dict[str, Any], y: int) -> int:
        details = data["details"]
        collapsed = details["collapsed"]
        tone = COLORS["text"] if self._hot("evidence") else COLORS["faint"]
        self._text(PAD + 2, y + 8, ("▸ " if collapsed else "▾ ") + "EVIDENCE",
                   self.f_caption, tone, "w", tags="frame evidence")
        self._text(PANEL_WIDTH - PAD - 2, y + 8, details["summary"],
                   self.f_label, COLORS["faint"], "e", tags="frame evidence")
        self._clickable("evidence", self.on_toggle_details)
        self._hoverable("evidence")
        y += 22
        if collapsed:
            return y

        lines: list[tuple[str, str]] = []
        calibration = data["calibration"]
        if calibration["text"]:
            lines.append((calibration["text"], calibration["color"]))
        proof = data["proof"]
        lines.append((proof["text"], proof["color"]))
        if data["lesson"]:
            lines.append((data["lesson"], COLORS["wait"]))
        for note in data["tuning"]:
            lines.append((note, COLORS["accent"]))
        if data["session"].get("taught"):
            lines.append((data["session"]["taught"], COLORS["accent"]))
        verdict = data["verdict"]
        lines.append((f"Duration fit {verdict['duration_display']} · "
                      f"suggested {verdict['recommended']}", COLORS["dim"]))

        for text, colour in lines:
            y = self._wrapped(PAD + 2, y, str(text), colour)
        return y + 4

    def _draw_risk(self, data: dict[str, Any], y: int) -> int:
        risk = data["risk"]
        collapsed = risk.get("collapsed")
        tone = COLORS["text"] if self._hot("riskhead") else COLORS["faint"]
        self._text(PAD + 2, y + 8, ("▸ " if collapsed else "▾ ") + "RISK",
                   self.f_caption, tone, "w", tags="frame riskhead")
        percent = risk["risk_percent"]
        self._text(PANEL_WIDTH - PAD - 2, y + 8,
                   f"{percent:.1f}% of balance", self.f_label,
                   COLORS["put"] if percent > 10
                   else COLORS["wait"] if percent > 5 else COLORS["faint"], "e",
                   tags="frame riskhead")
        self._clickable("riskhead", self.on_toggle_risk)
        self._hoverable("riskhead")
        y += 22
        if collapsed:
            for key in ("balance", "stake", "payout"):
                self._hide_entry(key)
            return y

        for key, label, value in (
            ("balance", "Balance", f"{risk['balance']:.2f}"),
            ("stake", "Stake", f"{risk['stake']:.2f}"),
        ):
            self._text(PAD + 2, y + 11, label, self.f_small, COLORS["dim"], "w")
            self._place_entry(key, PANEL_WIDTH - PAD - 4, y + 11, "e")
            self._set_entry(self._entries[key], value)
            y += 24

        self._text(PAD + 2, y + 9, "Payout", self.f_small, COLORS["dim"], "w")
        self._place_entry("payout", PANEL_WIDTH - PAD - 4, y + 9, "e")
        self._set_entry(self._entries["payout"], data["tiles"]["payout"])
        y += 24

        for label, value in (
            ("Win returns", f"+{risk['potential_profit']:.2f}"),
            ("Break-even rate", f"{risk['breakeven_rate']:.1f}%"),
        ):
            self._text(PAD + 2, y + 8, label, self.f_small, COLORS["dim"], "w")
            self._text(PANEL_WIDTH - PAD - 4, y + 8, value, self.f_mono,
                       COLORS["text"], "e")
            y += 18

        # The money warnings live here, with the money — not under the call.
        # Folded away with the rest of the block, so they are there to read
        # and never in the way of the reason a setup was refused.
        for line in list(risk.get("warnings", []))[:3]:
            y = self._wrapped(PAD + 2, y + 2, f"⚠ {line}", COLORS["wait"])
        return y + 4

    def _draw_footer(self, data: dict[str, Any], y: int) -> int:
        y = self._wrapped(PAD + 2, y + 4, data["reason"], COLORS["dim"])
        # Only what the chart said. The risk block's warnings are about money
        # — the stake, the balance, what a payout would need to break even —
        # and appending them here put "a 50% payout needs a 66.7% win rate"
        # underneath the reasoning for a WAIT, as though the broker's pricing
        # were evidence about the market. It is not, it was never asked for,
        # and it pushed the actual reasons off a list that shows four.
        warnings = list(data["warnings"])
        for line in warnings[:4]:
            y = self._wrapped(PAD + 2, y, f"⚠ {line}", COLORS["wait"])
        self._text(PANEL_WIDTH // 2, y + 10,
                   "Analysis only — not a trading recommendation.",
                   self.f_label, COLORS["faint"], "center")
        return y + 22

    def _measured_height(self, text: str, width: int) -> int:
        """How tall a wrapped block will render, asked of the canvas itself.

        A probe item is drawn, measured and removed — never estimated from
        the string's width. Tk breaks at word boundaries, and estimating is
        how the verdict face got clipped twice and the brake's explanation
        ran over the market strip.
        """
        if not text:
            return 0
        probe = self.c.create_text(
            0, 0, text=text, font=self.f_label, anchor="nw", width=width,
            tags="frame probe",
        )
        height = 13
        try:
            bounds = self.c.bbox(probe)
            if bounds:
                height = int(bounds[3] - bounds[1])
        except tk.TclError:  # pragma: no cover - window closing
            pass
        self.c.delete("probe")
        return max(13, height)

    def _wrapped(self, x: int, y: int, text: str, colour: str) -> int:
        """One block of wrapped prose, returning where the next thing starts."""
        if not text:
            return y
        item = self.c.create_text(
            x, y, text=text, font=self.f_label, fill=colour, anchor="nw",
            width=INNER - 4, tags="frame",
        )
        try:
            bounds = self.c.bbox(item)
            return (bounds[3] + 3) if bounds else y + 14
        except tk.TclError:  # pragma: no cover - window closing
            return y + 14

    # -- animation ----------------------------------------------------------

    PULSE_FRAMES = 24  # about two seconds at the overlay's repaint rate
    # How many distinct frames a pulse is allowed. Quantised on purpose: every
    # drawn shape is cached by appearance, so a continuously varying strength
    # would mean a fresh supersampled render — and for the glowing card a
    # fresh Gaussian blur — several times a second, to make a difference
    # nobody can see. Eight steps is a smooth breath and eight cached images.
    PULSE_STEPS = 8

    def _pulse_step(self) -> int:
        """Which frame of the pulse this repaint is on."""
        if not self.animate:
            # Resting at a little over half: lit enough to read as live,
            # still enough to read as still.
            return int(round(self.PULSE_STEPS * 0.6))
        half = self.PULSE_FRAMES / 2
        raw = abs(half - (self._pulse % self.PULSE_FRAMES)) / half
        return int(round(raw * self.PULSE_STEPS))

    def _pulse_phase(self) -> float:
        """0..1 and back again, so a pulse breathes rather than blinks."""
        return self._pulse_step() / self.PULSE_STEPS

    def _pulse_toward(self, colour: str, target: str | None = None) -> str:
        return gfx.mix(colour, target or COLORS["text"], self._pulse_phase() * 0.55)

    def _ease_score(self, score: float | None) -> float | None:
        """Move the dial toward a new reading instead of snapping to it.

        A quarter of the remaining distance per repaint settles in about a
        third of a second — long enough to be seen moving, short enough that
        the dial is never showing a number the panel is not.
        """
        if score is None:
            self._score_shown = None
            return None
        target = max(0.0, min(100.0, float(score)))
        if (
            not self.animate
            or self._score_shown is None
            or abs(target - self._score_shown) < 0.5
        ):
            self._score_shown = target
        else:
            self._score_shown += (target - self._score_shown) * 0.25
        return round(self._score_shown, 1)

    # -- window -------------------------------------------------------------

    def _fit(self, height: int) -> None:
        """Grow or shrink the window to fit what was drawn, without jitter."""
        if self._collapsed:
            return
        try:
            wanted = max(320, int(height))
            wanted = min(wanted, self.root.winfo_screenheight() - 60)
            # A few pixels of slack stops a one-pixel text difference from
            # resizing the window on every single repaint.
            if abs(wanted - self._height) > 6:
                self._height = wanted
                self.root.geometry(f"{PANEL_WIDTH}x{wanted}")
                self.c.configure(height=wanted)
        except tk.TclError:  # pragma: no cover - window closing
            pass

    def _drag_start(self, event) -> None:
        # Only the header is a handle. Bound to the whole canvas, a press on
        # SCAN followed by the smallest twitch dragged the window instead of
        # pressing the button.
        self._dragging = event.y <= HEADER_BOTTOM
        self._drag_origin = (event.x_root, event.y_root)
        self._window_origin = (self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event) -> None:
        if not self._dragging:
            return
        x = self._window_origin[0] + (event.x_root - self._drag_origin[0])
        y = self._window_origin[1] + (event.y_root - self._drag_origin[1])
        self.root.geometry(f"+{x}+{y}")

    def _scan_clicked(self) -> None:
        if self.vm.scan.scanning:
            return  # ignore repeat presses mid-scan
        self.on_scan()

    def toggle_collapse(self) -> None:
        """Shrink to just the header, so the panel can be parked out of the way."""
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.root.geometry(f"{PANEL_WIDTH}x{HEADER_BOTTOM + PAD}")
        else:
            self.root.geometry(f"{PANEL_WIDTH}x{self._height}")

    def _commit_payout(self, text: str) -> None:
        cleaned = text.strip().rstrip("%+").replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            return
        if value > 0:
            self.on_payout(value)

    def _commit_stake(self, text: str) -> None:
        """Empty text returns to percent-derived sizing; a number overrides it."""
        cleaned = text.strip().replace(",", "")
        if not cleaned:
            self.on_stake(None)
            return
        try:
            value = float(cleaned)
        except ValueError:
            return  # leave the previous stake untouched on a typo
        if value > 0:
            self.on_stake(value)

    def _commit_balance(self, text: str) -> None:
        cleaned = text.strip().replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            return
        if value > 0:
            self.on_balance(value)

    def _close(self) -> None:
        self.on_close()
        self.destroy()

    def destroy(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()
