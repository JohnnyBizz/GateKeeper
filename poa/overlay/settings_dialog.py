"""The in-app settings window.

Everything that previously required hand-editing ``config.yaml`` lives here:
which chart to read, the two timeframes, payout, confidence floor, and the
chart-area selection. Changes are applied to the running engine and written
back to disk, so they survive a restart.
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Callable

from ..config import CHART_TIMEFRAMES, TRADE_DURATIONS, Config
from ..logging_setup import get_logger
from ..models import format_duration
from .viewmodel import COLORS

log = get_logger(__name__)

SOURCE_LABELS = {
    "screen": "Read my screen (live chart)",
    "synthetic": "Demo data (practice, not a real market)",
    "csv": "Replay a recorded CSV file",
}


class SettingsDialog:
    """A modal-ish settings window owned by the overlay."""

    def __init__(
        self,
        parent: tk.Misc,
        config: Config,
        *,
        on_apply: Callable[[dict[str, Any]], None],
        on_pick_region: Callable[[Callable[[str], None]], None] | None = None,
    ) -> None:
        self.config = config
        self.on_apply = on_apply
        self.on_pick_region = on_pick_region
        self._vars: dict[str, tk.Variable] = {}

        self.window = tk.Toplevel(parent)
        self.window.title("GateKeeper settings")
        self.window.configure(bg=COLORS["panel"])
        self.window.attributes("-topmost", True)
        self.window.resizable(False, False)

        body = tk.Frame(self.window, bg=COLORS["panel"], padx=16, pady=14)
        body.pack(fill="both", expand=True)

        self._section(body, "CHART")
        self._source_row(body)
        self._entry_row(body, "asset", "Asset name", str(config.get("market.asset", "EUR/USD")))
        self._choice_row(
            body, "chart_timeframe", "Chart timeframe",
            CHART_TIMEFRAMES, int(config.get("market.chart_timeframe", 60)),
        )
        self._region_row(body)

        self._section(body, "TRADE")
        self._choice_row(
            body, "trade_duration", "Trade duration (expiry)",
            TRADE_DURATIONS, int(config.get("market.trade_duration", 180)),
        )
        self._entry_row(
            body, "payout", "Payout %",
            f"{float(config.get('market.payout', 0.92)) * 100:.0f}",
        )
        self._entry_row(
            body, "balance", "Balance",
            f"{float(config.get('risk.balance', 1000.0)):.2f}",
        )

        self._section(body, "SIGNALS")
        self._scale_row(
            body, "min_confidence", "Minimum confidence",
            float(config.get("signals.min_confidence", 75)),
        )
        self._scale_row(
            body, "min_duration_compatibility", "Minimum duration fit",
            float(config.get("signals.min_duration_compatibility", 65)),
        )
        self._check_row(
            body, "alerts_enabled", "Desktop alerts",
            bool(config.get("alerts.enabled", True)),
        )

        self._status = tk.Label(
            body, text="", bg=COLORS["panel"], fg=COLORS["dim"],
            wraplength=330, justify="left", font=("TkDefaultFont", 8),
        )
        self._status.pack(fill="x", pady=(10, 4))

        buttons = tk.Frame(body, bg=COLORS["panel"])
        buttons.pack(fill="x", pady=(6, 0))
        self._button(buttons, "Save", self._save, primary=True).pack(
            side="right", padx=(6, 0)
        )
        self._button(buttons, "Cancel", self.close).pack(side="right")

    # -- widgets ------------------------------------------------------------

    def _section(self, parent: tk.Widget, title: str) -> None:
        tk.Label(
            parent, text=title, bg=COLORS["panel"], fg=COLORS["faint"],
            font=("TkDefaultFont", 7, "bold"), anchor="w",
        ).pack(fill="x", pady=(10, 2))
        tk.Frame(parent, bg=COLORS["border"], height=1).pack(fill="x", pady=(0, 4))

    def _row(self, parent: tk.Widget, label: str) -> tk.Frame:
        row = tk.Frame(parent, bg=COLORS["panel"])
        row.pack(fill="x", pady=3)
        tk.Label(
            row, text=label, bg=COLORS["panel"], fg=COLORS["dim"],
            width=22, anchor="w", font=("TkDefaultFont", 9),
        ).pack(side="left")
        return row

    def _entry_row(self, parent: tk.Widget, key: str, label: str, value: str) -> None:
        row = self._row(parent, label)
        var = tk.StringVar(value=value)
        self._vars[key] = var
        tk.Entry(
            row, textvariable=var, width=16, bg=COLORS["raised"], fg=COLORS["text"],
            insertbackground=COLORS["text"], relief="flat", highlightthickness=1,
            highlightbackground=COLORS["border"], highlightcolor=COLORS["accent"],
        ).pack(side="right")

    def _choice_row(
        self, parent: tk.Widget, key: str, label: str, options, current: int
    ) -> None:
        row = self._row(parent, label)
        labels = [format_duration(s) for s in options]
        var = tk.StringVar(value=format_duration(current))
        self._vars[key] = var
        menu = tk.OptionMenu(row, var, *labels)
        menu.configure(
            bg=COLORS["raised"], fg=COLORS["text"], activebackground=COLORS["border"],
            activeforeground=COLORS["text"], relief="flat", highlightthickness=0,
            width=12, anchor="e",
        )
        menu["menu"].configure(bg=COLORS["raised"], fg=COLORS["text"])
        menu.pack(side="right")

    def _source_row(self, parent: tk.Widget) -> None:
        row = self._row(parent, "Where to read the chart")
        current = str(self.config.get("capture.source", "synthetic"))
        var = tk.StringVar(value=SOURCE_LABELS.get(current, SOURCE_LABELS["synthetic"]))
        self._vars["source"] = var
        menu = tk.OptionMenu(row, var, *SOURCE_LABELS.values())
        menu.configure(
            bg=COLORS["raised"], fg=COLORS["text"], activebackground=COLORS["border"],
            activeforeground=COLORS["text"], relief="flat", highlightthickness=0,
            width=28, anchor="e", font=("TkDefaultFont", 8),
        )
        menu["menu"].configure(bg=COLORS["raised"], fg=COLORS["text"])
        menu.pack(side="right")

    def _region_row(self, parent: tk.Widget) -> None:
        row = self._row(parent, "Chart area")
        region = self.config.get("capture.region") or {}
        has_region = int(region.get("width", 0)) > 50
        self._region_label = tk.Label(
            row,
            text=(
                f"{region.get('width')}×{region.get('height')}"
                if has_region
                else "not selected"
            ),
            bg=COLORS["panel"],
            fg=COLORS["call"] if has_region else COLORS["wait"],
            font=("TkDefaultFont", 8),
        )
        self._region_label.pack(side="right", padx=(6, 0))
        self._button(row, "Select…", self._pick_region).pack(side="right")

    def _scale_row(self, parent: tk.Widget, key: str, label: str, value: float) -> None:
        row = self._row(parent, label)
        var = tk.IntVar(value=int(value))
        self._vars[key] = var
        readout = tk.Label(
            row, textvariable=var, bg=COLORS["panel"], fg=COLORS["text"], width=4
        )
        readout.pack(side="right")
        tk.Scale(
            row, from_=50, to=95, orient="horizontal", variable=var,
            bg=COLORS["panel"], fg=COLORS["text"], troughcolor=COLORS["raised"],
            highlightthickness=0, showvalue=False, length=140, relief="flat",
            activebackground=COLORS["accent"],
        ).pack(side="right", padx=(0, 8))

    def _check_row(self, parent: tk.Widget, key: str, label: str, value: bool) -> None:
        row = self._row(parent, label)
        var = tk.BooleanVar(value=value)
        self._vars[key] = var

        # A native checkbutton fills its indicator with `selectcolor` when
        # checked, which on a dark panel is indistinguishable from unchecked.
        # A text toggle reads unambiguously in either state.
        toggle = tk.Label(
            row, padx=10, pady=2, cursor="hand2", font=("TkDefaultFont", 8, "bold")
        )

        def paint() -> None:
            on = bool(var.get())
            toggle.configure(
                text="ON" if on else "OFF",
                bg=COLORS["call"] if on else COLORS["raised"],
                fg="#04140a" if on else COLORS["dim"],
            )

        toggle.bind("<Button-1>", lambda _e: (var.set(not var.get()), paint()))
        paint()
        toggle.pack(side="right")

    def _button(
        self, parent: tk.Widget, text: str, command, primary: bool = False
    ) -> tk.Label:
        widget = tk.Label(
            parent, text=text, padx=14, pady=5, cursor="hand2",
            bg=COLORS["call"] if primary else COLORS["raised"],
            fg="#04140a" if primary else COLORS["text"],
            font=("TkDefaultFont", 9, "bold" if primary else "normal"),
        )
        widget.bind("<Button-1>", lambda _e: command())
        return widget

    # -- actions ------------------------------------------------------------

    def _pick_region(self) -> None:
        if self.on_pick_region is None:
            self._status.configure(text="Region selection is unavailable.")
            return
        self._status.configure(text="Selecting… drag a box around your chart.")
        self.window.withdraw()

        def done(message: str) -> None:
            try:
                self.window.deiconify()
                region = self.config.get("capture.region") or {}
                if int(region.get("width", 0)) > 50:
                    self._region_label.configure(
                        text=f"{region.get('width')}×{region.get('height')}",
                        fg=COLORS["call"],
                    )
                self._status.configure(text=message)
            except tk.TclError:  # pragma: no cover - window closed meanwhile
                pass

        self.on_pick_region(done)

    def _save(self) -> None:
        changes: dict[str, Any] = {}

        asset = str(self._vars["asset"].get()).strip()
        if asset:
            changes["asset"] = asset

        for key in ("chart_timeframe", "trade_duration"):
            label = str(self._vars[key].get())
            options = CHART_TIMEFRAMES if key == "chart_timeframe" else TRADE_DURATIONS
            for seconds in options:
                if format_duration(seconds) == label:
                    changes[key] = seconds
                    break

        source_label = str(self._vars["source"].get())
        for value, text in SOURCE_LABELS.items():
            if text == source_label:
                changes["source"] = value
                break

        # Payout is entered as a percentage but stored as a fraction, because
        # every calculation that uses it wants the fraction.
        try:
            payout = float(str(self._vars["payout"].get()).strip().rstrip("%"))
            if 1 <= payout <= 100:
                changes["payout"] = payout / 100.0
        except ValueError:
            pass

        try:
            balance = float(str(self._vars["balance"].get()).replace(",", ""))
            if balance > 0:
                changes["balance"] = balance
        except ValueError:
            pass

        changes["min_confidence"] = float(self._vars["min_confidence"].get())
        changes["min_duration_compatibility"] = float(
            self._vars["min_duration_compatibility"].get()
        )
        changes["alerts_enabled"] = bool(self._vars["alerts_enabled"].get())

        try:
            self.on_apply(changes)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("applying settings failed")
            self._status.configure(text=f"Could not apply: {exc}", fg=COLORS["put"])
            return
        self.close()

    def close(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:  # pragma: no cover
            pass
