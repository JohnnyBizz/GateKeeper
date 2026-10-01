"""Event-driven backtesting — specification section 22.

The only thing here that matters is that it cannot see the future, and
that it says what it assumed.
"""

from gtcc.backtest.engine import (
    BacktestResult,
    BacktestSettings,
    Backtester,
    BarCosts,
    ClosedTrade,
    ExitReason,
)
from gtcc.backtest.metrics import Metrics, measure
from gtcc.backtest.robustness import (
    Finding,
    Flag,
    RobustnessReport,
    assess,
    out_of_sample_degradation,
    rising_costs,
)
from gtcc.backtest.splits import (
    OutOfSampleLedger,
    Split,
    SplitError,
    Window,
    split,
    walk_forward,
)

__all__ = [
    "BacktestResult",
    "BacktestSettings",
    "Backtester",
    "BarCosts",
    "ClosedTrade",
    "ExitReason",
    "Metrics",
    "measure",
    "Finding",
    "Flag",
    "RobustnessReport",
    "assess",
    "out_of_sample_degradation",
    "rising_costs",
    "OutOfSampleLedger",
    "Split",
    "SplitError",
    "Window",
    "split",
    "walk_forward",
]
