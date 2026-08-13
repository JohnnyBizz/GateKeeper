"""Chart recognition, data validation and the chart sources.

The vision tests render a series to an image with known geometry, extract it
again, and check the recovered candles match the originals. That round trip is
the only honest way to test pixel analysis without a real screen.
"""

from __future__ import annotations

import statistics

import numpy as np
import pytest

from conftest import build_series, trending_series
from poa.chart_detection import (
    ChartSourceError,
    CsvChartSource,
    SyntheticChartSource,
    SyntheticConfig,
    generate_series,
    load_csv,
    validate_series,
    write_csv,
)
from poa.chart_detection.calibration import PriceCalibration, relative_calibration
from poa.models import Candle, Series

cv2 = pytest.importorskip("cv2", reason="OpenCV is only needed for the screen source")

from poa.chart_detection.candles import (  # noqa: E402
    ColorProfile,
    extract_pixel_candles,
    pixel_candles_to_series,
)
from poa.chart_detection.render import RenderStyle, render_series  # noqa: E402


class TestCandleExtraction:
    def _round_trip(self, series, height=460, style=None):
        image, mapping = render_series(series, height=height, style=style)
        extraction = extract_pixel_candles(image)
        recovered = pixel_candles_to_series(
            extraction.candles,
            mapping.price_at_row,
            timeframe_seconds=series.timeframe_seconds,
            symbol=series.symbol,
            mark_last_incomplete=False,
        )
        return extraction, recovered

    def test_every_candle_is_found(self):
        series = generate_series(60, seed=11)
        extraction, _ = self._round_trip(series)
        assert len(extraction.candles) == len(series)

    def test_recognition_confidence_is_high_on_a_clean_chart(self):
        extraction, _ = self._round_trip(generate_series(60, seed=11))
        assert extraction.confidence >= 90

    def test_candle_direction_is_recovered_exactly(self):
        series = generate_series(60, seed=11)
        _, recovered = self._round_trip(series)
        pairs = list(zip(series.candles, recovered.candles))
        agreeing = sum(1 for a, b in pairs if a.bullish == b.bullish)
        assert agreeing == len(pairs)

    def test_prices_are_recovered_to_sub_pixel_accuracy(self):
        # The body must be separated from the wick, otherwise open/close come
        # back as the candle's extremes and the error is an order larger.
        series = generate_series(60, seed=23)
        _, recovered = self._round_trip(series)
        span = float(series.high.max() - series.low.min())
        errors = [
            abs(a.close - b.close) for a, b in zip(series.candles, recovered.candles)
        ]
        assert statistics.median(errors) / span < 0.005

    def test_highs_and_lows_come_from_the_wicks(self):
        series = generate_series(60, seed=44)
        _, recovered = self._round_trip(series)
        span = float(series.high.max() - series.low.min())
        errors = [
            abs(a.high - b.high) for a, b in zip(series.candles, recovered.candles)
        ]
        assert statistics.median(errors) / span < 0.01

    def test_a_wick_is_never_reported_inside_the_body(self):
        image, _ = render_series(generate_series(40, seed=5), height=440)
        for candle in extract_pixel_candles(image).candles:
            assert candle.wick_top <= candle.body_top
            assert candle.wick_bottom >= candle.body_bottom

    def test_candle_pitch_is_detected(self):
        style = RenderStyle(candle_width=7, candle_gap=4)
        image, _ = render_series(generate_series(50, seed=3), height=440, style=style)
        extraction = extract_pixel_candles(image)
        assert extraction.pitch == pytest.approx(11, abs=1)

    def test_a_blank_image_reports_zero_confidence(self):
        blank = np.full((300, 400, 3), (24, 26, 32), dtype=np.uint8)
        extraction = extract_pixel_candles(blank)
        assert extraction.candles == []
        assert extraction.confidence == 0.0
        assert extraction.issues

    def test_an_unrecognised_palette_is_reported_rather_than_guessed(self):
        # Purple candles against the default green/red profile: the extractor
        # must say it found nothing, not invent candles from the background.
        style = RenderStyle(bullish=(200, 40, 160), bearish=(180, 30, 140))
        image, _ = render_series(generate_series(40, seed=7), height=420, style=style)
        extraction = extract_pixel_candles(image)
        assert extraction.confidence < 50

    def test_a_custom_palette_recovers_those_candles(self):
        style = RenderStyle(bullish=(200, 40, 160), bearish=(180, 30, 140))
        image, _ = render_series(generate_series(40, seed=7), height=420, style=style)
        profile = ColorProfile(
            bullish=[((140, 60, 60), (165, 255, 255))],
            bearish=[((140, 60, 60), (165, 255, 255))],
        )
        extraction = extract_pixel_candles(image, profile)
        assert len(extraction.candles) >= 35

    def test_non_colour_input_is_rejected(self):
        from poa.chart_detection.candles import CandleExtractionError

        with pytest.raises(CandleExtractionError):
            extract_pixel_candles(np.zeros((10, 10), dtype=np.uint8))


