"""The always-on-top overlay window.

Tkinter, on purpose: it ships with Python on Windows and macOS (and is one
`apt install python3-tk` away on Linux), so the overlay adds no pip dependency
to a tool the user is meant to be able to install and run in two commands.

This module only draws. Every decision about *what* to draw is made in
``viewmodel.py``, which is why the panel's behaviour can be tested without a
display.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable

from .viewmodel import COLORS, OverlayViewModel

PANEL_WIDTH = 320


class OverlayPanel:
    """A compact, draggable, frameless panel that floats above other windows."""

    def __init__(
        self,
        view_model: OverlayViewModel,
        *,
        on_scan: Callable[[], None] | None = None,
        on_reset: Callable[[], None] | None = None,
        on_adjust: Callable[[int, int], None] | None = None,
        on_asset: Callable[[str], None] | None = None,
        on_stake: Callable[[float | None], None] | None = None,
        on_balance: Callable[[float], None] | None = None,
        on_settings: Callable[[], None] | None = None,
        on_close: Callable[[], None] | None = None,
        on_toggle_risk: Callable[[], None] | None = None,
        on_payout: Callable[[float], None] | None = None,
        position: tuple[int, int] = (40, 80),
        opacity: float = 0.96,
    ) -> None:
        self.vm = view_model
        self.on_scan = on_scan or (lambda: None)
        self.on_reset = on_reset or (lambda: None)
        self.on_adjust = on_adjust or (lambda w, l: None)
        self.on_asset = on_asset or (lambda a: None)
        self.on_stake = on_stake or (lambda s: None)
        self.on_balance = on_balance or (lambda b: None)
        self.on_settings = on_settings or (lambda: None)
        self.on_close = on_close or (lambda: None)
        self.on_toggle_risk = on_toggle_risk or (lambda: None)
        self.on_payout = on_payout or (lambda p: None)

        self.root = tk.Tk()
        self.root.title("GateKeeper")
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry(f"{PANEL_WIDTH}x680+{position[0]}+{position[1]}")

        # Frameless and always on top. Both are best-effort: some window
        # managers refuse one or the other, and a panel with a title bar is
        # still perfectly usable, so a refusal must not be fatal.
        try:
            self.root.overrideredirect(True)
        except tk.TclError:
            pass
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            self.root.attributes("-alpha", opacity)
        except tk.TclError:
            pass

        self._collapsed = False
        self._drag_origin = (0, 0)
        self._widgets: dict[str, Any] = {}
        self._dots: list[tk.Label] = []

        self._build_fonts()
        self._build()

        # Size to the content rather than to a guessed height. Font metrics
        # differ enough between platforms that any fixed number clips the
        # footer somewhere, and the disclaimer is the first thing to go.
        self._natural_height = self._fit_height(position)

    # -- fonts -------------------------------------------------------------

    def _build_fonts(self) -> None:
        def pick(candidates: list[str], size: int, weight: str = "normal") -> tkfont.Font:
            available = set(tkfont.families())
            for name in candidates:
                if name in available:
                    return tkfont.Font(family=name, size=size, weight=weight)
            return tkfont.Font(size=size, weight=weight)

        sans = ["Inter", "Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial"]
        mono = ["JetBrains Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Courier"]

        self.f_title = pick(sans, 11, "bold")
        self.f_label = pick(sans, 7)
        self.f_body = pick(sans, 9)
        self.f_small = pick(sans, 8)
        self.f_verdict = pick(sans, 30, "bold")
        self.f_arrow = pick(sans, 15)
        self.f_mono = pick(mono, 9)
        self.f_mono_big = pick(mono, 11, "bold")
        self.f_badge = pick(sans, 7, "bold")

    # -- construction -------------------------------------------------------

    def _section(self, parent: tk.Widget, pady: tuple[int, int] = (0, 0)) -> tk.Frame:
        frame = tk.Frame(parent, bg=COLORS["panel"])
        frame.pack(fill="x", padx=8, pady=pady)
        return frame

    def _divider(self, parent: tk.Widget) -> None:
        tk.Frame(parent, bg=COLORS["border"], height=1).pack(fill="x", padx=8, pady=4)

    def _build(self) -> None:
        root = tk.Frame(self.root, bg=COLORS["panel"], highlightthickness=1)
        root.configure(highlightbackground=COLORS["border"], highlightcolor=COLORS["border"])
        root.pack(fill="both", expand=True, padx=1, pady=1)
        self._widgets["root"] = root

        self._build_header(root)
        self._body = tk.Frame(root, bg=COLORS["panel"])
        self._body.pack(fill="both", expand=True)

        self._build_tiles(self._body)
        self._build_verdict(self._body)
        self._build_score(self._body)
        self._build_buttons(self._body)
        self._divider(self._body)
        self._build_session(self._body)
        self._divider(self._body)
        self._build_risk(self._body)
        self._build_footer(self._body)

    def _build_header(self, parent: tk.Widget) -> None:
        header = tk.Frame(parent, bg=COLORS["raised"])
        header.pack(fill="x")
        # The header doubles as the drag handle, since the window is frameless.
        for widget in (header,):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        left = tk.Frame(header, bg=COLORS["raised"])
        left.pack(side="left", padx=8, pady=6)
        left.bind("<Button-1>", self._drag_start)
        left.bind("<B1-Motion>", self._drag_move)

        title = tk.Label(
            left, text="◪ GATEKEEPER", font=self.f_title,
            bg=COLORS["raised"], fg=COLORS["accent"],
        )
        title.pack(side="left")
        title.bind("<Button-1>", self._drag_start)
        title.bind("<B1-Motion>", self._drag_move)

        self._widgets["status"] = tk.Label(
            left, text="OFFLINE", font=self.f_label,
            bg=COLORS["raised"], fg=COLORS["put"],
        )
        self._widgets["status"].pack(side="left", padx=(8, 0))

        controls = tk.Frame(header, bg=COLORS["raised"])
        controls.pack(side="right", padx=6)
        self._icon_button(controls, "⚙", self.on_settings)
        self._icon_button(controls, "–", self.toggle_collapse)
        self._icon_button(controls, "✕", self._close)

    def _icon_button(self, parent: tk.Widget, text: str, command) -> tk.Label:
        label = tk.Label(
            parent, text=text, font=self.f_body,
            bg=COLORS["raised"], fg=COLORS["dim"], cursor="hand2", padx=6,
        )
        label.pack(side="left")
        label.bind("<Button-1>", lambda _event: command())
        label.bind("<Enter>", lambda _e, w=label: w.configure(fg=COLORS["text"]))
        label.bind("<Leave>", lambda _e, w=label: w.configure(fg=COLORS["dim"]))
        return label

    def _entry(
        self, parent: tk.Widget, *, width: int, on_commit: Callable[[str], None]
    ) -> tk.Entry:
        """A themed Entry that commits on Enter or when focus leaves it."""
        entry = tk.Entry(
            parent, font=self.f_mono, width=width,
            bg=COLORS["raised"], fg=COLORS["text"],
            insertbackground=COLORS["text"], relief="flat",
            highlightthickness=1, highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
        )

        def commit(_event=None):
            on_commit(entry.get())
            # Give focus back to the window so the field stops swallowing keys.
            self.root.focus_set()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        return entry

    @staticmethod
    def _set_entry(entry: tk.Entry, text: str) -> None:
        """Update an entry's text — but never while the user is typing in it."""
        if entry.focus_displayof() is entry:
            return
        if entry.get() != text:
            entry.delete(0, "end")
            entry.insert(0, text)

    def _build_tiles(self, parent: tk.Widget) -> None:
        row = self._section(parent, pady=(8, 4))
        for key, caption in (("pair", "PAIR"), ("payout", "PAYOUT"), ("time", "TIME")):
            tile = tk.Frame(row, bg=COLORS["raised"], highlightthickness=1)
            tile.configure(highlightbackground=COLORS["border"])
            tile.pack(side="left", fill="both", expand=True, padx=2)
            tk.Label(
                tile, text=caption, font=self.f_label,
                bg=COLORS["raised"], fg=COLORS["faint"],
            ).pack(anchor="w", padx=6, pady=(4, 0))
            if key == "pair":
                # Editable: GateKeeper cannot read the pair's name off the
                # screen, so when the user switches charts on the platform
                # they rename it here and the analysis restarts.
                value = self._entry(tile, width=9, on_commit=self.on_asset)
                value.pack(anchor="w", padx=4, pady=(0, 5))
            elif key == "payout":
                # Editable, and it matters more than it looks. Break-even is
                # 52.1% at a 92% payout and 55.6% at 80%, so a stale number
                # here moves the bar that every measurement in this panel is
                # judged against — quietly, and in the flattering direction.
                value = self._entry(tile, width=9, on_commit=self._commit_payout)
                value.pack(anchor="w", padx=4, pady=(0, 5))
            else:
                value = tk.Label(
                    tile, text="--", font=self.f_mono,
                    bg=COLORS["raised"], fg=COLORS["text"],
                )
                value.pack(anchor="w", padx=6, pady=(0, 5))
            self._widgets[f"tile_{key}"] = value

        # Chart timeframe is shown apart from the trade duration on purpose:
        # confusing the two is the single most consequential mistake here.
        chart_row = self._section(parent, pady=(0, 4))
        self._widgets["tile_chart"] = tk.Label(
            chart_row, text="chart --", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["faint"],
        )
        self._widgets["tile_chart"].pack(side="left", padx=2)
        self._widgets["price"] = tk.Label(
            chart_row, text="--", font=self.f_mono,
            bg=COLORS["panel"], fg=COLORS["dim"],
        )
        self._widgets["price"].pack(side="right", padx=2)

    def _build_verdict(self, parent: tk.Widget) -> None:
        box = tk.Frame(parent, bg=COLORS["bg"], highlightthickness=1)
        box.configure(highlightbackground=COLORS["border"])
        box.pack(fill="x", padx=10, pady=4)
        self._widgets["verdict_box"] = box

        tk.Label(
            box, text="S I G N A L", font=self.f_label,
            bg=COLORS["bg"], fg=COLORS["faint"],
        ).pack(pady=(8, 0))

        self._widgets["arrow"] = tk.Label(
            box, text="", font=self.f_arrow, bg=COLORS["bg"], fg=COLORS["neutral"]
        )
        self._widgets["arrow"].pack()

        self._widgets["verdict"] = tk.Label(
            box, text="--", font=self.f_verdict, bg=COLORS["bg"], fg=COLORS["neutral"]
        )
        self._widgets["verdict"].pack(pady=(0, 2))

        # The scanning indicator occupies the same space the verdict does, so
        # the panel does not jump between states.
        dots = tk.Frame(box, bg=COLORS["bg"])
        self._widgets["dots_frame"] = dots
        for _ in range(3):
            dot = tk.Label(dots, text="●", font=self.f_arrow, bg=COLORS["bg"], fg=COLORS["faint"])
            dot.pack(side="left", padx=4)
            self._dots.append(dot)

        self._widgets["state"] = tk.Label(
            box, text="", font=self.f_label, bg=COLORS["bg"], fg=COLORS["faint"]
        )
        self._widgets["state"].pack(pady=(0, 8))

    def _build_score(self, parent: tk.Widget) -> None:
        wrap = self._section(parent, pady=(2, 2))

        self._widgets["score_bar"] = tk.Canvas(
            wrap, height=6, bg=COLORS["raised"], highlightthickness=0
        )
        self._widgets["score_bar"].pack(fill="x", padx=2, pady=(2, 4))

        # How many trades this is calling for, spelled out as a number. WAIT
        # and NO TRADE both mean zero while looking nothing alike, and a count
        # is what you can read at a glance without parsing a word.
        take = tk.Frame(wrap, bg=COLORS["panel"])
        take.pack(fill="x", pady=(0, 3))
        tk.Label(
            take, text="TAKE NOW", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["faint"],
        ).pack(side="left", padx=2)
        self._widgets["take_now"] = tk.Label(
            take, text="--", font=self.f_mono,
            bg=COLORS["panel"], fg=COLORS["neutral"],
        )
        self._widgets["take_now"].pack(side="right", padx=2)

        row = tk.Frame(wrap, bg=COLORS["panel"])
        row.pack(fill="x")

        self._widgets["score"] = tk.Label(
            row, text="-- / 100", font=self.f_mono_big,
            bg=COLORS["panel"], fg=COLORS["neutral"],
        )
        self._widgets["score"].pack(side="left", padx=2)

        self._widgets["badge"] = tk.Label(
            row, text="--", font=self.f_badge,
            bg=COLORS["raised"], fg=COLORS["neutral"], padx=6, pady=1,
        )
        self._widgets["badge"].pack(side="right", padx=2)

        self._widgets["pattern"] = tk.Label(
            wrap, text="--", font=self.f_small, bg=COLORS["panel"], fg=COLORS["dim"]
        )
        self._widgets["pattern"].pack(anchor="w", padx=2, pady=(2, 0))

        # What the score above was actually worth here. Sits directly under it
        # on purpose: the score is a number the engine made up, and this is the
        # measurement that says whether that number means anything.
        self._widgets["calibration"] = tk.Label(
            wrap, text="", font=self.f_label, bg=COLORS["panel"], fg=COLORS["faint"],
            wraplength=PANEL_WIDTH - 32, justify="left",
        )
        self._widgets["calibration"].pack(anchor="w", padx=2)

        # Duration is reported on its own line with its own score, because a
        # right direction on a wrong expiration is not a tradeable setup.
        duration_row = tk.Frame(wrap, bg=COLORS["panel"])
        duration_row.pack(fill="x", pady=(4, 2))
        tk.Label(
            duration_row, text="DURATION FIT", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["faint"],
        ).pack(side="left", padx=2)
        self._widgets["duration_score"] = tk.Label(
            duration_row, text="--", font=self.f_mono,
            bg=COLORS["panel"], fg=COLORS["neutral"],
        )
        self._widgets["duration_score"].pack(side="right", padx=2)

        recommend_row = tk.Frame(wrap, bg=COLORS["panel"])
        recommend_row.pack(fill="x")
        tk.Label(
            recommend_row, text="SUGGESTED", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["faint"],
        ).pack(side="left", padx=2)
        self._widgets["recommended"] = tk.Label(
            recommend_row, text="--", font=self.f_mono,
            bg=COLORS["panel"], fg=COLORS["dim"],
        )
        self._widgets["recommended"].pack(side="right", padx=2)

    def _build_buttons(self, parent: tk.Widget) -> None:
        row = self._section(parent, pady=(6, 4))

        self._widgets["scan"] = tk.Label(
            row, text="Scan", font=self.f_body,
            bg=COLORS["call"], fg="#04140a", cursor="hand2", pady=7,
        )
        self._widgets["scan"].pack(side="left", fill="x", expand=True, padx=(2, 4))
        self._widgets["scan"].bind("<Button-1>", lambda _e: self._scan_clicked())

        reset = tk.Label(
            row, text="Reset", font=self.f_body,
            bg=COLORS["raised"], fg=COLORS["dim"], cursor="hand2", pady=7, padx=14,
        )
        reset.pack(side="right", padx=(4, 2))
        reset.bind("<Button-1>", lambda _e: self.on_reset())

    def _build_session(self, parent: tk.Widget) -> None:
        wrap = self._section(parent, pady=(2, 2))
        tk.Label(
            wrap, text="SESSION", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["faint"],
        ).pack(anchor="w", padx=2)

        counters = tk.Frame(wrap, bg=COLORS["panel"])
        counters.pack(fill="x", pady=2)

        for key, caption, color, delta in (
            ("win", "WIN", COLORS["call"], (1, 0)),
            ("loss", "LOSS", COLORS["put"], (0, 1)),
        ):
            cell = tk.Frame(counters, bg=COLORS["raised"], highlightthickness=1)
            cell.configure(highlightbackground=COLORS["border"])
            cell.pack(side="left", fill="both", expand=True, padx=2)

            tk.Label(
                cell, text=caption, font=self.f_label,
                bg=COLORS["raised"], fg=COLORS["faint"],
            ).pack(pady=(3, 0))
            self._widgets[f"session_{key}"] = tk.Label(
                cell, text="0", font=self.f_mono_big, bg=COLORS["raised"], fg=color
            )
            self._widgets[f"session_{key}"].pack()

            buttons = tk.Frame(cell, bg=COLORS["raised"])
            buttons.pack(pady=(0, 3))
            for symbol, sign in (("+", 1), ("−", -1)):
                btn = tk.Label(
                    buttons, text=symbol, font=self.f_small,
                    bg=COLORS["panel"], fg=color, cursor="hand2", padx=8,
                )
                btn.pack(side="left", padx=2)
                wins, losses = delta
                btn.bind(
                    "<Button-1>",
                    lambda _e, w=wins * sign, l=losses * sign: self.on_adjust(w, l),
                )

        self._widgets["session_rate"] = tk.Label(
            wrap, text="No trades recorded yet", font=self.f_small,
            bg=COLORS["panel"], fg=COLORS["dim"],
        )
        self._widgets["session_rate"].pack(anchor="w", padx=2, pady=(2, 0))

        self._widgets["session_edge"] = tk.Label(
            wrap, text="", font=self.f_label, bg=COLORS["panel"], fg=COLORS["faint"]
        )
        self._widgets["session_edge"].pack(anchor="w", padx=2)

        # How many setups have passed the gates since the session began. A
        # quiet panel is either a quiet market or a broken app, and the count
        # is what tells the two apart.
        self._widgets["session_calls"] = tk.Label(
            wrap, text="", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["faint"],
        )
        self._widgets["session_calls"].pack(anchor="w", padx=2)

        # The measured record of this engine on this chart's own history — the
        # one line on the panel that is a fact rather than a forecast.
        tk.Label(
            wrap, text="MEASURED ON THIS CHART", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["faint"],
        ).pack(anchor="w", padx=2, pady=(6, 0))
        self._widgets["proof"] = tk.Label(
            wrap, text="Measuring this chart…", font=self.f_small,
            bg=COLORS["panel"], fg=COLORS["faint"],
            wraplength=PANEL_WIDTH - 32, justify="left",
        )
        self._widgets["proof"].pack(anchor="w", padx=2)

        # What the losing calls had in common. The panel's only backward-
        # looking line, and the only one that explains a loss rather than
        # reporting it.
        self._widgets["lesson"] = tk.Label(
            wrap, text="", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["wait"],
            wraplength=PANEL_WIDTH - 32, justify="left",
        )
        self._widgets["lesson"].pack(anchor="w", padx=2)

        # What the record changed about the gates. A setting that moves
        # silently is indistinguishable from a bug.
        self._widgets["tuning"] = tk.Label(
            wrap, text="", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["accent"],
            wraplength=PANEL_WIDTH - 32, justify="left",
        )
        self._widgets["tuning"].pack(anchor="w", padx=2)

    def _build_risk(self, parent: tk.Widget) -> None:
        wrap = self._section(parent, pady=(2, 2))
        header = tk.Frame(wrap, bg=COLORS["panel"])
        header.pack(fill="x")

        # The whole header toggles, not just the caret — a 7pt triangle is a
        # miserable click target on a panel this size.
        self._widgets["risk_caret"] = tk.Label(
            header, text="▾", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["faint"], cursor="hand2",
        )
        self._widgets["risk_caret"].pack(side="left")
        title = tk.Label(
            header, text="RISK", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["faint"], cursor="hand2",
        )
        title.pack(side="left", padx=2)
        self._widgets["risk_percent"] = tk.Label(
            header, text="", font=self.f_label,
            bg=COLORS["panel"], fg=COLORS["faint"],
        )
        self._widgets["risk_percent"].pack(side="right", padx=2)
        for widget in (self._widgets["risk_caret"], title, header):
            widget.bind("<Button-1>", lambda _e: self.on_toggle_risk())

        # Everything below the header folds away together, so the stake stays
        # one click from view rather than gone.
        body = tk.Frame(wrap, bg=COLORS["panel"])
        body.pack(fill="x")
        self._widgets["risk_body"] = body

        # Balance and stake are typed in directly. The stake defaults to the
        # configured percentage of the balance; typing any number overrides it
        # (the percentage readout above shows what share that actually is).
        for key, caption, commit in (
            ("balance", "Balance", self._commit_balance),
            ("stake", "Stake", self._commit_stake),
        ):
            row = tk.Frame(body, bg=COLORS["panel"])
            row.pack(fill="x", pady=1)
            tk.Label(
                row, text=caption, font=self.f_small,
                bg=COLORS["panel"], fg=COLORS["dim"],
            ).pack(side="left", padx=2)
            entry = self._entry(row, width=10, on_commit=commit)
            entry.pack(side="right", padx=2)
            self._widgets[f"risk_{key}"] = entry

        for key, caption in (
            ("profit", "Win returns"),
            ("breakeven", "Break-even rate"),
        ):
            row = tk.Frame(body, bg=COLORS["panel"])
            row.pack(fill="x", pady=1)
            tk.Label(
                row, text=caption, font=self.f_small,
                bg=COLORS["panel"], fg=COLORS["dim"],
            ).pack(side="left", padx=2)
            self._widgets[f"risk_{key}"] = tk.Label(
                row, text="--", font=self.f_mono,
                bg=COLORS["panel"], fg=COLORS["text"],
            )
            self._widgets[f"risk_{key}"].pack(side="right", padx=2)

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

    def _build_footer(self, parent: tk.Widget) -> None:
        self._widgets["reason"] = tk.Label(
            parent, text="Waiting for chart data.", font=self.f_small,
            bg=COLORS["panel"], fg=COLORS["dim"],
            wraplength=PANEL_WIDTH - 28, justify="left",
        )
        self._widgets["reason"].pack(anchor="w", padx=10, pady=(6, 2))

        self._widgets["warnings"] = tk.Label(
            parent, text="", font=self.f_small,
            bg=COLORS["panel"], fg=COLORS["wait"],
            wraplength=PANEL_WIDTH - 28, justify="left",
        )
        self._widgets["warnings"].pack(anchor="w", padx=10, pady=(0, 2))

        tk.Label(
            parent, text="Analysis only — not a trading recommendation.",
            font=self.f_label, bg=COLORS["panel"], fg=COLORS["faint"],
        ).pack(pady=(4, 8))

    def _fit_height(self, position: tuple[int, int]) -> int:
        """Resize the window to exactly fit its content, and return that height."""
        self.root.update_idletasks()
        height = max(self.root.winfo_reqheight(), 320)
        # Never taller than the screen it sits on.
        try:
            height = min(height, self.root.winfo_screenheight() - 60)
        except tk.TclError:  # pragma: no cover - no display
            pass
        self.root.geometry(f"{PANEL_WIDTH}x{height}+{position[0]}+{position[1]}")
        return height

    # -- interaction --------------------------------------------------------

    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root)
        self._window_origin = (self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event) -> None:
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        x = self._window_origin[0] + dx
        y = self._window_origin[1] + dy
        self.root.geometry(f"+{x}+{y}")

    def _scan_clicked(self) -> None:
        if self.vm.scan.scanning:
            return  # ignore repeat presses mid-scan
        self.on_scan()

    def toggle_collapse(self) -> None:
        """Shrink to just the header, so the panel can be parked out of the way."""
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._body.pack_forget()
            self.root.geometry(f"{PANEL_WIDTH}x36")
        else:
            self._body.pack(fill="both", expand=True)
            self.root.geometry(f"{PANEL_WIDTH}x{self._natural_height}")

    def _close(self) -> None:
        self.on_close()
        self.destroy()

    def destroy(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # -- rendering ----------------------------------------------------------

    def refresh(self) -> None:
        """Paint the current view model onto the widgets."""
        data = self.vm.render()
        w = self._widgets

        header = data["header"]
        w["status"].configure(text=header["status"], fg=header["status_color"])

        tiles = data["tiles"]
        self._set_entry(w["tile_pair"], tiles["pair"])
        self._set_entry(w["tile_payout"], tiles["payout"])
        w["tile_time"].configure(text=tiles["time"])
        w["tile_chart"].configure(text=f"chart {tiles['chart']}")
        w["price"].configure(text=data["price"])

        verdict = data["verdict"]
        scanning = data["scan"]["scanning"]

        if scanning:
            w["verdict"].pack_forget()
            w["dots_frame"].pack(pady=(4, 6))
            self._animate_dots(data["scan"]["progress"])
        else:
            w["dots_frame"].pack_forget()
            w["verdict"].pack(pady=(0, 2))

        w["arrow"].configure(text=verdict["arrow"], fg=verdict["color"])
        w["verdict"].configure(text=verdict["direction_label"], fg=verdict["color"])
        w["verdict_box"].configure(highlightbackground=verdict["color"])
        # The state chip is only ever a warning about the *signal*. Scanning is
        # not a problem with the signal, so it must not borrow the alarm colour.
        state_colors = {
            "WEAKENING": COLORS["wait"],
            "INVALIDATED": COLORS["put"],
            "EXPIRED": COLORS["faint"],
            "SCANNING": COLORS["dim"],
        }
        w["state"].configure(
            text="" if verdict["state"] in ("ACTIVE", "IDLE") else verdict["state"],
            fg=state_colors.get(verdict["state"], COLORS["faint"]),
        )

        w["score"].configure(text=verdict["score_display"], fg=verdict["score_color"])
        w["badge"].configure(text=verdict["badge"], fg=verdict["badge_color"])
        w["pattern"].configure(text=verdict["pattern"] or "")
        w["duration_score"].configure(
            text=verdict["duration_display"], fg=verdict["score_color"]
        )
        w["recommended"].configure(text=verdict["recommended"])
        w["take_now"].configure(
            text=verdict["take_label"],
            fg=verdict["color"] if verdict["take_now"] else COLORS["dim"],
        )
        self._draw_score_bar(verdict["score"], verdict["score_color"])

        w["scan"].configure(
            text="Scanning…" if scanning else "Scan",
            bg=COLORS["raised"] if scanning else COLORS["call"],
            fg=COLORS["dim"] if scanning else "#04140a",
        )

        session = data["session"]
        w["session_win"].configure(text=str(session["wins"]))
        w["session_loss"].configure(text=str(session["losses"]))
        if session["total"] == 0:
            w["session_rate"].configure(text="No trades recorded yet", fg=COLORS["dim"])
            w["session_edge"].configure(text="")
        else:
            rate_text = f"{session['win_rate_display']} of {session['total']} trades"
            if not session["meaningful"]:
                rate_text += "  (too few to read)"
            w["session_rate"].configure(
                text=rate_text,
                fg=COLORS["dim"] if not session["meaningful"] else COLORS["text"],
            )
            edge = session["edge"]
            if edge is None:
                w["session_edge"].configure(text="")
            else:
                w["session_edge"].configure(
                    text=(
                        f"{edge:+.1f} pts vs {session['breakeven_rate']:.1f}% break-even"
                    ),
                    fg=COLORS["call"] if edge >= 0 else COLORS["put"],
                )

        calls = session.get("calls", 0)
        w["session_calls"].configure(
            text=(
                f"{calls} setup{'' if calls == 1 else 's'} called this session"
                if calls
                else "No setups called yet this session"
            )
        )

        calibration = data["calibration"]
        w["calibration"].configure(
            text=calibration["text"], fg=calibration["color"]
        )

        proof = data["proof"]
        w["proof"].configure(text=proof["text"], fg=proof["color"])
        w["lesson"].configure(text=data["lesson"])
        w["tuning"].configure(text="\n".join(data["tuning"]))

        risk = data["risk"]
        if risk.get("collapsed"):
            w["risk_body"].pack_forget()
            w["risk_caret"].configure(text="▸")
        else:
            w["risk_body"].pack(fill="x")
            w["risk_caret"].configure(text="▾")
        self._set_entry(w["risk_balance"], f"{risk['balance']:.2f}")
        self._set_entry(w["risk_stake"], f"{risk['stake']:.2f}")
        w["risk_profit"].configure(text=f"+{risk['potential_profit']:.2f}", fg=COLORS["call"])
        w["risk_breakeven"].configure(text=f"{risk['breakeven_rate']:.1f}%")
        # Show what share of the balance the stake actually is, and colour it
        # when that share is into territory a losing streak would hurt.
        percent = risk["risk_percent"]
        w["risk_percent"].configure(
            text=f"{percent:.1f}% of balance"
            + (" · manual" if risk.get("stake_overridden") else ""),
            fg=COLORS["put"] if percent > 10 else COLORS["wait"] if percent > 5 else COLORS["faint"],
        )

        w["reason"].configure(text=data["reason"])
        # Risk warnings (oversized stake, session below break-even) belong on
        # screen next to the signal warnings, not buried in a log.
        warnings = list(data["warnings"]) + list(risk.get("warnings", []))
        w["warnings"].configure(
            text="\n".join(f"⚠ {line}" for line in warnings[:5]) if warnings else ""
        )

        # The reason and warning blocks wrap to a variable number of lines, so
        # the window has to re-fit or the footer gets clipped exactly when there
        # is a warning worth reading.
        self._refit()

    def _refit(self) -> None:
        """Grow or shrink the window to fit the current text, without jitter."""
        if self._collapsed:
            return
        try:
            self.root.update_idletasks()
            required = max(self.root.winfo_reqheight(), 320)
            required = min(required, self.root.winfo_screenheight() - 60)
            # A few pixels of slack stops a one-pixel font difference from
            # resizing the window on every single repaint.
            if abs(required - self._natural_height) > 6:
                self._natural_height = required
                self.root.geometry(f"{PANEL_WIDTH}x{required}")
        except tk.TclError:  # pragma: no cover - window closing
            pass

    def _animate_dots(self, progress: float) -> None:
        """Cycle the three dots so the scan visibly progresses."""
        active = int(progress * 9) % 3
        for index, dot in enumerate(self._dots):
            dot.configure(fg=COLORS["call"] if index == active else COLORS["faint"])

    def _draw_score_bar(self, score: float | None, color: str) -> None:
        canvas: tk.Canvas = self._widgets["score_bar"]
        canvas.delete("all")
        width = canvas.winfo_width() or (PANEL_WIDTH - 20)
        if score is None:
            return
        filled = max(2, int(width * max(0.0, min(100.0, score)) / 100.0))
        canvas.create_rectangle(0, 0, filled, 6, fill=color, outline="")

    # -- loop ---------------------------------------------------------------

    def run(self) -> None:
        self.root.mainloop()
