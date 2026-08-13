"""Signal generation: scoring, confirmation gates, duration fit and lifecycle."""

from .duration import DurationAnalysis, analyze_duration
from .engine import Signal, SignalEngine, SignalRequest
from .gates import GateReport, GateResult, GateSettings, evaluate_gates
from .narrative import Narrative, build_narrative, contains_banned_language
from .scoring import WEIGHTS, ScoreResult, score_direction
from .tracker import SignalTracker, TrackedChange, TrackerSettings

__all__ = [
    "DurationAnalysis",
    "GateReport",
    "GateResult",
    "GateSettings",
    "Narrative",
    "ScoreResult",
    "Signal",
    "SignalEngine",
    "SignalRequest",
    "SignalTracker",
    "TrackedChange",
    "TrackerSettings",
    "WEIGHTS",
    "analyze_duration",
    "build_narrative",
    "contains_banned_language",
    "evaluate_gates",
    "score_direction",
]
