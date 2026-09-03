"""Render the overlay's frames to PNG without a display.

    python tools/preview_panel.py

The panel draws every shape as a Pillow image and every word as canvas text,
so a stub canvas that keeps the images and records the text is enough to
composite a faithful still of any state — idle, a live call with the switch
banner, scanning, and a tripped brake — into ``preview-*.png`` beside this
file, plus a contact sheet. Fonts are whatever the box has (Liberation or
DejaVu are looked for); on Windows the panel uses Segoe UI, so spacing
differs a little. This is a design tool: the shapes in the stills are the
real ones, and the 2026-09 redraw was judged on them before it shipped.
"""
from __future__ import annotations

import importlib
import sys
import textwrap
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import test_panel as harness  # noqa: E402  (the stub widgets)

LINE = 13  # px per wrapped line at the label size


def _wrap(text: str, width: float, px: float) -> list[str]:
    """Break like Tk does — at word boundaries, to a pixel width."""
    per_line = max(8, int(width / max(4.0, px * 0.52)))
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        lines.extend(textwrap.wrap(paragraph, per_line) or [""])
    return lines


class _MeasuringCanvas(harness._Widget):
    """The stub canvas, but with real text extents so the layout flows."""

    def create_text(self, x, y, **kw):
        item_id = super().create_text(x, y, **kw)
        self._item(item_id)["width"] = kw.get("width")
        return item_id

    def bbox(self, item_id):
        item = self._item(item_id)
        if item.get("kind") != "text":
            return (0, 0, 100, 12)
        font = item.get("font") if isinstance(item.get("font"), dict) else {}
        px = float(font.get("size", 8)) * 1.25
        width = item.get("width") or 320
        lines = _wrap(item["text"], width, px) if item.get("width") else [item["text"]]
        height = len(lines) * int(px * 1.35)
        return (item["x"], item["y"], item["x"] + width, item["y"] + height)


tk = types.ModuleType("tkinter")
for name in ("Tk", "Toplevel", "Frame", "Label", "Entry", "Text"):
    setattr(tk, name, type(name, (harness._Widget,), {}))
tk.Canvas = type("Canvas", (_MeasuringCanvas,), {})
tk.Entry.delete = lambda self, *a: None
tk.TclError = type("TclError", (Exception,), {})
tkfont = types.ModuleType("tkinter.font")
tkfont.Font = lambda **kw: kw
tkfont.families = lambda *a, **k: ("Segoe UI", "Consolas")
tk.font = tkfont
sys.modules["tkinter"] = tk
sys.modules["tkinter.font"] = tkfont
imagetk = types.ModuleType("PIL.ImageTk")
imagetk.PhotoImage = lambda image: image
sys.modules["PIL.ImageTk"] = imagetk

panel_module = importlib.reload(importlib.import_module("poa.overlay.panel"))
from poa.overlay.viewmodel import COLORS, OverlayViewModel  # noqa: E402

_FONT_DIRS = [Path("/usr/share/fonts/truetype/liberation"),
              Path("/usr/share/fonts/truetype/dejavu"),
              Path("C:/Windows/Fonts")]
_FONT_FILES = {
    ("sans", "normal"): ["LiberationSans-Regular.ttf", "DejaVuSans.ttf", "segoeui.ttf"],
    ("sans", "bold"): ["LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf", "segoeuib.ttf"],
    ("mono", "normal"): ["LiberationMono-Regular.ttf", "DejaVuSansMono.ttf", "consola.ttf"],
    ("mono", "bold"): ["LiberationMono-Bold.ttf", "DejaVuSansMono-Bold.ttf", "consolab.ttf"],
}
FONTS = {
    key: next((d / f for d in _FONT_DIRS for f in files if (d / f).exists()), None)
    for key, files in _FONT_FILES.items()
}


def _font(spec: dict) -> ImageFont.FreeTypeFont:
    family = "mono" if "Mono" in str(spec.get("family", "")) or "Consolas" in str(spec.get("family", "")) else "sans"
    weight = "bold" if spec.get("weight") == "bold" else "normal"
    size = int(round(float(spec.get("size", 10)) * 1.25))
    path = FONTS.get((family, weight)) or FONTS.get(("sans", "normal"))
    try:
        return ImageFont.truetype(str(path), size)
    except (OSError, TypeError):
        return ImageFont.load_default()


ANCHORS = {"nw": "la", "w": "lm", "e": "rm", "center": "mm", "n": "ma", "ne": "ra"}


