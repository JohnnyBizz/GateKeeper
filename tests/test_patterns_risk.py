"""Candlestick pattern naming, stake sizing and session statistics."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import START, build_series, pullback_trend
from poa.analysis import NO_PATTERN, detect_patterns, primary_pattern
from poa.models import Bias, Candle, Series
from poa.risk import (
    SessionStats,
    assess_risk,
    breakeven_win_rate,
    expected_value,
)


def series_from(spec, *, quiet_base: bool = True) -> Series:
    """Build a series from (open, high, low, close) tuples on a calm base.

    The base sets ATR, and every detector is ATR-scaled, so the base has to be
    quiet enough that the pattern candles read as significant.
    """
    base = [(1.0, 1.0025, 0.9975, 1.001)] * 15 if quiet_base else []
    rows = base + list(spec)
    return Series(
        [
            Candle(START + timedelta(minutes=i), o, h, l, c)
            for i, (o, h, l, c) in enumerate(rows)
        ],
        60,
        "TEST",
    )


def names(series: Series) -> set[str]:
    return {p.name for p in detect_patterns(series)}


class TestPatternDetection:
    def test_hammer_is_named_and_bullish(self):
        s = series_from([(1.000, 1.0012, 0.9930, 1.0005)])
        pattern = primary_pattern(s)
        assert pattern.name == "Hammer"
        assert pattern.bias is Bias.BULLISH

    def test_shooting_star_is_named_and_bearish(self):
        s = series_from([(1.000, 1.0070, 0.9988, 0.9995)])
        pattern = primary_pattern(s)
        assert pattern.name == "Shooting Star"
        assert pattern.bias is Bias.BEARISH

    def test_bullish_engulfing(self):
        s = series_from([(1.0040, 1.0045, 0.9985, 0.9990), (0.9980, 1.0070, 0.9975, 1.0060)])
        assert "Bullish Engulfing" in names(s)

    def test_bearish_engulfing(self):
        s = series_from([(0.9990, 1.0055, 0.9985, 1.0050), (1.0060, 1.0065, 0.9970, 0.9975)])
        assert "Bearish Engulfing" in names(s)

    def test_engulfing_requires_the_opposite_colour(self):
        # Two bullish candles, the second bigger — that is not engulfing.
        s = series_from([(0.9990, 1.0010, 0.9985, 1.0000), (0.9980, 1.0070, 0.9975, 1.0060)])
        assert "Bullish Engulfing" not in names(s)

    def test_doji_is_neutral(self):
        s = series_from([(1.0000, 1.0030, 0.9970, 1.00002)])
        patterns = [p for p in detect_patterns(s) if p.name == "Doji"]
        assert patterns
        assert patterns[0].bias is Bias.NEUTRAL

    def test_inside_bar_is_detected_and_neutral(self):
        s = series_from([(0.9980, 1.0080, 0.9920, 1.0060), (1.0000, 1.0020, 0.9990, 1.0015)])
        patterns = [p for p in detect_patterns(s) if p.name == "Inside Bar"]
        assert patterns
        assert patterns[0].bias is Bias.NEUTRAL

    def test_marubozu_needs_a_full_body(self):
        s = series_from([(0.9980, 1.00610, 0.99795, 1.0060)])
        assert "Bullish Marubozu" in names(s)

    def test_a_wicky_candle_is_not_a_marubozu(self):
        s = series_from([(0.9980, 1.0090, 0.9930, 1.0060)])
        assert "Bullish Marubozu" not in names(s)

    def test_morning_star(self):
        s = series_from([
            (1.0060, 1.0065, 0.9980, 0.9985),
            (0.9980, 0.9995, 0.9965, 0.9980),
            (0.9985, 1.0055, 0.9980, 1.0050),
        ])
        assert "Morning Star" in names(s)

    def test_evening_star(self):
        s = series_from([
            (0.9985, 1.0065, 0.9980, 1.0060),
            (1.0060, 1.0075, 1.0045, 1.0060),
            (1.0055, 1.0060, 0.9985, 0.9990),
        ])
        assert "Evening Star" in names(s)

    def test_three_white_soldiers(self):
        s = series_from([
            (0.9980, 1.0025, 0.9978, 1.0020),
            (1.0020, 1.0065, 1.0018, 1.0060),
            (1.0060, 1.0105, 1.0058, 1.0100),
        ])
        assert "Three White Soldiers" in names(s)

    def test_three_black_crows(self):
        s = series_from([
            (1.0100, 1.0102, 1.0058, 1.0060),
            (1.0060, 1.0062, 1.0018, 1.0020),
            (1.0020, 1.0022, 0.9978, 0.9980),
        ])
        assert "Three Black Crows" in names(s)

    def test_tweezer_bottom(self):
        s = series_from([(1.0020, 1.0025, 0.9960, 0.9985), (0.9985, 1.0035, 0.99605, 1.0030)])
        assert "Tweezer Bottom" in names(s)

    def test_a_quiet_candle_produces_no_directional_pattern(self):
        s = series_from([(1.0010, 1.0015, 1.0005, 1.0012)])
        for pattern in detect_patterns(s):
            assert pattern.bias is Bias.NEUTRAL

    def test_patterns_are_ordered_strongest_first(self):
        s = series_from([(1.0040, 1.0045, 0.9985, 0.9990), (0.9980, 1.0070, 0.9975, 1.0060)])
        found = detect_patterns(s)
        assert found == sorted(found, key=lambda p: -p.strength)

    def test_a_short_series_yields_nothing(self):
        assert detect_patterns(series_from([], quiet_base=False)) == []

    def test_primary_falls_back_to_no_pattern(self):
        short = Series(
            [Candle(START, 1.0, 1.0, 1.0, 1.0)], 60, "T"
        )
        assert primary_pattern(short) is NO_PATTERN

    def test_strength_is_bounded(self):
        for spec in (
            [(1.000, 1.0012, 0.9930, 1.0005)],
            [(0.9980, 1.00610, 0.99795, 1.0060)],
        ):
            for pattern in detect_patterns(series_from(spec)):
                assert 0.0 <= pattern.strength <= 1.0

    def test_patterns_are_immutable(self):
        pattern = primary_pattern(series_from([(1.000, 1.0012, 0.9930, 1.0005)]))
        with pytest.raises(Exception):
            pattern.name = "changed"  # type: ignore[misc]

    def test_a_named_pattern_reaches_the_analysis(self):
        from poa.analysis import analyze_timeframe

        analysis = analyze_timeframe(pullback_trend(300, direction=1))
        assert analysis.pattern is not None
        assert "pattern" in analysis.to_dict()


class TestBreakeven:
    def test_a_ninety_two_percent_payout_needs_more_than_half(self):
        # The number that matters most in the whole tool.
        assert breakeven_win_rate(0.92) == pytest.approx(52.1, abs=0.1)

    def test_a_lower_payout_demands_a_higher_win_rate(self):
        assert breakeven_win_rate(0.70) > breakeven_win_rate(0.92)

    def test_an_even_money_payout_breaks_even_at_half(self):
        assert breakeven_win_rate(1.0) == pytest.approx(50.0)

    def test_expected_value_is_zero_at_breakeven(self):
        payout = 0.92
        assert expected_value(breakeven_win_rate(payout), payout) == pytest.approx(
            0.0, abs=0.002
        )

    def test_a_coin_flip_loses_money_at_a_typical_payout(self):
        assert expected_value(50.0, 0.92) < 0

    def test_fifty_four_percent_still_loses_at_eighty(self):
        # The specific case worth naming: a win rate that looks like an edge
        # and is not one. At an 80% payout, break-even is 55.6%, so 54% loses.
        assert expected_value(54.0, 0.80) < 0
        # At 92% the same rate is barely positive — the payout is the variable.
        assert expected_value(54.0, 0.92) > 0


class TestRiskAssessment:
    def test_stake_is_the_configured_share_of_balance(self):
        risk = assess_risk(1000.0, 2.0, 0.92)
        assert risk.stake == pytest.approx(20.0)

    def test_profit_and_loss_are_asymmetric(self):
        risk = assess_risk(1000.0, 2.0, 0.92)
        assert risk.potential_profit == pytest.approx(18.4)
        assert risk.potential_loss == pytest.approx(20.0)
        assert risk.potential_profit < risk.potential_loss

    def test_trades_to_ruin_is_reported(self):
        assert assess_risk(1000.0, 2.0, 0.92).trades_to_ruin == 50

    def test_a_high_risk_percent_is_warned_about(self):
        risk = assess_risk(1000.0, 25.0, 0.92)
        assert any("high" in w.lower() for w in risk.warnings)

    def test_a_session_below_breakeven_is_warned_about(self):
        risk = assess_risk(1000.0, 2.0, 0.92, observed_win_rate=45.0)
        assert any("break even" in w.lower() for w in risk.warnings)

    def test_a_session_above_breakeven_is_not_warned_about(self):
        risk = assess_risk(1000.0, 2.0, 0.92, observed_win_rate=65.0)
        assert not any("break even" in w.lower() for w in risk.warnings)

    def test_a_zero_balance_does_not_divide_by_zero(self):
        risk = assess_risk(0.0, 2.0, 0.92)
        assert risk.stake == 0.0
        assert risk.trades_to_ruin == 0

    def test_the_payload_serialises(self):
        import json

        json.dumps(assess_risk(1000.0, 2.0, 0.92).to_dict())


class TestSessionStats:
    def test_an_empty_session_has_no_win_rate(self):
        assert SessionStats().win_rate is None

    def test_win_rate_is_computed_from_both_sources(self):
        stats = SessionStats(auto_wins=6, auto_losses=4)
        assert stats.win_rate == pytest.approx(60.0)

    def test_manual_adjustments_apply(self):
        stats = SessionStats(auto_wins=5, auto_losses=5)
        stats.adjust(wins=2)
        assert stats.wins == 7
        assert stats.win_rate == pytest.approx(58.3, abs=0.1)

    def test_counts_never_go_negative(self):
        stats = SessionStats(auto_wins=2, auto_losses=1)
        for _ in range(10):
            stats.adjust(wins=-1, losses=-1)
        assert stats.wins >= 0
        assert stats.losses >= 0

    def test_a_journal_refresh_preserves_manual_adjustments(self):
        stats = SessionStats(auto_wins=5, auto_losses=5)
        stats.adjust(wins=3)  # a trade the assistant never signalled
        stats.set_auto(8, 5)  # the journal catches up
        assert stats.wins == 11
        assert stats.manual_wins == 3

    def test_edge_over_breakeven_is_signed(self):
        assert SessionStats(auto_wins=6, auto_losses=4).edge_over_breakeven(0.92) > 0
        assert SessionStats(auto_wins=4, auto_losses=6).edge_over_breakeven(0.92) < 0

    def test_a_small_sample_is_flagged_as_not_meaningful(self):
        assert not SessionStats(auto_wins=3, auto_losses=1).to_dict()["meaningful"]
        assert SessionStats(auto_wins=15, auto_losses=10).to_dict()["meaningful"]

    def test_reset_clears_both_sources(self):
        stats = SessionStats(auto_wins=5, auto_losses=5)
        stats.adjust(wins=2)
        stats.reset()
        assert stats.total == 0
        assert stats.win_rate is None
