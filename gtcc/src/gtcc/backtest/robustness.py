"""Try to break a result before believing it — specification section 23.

A backtest produces a number. The number's job is to feel like evidence,
and it does that whether or not it is. Everything in this module exists to
attack a result that looks good, because the strategies that lose money
live are overwhelmingly the ones whose backtest nobody attacked.

Four attacks, each cheap, each answering a specific way a result lies.

**Costs.** Re-run at rising cost assumptions. An edge that dies at two
basis points was never an edge; it was a transaction-cost artifact that
happened to be measured with the costs turned down.

**One lucky trade.** Remove the single best trade. If expectancy goes
negative, the strategy has no edge — it has one outlier and thirty-three
trades of noise around it.

**One lucky period.** Split the trades in half by time. If all the profit
is in one half, the result describes a market condition that has ended,
not a repeatable edge.

**Parameters.** Nudge each parameter and re-run. An edge that exists at
exactly 14 and vanishes at 13 and 15 is a curve fit; the market does not
know what number you picked.

A note on what a clean report means. Nothing here can establish that a
strategy works. Passing every check means only that these particular ways
of being wrong were not detected, which is a far weaker statement than
"validated" and is worded that way everywhere in the output.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Callable, Mapping, Sequence

from gtcc.backtest.engine import BacktestResult, BarCosts, ClosedTrade
from gtcc.backtest.metrics import MIN_TRADES_FOR_RATIOS, measure
from gtcc.domain.money import D, ZERO

#: An edge that dies below this multiple of the assumed cost is called
#: fragile. A judgement, not a finding: real spreads widen by more than this
#: in the conditions that matter, so an edge with less headroom than this is
#: relying on the cost assumption being generous. The ratio itself is always
#: reported so a reader can disagree with the threshold.
FRAGILE_BELOW_COST_MULTIPLE = D("2")

#: A runner re-executes the same backtest with different costs.
CostRunner = Callable[[BarCosts], BacktestResult]

#: A parameter runner re-executes with one parameter changed.
ParameterRunner = Callable[[str, object], BacktestResult]


class Flag(StrEnum):
    TOO_FEW_TRADES = "TOO_FEW_TRADES"
    FAILS_OUT_OF_SAMPLE = "FAILS_OUT_OF_SAMPLE"
    FRAGILE_TO_COSTS = "FRAGILE_TO_COSTS"
    DEPENDS_ON_ONE_TRADE = "DEPENDS_ON_ONE_TRADE"
    PROFIT_IN_ONE_PERIOD = "PROFIT_IN_ONE_PERIOD"
    FRAGILE_TO_PARAMETERS = "FRAGILE_TO_PARAMETERS"
    NOT_PROFITABLE = "NOT_PROFITABLE"


@dataclass(frozen=True, slots=True)
class Finding:
    flag: Flag
    detail: str
    #: True when this alone is reason enough not to trade the strategy.
    disqualifying: bool = True


@dataclass(frozen=True, slots=True)
class CostLevel:
    costs: BarCosts
    net_pnl: Decimal
    trades: int


@dataclass(frozen=True, slots=True)
class RobustnessReport:
    findings: tuple[Finding, ...] = ()
    cost_curve: tuple[CostLevel, ...] = ()
    #: Cost in basis points per side at which the edge stops being positive,
    #: or None when it survived every level tested. None does not mean "no
    #: such level exists" — only that none was tested.
    breaks_even_at_bps: Decimal | None = None
    #: How many times the assumed cost the edge survives. Reported whether or
    #: not it was flagged: the threshold for calling something fragile is a
    #: judgement, while this ratio is a fact the reader can judge for
    #: themselves. None when the sweep found no break-even point.
    cost_headroom: Decimal | None = None
    checks_run: tuple[str, ...] = ()

    @property
    def disqualified(self) -> bool:
        return any(finding.disqualifying for finding in self.findings)

    def describe(self) -> list[str]:
        if not self.checks_run:
            return ["No robustness check was run. That is not a clean result."]

        lines: list[str] = []
        if self.findings:
            lines.append("FINDINGS:")
            for finding in self.findings:
                marker = "  ✗" if finding.disqualifying else "  !"
                lines.append(f"{marker} {finding.flag}: {finding.detail}")
        else:
            lines.append(
                "No finding from the checks that were run. This is NOT evidence "
                "the strategy works — it means these particular ways of being "
                "wrong were not detected."
            )

        if self.cost_curve:
            lines += ["", "Net P&L against assumed cost per side:"]
            for level in self.cost_curve:
                lines.append(
                    f"  {level.costs.per_side_bps():>6.2f} bps  "
                    f"{level.net_pnl:>14,.2f}  ({level.trades} trades)"
                )
            if self.breaks_even_at_bps is not None:
                lines.append(
                    f"  The edge turns negative by {self.breaks_even_at_bps:.2f} bps "
                    "per side"
                    + (
                        f", which is {self.cost_headroom:.1f}x the assumed cost."
                        if self.cost_headroom is not None
                        else "."
                    )
                )
            else:
                lines.append(
                    "  No level tested turned it negative. That is the range "
                    "tested, not a claim about higher costs."
                )

        lines += ["", f"Checks run: {', '.join(self.checks_run)}."]
        return lines


def _expectancy(trades: Sequence[ClosedTrade]) -> Decimal | None:
    if not trades:
        return None
    return sum((trade.pnl for trade in trades), ZERO) / D(len(trades))


def depends_on_one_trade(result: BacktestResult) -> Finding | None:
    """Would removing the best single trade destroy the edge?"""
    trades = result.trades
    if len(trades) < 2:
        return None
    best = max(trades, key=lambda trade: trade.pnl)
    without = [trade for trade in trades if trade is not best]
    before = _expectancy(trades)
    after = _expectancy(without)
    if before is None or after is None or before <= ZERO:
        return None
    if after <= ZERO:
        share = (best.pnl / sum((t.pnl for t in trades), ZERO) * D(100)) if sum(
            (t.pnl for t in trades), ZERO
        ) > ZERO else None
        detail = (
            f"removing the single best trade ({best.pnl:,.2f} on "
            f"{best.entry_at:%Y-%m-%d}) takes expectancy from {before:,.2f} to "
            f"{after:,.2f} per trade"
        )
        if share is not None:
            detail += f"; that one trade is {share:.0f}% of the net profit"
        return Finding(flag=Flag.DEPENDS_ON_ONE_TRADE, detail=detail)
    return None


def profit_in_one_period(result: BacktestResult) -> Finding | None:
    """Is the whole result one favourable stretch of market?"""
    trades = result.trades
    if len(trades) < 4:
        return None
    ordered = sorted(trades, key=lambda trade: trade.entry_at)
    middle = len(ordered) // 2
    first = sum((trade.pnl for trade in ordered[:middle]), ZERO)
    second = sum((trade.pnl for trade in ordered[middle:]), ZERO)
    total = first + second
    if total <= ZERO:
        return None
    # One half negative while the total is positive means the edge is not
    # present throughout the sample.
    if first <= ZERO or second <= ZERO:
        losing, winning = (
            ("first", "second") if first <= ZERO else ("second", "first")
        )
        return Finding(
            flag=Flag.PROFIT_IN_ONE_PERIOD,
            detail=(
                f"the {winning} half of the trades made {max(first, second):,.2f} "
                f"while the {losing} half made {min(first, second):,.2f}; the edge "
                "is not present across the whole sample"
            ),
        )
    return None


def out_of_sample_degradation(
    in_sample: BacktestResult, out_of_sample: BacktestResult
) -> Finding | None:
    """The clearest overfitting signal there is.

    A strategy that makes money on the data it was built against and
    loses on data it has never seen is not a strategy. This check is the
    reason the splits exist, and it is the one a disappointed author is
    most tempted to re-run with different fractions until it passes —
    which is what `OutOfSampleLedger` counts.

    Degradation is expected and is not by itself a finding: an edge that
    is weaker out-of-sample is normal. Turning negative is the line.
    """
    inside = _expectancy(in_sample.trades)
    outside = _expectancy(out_of_sample.trades)

    if inside is None or inside <= ZERO:
        # Nothing was claimed in-sample, so there is nothing to fail.
        return None
    if outside is None:
        return Finding(
            flag=Flag.FAILS_OUT_OF_SAMPLE,
            detail=(
                "the strategy took no trades at all out-of-sample, so its "
                f"in-sample expectancy of {inside:,.2f} per trade is untested"
            ),
        )
    if outside <= ZERO:
        return Finding(
            flag=Flag.FAILS_OUT_OF_SAMPLE,
            detail=(
                f"expectancy is {inside:,.2f} per trade in-sample and "
                f"{outside:,.2f} out-of-sample, on "
                f"{len(out_of_sample.trades)} held-out trades. An edge that "
                "does not survive unseen data is a description of the data "
                "it was built on"
            ),
        )

    decay = (inside - outside) / inside * D(100)
    if decay >= D(70):
        return Finding(
            flag=Flag.FAILS_OUT_OF_SAMPLE,
            detail=(
                f"expectancy falls {decay:.0f}% out-of-sample "
                f"({inside:,.2f} to {outside:,.2f} per trade). Still positive, "
                "but most of the measured edge did not survive unseen data"
            ),
            # Not disqualifying on its own: a weakened edge may still be
            # real, and calling it fatal would be a judgement this module
            # is not entitled to make.
            disqualifying=False,
        )
    return None


def cost_stress(runner: CostRunner, levels: Sequence[BarCosts]) -> tuple[
    tuple[CostLevel, ...], Decimal | None, Decimal | None, Finding | None
]:
    """Re-run at each cost level and find where the edge dies."""
    curve: list[CostLevel] = []
    for costs in levels:
        result = runner(costs)
        net = sum((trade.pnl for trade in result.trades), ZERO)
        curve.append(
            CostLevel(costs=costs, net_pnl=net, trades=len(result.trades))
        )

    breaks_at: Decimal | None = None
    for level in curve:
        if level.net_pnl <= ZERO:
            breaks_at = level.costs.per_side_bps()
            break

    headroom: Decimal | None = None
    finding: Finding | None = None
    cheapest = curve[0].costs.per_side_bps() if curve else ZERO
    if breaks_at is not None and cheapest > ZERO:
        headroom = breaks_at / cheapest
        if curve[0].net_pnl > ZERO and headroom <= FRAGILE_BELOW_COST_MULTIPLE:
            finding = Finding(
                flag=Flag.FRAGILE_TO_COSTS,
                detail=(
                    f"profitable at {cheapest:.2f} bps per side and negative by "
                    f"{breaks_at:.2f} bps, only {headroom:.1f}x the assumed cost. "
                    "A real spread wider than assumed would erase this"
                ),
            )
    return tuple(curve), breaks_at, headroom, finding


def parameter_sensitivity(
    runner: ParameterRunner,
    sweeps: Mapping[str, Sequence[object]],
    *,
    baseline_pnl: Decimal,
) -> list[Finding]:
    """Nudge each parameter and see whether the edge survives.

    A parameter whose neighbouring values turn the result negative means
    the result belongs to the value, not to the market.
    """
    findings: list[Finding] = []
    for name, values in sweeps.items():
        outcomes: list[tuple[object, Decimal]] = []
        for value in values:
            result = runner(name, value)
            outcomes.append(
                (value, sum((trade.pnl for trade in result.trades), ZERO))
            )
        negatives = [value for value, pnl in outcomes if pnl <= ZERO]
        if baseline_pnl > ZERO and negatives and len(negatives) >= len(outcomes) / 2:
            findings.append(
                Finding(
                    flag=Flag.FRAGILE_TO_PARAMETERS,
                    detail=(
                        f"{name}: {len(negatives)} of {len(outcomes)} nearby values "
                        f"are unprofitable ({', '.join(str(v) for v in negatives)}). "
                        "The market does not know which number was chosen"
                    ),
                )
            )
    return findings


def assess(
    result: BacktestResult,
    *,
    out_of_sample: BacktestResult | None = None,
    cost_runner: CostRunner | None = None,
    cost_levels: Sequence[BarCosts] | None = None,
    parameter_runner: ParameterRunner | None = None,
    sweeps: Mapping[str, Sequence[object]] | None = None,
) -> RobustnessReport:
    """Run every check that the supplied inputs allow.

    Checks that were not run are named in ``checks_run`` by their absence.
    A report that skipped the cost sweep is not a report that passed it,
    and the output says which were done.
    """
    findings: list[Finding] = []
    checks: list[str] = []

    metrics = measure(result)
    checks.append("sample size")
    if metrics.trades < MIN_TRADES_FOR_RATIOS:
        findings.append(
            Finding(
                flag=Flag.TOO_FEW_TRADES,
                detail=(
                    f"{metrics.trades} closed trades, below the {MIN_TRADES_FOR_RATIOS} "
                    "this module will call a sample"
                ),
            )
        )
    if metrics.total_pnl <= ZERO:
        findings.append(
            Finding(
                flag=Flag.NOT_PROFITABLE,
                detail=f"net P&L is {metrics.total_pnl:,.2f} before any stress test",
            )
        )

    if out_of_sample is not None:
        checks.append("out-of-sample")
        degraded = out_of_sample_degradation(result, out_of_sample)
        if degraded is not None:
            findings.append(degraded)

    checks.append("single-trade dependence")
    single = depends_on_one_trade(result)
    if single is not None:
        findings.append(single)

    checks.append("period concentration")
    period = profit_in_one_period(result)
    if period is not None:
        findings.append(period)

    curve: tuple[CostLevel, ...] = ()
    breaks_at: Decimal | None = None
    headroom: Decimal | None = None
    if cost_runner is not None and cost_levels:
        checks.append("cost stress")
        curve, breaks_at, headroom, cost_finding = cost_stress(
            cost_runner, cost_levels
        )
        if cost_finding is not None:
            findings.append(cost_finding)

    if parameter_runner is not None and sweeps:
        checks.append("parameter sensitivity")
        findings.extend(
            parameter_sensitivity(
                parameter_runner, sweeps, baseline_pnl=metrics.total_pnl
            )
        )

    return RobustnessReport(
        findings=tuple(findings),
        cost_curve=curve,
        breaks_even_at_bps=breaks_at,
        cost_headroom=headroom,
        checks_run=tuple(checks),
    )


def rising_costs(
    base: BarCosts, *, multiples: Sequence[str] = ("1", "1.5", "2", "3", "5")
) -> list[BarCosts]:
    """The base assumption scaled up, for a cost sweep."""
    out: list[BarCosts] = []
    for multiple in multiples:
        factor = D(multiple)
        out.append(
            BarCosts(
                spread_bps=base.spread_bps * factor,
                slippage_bps=base.slippage_bps * factor,
                commission_bps=base.commission_bps * factor,
            )
        )
    return out
