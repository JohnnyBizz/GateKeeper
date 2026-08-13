"""In-app chart region selection and price calibration.

Selecting the chart area used to mean quitting the app and running a separate
terminal script. This does the same job from a button inside the overlay:
freeze the screen, drag a box, click two known prices, done.

Implemented in pure Tkinter over a screenshot rather than with OpenCV's
``selectROI`` so it needs no extra window toolkit and matches the app's look.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..logging_setup import get_logger

log = get_logger(__name__)

try:  # pragma: no cover - optional
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

try:  # pragma: no cover - optional
    import mss
except ImportError:  # pragma: no cover
    mss = None  # type: ignore[assignment]


@dataclass
class RegionSelection:
    """What the picker came back with."""

    left: int
    top: int
    width: int
    height: int
    candles_found: int = 0
    confidence: float = 0.0
    issues: list[str] | None = None
    # Calibration, when the user supplied two reference prices.
    top_pixel: int | None = None
    top_price: float | None = None
    bottom_pixel: int | None = None
    bottom_price: float | None = None

    @property
    def calibrated(self) -> bool:
        return (
            self.top_price is not None
            and self.bottom_price is not None
            and self.top_pixel is not None
            and self.bottom_pixel is not None
            and self.top_price > self.bottom_price
        )

    def region_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }

    def calibration_dict(self) -> dict[str, Any]:
        if not self.calibrated:
            return {"enabled": False}
        return {
            "enabled": True,
            "top_pixel": int(self.top_pixel),      # type: ignore[arg-type]
            "top_price": float(self.top_price),    # type: ignore[arg-type]
            "bottom_pixel": int(self.bottom_pixel),  # type: ignore[arg-type]
            "bottom_price": float(self.bottom_price),  # type: ignore[arg-type]
        }


def capture_screen(monitor_index: int = 1):
    """Grab a full-screen BGR image plus the monitor's origin."""
    if mss is None or cv2 is None:
        raise RuntimeError(
            "Selecting a chart area needs the 'mss' and 'opencv-python' packages."
        )
    with mss.mss() as sct:
        monitors = sct.monitors
        index = monitor_index if monitor_index < len(monitors) else (
            1 if len(monitors) > 1 else 0
        )
        monitor = monitors[index]
        raw = sct.grab(monitor)
        frame = cv2.cvtColor(np.asarray(raw), cv2.COLOR_BGRA2BGR)
    return frame, monitor


def analyse_region(image, colors: dict[str, Any] | None = None):
    """Run candle extraction on a cropped region, for immediate feedback."""
    from ..chart_detection.candles import ColorProfile, extract_pixel_candles

    try:
        return extract_pixel_candles(image, ColorProfile.from_config(colors))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("region analysis failed: %s", exc)
        return None


