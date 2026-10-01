"""Strategy modules. Each is independent and each must earn its status."""

from gtcc.strategies.base import (
    Proposal,
    Strategy,
    StrategyContext,
    ValidationStatus,
)
from gtcc.strategies.registry import StrategyRegistry
from gtcc.strategies.trend_continuation import TrendContinuation

__all__ = [
    "Proposal", "Strategy", "StrategyContext", "StrategyRegistry",
    "TrendContinuation", "ValidationStatus",
]
