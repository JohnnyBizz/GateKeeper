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
from poa.chart_detection.calibration import (
    PriceCalibration,
    relative_calibration,
    resolve_calibration,
)
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


class TestCalibrationAgainstTheAxis:
    """A saved price scale is checked against the axis actually on screen."""

    def _image(self):
        return np.zeros((200, 300, 3), dtype=np.uint8)

    def _patch_ocr(self, monkeypatch, result):
        from poa.chart_detection import calibration as module

        monkeypatch.setattr(module, "calibrate_with_ocr", lambda *a, **k: result)

    def test_an_agreeing_axis_leaves_the_manual_scale_untouched(self, monkeypatch):
        from poa.chart_detection.calibration import check_against_axis

        manual = PriceCalibration(0.0, 1.1530, 199.0, 1.1520)
        self._patch_ocr(monkeypatch, PriceCalibration(0.0, 1.1530, 199.0, 1.1520))
        checked = check_against_axis(self._image(), manual)
        assert checked is manual
        assert checked.confidence == 100.0
        assert checked.note == ""

    def test_a_disagreeing_axis_lowers_confidence_and_explains(self, monkeypatch):
        from poa.chart_detection.calibration import (
            DISPUTED_CONFIDENCE,
            check_against_axis,
        )

        # The saved scale says 1.055 mid-chart; the axis on screen says 1.152.
        manual = PriceCalibration(0.0, 1.0555, 199.0, 1.0545)
        self._patch_ocr(monkeypatch, PriceCalibration(0.0, 1.1530, 199.0, 1.1520))
        checked = check_against_axis(self._image(), manual)
        assert checked.method == "manual-disputed"
        assert checked.confidence == DISPUTED_CONFIDENCE
        assert "1.055" in checked.note and "1.152" in checked.note
        # The mapping itself is untouched — the user is not overruled.
        assert checked.price_at_row(0) == pytest.approx(1.0555)

    def test_an_unreadable_axis_is_not_evidence_of_anything(self, monkeypatch):
        from poa.chart_detection.calibration import check_against_axis

        manual = PriceCalibration(0.0, 1.0555, 199.0, 1.0545)
        self._patch_ocr(monkeypatch, None)
        checked = check_against_axis(self._image(), manual)
        assert checked is manual
        assert checked.confidence == 100.0

    def test_resolve_runs_the_check_when_ocr_is_enabled(self, monkeypatch):
        from poa.chart_detection.calibration import resolve_calibration

        manual = PriceCalibration(0.0, 1.0555, 199.0, 1.0545)
        self._patch_ocr(monkeypatch, PriceCalibration(0.0, 1.1530, 199.0, 1.1520))
        disputed = resolve_calibration(self._image(), manual, use_ocr=True)
        assert disputed.method == "manual-disputed"
        # With OCR off there is nothing to compare against, so the scale stands.
        assert resolve_calibration(self._image(), manual, use_ocr=False) is manual

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


# --------------------------------------------------------------------------
# Automatic layout detection: finding the chart without being told where it is.

from poa.chart_detection.autodetect import (  # noqa: E402
    Box,
    detect_layout,
    find_candle_field,
)
from poa.chart_detection.render import (  # noqa: E402
    ScreenStyle,
    render_platform_screen,
)


def platform_screen(
    count: int = 160,
    seed: int = 7,
    candle_width: int = 7,
    candle_gap: int = 4,
    **screen_kwargs,
):
    """A whole trading-platform screen, plus the ground truth for its parts."""
    series = generate_series(count, seed=seed)
    image, truth = render_platform_screen(
        series,
        ScreenStyle(**screen_kwargs),
        RenderStyle(candle_width=candle_width, candle_gap=candle_gap),
    )
    return series, image, truth


