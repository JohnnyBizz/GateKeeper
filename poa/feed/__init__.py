"""Reading the platform's own data feed instead of a picture of it.

The chart is drawn on a canvas from numbers that arrive over a WebSocket.
Recovering those numbers from pixels means inferring them at every stage —
which shapes are candles, where each one begins, what price each row is — and
each stage fails on its own. This package reads the numbers.

It attaches to the user's browser through the DevTools protocol, the same way
the Network tab does, and listens. Nothing is sent to the platform.
"""

from .cdp import BrowserError, Target, find_browser, launch_browser, list_targets, pick_target
from .frames import Frame, Summary, decode_frame
from .recorder import Capture, record_platform

__all__ = [
    "BrowserError",
    "Capture",
    "Frame",
    "Summary",
    "Target",
    "decode_frame",
    "find_browser",
    "launch_browser",
    "list_targets",
    "pick_target",
    "record_platform",
]