class RegionPicker:
    """A full-screen overlay the user drags a selection box on."""

    def __init__(
        self,
        parent,
        *,
        monitor_index: int = 1,
        colors: dict[str, Any] | None = None,
        on_done: Callable[[RegionSelection | None], None] | None = None,
    ) -> None:
        import tkinter as tk
        from PIL import Image, ImageTk  # type: ignore[import-untyped]

        self.tk = tk
        self.parent = parent
        self.colors = colors
        self.on_done = on_done or (lambda _selection: None)

        frame, monitor = capture_screen(monitor_index)
        self.frame = frame
        self.origin = (int(monitor["left"]), int(monitor["top"]))

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        self._image = Image.fromarray(rgb)

        self.window = tk.Toplevel(parent)
        self.window.attributes("-topmost", True)
        try:
            self.window.overrideredirect(True)
        except tk.TclError:  # pragma: no cover - window manager dependent
            pass
        self.window.geometry(
            f"{frame.shape[1]}x{frame.shape[0]}+{self.origin[0]}+{self.origin[1]}"
        )

        self.canvas = tk.Canvas(
            self.window, highlightthickness=0, cursor="crosshair",
            width=frame.shape[1], height=frame.shape[0],
        )
        self.canvas.pack(fill="both", expand=True)
        self._photo = ImageTk.PhotoImage(self._image)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        # Dim everything so the eventual selection reads as "the bright part".
        self.canvas.create_rectangle(
            0, 0, frame.shape[1], frame.shape[0], fill="#000000", stipple="gray50",
            outline="",
        )

        self._hint = self.canvas.create_text(
            frame.shape[1] // 2, 40,
            text="Drag a box around the chart — candles and the price numbers, "
                 "not the BUY/SELL buttons.   Esc to cancel.",
            fill="#e2e8f0", font=("TkDefaultFont", 13, "bold"),
        )

        self._start: tuple[int, int] | None = None
        self._rect = None
        self._selection: RegionSelection | None = None

        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.window.bind("<Escape>", lambda _e: self._cancel())
        self.window.focus_force()

    # ------------------------------------------------------------------

    def _on_press(self, event) -> None:
        self._start = (event.x, event.y)
        if self._rect is not None:
            self.canvas.delete(self._rect)
        self._rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#3b82f6", width=2
        )

    def _on_drag(self, event) -> None:
        if self._start is None or self._rect is None:
            return
        self.canvas.coords(self._rect, self._start[0], self._start[1], event.x, event.y)

    def _on_release(self, event) -> None:
        if self._start is None:
            return
        x0, y0 = self._start
        x1, y1 = event.x, event.y
        left, right = sorted((int(x0), int(x1)))
        top, bottom = sorted((int(y0), int(y1)))
        width, height = right - left, bottom - top

        if width < 50 or height < 50:
            self.canvas.itemconfigure(
                self._hint, text="That box is too small — drag a larger area."
            )
            self._start = None
            return

        crop = self.frame[top:bottom, left:right]
        extraction = analyse_region(crop, self.colors)

        self._selection = RegionSelection(
            left=self.origin[0] + left,
            top=self.origin[1] + top,
            width=width,
            height=height,
            candles_found=len(extraction.candles) if extraction else 0,
            confidence=extraction.confidence if extraction else 0.0,
            issues=list(extraction.issues) if extraction else ["Recognition failed."],
        )
        self._finish()

    def _cancel(self) -> None:
        self._selection = None
        self._close()
        self.on_done(None)

    def _finish(self) -> None:
        self._close()
        self.on_done(self._selection)

    def _close(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # pragma: no cover - already gone
            pass


class CalibrationPicker:
    """Click two rows whose prices the user can read off the chart's axis."""

    def __init__(
        self,
        parent,
        selection: RegionSelection,
        *,
        on_done: Callable[[RegionSelection | None], None] | None = None,
    ) -> None:
        import tkinter as tk
        from PIL import Image, ImageTk  # type: ignore[import-untyped]

        self.tk = tk
        self.selection = selection
        self.on_done = on_done or (lambda _s: None)

        frame, _ = capture_screen()
        crop = frame[
            selection.top - 0 : selection.top + selection.height,
            selection.left : selection.left + selection.width,
        ]
        # The stored region is in virtual-screen coordinates; re-grab just it.
        if mss is not None:
            with mss.mss() as sct:
                raw = sct.grab(selection.region_dict())
                crop = cv2.cvtColor(np.asarray(raw), cv2.COLOR_BGRA2BGR)

        self.window = tk.Toplevel(parent)
        self.window.title("Calibrate the price scale")
        self.window.attributes("-topmost", True)

        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        self._image = Image.fromarray(rgb)
        self._photo = ImageTk.PhotoImage(self._image)

        tk.Label(
            self.window,
            text="Click two horizontal levels whose prices you can read on the "
                 "chart's axis (gridlines work well).",
            bg="#121826", fg="#e2e8f0", pady=6,
        ).pack(fill="x")

        self.canvas = tk.Canvas(
            self.window, width=crop.shape[1], height=crop.shape[0],
            highlightthickness=0, cursor="crosshair",
        )
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self.canvas.bind("<Button-1>", self._on_click)

        self._rows: list[int] = []
        self.window.bind("<Escape>", lambda _e: self._cancel())
        self.window.focus_force()

    def _on_click(self, event) -> None:
        if len(self._rows) >= 2:
            return
        self._rows.append(int(event.y))
        self.canvas.create_line(
            0, event.y, self.canvas.winfo_width(), event.y, fill="#facc15", width=2
        )
        if len(self._rows) == 2:
            self.window.after(120, self._ask_prices)

    def _ask_prices(self) -> None:
        from tkinter import simpledialog

        top_row, bottom_row = sorted(self._rows)
        upper = simpledialog.askfloat(
            "Upper price",
            f"Price at the upper line (row {top_row}):",
            parent=self.window,
        )
        lower = simpledialog.askfloat(
            "Lower price",
            f"Price at the lower line (row {bottom_row}):",
            parent=self.window,
        )

        if upper is None or lower is None or upper <= lower:
            # Price increases upward on a chart; anything else is a mistake,
            # and a wrong scale is worse than none at all.
            self._cancel()
            return

        self.selection.top_pixel = top_row
        self.selection.top_price = upper
        self.selection.bottom_pixel = bottom_row
        self.selection.bottom_price = lower
        self._close()
        self.on_done(self.selection)

    def _cancel(self) -> None:
        self._close()
        self.on_done(self.selection)  # region kept, calibration skipped

    def _close(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # pragma: no cover
            pass