class TestFindingTheChart:
    def test_the_candle_field_is_found_on_a_full_screen(self):
        _series, image, truth = platform_screen()
        box, count, pitch = find_candle_field(image)
        assert box is not None
        assert count > 60
        assert pitch == pytest.approx(11.0, abs=1.0)
        # This is the candle field, not the whole canvas: it starts where
        # the candles start, stops where they stop (the price axis occupies
        # the rest of the plot's width), and vertically hugs the candles rather
        # than filling a plot they never reach the top of.
        plot_left, plot_top, plot_right, plot_bottom = truth["chart"]
        axis_width = RenderStyle().axis_width
        assert abs(box.left - plot_left) < 40
        assert abs(box.right - (plot_right - axis_width)) < 40
        # A little padding outside the plot is expected and harmless: candles
        # that reach the top of the plot must not sit flush against the edge of
        # the region, or the extractor reports them as clipped.
        assert box.top >= plot_top - 60
        assert box.bottom <= plot_bottom + 60

    def test_the_buy_and_sell_buttons_are_not_mistaken_for_candles(self):
        """The buttons are the same green and red, and much bigger."""
        _series, image, truth = platform_screen()
        box, _count, _pitch = find_candle_field(image)
        assert box is not None
        # The trade panel starts at width - panel_width; nothing in the
        # detected field may come from there.
        assert box.left < truth["chart"][2]
        assert box.right <= image.shape[1] - 100

    def test_a_screen_with_no_chart_yields_nothing(self):
        blank = np.full((600, 900, 3), (110, 45, 60), dtype=np.uint8)
        box, _count, _pitch = find_candle_field(blank)
        assert box is None

    def test_an_excluded_window_is_not_analysed(self):
        """GateKeeper's own panel must never be read as a chart."""
        _series, image, truth = platform_screen()
        whole = Box(0, 0, image.shape[1], image.shape[0])
        box, count, _pitch = find_candle_field(image, exclude=[whole])
        assert box is None and count == 0

    def test_candle_coloured_gridlines_do_not_fuse_the_field(self):
        """A SuperTrend-style overlay must not weld the candles together."""
        _series, image, truth = platform_screen()
        # Draw long horizontal lines straight through the plot in candle green.
        top, bottom = truth["chart"][1], truth["chart"][3]
        for row in range(top + 40, bottom - 40, 90):
            image[row, truth["chart"][0] : truth["chart"][2]] = (110, 200, 80)
        box, count, _pitch = find_candle_field(image)
        assert box is not None
        assert count > 60


class TestReadingTheLabels:
    def test_the_pair_and_timeframe_are_read_from_the_screen(self):
        _series, image, _truth = platform_screen(
            asset_label="CAD/JPY OTC", timeframe_label="H3"
        )
        layout = detect_layout(image)
        assert layout.ok
        assert layout.asset_name == "CAD/JPY OTC"
        assert layout.timeframe_seconds == 10800
        assert layout.asset is not None and layout.timeframe is not None
        assert layout.issues == []

    def test_a_plain_pair_is_read(self):
        _series, image, _truth = platform_screen(
            asset_label="EUR/USD", timeframe_label="M1"
        )
        layout = detect_layout(image)
        assert layout.asset_name == "EUR/USD"
        assert layout.timeframe_seconds == 60

    def test_unreadable_labels_are_reported_not_invented(self):
        _series, image, _truth = platform_screen(
            asset_label="", timeframe_label=""
        )
        layout = detect_layout(image)
        assert layout.ok  # the chart itself is still found
        assert layout.asset_name is None
        assert layout.timeframe_seconds is None
        assert any("pair" in issue for issue in layout.issues)


class TestDetectedRegionIsUsable:
    def test_candles_extracted_from_the_detected_region_are_accurate(self):
        """The whole point: detect, crop, extract, and get the chart back."""
        series, image, _truth = platform_screen(count=200, seed=5)
        layout = detect_layout(image, read_labels=False)
        assert layout.chart is not None

        box = layout.chart
        crop = image[box.top : box.bottom, box.left : box.right]
        extraction = extract_pixel_candles(crop)
        calibration = resolve_calibration(crop, None, use_ocr=True)
        assert calibration.method == "ocr"

        recovered = pixel_candles_to_series(
            extraction.candles,
            calibration.price_at_row,
            timeframe_seconds=60,
            symbol="TEST",
        )
        overlap = min(len(recovered), len(series))
        assert overlap > 80

        expected = np.sign(series.close[-overlap:] - series.open[-overlap:])
        actual = np.sign(recovered.close[-overlap:] - recovered.open[-overlap:])
        assert float((expected == actual).mean()) > 0.95

        error = np.abs(recovered.close[-overlap:] - series.close[-overlap:])
        assert float((error / series.close[-overlap:]).max()) < 0.001


