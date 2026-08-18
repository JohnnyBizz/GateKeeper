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

Only the fields that must accept typing are still real widgets, dropped onto
the canvas where they are needed.

This module only draws. Every decision about *what* to draw is made in
``viewmodel.py``, which is why the panel's behaviour can be tested without a
display.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable

from . import graphics as gfx
from .viewmodel import COLORS, OverlayViewModel

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
HEADER_BOTTOM = PAD + 38


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
        position: tuple[int, int] = (40, 80),
        opacity: float = 0.96,
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

        sans = ["Inter", "Segoe UI Variable", "Segoe UI", "SF Pro Text",
                "Helvetica Neue", "DejaVu Sans", "Arial"]
        mono = ["JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono",
                "Menlo", "DejaVu Sans Mono", "Courier"]

        # A type scale rather than nine independent guesses, and nothing below
        # eight point: this panel is meant to be glanced at from across a desk
        # rather than studied.
        self.f_brand = pick(sans, 12, "bold")
        self.f_caption = pick(sans, 8, "bold")
        self.f_label = pick(sans, 8)
        self.f_small = pick(sans, 9)
        self.f_body = pick(sans, 10)
        self.f_pair = pick(sans, 14, "bold")
        self.f_verdict = pick(sans, 27, "bold")
        self.f_score = pick(sans, 20, "bold")
        self.f_button = pick(sans, 11, "bold")
        self.f_mono = pick(mono, 10)
        self.f_price = pick(mono, 15, "bold")
        self.f_stat = pick(mono, 13, "bold")

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
            bg=COLORS["raised"], fg=COLORS["text"],
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
        fill: str = COLORS["panel"], fill_to: str | None = None,
        border: str | None = None, glow: str | None = None,
        glow_strength: float = 0.0, tags: str = "",
    ) -> None:
        """A rounded surface. Glowing ones are offset by their own halo."""
        key = ("card", w, h, radius, fill, fill_to, border, glow,
               round(glow_strength, 2))
        pad = 12 if glow else 0
        self._image(
            x - pad, y - pad, key,
            lambda: gfx.card(
                w, h, radius=radius, fill=fill, fill_to=fill_to, border=border,
                glow=glow, glow_strength=glow_strength,
            ),
            tags=tags,
        )

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

    # -- rendering ----------------------------------------------------------

    def refresh(self) -> None:
        """Paint the current view model onto the canvas."""
        self._pulse += 1
        data = self.vm.render()

        self.c.delete("frame")
        y = self._draw_header(data)
        if self._collapsed:
            # Parked out of the way. Everything below the header is drawn into
            # a window 56 pixels tall, which is work nobody can see.
            for key in self._entry_items:
                self._hide_entry(key)
            return
        y = self._draw_market(data, y)
        y = self._draw_watchlist(data, y)
        y = self._draw_signal(data, y)
        y = self._draw_trend(data, y)
        y = self._draw_chart(data, y)
        y = self._draw_actions(data, y)
        y = self._draw_session(data, y)
        y = self._draw_details(data, y)
        y = self._draw_risk(data, y)
        y = self._draw_footer(data, y)
        self._fit(y + PAD)

    # -- sections -----------------------------------------------------------

    def _draw_header(self, data: dict[str, Any]) -> int:
        header = data["header"]
        self._card(PAD, PAD, INNER, 38, radius=11, fill="#18233a",
                   fill_to="#131c2e", border=COLORS["line"])
        colour = header["status_color"]
        self._image(PAD + 12, PAD + 14, ("dot", colour),
                    lambda: gfx.pill(9, 9, color=colour, opacity=255))
        self._text(PAD + 28, PAD + 19, "GATEKEEPER", self.f_brand,
                   COLORS["text"], "w")

        label = header["status"]
        width = max(52, len(label) * 6 + 16)
        self._image(PANEL_WIDTH - PAD - 62 - width, PAD + 11,
                    ("pill", width, colour),
                    lambda: gfx.pill(width, 16, color=colour))
        self._text(PANEL_WIDTH - PAD - 62 - width // 2, PAD + 19, label,
                   self.f_caption, colour, "center")

        for index, (glyph, command, tag) in enumerate(
            (("⚙", self.on_settings, "gear"),
             ("–", self.toggle_collapse, "fold"),
             ("✕", self._close, "shut"))
        ):
            self._text(PANEL_WIDTH - PAD - 46 + index * 16, PAD + 19, glyph,
                       self.f_body, COLORS["faint"], "center", tags=f"frame {tag}")
            self._clickable(tag, command)
        return PAD + 38 + 8

    def _draw_market(self, data: dict[str, Any], y: int) -> int:
        """The pair, the price, and the shape of the session so far."""
        tiles, chart = data["tiles"], data["chart"]
        height = 104 if chart["ready"] else 60
        self._card(PAD, y, INNER, height, radius=14, fill=COLORS["raised"],
                   fill_to=COLORS["panel"], border=COLORS["line"])

        self._place_entry("pair", PAD + 12, y + 18)
        self._set_entry(self._entries["pair"], tiles["pair"])

        self._text(PAD + 12, y + 40,
                   f"{tiles['chart']} CHART   ·   {tiles['time']} EXPIRY",
                   self.f_label, COLORS["faint"], "w")

        colour = (
            COLORS["call"] if chart.get("rising") else COLORS["put"]
        ) if chart["ready"] else COLORS["text"]
        self._text(PANEL_WIDTH - PAD - 12, y + 18, data["price"],
                   self.f_price, colour, "e")
        if chart["ready"]:
            self._text(PANEL_WIDTH - PAD - 12, y + 40, chart["change_label"],
                       self.f_label, colour, "e")

            closes = chart["closes"][-140:]
            key = (colour, len(closes), round(closes[0], 6), round(closes[-1], 6))
            self._image_slot(
                PAD + 12, y + 52, "spark", key,
                lambda: gfx.sparkline(INNER - 24, 44, closes, color=colour),
            )
        return y + height + 8

    def _draw_watchlist(self, data: dict[str, Any], y: int) -> int:
        rows = data["watchlist"]
        if not rows:
            return y
        width = (INNER - 6) // 2
        for index, row in enumerate(rows):
            x = PAD + (index % 2) * (width + 6)
            top = y + (index // 2) * 30
            tag = f"watch{index}"
            active, colour = row["active"], row["color"]
            self._card(x, top, width, 26, radius=8,
                       fill="#1d2a40" if active else "#141d2b",
                       border=colour if active else COLORS["line"], tags=f"frame {tag}")
            self._text(x + 9, top + 13, row["label"], self.f_caption,
                       COLORS["text"] if active else COLORS["dim"], "w",
                       tags=f"frame {tag}")
            score = row["score"]
            if score is not None:
                self._text(x + width - 9, top + 13, f"{score:.0f}", self.f_mono,
                           colour, "e", tags=f"frame {tag}")
            # The expiry this one was scored at, whenever that is not the
            # expiry the platform is set to. Without it a green tab invites a
            # trade at the wrong length — which is a different trade from the
            # one that passed, and often one the engine would have refused.
            needs = row.get("needs")
            if needs:
                self._text(x + width - 34, top + 13, f"▸{needs}", self.f_label,
                           COLORS["wait"], "e", tags=f"frame {tag}")
            self._clickable(
                tag,
                lambda name=row["asset"], tf=row.get("timeframe"): self.on_asset(
                    name, tf
                ),
            )
        return y + ((len(rows) + 1) // 2) * 30 + 8

    def _draw_signal(self, data: dict[str, Any], y: int) -> int:
        verdict, entry = data["verdict"], data["entry"]
        scanning = data["scan"]["scanning"]
        colour = verdict["color"]
        height = 214

        # The border breathes only while there is something to act on, so the
        # movement means "this one" rather than "the app is running".
        strength = 0.95 * self._pulse_phase() if verdict["actionable"] else 0.0
        self._card(
            PAD, y, INNER, height, radius=18, fill="#122033", fill_to="#0c1524",
            border=colour if verdict["actionable"] else COLORS["line"],
            glow=colour if verdict["actionable"] else None,
            glow_strength=strength,
        )

        self._text(PAD + 16, y + 15, "SIGNAL", self.f_caption, COLORS["faint"], "w")
        if verdict["state"] not in ("ACTIVE", "IDLE"):
            self._text(PANEL_WIDTH - PAD - 16, y + 15, verdict["state"],
                       self.f_caption, COLORS["wait"], "e")
        elif verdict["actionable"]:
            self._text(PANEL_WIDTH - PAD - 16, y + 15, "GATE PASSED",
                       self.f_caption, colour, "e")

        # The dial holds the score and nothing else; the arrow and the word sit
        # beside it, so neither has to share the middle.
        score = verdict["score"]
        shown = self._ease_score(score)
        self._image(PAD + 16, y + 34, ("gauge", int(shown if shown is not None else -1), colour),
                    lambda: gfx.arc_gauge(88, shown, color=colour,
                                          track="#1c2739", thickness=7))
        self._text(PAD + 60, y + 72, "--" if score is None else f"{score:.0f}",
                   self.f_score, COLORS["text"], "center")
        self._text(PAD + 60, y + 92, "/100", self.f_label, COLORS["faint"], "center")

        if scanning:
            # A narrower face than the verdict's: "SCANNING" is eight
            # characters where "BUY" is three, and at the verdict's size it
            # ran off the edge of the panel.
            self._text(PAD + 118, y + 58, "SCANNING", self.f_score,
                       COLORS["dim"], "w")
            phase = (self._pulse % 40) / 40.0
            self._image(PAD + 118, y + 86, ("shimmer", round(phase, 2)),
                        lambda: gfx.shimmer(INNER - 140, 5, phase,
                                            color=COLORS["accent"]))
        else:
            self._image(PAD + 118, y + 42,
                        ("glyph", verdict["direction"], colour),
                        lambda: gfx.direction_glyph(26, verdict["direction"], colour))
            self._text(PAD + 152, y + 58, verdict["direction_label"],
                       self.f_verdict, colour, "w")

            badge, badge_colour = verdict["badge"], verdict["badge_color"]
            if badge and badge != "--":
                width = max(96, len(badge) * 7 + 26)
                self._image(PAD + 118, y + 82, ("badge", width, badge_colour),
                            lambda: gfx.pill(width, 18, color=badge_colour))
                self._text(PAD + 118 + width // 2, y + 91, badge,
                           self.f_caption, badge_colour, "center")

            pattern = verdict["pattern"] or ""
            if pattern and pattern != "--":
                self._text(PAD + 118, y + 110, pattern[:34], self.f_label,
                           COLORS["dim"], "w")

        # When to get in, in its own inset. The ring is the countdown and
        # carries no number: a clock face inside a 32px ring is a number
        # fighting the shape drawn to replace it, and the ring loses.
        self._card(PAD + 12, y + 140, INNER - 24, 54, radius=11,
                   fill="#17223a", border=COLORS["line"])
        ready, urgent = entry["ready"], entry["urgent"]
        tone = (
            self._pulse_toward(colour) if ready and urgent
            else colour if ready else COLORS["faint"]
        )
        left = max(0.0, min(1.0, 1.0 - entry["progress"]))
        left = round(left * RING_STEPS) / RING_STEPS
        self._image(PAD + 24, y + 152, ("ring", left, tone),
                    lambda: gfx.countdown_ring(30, left, color=tone,
                                               track="#1c2739", thickness=4))
        text_x = PAD + 64
        self._text(text_x, y + 158, entry["text"], self.f_button, tone, "w")
        if entry["clock"]:
            self._text(PANEL_WIDTH - PAD - 24, y + 158, entry["clock"],
                       self.f_mono, tone, "e")
        # Wrapped rather than cut: the line explains what the countdown is
        # counting toward, and half of that explains nothing.
        self.c.create_text(
            text_x, y + 170, text=entry["detail"], font=self.f_label,
            fill=COLORS["dim"], anchor="nw",
            width=PANEL_WIDTH - PAD - 24 - text_x, tags="frame",
        )
        return y + height + 8

    def _draw_trend(self, data: dict[str, Any], y: int) -> int:
        trend = data["trend"]
        if trend["blanked"]:
            return y
        self._card(PAD, y, INNER, 34, radius=10, fill=COLORS["panel"],
                   border=COLORS["line"])
        self._text(PAD + 14, y + 17, "MARKET", self.f_caption,
                   COLORS["faint"], "w")
        self._text(PAD + 72, y + 17, f"{trend['arrow']}  {trend['label']}",
                   self.f_small, trend["color"], "w")
        for index, view in enumerate(trend["views"][:3]):
            x = PANEL_WIDTH - PAD - 142 + index * 46
            self._text(x, y + 18, view["name"], self.f_label, COLORS["faint"], "w")
            self._text(x + 32, y + 17, view["arrow"], self.f_caption,
                       view["color"], "w")
        return y + 34 + 8

    def _draw_chart(self, data: dict[str, Any], y: int) -> int:
        """The candles the analysis is actually reading."""
        chart = data["chart"]
        bars = chart["bars"][-34:]
        if len(bars) < 4:
            return y
        self._card(PAD, y, INNER, 88, radius=14, fill=COLORS["panel"],
                   border=COLORS["line"])
        self._text(PAD + 14, y + 13, f"LAST {len(bars)} CANDLES", self.f_label,
                   COLORS["faint"], "w")
        key = (len(bars), round(bars[0][0], 6), round(bars[-1][3], 6))
        self._image_slot(
            PAD + 14, y + 24, "candles", key,
            lambda: gfx.candles(
                INNER - 28, 58,
                [gfx.Bar(*bar) for bar in bars],
                up=COLORS["call"], down=COLORS["put"],
            ),
        )
        return y + 88 + 8

    def _draw_actions(self, data: dict[str, Any], y: int) -> int:
        scanning = data["scan"]["scanning"]
        wide = INNER - 118
        self._card(PAD, y, wide, 38, radius=11,
                   fill="#243449" if scanning else "#1c8f4a",
                   fill_to="#1b2739" if scanning else "#15803d",
                   border=COLORS["line"] if scanning else "#2fbb66",
                   tags="frame scan")
        self._text(PAD + wide // 2, y + 19, "SCANNING…" if scanning else "SCAN",
                   self.f_button, COLORS["dim"] if scanning else "#eafff2",
                   "center", tags="frame scan")
        self._clickable("scan", self._scan_clicked)

        self._card(PAD + wide + 8, y, 110, 38, radius=11, fill=COLORS["raised"],
                   border=COLORS["line"], tags="frame reset")
        self._text(PAD + wide + 63, y + 19, "RESET", self.f_button,
                   COLORS["dim"], "center", tags="frame reset")
        self._clickable("reset", self.on_reset)
        return y + 38 + 8

    def _draw_session(self, data: dict[str, Any], y: int) -> int:
        session, risk = data["session"], data["risk"]
        width = (INNER - 12) // 3
        cells = (
            ("WINS", str(session["wins"]), COLORS["call"], (1, 0), "win"),
            ("LOSSES", str(session["losses"]), COLORS["put"], (0, 1), "loss"),
            ("CALLED", str(session.get("calls", 0)), COLORS["accent"], None, ""),
        )
        for index, (label, value, colour, delta, tag) in enumerate(cells):
            x = PAD + index * (width + 6)
            self._card(x, y, width, 52, radius=10, fill=COLORS["panel"],
                       border=COLORS["line"])
            self._text(x + width // 2, y + 12, label, self.f_label,
                       COLORS["faint"], "center")
            self._text(x + width // 2, y + 30, value, self.f_stat, colour, "center")
            if delta is None:
                continue
            wins, losses = delta
            for sign, symbol, offset in ((1, "+", -14), (-1, "−", 14)):
                mark = f"{tag}{'up' if sign > 0 else 'dn'}"
                self._text(x + width // 2 + offset, y + 44, symbol,
                           self.f_caption, colour, "center", tags=f"frame {mark}")
                self._clickable(
                    mark,
                    lambda w=wins * sign, l=losses * sign: self.on_adjust(w, l),
                )
        y += 52 + 6

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
        self._text(PAD + 2, y + 8, ("▸ " if collapsed else "▾ ") + "EVIDENCE",
                   self.f_caption, COLORS["faint"], "w", tags="frame evidence")
        self._text(PANEL_WIDTH - PAD - 2, y + 8, details["summary"],
                   self.f_label, COLORS["faint"], "e", tags="frame evidence")
        self._clickable("evidence", self.on_toggle_details)
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
        self._text(PAD + 2, y + 8, ("▸ " if collapsed else "▾ ") + "RISK",
                   self.f_caption, COLORS["faint"], "w", tags="frame riskhead")
        percent = risk["risk_percent"]
        self._text(PANEL_WIDTH - PAD - 2, y + 8,
                   f"{percent:.1f}% of balance", self.f_label,
                   COLORS["put"] if percent > 10
                   else COLORS["wait"] if percent > 5 else COLORS["faint"], "e",
                   tags="frame riskhead")
        self._clickable("riskhead", self.on_toggle_risk)
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
        return y + 4

    def _draw_footer(self, data: dict[str, Any], y: int) -> int:
        y = self._wrapped(PAD + 2, y + 4, data["reason"], COLORS["dim"])
        warnings = list(data["warnings"]) + list(data["risk"].get("warnings", []))
        for line in warnings[:4]:
            y = self._wrapped(PAD + 2, y, f"⚠ {line}", COLORS["wait"])
        self._text(PANEL_WIDTH // 2, y + 10,
                   "Analysis only — not a trading recommendation.",
                   self.f_label, COLORS["faint"], "center")
        return y + 22

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
        if self._score_shown is None or abs(target - self._score_shown) < 0.5:
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
            self.root.geometry(f"{PANEL_WIDTH}x56")
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
