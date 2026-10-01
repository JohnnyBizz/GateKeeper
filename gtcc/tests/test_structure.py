"""Market structure.

Specification section 6 warns against pretending subjective concepts
are mathematically certain, and asks that the exact rule be stored. So
these tests check two things of roughly equal importance: that the
detections are right, and that each one carries the rule and the
parameters that produced it.

The series are built by hand with known pivots, so a failure says which
definition drifted rather than merely that something changed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from gtcc.domain.money import D
from gtcc.structure.engine import (
    Confidence,
    StructureEngine,
    StructureKind,
    StructureSettings,
    Trend,
)
from tests.test_features import make_bars


def zigzag(legs: int = 6, base: int = 100, step: int = 8, amplitude: int = 6) -> list[float]:
    """Peaks and troughs separated by enough bars to confirm as pivots.

    Each leg is low, mid, high, mid, low with the base rising, so the
    series has unambiguous higher highs and higher lows.
    """
    out: list[float] = []
    for leg in range(legs):
        low = base + leg * step
        out += [low, low + amplitude / 2, low + amplitude, low + amplitude / 2, low]
    return out


def bars_from(closes, *, pad: float = 1.0):
    return make_bars(
        closes, highs=[c + pad for c in closes], lows=[c - pad for c in closes]
    )


@pytest.fixture
def engine() -> StructureEngine:
    return StructureEngine()


class TestSwingDetection:
    def test_a_pivot_needs_to_beat_both_sides(self, engine):
        report = engine.analyse(bars_from(zigzag()))

        highs = report.of(StructureKind.SWING_HIGH)
        assert highs
        for swing in highs:
            assert swing.parameters["swing_lookback"] == 2

    def test_a_monotonic_series_has_no_swings(self, engine):
        """A straight line has no local extremes. Reporting one would
        be inventing structure that is not there."""
        report = engine.analyse(bars_from(list(range(100, 160))))

        assert report.of(StructureKind.SWING_HIGH) == ()
        assert report.of(StructureKind.SWING_LOW) == ()
        assert report.trend is Trend.UNCLEAR

    def test_a_swing_is_confirmed_late_not_at_the_time(self, engine):
        """A pivot cannot be known until the bars after it have
        printed. Reporting it earlier would use data from the future."""
        closes = zigzag(legs=3)
        full = engine.analyse(bars_from(closes))
        last_swing = max(d.index for d in full.of(StructureKind.SWING_HIGH))

        # Truncating right after the pivot removes the confirmation.
        truncated = engine.analyse(bars_from(closes[: last_swing + 1]))

        assert last_swing not in [d.index for d in truncated.of(StructureKind.SWING_HIGH)]

    def test_the_lookback_changes_what_counts(self, engine):
        """The reading is a function of a parameter somebody chose."""
        closes = zigzag()

        tight = StructureEngine(StructureSettings(swing_lookback=1)).analyse(bars_from(closes))
        loose = StructureEngine(StructureSettings(swing_lookback=4)).analyse(bars_from(closes))

        assert len(tight.of(StructureKind.SWING_HIGH)) >= len(
            loose.of(StructureKind.SWING_HIGH)
        )

    def test_too_little_history_says_so(self, engine):
        report = engine.analyse(bars_from([100, 101]))

        assert report.insufficient_history
        assert report.trend is Trend.UNCLEAR
        assert report.detections == ()


class TestSequenceAndTrend:
    def test_a_rising_market_reads_as_higher_highs_and_higher_lows(self, engine):
        report = engine.analyse(bars_from(zigzag()))

        assert report.of(StructureKind.HIGHER_HIGH)
        assert report.of(StructureKind.HIGHER_LOW)
        assert report.of(StructureKind.LOWER_LOW) == ()
        assert report.trend is Trend.UP

    def test_a_falling_market_reads_as_lower_highs_and_lower_lows(self, engine):
        report = engine.analyse(bars_from(list(reversed(zigzag()))))

        assert report.of(StructureKind.LOWER_HIGH)
        assert report.of(StructureKind.LOWER_LOW)
        assert report.trend is Trend.DOWN

    def test_disagreement_between_highs_and_lows_is_ranging(self, engine):
        """Higher highs with lower lows is a broadening pattern, which
        is neither an uptrend nor a downtrend."""
        closes = []
        for leg in range(5):
            closes += [100 - leg * 5, 103, 106 + leg * 5, 103, 100 - leg * 5]

        report = engine.analyse(bars_from(closes))

        assert report.trend is Trend.RANGING

    def test_the_trend_carries_the_rule_that_produced_it(self, engine):
        report = engine.analyse(bars_from(zigzag()))

        assert "swing highs and lows" in report.trend_rule
        assert str(report.trend) in (t.value for t in Trend)


class TestStructureBreaks:
    def test_a_break_requires_a_close_beyond_not_a_wick(self, engine):
        """Conflating a wick with a close is the most common way this
        reading goes wrong, so it has its own test."""
        # Rises, pivots, then a bar whose HIGH exceeds the swing high
        # but whose CLOSE does not.
        closes = [100, 103, 106, 103, 100, 101, 102]
        bars = make_bars(
            closes,
            highs=[c + 1 for c in closes[:-1]] + [110],
            lows=[c - 1 for c in closes],
        )

        report = engine.analyse(bars)

        assert report.of(StructureKind.BREAK_OF_STRUCTURE) == ()
        assert report.of(StructureKind.CHANGE_OF_CHARACTER) == ()

    def test_a_close_beyond_the_last_swing_high_breaks_structure(self, engine):
        report = engine.analyse(bars_from(zigzag()))

        breaks = report.of(StructureKind.BREAK_OF_STRUCTURE)
        assert breaks
        for detection in breaks:
            assert "close above" in detection.rule or "close below" in detection.rule
            assert "level" in detection.parameters

    def test_the_first_break_against_the_trend_is_a_change_of_character(self, engine):
        """Up for several legs, then a decisive break of the last low."""
        closes = zigzag(legs=4) + [95, 90, 85, 80, 75]

        report = engine.analyse(bars_from(closes))

        changes = report.of(StructureKind.CHANGE_OF_CHARACTER)
        assert changes
        assert changes[0].parameters["prevailing_trend"] == "UP"

    def test_breaks_are_marked_as_interpreted(self, engine):
        """They depend on which swing counts as the last one, which
        depends on the lookback."""
        report = engine.analyse(bars_from(zigzag()))

        for detection in report.of(
            StructureKind.BREAK_OF_STRUCTURE, StructureKind.CHANGE_OF_CHARACTER
        ):
            assert detection.confidence is Confidence.INTERPRETED


class TestFairValueGaps:
    def test_a_three_bar_gap_up_is_detected(self, engine):
        """Bar three's low above bar one's high. Fully objective: the
        ranges either overlap or they do not."""
        closes = [100, 105, 120, 121, 122]
        bars = make_bars(
            closes,
            highs=[101, 106, 121, 122, 123],
            lows=[99, 104, 119, 120, 121],
        )

        report = engine.analyse(bars)

        gaps = report.of(StructureKind.FAIR_VALUE_GAP)
        assert gaps
        assert gaps[0].parameters["direction"] == "up"
        assert gaps[0].confidence is Confidence.OBJECTIVE

    def test_overlapping_bars_are_not_a_gap(self, engine):
        closes = [100, 101, 102, 103, 104]
        bars = make_bars(
            closes, highs=[c + 5 for c in closes], lows=[c - 5 for c in closes]
        )

        assert engine.analyse(bars).of(StructureKind.FAIR_VALUE_GAP) == ()

    def test_a_gap_below_the_minimum_fraction_is_noise(self):
        closes = [100, 100.01, 100.02, 100.03, 100.04]
        bars = make_bars(
            closes,
            highs=[100.001, 100.011, 100.021, 100.031, 100.041],
            lows=[99.999, 100.009, 100.019, 100.029, 100.039],
        )
        strict = StructureEngine(StructureSettings(min_gap_fraction=D("0.01")))

        assert strict.analyse(bars).of(StructureKind.FAIR_VALUE_GAP) == ()

    def test_the_observed_size_is_recorded(self, engine):
        closes = [100, 105, 120, 121, 122]
        bars = make_bars(
            closes, highs=[101, 106, 121, 122, 123], lows=[99, 104, 119, 120, 121]
        )

        gap = engine.analyse(bars).of(StructureKind.FAIR_VALUE_GAP)[0]

        assert Decimal(gap.parameters["observed_fraction"]) > 0
        assert "min_gap_fraction" in gap.parameters


class TestEqualLevelsAndSweeps:
    def test_two_swings_at_the_same_price_are_equal_highs(self, engine):
        closes = [100, 103, 106, 103, 100, 103, 106, 103, 100]
        bars = bars_from(closes)

        report = engine.analyse(bars)

        equal = report.of(StructureKind.EQUAL_HIGHS)
        assert equal
        assert "tolerance" in equal[0].parameters

    def test_a_sweep_needs_a_wick_through_and_a_close_back_inside(self, engine):
        """A bar that pokes above a swing high and closes below it,
        then stays below."""
        closes = [100, 103, 106, 103, 100, 104, 101, 100, 99, 98]
        highs = [101, 104, 107, 104, 101, 110, 102, 101, 100, 99]
        lows = [99, 102, 105, 102, 99, 103, 100, 99, 98, 97]
        bars = make_bars(closes, highs=highs, lows=lows)

        report = engine.analyse(bars)

        sweeps = report.of(StructureKind.LIQUIDITY_SWEEP)
        assert sweeps
        assert sweeps[0].parameters["reclaim_bars"] == 3
        assert sweeps[0].confidence is Confidence.INTERPRETED

    def test_a_clean_breakout_is_not_a_sweep(self, engine):
        """Without the hold requirement every breakout bar would read
        as a sweep for one bar before continuing."""
        closes = zigzag(legs=3) + [130, 135, 140, 145, 150]
        bars = bars_from(closes)

        report = engine.analyse(bars)

        for sweep in report.of(StructureKind.LIQUIDITY_SWEEP):
            # Any sweep found must be a genuine reversal, not the run-up.
            assert sweep.index < len(closes) - 5

    def test_repeated_touches_become_support_or_resistance(self, engine):
        closes = [100, 103, 106, 103, 100, 103, 106, 103, 100, 103, 106, 103, 100]
        bars = bars_from(closes)

        report = engine.analyse(bars)

        levels = report.of(StructureKind.SUPPORT, StructureKind.RESISTANCE)
        assert levels
        assert levels[0].parameters["touches"] >= 2
        assert "touch_indices" in levels[0].parameters


class TestRangeAndBreakout:
    def test_a_range_is_reported_with_its_bounds(self, engine):
        # Long enough for two swing lows to confirm: the first and last
        # bars can never be pivots, so a short series has fewer swings
        # than it looks like it should.
        closes = [100, 103, 106, 103, 100, 103, 106, 103, 100, 103, 106, 103, 100, 102]

        report = engine.analyse(bars_from(closes))

        found = report.latest(StructureKind.RANGE)
        assert found is not None
        assert found.price < found.price_to
        assert "top" in found.parameters and "bottom" in found.parameters

    def test_a_close_beyond_the_range_is_a_breakout(self, engine):
        closes = [
            100, 103, 106, 103, 100, 103, 106, 103, 100, 103, 106, 103, 100,
            115, 120, 125, 130,
        ]

        report = engine.analyse(bars_from(closes))

        assert report.of(StructureKind.BREAKOUT) or report.of(StructureKind.FAILED_BREAKOUT)

    def test_a_breakout_that_closes_back_inside_is_marked_failed(self, engine):
        closes = [
            100, 103, 106, 103, 100, 103, 106, 103, 100, 103, 106, 103, 100,
            115, 101, 100, 99,
        ]

        report = engine.analyse(bars_from(closes))

        failed = report.of(StructureKind.FAILED_BREAKOUT)
        assert failed
        assert failed[0].parameters["bars_checked_for_failure"] == 3


class TestEveryDetectionCarriesItsRule:
    """Section 6's actual requirement, tested directly."""

    def test_no_detection_is_rule_free(self, engine):
        closes = zigzag() + [95, 90, 85] + zigzag(legs=2, base=90)

        report = engine.analyse(bars_from(closes))

        assert len(report.detections) > 20
        for detection in report.detections:
            assert detection.rule, f"{detection.kind} has no rule"
            assert len(detection.rule) > 15, f"{detection.kind} rule is not a rule"
            assert detection.describe()

    def test_interpreted_detections_record_their_thresholds(self, engine):
        report = engine.analyse(bars_from(zigzag()))

        for detection in report.detections:
            if detection.confidence is Confidence.INTERPRETED:
                assert detection.parameters, (
                    f"{detection.kind} is a judgement call but records no parameters"
                )

    def test_the_settings_can_be_recorded_alongside_a_trade(self, engine):
        """The journal stores these, so they must serialise."""
        import json

        settings = StructureSettings()

        as_json = json.dumps(settings.as_dict())

        assert "swing_lookback" in as_json
        assert json.loads(as_json)["swing_lookback"] == 2

    def test_the_same_series_reads_the_same_way_twice(self, engine):
        """Deterministic, like the risk engine. A structure read that
        varies run to run cannot be audited."""
        bars = bars_from(zigzag())

        first = engine.analyse(bars)
        second = engine.analyse(bars)

        assert [d.describe() for d in first.detections] == [
            d.describe() for d in second.detections
        ]
        assert first.trend is second.trend


