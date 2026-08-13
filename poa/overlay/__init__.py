"""The always-on-top overlay panel.

``viewmodel`` holds every rendering decision as plain data and imports no GUI
toolkit, so it is fully testable headlessly. ``panel`` and ``app`` are imported
lazily because they need Tkinter, which is not present on every Python build.
"""

from __future__ import annotations

from .viewmodel import (
    COLORS,
    SCAN_DURATION_SECONDS,
    OverlayViewModel,
    ScanController,
    ScanState,
    direction_color,
    score_color,
    strength_badge,
)

__all__ = [
    "COLORS",
    "OverlayApp",
    "OverlayPanel",
    "OverlayViewModel",
    "SCAN_DURATION_SECONDS",
    "ScanController",
    "ScanState",
    "direction_color",
    "run",
    "score_color",
    "strength_badge",
]


def __getattr__(name: str):
    """Import the Tk-dependent pieces only when they are actually asked for."""
    if name in ("OverlayApp", "run"):
        from . import app

        return getattr(app, name)
    if name == "OverlayPanel":
        from .panel import OverlayPanel

        return OverlayPanel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