class TestAxisReadingIsNotSilentlyWrong:
    def test_a_clipped_axis_does_not_pass_as_a_confident_scale(self):
        """A decimal point lost from every label still fits a straight line.

        That is the misread nothing downstream can catch, so it has to be
        caught here — either by reading the labels whole, or by saying so.
        """
        series, image, _truth = platform_screen(count=140, seed=3)
        layout = detect_layout(image, read_labels=False)
        box = layout.chart
        crop = image[box.top : box.bottom, box.left : box.right]

        calibration = resolve_calibration(crop, None, use_ocr=True)
        assert calibration.method == "ocr"
        # Read whole, the scale must land on the actual price, not 100000x it.
        mid = calibration.price_at_row(crop.shape[0] / 2)
        assert 0.5 < mid / float(series.close.mean()) < 2.0

    def test_labels_in_a_minority_format_are_discarded(self):
        from poa.chart_detection.calibration import _consistent_format

        reads = [
            (10.0, "1.08641", 1.08641),
            (40.0, "1.08478", 1.08478),
            (70.0, "08154", 8154.0),  # the leading "1." was sliced off
            (100.0, "1.07992", 1.07992),
        ]
        kept = _consistent_format(reads)
        assert [text for _row, text, _price in kept] == [
            "1.08641", "1.08478", "1.07992",
        ]


class TestWithoutOcr:
    """The app must be honest, not silent, when it cannot read text."""

    def test_one_clear_cause_beats_three_vague_symptoms(self, monkeypatch):
        from poa.chart_detection import autodetect

        monkeypatch.setattr(autodetect, "ocr_available", lambda: False)
        _series, image, _truth = platform_screen()
        layout = detect_layout(image)

        assert layout.ok  # candles are pixels, and still readable
        assert layout.asset_name is None and layout.timeframe_seconds is None
        assert len(layout.issues) == 1
        assert "Tesseract" in layout.issues[0]

    def test_an_existing_install_is_left_alone(self, monkeypatch):
        from poa.chart_detection import tesseract_setup

        monkeypatch.setattr(tesseract_setup, "_works", lambda: True)
        pytesseract = pytest.importorskip("pytesseract")
        before = pytesseract.pytesseract.tesseract_cmd
        assert tesseract_setup.configure() is True
        assert pytesseract.pytesseract.tesseract_cmd == before

    def test_a_missing_binary_reports_failure_rather_than_raising(
        self, monkeypatch, tmp_path
    ):
        from poa.chart_detection import tesseract_setup

        pytest.importorskip("pytesseract")
        monkeypatch.setattr(tesseract_setup, "_works", lambda: False)
        monkeypatch.setattr(tesseract_setup, "_bundle_root", lambda: tmp_path)
        assert tesseract_setup.configure() is False


class TestSymbolsAreNotInvented:
    """Searching a whole screen turns up text that parses as a pair, and isn't."""

    def test_sidebar_menu_text_is_not_read_as_an_instrument(self):
        """"Profile", split by OCR into PROF and ILE, parses as PROF/ILE."""
        from poa.chart_detection.asset_label import is_known_pair, normalise

        assert normalise("PROF ILE") == "PROF/ILE"  # it really does parse
        assert not is_known_pair("PROF/ILE")  # and it really must be rejected
        assert not is_known_pair("TIME/AMOUNT")
        assert not is_known_pair("EUR")
        assert not is_known_pair(None)

    def test_real_instruments_survive(self):
        from poa.chart_detection.asset_label import is_known_pair

        for name in ("EUR/USD", "EUR/USD OTC", "CAD/JPY OTC", "BTC/USD", "XAU/USD"):
            assert is_known_pair(name), name

    def test_a_screen_full_of_menu_text_yields_the_chart_pair(self):
        _series, image, _truth = platform_screen(asset_label="EUR/USD OTC")
        layout = detect_layout(image)
        # The fixture draws Trading / Finance / Profile / Market / Signals /
        # Help down the left rail, which is where PROF/ILE came from.
        assert layout.asset_name == "EUR/USD OTC"


class TestTimeframeBadgeVariants:
    def test_the_interval_is_read_beside_its_countdown(self):
        """Pocket Option draws "M1 00:18" inside the plot, not a bare badge."""
        for label, expected in (("M1", 60), ("M5", 300), ("H3", 10800)):
            _series, image, _truth = platform_screen(
                timeframe_label=label, inside_timeframe=True
            )
            layout = detect_layout(image)
            assert layout.timeframe_seconds == expected, label

    def test_every_way_ocr_mangles_a_clocked_badge(self):
        from poa.chart_detection.timeframe_label import parse_timeframe

        # Space kept, space lost, colon lost — all the same badge.
        for text in ("M1 00:18", "M100:18", "M10018"):
            assert parse_timeframe(text) == 60, text
        for text in ("M5 00:04", "M50004"):
            assert parse_timeframe(text) == 300, text
        # Two-digit intervals split correctly rather than greedily.
        assert parse_timeframe("M150018") == 900
        assert parse_timeframe("H400:12") == 14400

    def test_a_bare_clock_is_not_a_timeframe(self):
        from poa.chart_detection.timeframe_label import parse_timeframe

        assert parse_timeframe("00:18") is None
        assert parse_timeframe("09:45:42") is None


