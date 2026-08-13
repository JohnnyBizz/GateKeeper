"""The chart source interface.

Every way of getting candles — synthetic generator, CSV replay, screen capture
— implements ``ChartSource``. The engine never knows which one it is talking
to; it only sees candles plus a ``DataQuality`` report telling it how much to
trust them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..models import DataQuality, Series


@dataclass
class Capture:
    """One read of the chart."""

    series: Series | None
    quality: DataQuality
    asset: str | None = None
    timeframe_seconds: int | None = None
    screenshot_path: str | None = None
    # Raw image bytes (PNG) when the source produced one, for the journal.
    screenshot_png: bytes | None = field(default=None, repr=False)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.series is not None and self.quality.ok


class ChartSource(ABC):
    """Produces candles for the engine to analyse."""

    #: Human-readable name shown in the dashboard.
    name: str = "unknown"

    #: True when the source reads pixels and can therefore misread the chart.
    vision_based: bool = False

    @abstractmethod
    def capture(self) -> Capture:
        """Read the current chart state."""

    def start(self) -> None:
        """Optional hook for sources that hold resources."""

    def stop(self) -> None:
        """Optional teardown hook."""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "vision_based": self.vision_based}


class ChartSourceError(RuntimeError):
    """Raised when a source cannot be constructed or used."""
