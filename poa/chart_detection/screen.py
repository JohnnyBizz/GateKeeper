"""Screen-capture chart source.

Why screen capture and not a browser extension or an API:

* Pocket Option publishes no public market-data API, and reverse-engineering
  their private socket protocol would mean shipping something that breaks on
  every deploy and sits badly against their terms of service;
* a browser extension can read the DOM, but the chart is drawn on a ``<canvas>``
  element, so the DOM contains no candle values to read — an extension would
  end up doing pixel analysis too, just inside the page;
* screen capture works against whatever is on screen — the web platform, the
  desktop client, a demo account, or a chart in another window entirely — and
  needs no injection into anyone's site.

The trade-off is honest: this reads pixels, so it can be wrong. Every capture
carries a recognition confidence, and low confidence produces WAIT.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

from ..logging_setup import get_logger
from ..models import Series
from .base import Capture, ChartSource, ChartSourceError
from .asset_label import AssetLabelReader
from .calibration import PriceCalibration, resolve_calibration
from .timeframe_label import TimeframeLabelReader
from .candles import (
    CandleExtractionError,
    ColorProfile,
    extract_pixel_candles,
    pixel_candles_to_series,
)
from .quality import validate_series

log = get_logger(__name__)

try:  # pragma: no cover - optional
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

try:  # pragma: no cover - optional
    import mss
except ImportError:  # pragma: no cover
    mss = None  # type: ignore[assignment]


@dataclass
class Region:
    left: int
    top: int
    width: int
    height: int

    @property
    def valid(self) -> bool:
        return self.width > 50 and self.height > 50

    def to_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> "Region | None":
        if not raw:
            return None
        try:
            region = cls(
                left=int(raw.get("left", 0)),
                top=int(raw.get("top", 0)),
                width=int(raw.get("width", 0)),
                height=int(raw.get("height", 0)),
            )
        except (TypeError, ValueError):
            return None
        return region if region.valid else None


class ScreenChartSource(ChartSource):
    """Captures a screen region and extracts candles from it."""

    name = "screen"
    vision_based = True

    def __init__(
        self,
        region: Region,
        *,
        symbol: str = "UNKNOWN",
        timeframe_seconds: int = 60,
        calibration: PriceCalibration | None = None,
        color_profile: ColorProfile | None = None,
        use_ocr: bool = True,
        axis_width_px: int = 70,
        min_candles: int = 60,
        save_screenshots: bool = True,
        asset_region: dict[str, int] | None = None,
        timeframe_region: dict[str, int] | None = None,
    ) -> None:
        if mss is None:
            raise ChartSourceError(
                "Screen capture needs the 'mss' package. Install it with "
                "'pip install mss', or use the csv/synthetic source instead."
            )
        if cv2 is None:
            raise ChartSourceError(
                "Screen capture needs OpenCV. Install it with "
                "'pip install opencv-python'."
            )
        if not region.valid:
            raise ChartSourceError(
                "No chart area has been selected yet. Open settings (the gear "
                "button) and press Select next to Chart area."
            )

        self.region = region
        self.symbol = symbol
        self.timeframe_seconds = int(timeframe_seconds)
        self.manual_calibration = calibration
        self.color_profile = color_profile or ColorProfile()
        self.use_ocr = use_ocr
        self.axis_width_px = axis_width_px
        self.min_candles = min_candles
        self.save_screenshots = save_screenshots
        self._sct = None
        self._last_calibration: PriceCalibration | None = None
        self._consecutive_failures = 0
        # Manual calibration describes one specific chart: those two reference
        # prices were read off *that* axis. When the user switches pairs on the
        # platform they no longer apply, and continuing to use them reports
        # confident, precisely wrong prices — worse than reporting none. The
        # engine calls invalidate_calibration() when it detects a switch.
        self._calibration_suspect = False
        # Optional: small regions over the platform's pair and timeframe
        # badges, so both follow the user when they switch charts. Neither can
        # be inferred from candles — a 1-minute and a 5-minute chart draw
        # identical-looking bars, and prices carry no instrument name.
        self.asset_reader = AssetLabelReader(asset_region)
        self.timeframe_reader = TimeframeLabelReader(timeframe_region)

    # ------------------------------------------------------------------

    def invalidate_calibration(self) -> None:
        """Stop trusting the manual price scale — the chart changed."""
        if self.manual_calibration is not None:
            log.info(
                "chart changed; dropping the manual price calibration and "
                "re-reading the scale from the new chart"
            )
        self._calibration_suspect = True

    def start(self) -> None:
        if self._sct is None:
            self._sct = mss.mss()

    def stop(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # pragma: no cover - best effort
                pass
            self._sct = None

    def _grab_region(self, region: dict[str, int]) -> np.ndarray:
        """Grab an arbitrary screen region as BGR — used for the pair label."""
        self.start()
        assert self._sct is not None
        raw = self._sct.grab(region)
        return cv2.cvtColor(np.asarray(raw), cv2.COLOR_BGRA2BGR)

    def grab(self) -> np.ndarray:
        """Grab the configured region as a BGR image."""
        self.start()
        assert self._sct is not None
        raw = self._sct.grab(self.region.to_dict())
        frame = np.asarray(raw)  # BGRA
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    # ------------------------------------------------------------------

    def capture(self) -> Capture:
        try:
            image = self.grab()
        except Exception as exc:
            self._consecutive_failures += 1
            return self._failure(
                f"Screen capture failed: {exc}. The chart window may have been "
                "closed, moved, or the screen resolution may have changed."
            )

        if image is None or image.size == 0:
            self._consecutive_failures += 1
            return self._failure("Screen capture returned an empty image.")

        try:
            extraction = extract_pixel_candles(image, self.color_profile)
        except CandleExtractionError as exc:
            self._consecutive_failures += 1
            return self._failure(f"Candle recognition failed: {exc}")

        if not extraction.candles:
            self._consecutive_failures += 1
            return self._failure(
                "No candles were recognised in the selected region. "
                + "; ".join(extraction.issues)
            )

        calibration = resolve_calibration(
            image,
            None if self._calibration_suspect else self.manual_calibration,
            use_ocr=self.use_ocr,
            axis_width_px=self.axis_width_px,
        )
        # Once the axis has been read successfully from the new chart, the
        # manual calibration is genuinely superseded rather than merely
        # distrusted, so drop it for good instead of re-testing every poll.
        if self._calibration_suspect and calibration.method == "ocr":
            self.manual_calibration = None
            self._calibration_suspect = False
        self._last_calibration = calibration

        # Read the pair's name, if the user pointed us at the label. The
        # candles cannot tell us which instrument they belong to.
        asset_reading = None
        if self.asset_reader.enabled:
            asset_reading = self.asset_reader.read(self._grab_region)
        symbol = self.asset_reader.current or self.symbol

        # Reading the timeframe changes what the candles *mean*: every duration
        # recommendation is expressed in candles of this length, so a chart read
        # as M1 when it is really M5 would recommend expirations five times too
        # short. Adopt it for real rather than only reporting it.
        timeframe_reading = None
        if self.timeframe_reader.enabled:
            timeframe_reading = self.timeframe_reader.read(self._grab_region)
            detected = self.timeframe_reader.current
            if detected and detected != self.timeframe_seconds:
                log.info(
                    "chart timeframe detected as %ss (was %ss)",
                    detected, self.timeframe_seconds,
                )
                self.timeframe_seconds = int(detected)

        series = pixel_candles_to_series(
            extraction.candles,
            calibration.price_at_row,
            timeframe_seconds=self.timeframe_seconds,
            symbol=symbol,
            end_time=datetime.now(timezone.utc).replace(microsecond=0),
        )

        # Recognition confidence blends how well candles were found with how
        # well the price scale is known.
        recognition_confidence = min(extraction.confidence, calibration.confidence)
        quality = validate_series(
            series,
            min_candles=self.min_candles,
            source="screen",
            recognition_confidence=recognition_confidence,
            expected_timeframe=self.timeframe_seconds,
        )
        quality.issues.extend(extraction.issues)
        if calibration.method == "uncalibrated":
            quality.issues.append(
                "Price scale is not calibrated — levels shown are relative, not "
                "real prices. Open settings, press Select next to Chart area, "
                "and click two prices when asked."
            )

        self._consecutive_failures = 0

        png: bytes | None = None
        if self.save_screenshots:
            ok, buffer = cv2.imencode(".png", image)
            if ok:
                png = buffer.tobytes()

        return Capture(
            series=series,
            quality=quality,
            asset=symbol,
            timeframe_seconds=self.timeframe_seconds,
            screenshot_png=png,
            meta={
                "extraction": extraction.to_dict(),
                "calibration": calibration.to_dict(),
                "region": self.region.to_dict(),
                "asset_label": asset_reading.to_dict() if asset_reading else None,
                "timeframe_label": (
                    timeframe_reading.to_dict() if timeframe_reading else None
                ),
            },
        )

    # ------------------------------------------------------------------

    def _failure(self, message: str) -> Capture:
        log.warning("%s", message)
        issues = [message]
        if self._consecutive_failures >= 3:
            issues.append(
                f"{self._consecutive_failures} consecutive capture failures — "
                "re-select the chart region."
            )
        return Capture(
            series=None,
            quality=validate_series(None, source="screen"),
            asset=self.symbol,
            timeframe_seconds=self.timeframe_seconds,
            meta={"error": message},
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "vision_based": True,
            "symbol": self.symbol,
            "timeframe_seconds": self.timeframe_seconds,
            "region": self.region.to_dict(),
            "calibration": (
                self._last_calibration.to_dict() if self._last_calibration else None
            ),
        }
