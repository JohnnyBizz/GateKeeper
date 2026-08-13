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


class TestIndicatorOverlays:
    """Charts carry indicator lines drawn in candle colours.

    A SuperTrend, a moving average or a horizontal level is the same green or
    red as the candles, so colour cannot separate them — and where one touches
    a candle the two merge into a single contour, corrupting that candle's
    high, low and body. These tests pin down that overlays are removed without
    costing anything on charts that have none.
    """

    def _chart(self, seed=11, height=460):
        series = generate_series(60, seed=seed)
        image, mapping = render_series(series, height=height)
        return series, image, mapping

    def _recover(self, image, mapping, series):
        extraction = extract_pixel_candles(image)
        recovered = pixel_candles_to_series(
            extraction.candles,
            mapping.price_at_row,
            timeframe_seconds=60,
            symbol="X",
            mark_last_incomplete=False,
        )
        return extraction, recovered

    @staticmethod
    def _staircase(image, colour=(110, 200, 80)):
        """Draw a SuperTrend-style rising staircase across the chart."""
        height, width = image.shape[:2]
        y = int(height * 0.82)
        points = []
        for x in range(20, width - 80, 40):
            y = max(int(height * 0.26), y - 14)
            points.append((x, y))
            points.append((x + 40, y))
        for i in range(len(points) - 1):
            cv2.line(image, points[i], points[i + 1], colour, 2)
        return image

    def test_horizontal_lines_are_removed_entirely(self):
        series, clean, mapping = self._chart()
        overlaid = clean.copy()
        width = overlaid.shape[1]
        for row in (150, 200, 300, 350):
            cv2.line(overlaid, (0, row), (width - 80, row), (70, 70, 225), 1)

        baseline, _ = self._recover(clean, mapping, series)
        result, _ = self._recover(overlaid, mapping, series)
        assert len(result.candles) == len(baseline.candles)

    def test_a_staircase_indicator_does_not_destroy_the_read(self):
        series, image, mapping = self._chart()
        self._staircase(image)
        cv2.line(image, (0, 250), (image.shape[1] - 80, 250), (110, 200, 80), 1)

        extraction, recovered = self._recover(image, mapping, series)
        # Without line removal this collapses to roughly two-thirds of the
        # candles with wildly wrong prices.
        assert len(extraction.candles) >= len(series) - 2

        count = min(len(recovered), len(series))
        agreeing = sum(
            1
            for a, b in zip(series.candles[-count:], recovered.candles[-count:])
            if a.bullish == b.bullish
        )
        assert agreeing >= count - 3

    def test_prices_survive_an_overlay(self):
        series, image, mapping = self._chart()
        self._staircase(image)
        _, recovered = self._recover(image, mapping, series)

        span = float(series.high.max() - series.low.min())
        count = min(len(recovered), len(series))
        errors = [
            abs(a.close - b.close)
            for a, b in zip(series.candles[-count:], recovered.candles[-count:])
        ]
        assert statistics.median(errors) / span < 0.01

    def test_clean_charts_are_not_degraded_by_the_removal(self):
        # The removal must be surgical: a blanket morphological opening would
        # erode every candle slightly and measurably cost accuracy here.
        for seed in (11, 23, 44):
            series, image, mapping = self._chart(seed=seed)
            extraction, recovered = self._recover(image, mapping, series)
            assert len(extraction.candles) == len(series)

            count = min(len(recovered), len(series))
            agreeing = sum(
                1
                for a, b in zip(series.candles[-count:], recovered.candles[-count:])
                if a.bullish == b.bullish
            )
            assert agreeing == count

    def test_stripping_can_be_disabled(self):
        series, image, mapping = self._chart()
        assert extract_pixel_candles(image, strip_lines=False).candles

    def test_a_mask_that_is_mostly_line_is_left_alone(self):
        # On a heavily zoomed chart the bodies themselves could be wide enough
        # to look like runs; wiping them would be catastrophic, so the removal
        # backs off instead.
        from poa.chart_detection.candles import strip_horizontal_lines

        mask = np.zeros((40, 200), dtype=np.uint8)
        mask[10:30, :] = 255  # one enormous solid block
        assert (strip_horizontal_lines(mask) > 0).sum() == (mask > 0).sum()