def composite(panel, height: int | None = None) -> Image.Image:
    height = height or panel._height
    out = Image.new("RGBA", (panel_module.PANEL_WIDTH, height), COLORS["bg"])
    draw = ImageDraw.Draw(out)
    # Embedded fields sit above every canvas item in Tk, whatever their
    # creation order; painted last here for the same reason.
    ordered = sorted(panel.c.recorder.items, key=lambda i: i["kind"] == "window")
    for item in ordered:
        if item.get("state") == "hidden":
            continue
        kind = item["kind"]
        if kind == "image":
            image = item["image"]
            x, y = int(item["x"]), int(item["y"])
            layer = Image.new("RGBA", out.size, (0, 0, 0, 0))
            # Clip to the canvas the way Tk would.
            box = image.crop((max(0, -x), max(0, -y), image.width, image.height))
            layer.paste(box, (max(0, x), max(0, y)))
            out.alpha_composite(layer)
        elif kind == "text":
            text = item["text"]
            font = _font(item["font"] if isinstance(item["font"], dict) else {})
            anchor = ANCHORS.get(item["anchor"] or "nw", "la")
            x, y = item["x"], item["y"]
            if item.get("width"):
                px = float(item["font"].get("size", 8)) * 1.25 if isinstance(item["font"], dict) else 10
                for index, line in enumerate(_wrap(text, item["width"], px)):
                    draw.text((x, y + index * int(px * 1.35)), line, fill=item["fill"], font=font, anchor=anchor)
                continue
            draw.text((x, y), text, fill=item["fill"], font=font, anchor=anchor)
        elif kind == "window":
            entry = item["window"]
            x, y = item["x"], item["y"]
            anchor = item.get("anchor", "w")
            width = int(entry.kw.get("width", 9)) * 8 + 10
            left = x if anchor == "w" else x - width
            draw.rounded_rectangle([left, y - 10, left + width, y + 10], radius=4,
                                   fill="#182339", outline="#243044")
            draw.text((left + 6, y), getattr(entry, "_text", ""), fill=COLORS["text"],
                      font=_font(entry.kw.get("font", {})), anchor="lm")
    return out


def _live_vm(losses: int = 0, banner: bool = True):
    from conftest import good_quality, pullback_trend
    from poa.risk import SessionStats
    from poa.signals import GateSettings, SignalEngine, SignalRequest

    series = pullback_trend(400, direction=1)
    signal = SignalEngine().evaluate(SignalRequest(
        series=series, asset="EUR/USD OTC", chart_timeframe=60,
        trade_duration=180, quality=good_quality(series), settings=GateSettings(),
    ))
    vm = OverlayViewModel(session=SessionStats(), asset="EUR/USD OTC",
                          chart_timeframe=60, trade_duration=180,
                          max_losses_in_a_row=4, source="feed")
    vm.signal = signal
    vm.connected = True
    vm.recent = series
    vm.watchlist = [
        {"asset": "EUR/USD OTC", "timeframe": 60, "expiry": 180, "direction": "CALL", "score": 82.0, "actionable": True},
        {"asset": "GBP/USD OTC", "timeframe": 5, "expiry": 30, "direction": "PUT", "score": 88.0, "actionable": True},
        {"asset": "AUD/CHF OTC", "timeframe": 60, "expiry": 180, "direction": "WAIT", "score": 54.0, "actionable": False},
        {"asset": "USD/JPY OTC", "timeframe": 60, "expiry": 180, "direction": "WAIT", "score": 61.0, "actionable": False},
    ]
    for _ in range(3):
        vm.session.adjust(1, 0); vm.session.record(True)
    vm.session.adjust(0, 1); vm.session.record(False)
    vm.calls_this_session = 7
    if banner:
        vm.post_notice("GBP/USD 5SEC — PUT 88",
                       "You're on EUR/USD 1 MIN — open GBP/USD OTC on a 5 SEC chart and set a 30 SEC expiry.",
                       asset="GBP/USD OTC", timeframe=5)
    for _ in range(losses):
        vm.session.adjust(0, 1); vm.session.record(False)
    return vm


def render(name: str, vm, frames: int = 30, **kw) -> Path:
    panel = panel_module.OverlayPanel(vm, **kw)
    for entry in panel._entries.values():
        entry._text = ""
    panel._entries["pair"]._text = vm.asset
    for _ in range(frames):
        panel.refresh()
    for key, text in (("payout", f"{vm.payout*100:.0f}%"), ("balance", f"{vm.risk.balance:.2f}"), ("stake", f"{vm.risk.stake:.2f}")):
        panel._entries[key]._text = text
    image = composite(panel)
    path = Path(__file__).with_name(f"preview-{name}.png")
    image.save(path)
    return path


if __name__ == "__main__":
    out = []
    out.append(render("idle", OverlayViewModel(), frames=8))
    out.append(render("live", _live_vm(), frames=14))
    scanning = _live_vm(banner=False)
    scanning.scan.begin()
    out.append(render("scanning", scanning, frames=5))
    out.append(render("brake", _live_vm(losses=4, banner=False), frames=10))
    sheet = Image.new("RGBA", (sum(Image.open(p).width + 16 for p in out) + 16, max(Image.open(p).height for p in out) + 32), "#03050a")
    x = 16
    for p in out:
        im = Image.open(p)
        sheet.alpha_composite(im, (x, 16))
        x += im.width + 16
    sheet_path = Path(__file__).with_name("preview-sheet.png")
    sheet.save(sheet_path)
    print("\n".join(str(p) for p in out + [sheet_path]))