class TestNothingUsesBarsThatHadNotPrinted:
    """Look-ahead is the bias that invents an edge and then loses it in
    production, so the causal walks get their own tests.

    A pivot needs `swing_lookback` bars after it before anyone could
    know it was a pivot. An earlier version registered swings at their
    own index, which meant a breakout bar's own swing raised the
    ceiling it was breaking, and a break of structure could reference a
    level that had not been established yet. Both produced output that
    looked entirely reasonable.
    """

    def test_a_detection_never_depends_on_a_later_bar(self, engine):
        """Analysing a prefix must agree with analysing the whole series
        about everything inside that prefix.

        This is the general property. If any detector reaches forward,
        the longer run will disagree with the shorter one somewhere.
        """
        closes = zigzag(legs=5) + [95, 90, 85, 88, 92, 96, 100]
        bars = bars_from(closes)
        look = engine.settings.swing_lookback

        full = engine.analyse(bars)

        for cut in range(len(bars) - 12, len(bars)):
            prefix = engine.analyse(bars[:cut])
            # Detections are comparable only where the shorter run had
            # enough room to confirm them, and the range and breakout
            # readings are explicitly "as of the last bar".
            horizon = cut - look - 1
            windowed = {
                (d.index, str(d.kind), str(d.price))
                for d in prefix.detections
                if d.index <= horizon
                and d.kind
                not in (StructureKind.RANGE, StructureKind.BREAKOUT,
                        StructureKind.FAILED_BREAKOUT, StructureKind.SUPPORT,
                        StructureKind.RESISTANCE)
            }
            from_full = {
                (d.index, str(d.kind), str(d.price))
                for d in full.detections
                if d.index <= horizon
                and d.kind
                not in (StructureKind.RANGE, StructureKind.BREAKOUT,
                        StructureKind.FAILED_BREAKOUT, StructureKind.SUPPORT,
                        StructureKind.RESISTANCE)
            }
            assert windowed == from_full, f"prefix of {cut} bars disagrees with the full series"

    def test_a_breakout_is_not_cancelled_by_its_own_swing(self, engine):
        """The specific bug. The breakout bar becomes a swing high two
        bars later; using it to set the range made the break vanish."""
        closes = [
            100, 103, 106, 103, 100, 103, 106, 103, 100, 103, 106, 103, 100,
            115, 101, 100, 99,
        ]

        report = engine.analyse(bars_from(closes))

        breaks = report.of(StructureKind.BREAKOUT, StructureKind.FAILED_BREAKOUT)
        assert breaks, "the break at the 115 close was swallowed by its own swing"
        assert breaks[0].index == 13

    def test_a_structure_break_references_an_already_confirmed_level(self, engine):
        """Every break must name a swing that was knowable before it."""
        closes = zigzag(legs=5) + [95, 90, 85, 80]
        look = engine.settings.swing_lookback

        report = engine.analyse(bars_from(closes))

        for detection in report.of(
            StructureKind.BREAK_OF_STRUCTURE, StructureKind.CHANGE_OF_CHARACTER
        ):
            level_index = detection.parameters["level_index"]
            assert level_index + look <= detection.index, (
                f"break at {detection.index} used a swing from {level_index} "
                f"that was not confirmed until {level_index + look}"
            )
