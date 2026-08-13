"""Getting candles into the engine, from a screen, a file, or a generator."""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..logging_setup import get_logger
from .base import Capture, ChartSource, ChartSourceError
from .calibration import PriceCalibration, calibrate_with_ocr, resolve_calibration
from .candles import (
    CandleExtractionError,
    ColorProfile,
    ExtractionResult,
    PixelCandle,
    extract_pixel_candles,
    pixel_candles_to_series,
)
from .csv_source import CsvChartSource, load_csv, write_csv
from .quality import validate_series
from .synthetic import SyntheticChartSource, SyntheticConfig, generate_series

log = get_logger(__name__)

__all__ = [
    "Capture",
    "CandleExtractionError",
    "ChartSource",
    "ChartSourceError",
    "ColorProfile",
    "CsvChartSource",
    "ExtractionResult",
    "PixelCandle",
    "PriceCalibration",
    "SyntheticChartSource",
    "SyntheticConfig",
    "build_source",
    "calibrate_with_ocr",
    "extract_pixel_candles",
    "generate_series",
    "load_csv",
    "pixel_candles_to_series",
    "resolve_calibration",
    "validate_series",
    "write_csv",
]


def build_source(config: Config) -> ChartSource:
    """Construct the chart source named in the configuration.

    Falls back to the synthetic source when the requested one cannot be built,
    so the application always starts and always tells the user why.
    """
    kind = str(config.get("capture.source", "synthetic")).lower()
    asset = str(config.get("market.asset", "EUR/USD"))
    timeframe = int(config.get("market.chart_timeframe", 60))
    max_candles = int(config.get("market.max_candles", 600))
    min_candles = int(config.get("market.min_candles", 60))

    if kind == "screen":
        try:
            from .screen import Region, ScreenChartSource

            region = Region.from_config(config.get("capture.region"))
            if region is None:
                raise ChartSourceError(
                    "capture.region is not set. Run 'python tools/select_region.py' "
                    "to select your chart area."
                )
            return ScreenChartSource(
                region=region,
                symbol=asset,
                timeframe_seconds=timeframe,
                calibration=PriceCalibration.from_config(
                    config.get("capture.calibration")
                ),
                color_profile=ColorProfile.from_config(config.get("capture.colors")),
                use_ocr=bool(config.get("capture.ocr.enabled", True)),
                axis_width_px=int(config.get("capture.ocr.axis_width_px", 70)),
                min_candles=min_candles,
                save_screenshots=bool(config.get("capture.save_screenshots", True)),
            )
        except (ChartSourceError, ImportError) as exc:
            log.error("Screen source unavailable (%s); falling back to synthetic.", exc)

    elif kind == "csv":
        try:
            return CsvChartSource(
                config.resolve_path("capture.csv_path"),
                symbol=asset,
                timeframe_seconds=timeframe,
                window=max_candles,
            )
        except ChartSourceError as exc:
            log.error("CSV source unavailable (%s); falling back to synthetic.", exc)

    elif kind != "synthetic":
        log.error("Unknown capture.source %r; falling back to synthetic.", kind)

    return SyntheticChartSource(
        SyntheticConfig(
            symbol=asset,
            timeframe_seconds=timeframe,
            max_candles=max_candles,
            history=min(max_candles, 400),
        )
    )
