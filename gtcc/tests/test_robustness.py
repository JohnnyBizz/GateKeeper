"""These checks exist to break a result that looks good.

The strategies that lose money live are overwhelmingly the ones whose
backtest nobody attacked. Each test here pins one specific way a backtest
lies, and the last class pins the thing that matters most about the
output: a clean report must not read as an endorsement.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gtcc.backtest.engine import (
    BacktestResult,
    BarCosts,
    ClosedTrade,
    ExitReason,
)
from gtcc.backtest.robustness import (
    Flag,
    assess,
    cost_stress,
    depends_on_one_trade,
    parameter_sensitivity,
    profit_in_one_period,
    rising_costs,
)
from gtcc.domain.enums import Side, Timeframe
from gtcc.domain.money import D

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _trade(pnl: str, *, day: int = 0, quantity: str = "10") -> ClosedTrade:
    at = START + timedelta(days=day)
    return ClosedTrade(
        symbol="TEST", strategy="s", side=Side.BUY, quantity=D(quantity),
        entry_index=day, entry_at=at, entry_price=D("100"),
        exit_index=day + 1, exit_at=at + timedelta(hours=1), exit_price=D("101"),
        reason=ExitReason.TARGET, planned_stop=D("99"), planned_target=D("103"),
        costs=D("1"), pnl=D(pnl), r_multiple=D(pnl) / D("10"),
    )


def _result(*pnls: str, equity: str = "100000") -> BacktestResult:
    trades = tuple(_trade(pnl, day=index) for index, pnl in enumerate(pnls))
    running = D(equity)
    curve = [running]
    for trade in trades:
        running += trade.pnl
        curve.append(running)
    return BacktestResult(
        symbol="TEST", timeframe=Timeframe.M15, strategy="s",
        bars_seen=1000, bars_traded=900, first_bar_at=START, last_bar_at=START,
        starting_equity=D(equity), ending_equity=running,
        costs=BarCosts(), trades=trades, equity_curve=tuple(curve),
    )


class TestOneLuckyTrade:
    def test_an_edge_that_is_one_outlier_is_flagged(self):
        """Thirty small losses and one enormous win is not an edge."""
        result = _result(*(["-10"] * 30 + ["400"]))

        finding = depends_on_one_trade(result)

        assert finding is not None
        assert finding.flag is Flag.DEPENDS_ON_ONE_TRADE
        assert "best trade" in finding.detail
        assert finding.disqualifying

    def test_a_broad_edge_is_not_flagged(self):
        result = _result(*(["20"] * 20 + ["-10"] * 10))

        assert depends_on_one_trade(result) is None

    def test_an_already_losing_strategy_is_not_flagged_for_this(self):
        """It has a different problem, and naming this one would mislead."""
        result = _result(*(["-10"] * 20 + ["5"] * 5))

        assert depends_on_one_trade(result) is None

    def test_a_single_trade_cannot_be_assessed_this_way(self):
        assert depends_on_one_trade(_result("100")) is None


class TestOneLuckyPeriod:
    def test_profit_confined_to_one_half_is_flagged(self):
        result = _result(*(["50"] * 10 + ["-5"] * 10))

        finding = profit_in_one_period(result)

        assert finding is not None
        assert finding.flag is Flag.PROFIT_IN_ONE_PERIOD
        assert "not present across the whole sample" in finding.detail

    def test_a_consistent_edge_is_not_flagged(self):
        result = _result(*(["10", "-4"] * 10))

        assert profit_in_one_period(result) is None

    def test_the_later_half_carrying_it_is_flagged_too(self):
        """A regime that only recently started is still not a proven edge."""
        result = _result(*(["-5"] * 10 + ["50"] * 10))

        finding = profit_in_one_period(result)

        assert finding is not None
        assert "first" in finding.detail and "second" in finding.detail


class TestCostStress:
    def test_an_edge_that_dies_within_twice_the_assumed_cost_is_flagged(self):
        base = BarCosts(spread_bps=D("1"), slippage_bps=D("1"), commission_bps=D("1"))

        def runner(costs: BarCosts) -> BacktestResult:
            # 2.5 bps per side at the base assumption; this turns negative at
            # 3.75, which is 1.5x it — inside the fragility threshold.
            per_side = costs.per_side_bps()
            pnl = D("1000") - per_side * D("300")
            return _result(str(pnl))

        curve, breaks_at, headroom, finding = cost_stress(runner, rising_costs(base))

        assert len(curve) == 5
        assert breaks_at == D("3.75")
        assert headroom == D("1.5")
        assert finding is not None
        assert finding.flag is Flag.FRAGILE_TO_COSTS
        assert "1.5x the assumed cost" in finding.detail

    def test_a_robust_edge_survives_the_sweep(self):
        base = BarCosts(spread_bps=D("1"), slippage_bps=D("1"), commission_bps=D("1"))

        def runner(costs: BarCosts) -> BacktestResult:
            return _result("5000")

        curve, breaks_at, headroom, finding = cost_stress(runner, rising_costs(base))

        assert breaks_at is None
        assert headroom is None
        assert finding is None
        assert all(level.net_pnl > 0 for level in curve)

    def test_the_curve_records_every_level_tested(self):
        base = BarCosts()

        def runner(costs: BarCosts) -> BacktestResult:
            return _result("100")

        curve, _, _, _ = cost_stress(runner, rising_costs(base, multiples=("1", "2")))

        assert [level.costs.per_side_bps() for level in curve] == [
            base.per_side_bps(), base.per_side_bps() * D("2")
        ]


class TestParameterSensitivity:
    def test_an_edge_that_only_exists_at_one_value_is_flagged(self):
        def runner(name: str, value: object) -> BacktestResult:
            return _result("500" if value == 14 else "-200")

        findings = parameter_sensitivity(
            runner, {"period": [12, 13, 15, 16]}, baseline_pnl=D("500")
        )

        assert len(findings) == 1
        assert findings[0].flag is Flag.FRAGILE_TO_PARAMETERS
        assert "does not know which number was chosen" in findings[0].detail

    def test_a_broad_plateau_is_not_flagged(self):
        def runner(name: str, value: object) -> BacktestResult:
            return _result("400")

        findings = parameter_sensitivity(
            runner, {"period": [12, 13, 15, 16]}, baseline_pnl=D("500")
        )

        assert findings == []

    def test_a_single_bad_neighbour_is_tolerated(self):
        """One unprofitable neighbour out of four is not a curve fit."""
        def runner(name: str, value: object) -> BacktestResult:
            return _result("-50" if value == 16 else "400")

        findings = parameter_sensitivity(
            runner, {"period": [12, 13, 15, 16]}, baseline_pnl=D("500")
        )

        assert findings == []


class TestTheCombinedAssessment:
    def test_a_small_sample_is_flagged_before_anything_else(self):
        report = assess(_result("100", "100"))

        assert Flag.TOO_FEW_TRADES in {finding.flag for finding in report.findings}
        assert report.disqualified

    def test_an_unprofitable_result_says_so_plainly(self):
        report = assess(_result(*(["-10"] * 40)))

        assert Flag.NOT_PROFITABLE in {finding.flag for finding in report.findings}

    def test_every_check_run_is_named(self):
        report = assess(_result(*(["10", "-4"] * 20)))

        assert "sample size" in report.checks_run
        assert "single-trade dependence" in report.checks_run
        assert "period concentration" in report.checks_run
        # Not run, because no runner was supplied.
        assert "cost stress" not in report.checks_run
        assert "parameter sensitivity" not in report.checks_run

    def test_a_skipped_check_is_visible_in_the_output(self):
        """A report that skipped the cost sweep did not pass it."""
        report = assess(_result(*(["10", "-4"] * 20)))

        text = "\n".join(report.describe())
        assert "Checks run:" in text
        assert "cost stress" not in text

    def test_surviving_the_sweep_is_reported_as_the_range_tested(self):
        """Not as proof that higher costs would also survive."""
        report = assess(
            _result(*(["10", "-4"] * 20)),
            cost_runner=lambda costs: _result(*(["10", "-4"] * 20)),
            cost_levels=rising_costs(BarCosts()),
        )

        text = "\n".join(report.describe())
        assert "That is the range tested, not a claim about higher costs." in text

    def test_cost_and_parameter_checks_run_when_given_runners(self):
        base = BarCosts()
        report = assess(
            _result(*(["10", "-4"] * 20)),
            cost_runner=lambda costs: _result(*(["10", "-4"] * 20)),
            cost_levels=rising_costs(base),
            parameter_runner=lambda name, value: _result(*(["10", "-4"] * 20)),
            sweeps={"period": [13, 15]},
        )

        assert "cost stress" in report.checks_run
        assert "parameter sensitivity" in report.checks_run


class TestACleanReportIsNotAnEndorsement:
    """The most important property in the module.

    "Passed robustness checks" would be read as "this works". It does not
    mean that and must not say it.
    """

    def test_a_clean_report_denies_being_evidence(self):
        report = assess(_result(*(["10", "-4"] * 20)))

        text = "\n".join(report.describe())
        assert "NOT evidence" in text
        assert "were not detected" in text
        assert "validated" not in text.lower()

    def test_no_checks_run_is_not_a_clean_result(self):
        from gtcc.backtest.robustness import RobustnessReport

        text = "\n".join(RobustnessReport().describe())

        assert "not a clean result" in text

    def test_a_surviving_edge_is_never_called_robust(self):
        report = assess(
            _result(*(["10", "-4"] * 20)),
            cost_runner=lambda costs: _result(*(["10", "-4"] * 20)),
            cost_levels=rising_costs(BarCosts()),
        )

        text = "\n".join(report.describe()).lower()
        assert not report.disqualified
        for word in ("robust", "proven", "profitable strategy", "endorse"):
            assert word not in text


class TestOutOfSampleDegradation:
    """The clearest overfitting signal, and the one most worth getting right."""

    def test_losing_out_of_sample_is_disqualifying(self):
        from gtcc.backtest.robustness import out_of_sample_degradation

        inside = _result(*(["20"] * 20 + ["-5"] * 10))
        outside = _result(*(["-8"] * 15))

        finding = out_of_sample_degradation(inside, outside)

        assert finding is not None
        assert finding.flag is Flag.FAILS_OUT_OF_SAMPLE
        assert finding.disqualifying
        assert "description of the data" in finding.detail

    def test_taking_no_trades_out_of_sample_is_also_a_failure(self):
        """An untested edge is not a surviving edge."""
        from gtcc.backtest.robustness import out_of_sample_degradation

        inside = _result(*(["20"] * 30))
        outside = _result()

        finding = out_of_sample_degradation(inside, outside)

        assert finding is not None
        assert "no trades at all out-of-sample" in finding.detail
        assert finding.disqualifying

    def test_a_mildly_weaker_edge_is_not_flagged(self):
        """Some decay is normal and flagging it would cry wolf."""
        from gtcc.backtest.robustness import out_of_sample_degradation

        inside = _result(*(["20"] * 20))
        outside = _result(*(["14"] * 20))

        assert out_of_sample_degradation(inside, outside) is None

    def test_most_of_the_edge_vanishing_is_reported_but_not_fatal(self):
        from gtcc.backtest.robustness import out_of_sample_degradation

        inside = _result(*(["20"] * 20))
        outside = _result(*(["2"] * 20))

        finding = out_of_sample_degradation(inside, outside)

        assert finding is not None
        assert not finding.disqualifying, "a weakened edge may still be real"
        assert "did not survive" in finding.detail

    def test_nothing_claimed_in_sample_means_nothing_to_fail(self):
        from gtcc.backtest.robustness import out_of_sample_degradation

        inside = _result(*(["-10"] * 20))
        outside = _result(*(["-10"] * 20))

        assert out_of_sample_degradation(inside, outside) is None

    def test_the_check_is_named_only_when_held_out_data_was_supplied(self):
        inside = _result(*(["10", "-4"] * 20))

        without = assess(inside)
        with_oos = assess(inside, out_of_sample=_result(*(["8", "-4"] * 10)))

        assert "out-of-sample" not in without.checks_run
        assert "out-of-sample" in with_oos.checks_run
