"""Paper trading and backtesting over historical candles."""

from .paper import BacktestResult, Backtester, PaperTrade
from .stats import MIN_MEANINGFUL_SAMPLE, Outcome, breakeven_rate, summarise_outcomes

__all__ = [
    "BacktestResult",
    "Backtester",
    "MIN_MEANINGFUL_SAMPLE",
    "Outcome",
    "PaperTrade",
    "breakeven_rate",
    "summarise_outcomes",
]