class TestAssetLabelReading:
    """The candles say nothing about which instrument they belong to."""

    def test_common_formats_normalise(self):
        from poa.chart_detection.asset_label import normalise

        assert normalise("EUR/USD") == "EUR/USD"
        assert normalise("eur/usd") == "EUR/USD"
        assert normalise("EURUSD") == "EUR/USD"
        assert normalise("AED/CNY OTC") == "AED/CNY OTC"
        assert normalise("  GBP/JPY  ") == "GBP/JPY"
        assert normalise("EUR/USD ▾") == "EUR/USD"

    def test_ocr_digit_confusions_are_corrected(self):
        from poa.chart_detection.asset_label import normalise

        # Currency codes contain no digits, so these are unambiguous misreads.
        assert normalise("EUR/U5D") == "EUR/USD"
        assert normalise("GBP/CHF").startswith("GBP")

    def test_implausible_text_is_rejected(self):
        from poa.chart_detection.asset_label import normalise

        # A misread name silently splits the journal, so anything that is not
        # instrument-shaped must be refused rather than guessed at.
        for junk in ("", "   ", "Expiration time", "1.15262", "!!!", "a" * 20):
            assert normalise(junk) is None

    def test_the_reader_is_disabled_without_a_region(self):
        from poa.chart_detection.asset_label import AssetLabelReader

        assert not AssetLabelReader(None).enabled
        assert not AssetLabelReader({"width": 0, "height": 0}).enabled

    def test_a_name_change_needs_consecutive_confirmations(self):
        # A single frame can be misread while the platform animates a
        # transition; a label that flickers between two names is worse than
        # one that lags by a second.
        from poa.chart_detection.asset_label import AssetLabelReader

        reader = AssetLabelReader({"left": 0, "top": 0, "width": 80, "height": 20})
        reader.current = "EUR/USD"

        reader._accept("GBP/JPY")
        assert reader.current == "EUR/USD"  # one frame is not enough
        reader._accept("GBP/JPY")
        assert reader.current == "GBP/JPY"

    def test_a_flickering_read_never_takes_hold(self):
        from poa.chart_detection.asset_label import AssetLabelReader

        reader = AssetLabelReader({"left": 0, "top": 0, "width": 80, "height": 20})
        reader.current = "EUR/USD"
        for name in ("GBP/JPY", "AUD/CAD", "GBP/JPY", "USD/CHF"):
            reader._accept(name)
        assert reader.current == "EUR/USD"

    def test_reading_the_same_name_is_a_no_op(self):
        from poa.chart_detection.asset_label import AssetLabelReader

        reader = AssetLabelReader({"left": 0, "top": 0, "width": 80, "height": 20})
        reader.current = "EUR/USD"
        reader._accept("EUR/USD")
        assert reader.current == "EUR/USD"
        assert reader._streak == 0


class TestTimeframeLabelReading:
    """A 1-minute and a 5-minute chart draw identical-looking candles."""

    def test_platform_formats_parse(self):
        from poa.chart_detection.timeframe_label import parse_timeframe

        assert parse_timeframe("M1") == 60
        assert parse_timeframe("M5") == 300
        assert parse_timeframe("H1") == 3600
        assert parse_timeframe("H2") == 7200
        assert parse_timeframe("S30") == 30
        assert parse_timeframe("5m") == 300
        assert parse_timeframe("15min") == 900
        assert parse_timeframe("1 MIN") == 60
        assert parse_timeframe("D1") == 86400

    def test_non_timeframes_are_rejected(self):
        from poa.chart_detection.timeframe_label import parse_timeframe

        for junk in ("", "   ", "EUR/USD", "1.15262", "xyz", "BUY", "!!"):
            assert parse_timeframe(junk) is None

    def test_a_plausible_looking_misread_is_rejected(self):
        # "M999" parses to just under 17 hours — perfectly valid as a number,
        # and nothing any platform offers. Adopting it would silently reshape
        # every duration recommendation.
        from poa.chart_detection.timeframe_label import TimeframeLabelReader

        reader = TimeframeLabelReader({"left": 0, "top": 0, "width": 40, "height": 20})
        assert reader.parse("M999") is None
        assert reader.parse("M7") is None
        assert reader.parse("M1") == 60

    def test_a_near_miss_snaps_to_the_real_timeframe(self):
        from poa.chart_detection.timeframe_label import snap_to_known

        assert snap_to_known(66) == 60
        assert snap_to_known(305) == 300
        assert snap_to_known(59940) is None

    def test_a_timeframe_change_needs_confirmation_too(self):
        from poa.chart_detection.timeframe_label import TimeframeLabelReader

        reader = TimeframeLabelReader({"left": 0, "top": 0, "width": 40, "height": 20})
        reader.current = 60
        reader._accept(300)
        assert reader.current == 60
        reader._accept(300)
        assert reader.current == 300

    def test_the_reader_is_disabled_without_a_region(self):
        from poa.chart_detection.timeframe_label import TimeframeLabelReader

        assert not TimeframeLabelReader(None).enabled


