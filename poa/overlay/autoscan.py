"""Finding the chart on screen and configuring the app from what it finds.

This is the glue between the layout detector and the app's settings. It exists
so the user never has to describe their screen to GateKeeper: press Scan, and
the chart, its price axis, its pair name and its timeframe are located, written
into the config and picked up by the engine.

Kept out of ``app.py`` and free of Tk so it can be tested without a display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..chart_detection.autodetect import Box, Layout, detect_layout
from ..config import Config
from ..logging_setup import get_logger
from ..models import format_duration

log = get_logger(__name__)


@dataclass
class AutoScanResult:
    """What an automatic scan found, and what it changed."""

    layout: Layout
    applied: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "message": self.message,
            "layout": self.layout.to_dict(),
        }


def apply_layout(
    config: Config,
    layout: Layout,
    origin: tuple[int, int] = (0, 0),
) -> bool:
    """Write a detected layout into the config. Returns True if anything changed.

    ``origin`` is the monitor's top-left in virtual-screen coordinates, because
    the detector works on one monitor's image while capture regions are stored
    against the whole desktop.
    """
    if not layout.ok or layout.chart is None:
        return False

    dx, dy = origin
    config.set("capture.region", layout.chart.offset(dx, dy).to_dict())
    config.set("capture.source", "screen")

    # A calibration is a pixel row in the *old* region. The new region has a
    # different origin and height, so those rows now point at different prices.
    # Dropping it costs one re-calibration; keeping it invents prices.
    config.set("capture.calibration", {"enabled": False})

    if layout.asset is not None:
        config.set("capture.asset_region", layout.asset.offset(dx, dy).to_dict())
    if layout.asset_name:
        config.set("market.asset", layout.asset_name)
    if layout.timeframe is not None:
        config.set("capture.timeframe_region", layout.timeframe.offset(dx, dy).to_dict())
    if layout.timeframe_seconds:
        config.set("market.chart_timeframe", int(layout.timeframe_seconds))
    return True


def describe(layout: Layout) -> str:
    """A one-line summary of a scan, in the user's terms."""
    if not layout.ok:
        return layout.issues[0] if layout.issues else "No chart was found on screen."

    parts = [f"Found a chart with {layout.candles_found} candles"]
    if layout.asset_name:
        parts.append(f"on {layout.asset_name}")
    if layout.timeframe_seconds:
        parts.append(f"at {format_duration(layout.timeframe_seconds)} per candle")
    summary = " ".join(parts) + "."
    if layout.issues:
        summary += " " + " ".join(layout.issues[:2])
    return summary


def scan_screen(
    config: Config,
    *,
    grab: Callable[[int], tuple[Any, dict[str, int]]] | None = None,
    exclude: list[Box] | None = None,
) -> AutoScanResult:
    """Capture the screen, find the chart, and configure the app from it.

    ``grab`` returns (image, monitor) for a monitor index — injected so tests
    can supply a rendered screen instead of a real one.
    """
    if grab is None:
        from .region_picker import capture_screen

        grab = capture_screen

    monitor_index = int(config.get("capture.monitor", 1))
    try:
        image, monitor = grab(monitor_index)
    except Exception as exc:
        log.warning("screen capture for auto-scan failed: %s", exc)
        return AutoScanResult(
            layout=Layout(issues=[f"Could not capture the screen: {exc}"]),
            applied=False,
            message=f"Could not capture the screen: {exc}",
        )

    from ..chart_detection.candles import ColorProfile

    layout = detect_layout(
        image,
        profile=ColorProfile.from_config(config.get("capture.colors")),
        exclude=exclude or [],
    )
    origin = (int(monitor.get("left", 0)), int(monitor.get("top", 0)))
    applied = apply_layout(config, layout, origin)
    if applied:
        try:
            config.save()
        except OSError as exc:  # pragma: no cover - filesystem dependent
            log.warning("could not save the detected layout: %s", exc)
    return AutoScanResult(layout=layout, applied=applied, message=describe(layout))