class TestCalibration:
    def test_a_linear_map_round_trips(self):
        calibration = PriceCalibration(0.0, 1.09, 100.0, 1.08)
        assert calibration.price_at_row(0) == pytest.approx(1.09)
        assert calibration.price_at_row(100) == pytest.approx(1.08)
        assert calibration.price_at_row(50) == pytest.approx(1.085)
        assert calibration.row_at_price(1.085) == pytest.approx(50)

    def test_price_decreases_as_rows_increase(self):
        calibration = PriceCalibration(0.0, 1.09, 100.0, 1.08)
        assert calibration.price_at_row(10) > calibration.price_at_row(90)

    def test_a_degenerate_calibration_is_invalid(self):
        assert not PriceCalibration(10.0, 1.08, 10.0, 1.09).valid
        assert not PriceCalibration(0.0, 1.08, 100.0, 1.08).valid

    def test_the_relative_fallback_reports_low_confidence(self):
        calibration = relative_calibration(400)
        assert calibration.method == "uncalibrated"
        assert calibration.confidence < 50

    def test_config_parsing_requires_enabled(self):
        raw = {
            "enabled": False,
            "top_pixel": 0,
            "top_price": 1.09,
            "bottom_pixel": 100,
            "bottom_price": 1.08,
        }
        assert PriceCalibration.from_config(raw) is None
        raw["enabled"] = True
        assert PriceCalibration.from_config(raw) is not None

    def test_malformed_config_is_rejected_rather_than_guessed(self):
        assert PriceCalibration.from_config({"enabled": True, "top_pixel": "x"}) is None


class TestValidation:
    def test_a_healthy_series_passes(self):
        quality = validate_series(trending_series(200), min_candles=60)
        assert quality.ok
        assert quality.confidence > 80

    def test_none_is_rejected(self):
        quality = validate_series(None)
        assert not quality.ok
        assert quality.confidence == 0.0

    def test_too_few_candles_is_rejected(self):
        quality = validate_series(trending_series(10), min_candles=60)
        assert not quality.ok
        assert any("Too few candles" in issue for issue in quality.issues)

    def test_a_short_series_lowers_confidence(self):
        full = validate_series(trending_series(200), min_candles=60)
        short = validate_series(trending_series(40), min_candles=60)
        assert short.confidence < full.confidence

    def test_a_frozen_chart_is_detected(self):
        flat = build_series([1.08] * 120, wick=0.0)
        quality = validate_series(flat, min_candles=60)
        assert any("no range" in issue or "not moved" in issue for issue in quality.issues)

    def test_an_implausible_jump_is_flagged(self):
        closes = [1.08 + 0.00001 * i for i in range(120)]
        closes[60] = 5.0  # a misread candle, not a real gap
        quality = validate_series(build_series(closes), min_candles=60)
        assert any("implausible" in issue for issue in quality.issues)

    def test_low_recognition_confidence_caps_the_result(self):
        quality = validate_series(
            trending_series(200), min_candles=60, recognition_confidence=40.0
        )
        assert quality.confidence <= 40.0
        assert not quality.ok

    def test_irregular_spacing_is_flagged(self):
        from datetime import timedelta

        from conftest import START

        candles = []
        price = 1.08
        offset = 0
        for i in range(120):
            offset += 60 if i % 3 else 137  # deliberately uneven
            candles.append(
                Candle(
                    timestamp=START + timedelta(seconds=offset),
                    open=price,
                    high=price + 0.0002,
                    low=price - 0.0002,
                    close=price + 0.0001,
                )
            )
            price += 0.0001
        quality = validate_series(Series(candles, 60, "X"), min_candles=60)
        assert any("irregular" in issue for issue in quality.issues)

    def test_a_timeframe_mismatch_is_reported(self):
        quality = validate_series(
            trending_series(200, timeframe=60), min_candles=60, expected_timeframe=300
        )
        assert any("spacing" in issue for issue in quality.issues)


class TestSyntheticSource:
    def test_capture_returns_a_usable_series(self):
        source = SyntheticChartSource(SyntheticConfig(history=200, seed=1))
        capture = source.capture()
        assert capture.ok
        assert len(capture.series) > 100

    def test_each_capture_advances_by_one_candle(self):
        source = SyntheticChartSource(SyntheticConfig(history=100, seed=1))
        first = source.capture().series.last.timestamp
        second = source.capture().series.last.timestamp
        assert (second - first).total_seconds() == source.config.timeframe_seconds

    def test_history_is_capped(self):
        source = SyntheticChartSource(
            SyntheticConfig(history=100, max_candles=120, seed=1)
        )
        for _ in range(60):
            capture = source.capture()
        assert len(capture.series) <= 120

    def test_the_demo_nature_is_disclosed(self):
        capture = SyntheticChartSource(SyntheticConfig(history=100, seed=1)).capture()
        assert any("Synthetic" in issue for issue in capture.quality.issues)
        assert capture.meta["demo"] is True


class TestCsvSource:
    def test_write_then_read_round_trips(self, tmp_path):
        original = generate_series(120, seed=8)
        path = write_csv(original, tmp_path / "candles.csv")
        loaded = load_csv(path, symbol="EUR/USD")
        assert len(loaded) == len(original)
        assert loaded[0].close == pytest.approx(original[0].close, abs=1e-6)

    def test_the_timeframe_is_inferred(self, tmp_path):
        path = write_csv(generate_series(60, timeframe_seconds=300, seed=8), tmp_path / "c.csv")
        assert load_csv(path).timeframe_seconds == 300

    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(ChartSourceError):
            load_csv(tmp_path / "nope.csv")

    def test_missing_columns_are_reported_by_name(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("timestamp,open,close\n2026-01-01T00:00:00+00:00,1,1\n")
        with pytest.raises(ChartSourceError, match="high"):
            load_csv(path)

    def test_replay_advances_one_candle_per_capture(self, tmp_path):
        path = write_csv(generate_series(300, seed=8), tmp_path / "c.csv")
        source = CsvChartSource(path, window=100)
        first = source.capture().series.last.timestamp
        second = source.capture().series.last.timestamp
        assert second > first

    def test_replay_loops_when_exhausted(self, tmp_path):
        path = write_csv(generate_series(120, seed=8), tmp_path / "c.csv")
        source = CsvChartSource(path, window=60, loop=True)
        for _ in range(200):
            capture = source.capture()
        assert capture.series is not None