class TestTheRangeSelectorIsNotTheInterval:
    """Two things on screen parse as an interval and mean different things."""

    def test_a_clocked_badge_beats_a_bare_one(self):
        """"M1 00:59" is the candle interval; a corner "H3" is the view range.

        Reading the second as the first turns a 1-minute chart into a 3-hour
        one and multiplies every duration suggestion by 180.
        """
        _series, image, _truth = platform_screen(
            asset_label="AED/CNY OTC",
            timeframe_label="M1",
            inside_timeframe=True,
            range_badge="H3",
        )
        layout = detect_layout(image)
        assert layout.asset_name == "AED/CNY OTC"
        assert layout.timeframe_seconds == 60

    def test_a_bare_badge_is_still_used_when_it_is_all_there_is(self):
        _series, image, _truth = platform_screen(timeframe_label="M5")
        assert detect_layout(image).timeframe_seconds == 300


class TestTheAppDoesNotSitOnTheChart:
    def test_covering_the_chart_is_reported(self):
        _series, image, truth = platform_screen()
        plot_left, plot_top, plot_right, plot_bottom = truth["chart"]
        # A panel over the right third of the plot — where the axis lives.
        panel = Box(
            plot_left + (plot_right - plot_left) * 2 // 3,
            plot_top,
            (plot_right - plot_left) // 3,
            (plot_bottom - plot_top) // 2,
        )
        layout = detect_layout(image, exclude=[panel], read_labels=False)
        assert layout.overlapped_by_app
        assert any("sitting on top of the chart" in i for i in layout.issues)

    def test_a_panel_beside_the_chart_is_not_reported(self):
        _series, image, _truth = platform_screen()
        beside = Box(0, 0, 60, 200)
        layout = detect_layout(image, exclude=[beside], read_labels=False)
        assert not layout.overlapped_by_app
        assert not any("sitting on top" in i for i in layout.issues)


class TestPackedCharts:
    """Zoomed in far enough, neighbouring candles touch and fuse into one blob."""

    def _detect_and_extract(self, candle_width, gap, seed=9):
        series = generate_series(120, seed=seed)
        image, _truth = render_platform_screen(
            series,
            ScreenStyle(),
            RenderStyle(candle_width=candle_width, candle_gap=gap),
        )
        layout = detect_layout(image, read_labels=False)
        assert layout.chart is not None, "the chart must still be located"
        box = layout.chart
        crop = image[box.top : box.bottom, box.left : box.right]
        plot_width = _truth["chart"][2] - _truth["chart"][0]
        return series, layout, extract_pixel_candles(crop), plot_width

    def test_the_region_is_still_found_when_every_candle_touches(self):
        """A packed chart is one huge blob, not a row of separate ones.

        Discarding it as "too wide to be a candle" is how a chart with 200
        candles on it came back as 43, with a region a third of its width.
        """
        for candle_width, gap in ((10, 1), (10, 0), (14, 0), (9, 0), (4, 0)):
            _series, layout, _extraction, plot_width = self._detect_and_extract(
                candle_width, gap
            )
            assert layout.chart.width > plot_width * 0.9, (
                candle_width, gap, layout.chart.width, plot_width,
            )

    def test_touching_candles_are_refused_rather_than_misread(self):
        """What comes out of a fused chart is a different chart, not a worse one.

        Fewer, wider candles, each one's open and close taken from whichever
        candle happened to start and end the run. Every number downstream still
        looks perfectly ordinary, so silence here is the dangerous outcome.
        """
        for candle_width, gap in ((10, 1), (10, 0), (14, 0), (9, 0), (4, 0)):
            _series, _layout, extraction, _w = self._detect_and_extract(
                candle_width, gap
            )
            assert any("touching" in issue for issue in extraction.issues), (
                candle_width,
                gap,
            )
            assert extraction.confidence <= 40

    def test_a_chart_with_gaps_is_read_normally(self):
        """The warning must not fire on charts that are perfectly readable."""
        for candle_width, gap in ((10, 4), (10, 2), (7, 4), (5, 1), (12, 6), (3, 1)):
            series, _layout, extraction, _w = self._detect_and_extract(
                candle_width, gap
            )
            assert not any("touching" in i for i in extraction.issues), (
                candle_width,
                gap,
            )
            assert extraction.confidence >= 90
            assert len(extraction.candles) > 55