class TestChartChangeDetection:
    """Switching charts must restart the analysis, not carry it over."""

    def _engine(self, tmp_path):
        from poa.config import load_config
        from poa.engine import AnalysisEngine

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        engine = AnalysisEngine(config)
        # Pretend the source reads pixels; the detection only applies there.
        engine.source.vision_based = True
        return engine

    def test_a_price_level_jump_is_detected(self, tmp_path):
        engine = self._engine(tmp_path)
        try:
            first = generate_series(120, seed=3, start_price=1.08)
            assert not engine._detect_chart_change(first)  # first frame
            second = generate_series(120, seed=3, start_price=1.99)
            assert engine._detect_chart_change(second)
        finally:
            engine.close()

    def test_a_similar_priced_pair_is_caught_by_shape(self, tmp_path):
        # The price test alone misses this: two instruments can trade at
        # almost the same level while drawing completely different candles.
        engine = self._engine(tmp_path)
        try:
            first = generate_series(120, seed=3, start_price=1.08)
            engine._detect_chart_change(first)
            second = generate_series(120, seed=99, start_price=1.081)
            assert engine._detect_chart_change(second)
        finally:
            engine.close()

    def test_an_ordinary_new_candle_is_not_a_chart_change(self, tmp_path):
        engine = self._engine(tmp_path)
        try:
            full = generate_series(200, seed=7)
            engine._detect_chart_change(full[:120])
            # The next poll sees the same candles shifted by one.
            assert not engine._detect_chart_change(full[1:121])
        finally:
            engine.close()

    def test_an_identical_frame_is_not_a_chart_change(self, tmp_path):
        engine = self._engine(tmp_path)
        try:
            series = generate_series(120, seed=7)
            engine._detect_chart_change(series)
            assert not engine._detect_chart_change(series)
        finally:
            engine.close()

    def test_non_vision_sources_are_exempt(self, tmp_path):
        # A CSV or the generator never "switches charts" underneath us.
        engine = self._engine(tmp_path)
        try:
            engine.source.vision_based = False
            engine._detect_chart_change(generate_series(120, seed=3, start_price=1.08))
            assert not engine._detect_chart_change(
                generate_series(120, seed=99, start_price=9.99)
            )
        finally:
            engine.close()


class TestCalibrationInvalidation:
    """A manual price scale belongs to one chart and must not outlive it."""

    def _source(self):
        from poa.chart_detection.calibration import PriceCalibration
        from poa.chart_detection.screen import Region, ScreenChartSource

        return ScreenChartSource(
            Region(0, 0, 400, 300),
            calibration=PriceCalibration(0.0, 1.09, 100.0, 1.08),
        )

    def test_calibration_starts_trusted(self):
        source = self._source()
        assert not source._calibration_suspect
        assert source.manual_calibration is not None

    def test_invalidation_marks_it_suspect(self):
        source = self._source()
        source.invalidate_calibration()
        assert source._calibration_suspect

    def test_the_engine_invalidates_on_a_chart_change(self, tmp_path):
        from poa.config import load_config
        from poa.engine import AnalysisEngine

        config = load_config()
        config.set("storage.database", str(tmp_path / "j.db"))
        config.set("storage.screenshot_dir", str(tmp_path / "s"))
        config.set("logging.file", str(tmp_path / "p.log"))
        config.set("alerts.desktop_notifications", False)
        engine = AnalysisEngine(config)
        try:
            engine.source.vision_based = True
            calls: list[bool] = []
            engine.source.invalidate_calibration = lambda: calls.append(True)  # type: ignore[attr-defined]

            engine._detect_chart_change(generate_series(120, seed=3, start_price=1.08))
            # Drive a change through the same path tick() uses.
            if engine._detect_chart_change(
                generate_series(120, seed=3, start_price=1.99)
            ):
                invalidate = getattr(engine.source, "invalidate_calibration", None)
                invalidate()
            assert calls == [True]
        finally:
            engine.close()
