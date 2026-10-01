"""Render a backtest as text.

Separate from the engine so presentation cannot change what was measured.
The ordering is deliberate: the sample-size warning comes before the
numbers it undermines, because a reader who sees a profit factor first has
already formed an impression by the time the caveat arrives.
"""

from __future__ import annotations

from gtcc.backtest.engine import BacktestResult
from gtcc.backtest.attribution import all_breakdowns
from gtcc.backtest.metrics import measure
from gtcc.backtest.robustness import RobustnessReport


def render(
    result: BacktestResult, robustness: "RobustnessReport | None" = None
) -> list[str]:
    """The backtest, and the attacks on it when they were run.

    The robustness findings come LAST on purpose, as the thing a reader
    finishes on. Leading with them would let a reader skim past to the
    profit figure; ending on them means the last thing read is what is
    wrong with the result.
    """
    metrics = measure(result)
    lines = [result.describe(), ""]

    lines += metrics.describe_with_warning()

    if result.skipped:
        lines += [
            "",
            f"{len(result.skipped)} setup(s) the strategy wanted and risk refused:",
        ]
        counts: dict[str, int] = {}
        for setup in result.skipped:
            for reason in setup.reasons:
                code = reason.split(":")[0]
                counts[code] = counts.get(code, 0) + 1
        for code, count in sorted(counts.items(), key=lambda pair: -pair[1]):
            lines.append(f"  {count:>4}  {code}")
        lines.append(
            "  These are kept because a backtest that discards them cannot say "
            "whether the limits cost money or saved it."
        )

    if result.trades:
        lines += ["", "WHERE IT CAME FROM", ""]
        for report in all_breakdowns(result.trades):
            lines += report.describe()
            lines.append("")

    if robustness is not None:
        lines += ["", "ROBUSTNESS", ""] + robustness.describe()
    else:
        lines += [
            "",
            "No robustness check was run against this result. Costs, parameter "
            "sensitivity and outlier dependence are untested, so the figures "
            "above are the most favourable reading available.",
        ]

    if result.open_at_end:
        lines += [
            "",
            "A position was still open when the data ran out. It is closed at the "
            "last bar's close and counted separately, because that is not a result "
            "the strategy produced.",
        ]
    return lines
