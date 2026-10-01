#!/usr/bin/env python
"""Safety invariants CI asserts outside the test suite.

These are properties whose failure would be worst and least obvious
from a unit-test name, and several are structural: a refactor that
introduced a second path to a broker, or let the risk engine reach the
model, would pass every behavioural test while breaking the design.
Grepping the source catches that; a mock does not.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from gtcc.config import Settings  # noqa: E402
from gtcc.domain.enums import TradingMode  # noqa: E402
from gtcc.domain.money import D  # noqa: E402
from gtcc.risk.limits import RiskConfigError, load_limits  # noqa: E402
from gtcc.risk.safety import (  # noqa: E402
    LIVE_CONFIRMATION_PHRASE,
    LiveArmingError,
    TripReason,
    initial_state,
)

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {message}")
    if not condition:
        failures.append(message)


def refuses(callable_, message: str, *exceptions: type[BaseException]) -> None:
    try:
        callable_()
    except exceptions or (Exception,):
        check(True, message)
    else:
        check(False, message)


print("Configuration cannot arm live trading")
refuses(lambda: Settings(mode=TradingMode.LIVE, secret_key="x" * 40),
        "GTCC_MODE=LIVE is refused at startup", ValueError)
check("live_confirmation" not in Settings.model_fields,
      "no confirmation phrase can be set from the environment")
check("live_trading" not in Settings.model_fields,
      "no setting named live_trading exists to be mistaken for arming")
check(Settings(secret_key="x" * 40).mode is TradingMode.PAPER, "mode defaults to PAPER")
check(Settings(secret_key="x" * 40).allow_live_trading is False,
      "live permission defaults to off")
check(Settings(secret_key="x" * 40).automatic_execution is False,
      "automatic execution defaults to off")
check(Settings(secret_key="x" * 40).grok_model == "", "no Grok model name is assumed")

settings = Settings(secret_key="x" * 40)
refuses(lambda: setattr(settings, "mode", TradingMode.LIVE),
        "settings are frozen: assignment cannot bypass validation")
refuses(lambda: setattr(settings, "allow_live_trading", True),
        "live permission cannot be switched on by assignment")

print("\nRuntime starts disarmed and latches safely")
state = initial_state(TradingMode.PAPER)
check(state.live_armed is False and state.live_permitted is False,
      "a fresh process is not armed")
refuses(lambda: initial_state(TradingMode.LIVE),
        "LIVE is not an acceptable startup state", LiveArmingError)
refuses(lambda: state.arm_live(actor="x", confirmation="wrong", deployment_allows_live=True),
        "the wrong phrase does not arm", LiveArmingError)
refuses(lambda: state.arm_live(actor="x", confirmation=LIVE_CONFIRMATION_PHRASE,
                               deployment_allows_live=False),
        "arming without deployment permission is refused", LiveArmingError)

armed = state.arm_live(actor="owner", confirmation=LIVE_CONFIRMATION_PHRASE,
                       deployment_allows_live=True)
check(armed.live_permitted and armed.armed_by == "owner", "a valid arming records the actor")

tripped = armed.trip(TripReason.BROKER_UNHEALTHY, "down")
check(tripped.tripped and not tripped.live_permitted, "a trip latches execution off")
check(tripped.new_trades_blocked, "a trip blocks new trades in every mode")
refuses(lambda: tripped.reset_breaker(actor="owner", healthy=False),
        "a reset is refused while still unhealthy", LiveArmingError)
refuses(lambda: tripped.arm_live(actor="owner", confirmation=LIVE_CONFIRMATION_PHRASE,
                                 deployment_allows_live=True),
        "arming is refused while latched", LiveArmingError)
cleared = tripped.reset_breaker(actor="owner", healthy=True)
check(not cleared.tripped and not cleared.live_armed,
      "a reset clears the latch and leaves live disarmed")

print("\nFinancial values")
for bad in ("NaN", "sNaN", "Infinity", "-Infinity"):
    refuses(lambda b=bad: D(b), f"D() refuses {bad}", ValueError)

print("\nRisk limits")
try:
    load_limits(ROOT / "config" / "risk.example.yaml")
    check(False, "the shipped example risk file is refused")
except RiskConfigError:
    check(True, "the shipped example risk file is refused")

print("\nStructure")
engine_source = (SRC / "gtcc" / "risk" / "engine.py").read_text(encoding="utf-8")
check("gtcc.ai" not in engine_source, "the risk engine imports nothing from ai/")
check("import httpx" not in engine_source and "requests" not in engine_source,
      "the risk engine performs no network I/O")

runtime_source = (SRC / "gtcc" / "runtime.py").read_text(encoding="utf-8")
check(runtime_source.count("self.broker.place_order") == 1,
      "there is exactly one call site that places an order")
check("self.engine.evaluate" in runtime_source, "the submission path calls the risk engine")

ai_sources = "\n".join(
    path.read_text(encoding="utf-8") for path in (SRC / "gtcc" / "ai").glob("*.py")
)
for forbidden in ("arm_live", "reset_breaker", "gtcc.risk", "place_order"):
    check(forbidden not in ai_sources, f"the ai package never references {forbidden}")

config_source = (SRC / "gtcc" / "config.py").read_text(encoding="utf-8")
check("frozen=True" in config_source, "settings are declared frozen")

# The backtester measures the real system. A backtest whose risk engine is
# not the live risk engine, or that relaxes a check because the mode is
# BACKTEST, produces numbers for a platform that does not exist — and the
# numbers would be better than the real ones, which is the direction that
# gets a strategy promoted.
backtest_source = "\n".join(
    path.read_text(encoding="utf-8")
    for path in (SRC / "gtcc" / "backtest").glob("*.py")
)
check(
    "RiskEngine()" in backtest_source and "self.engine.evaluate" in backtest_source,
    "the backtester sizes through the real risk engine",
)
for smell in ("skip_risk", "bypass", "ignore_limits", "force=True"):
    check(smell not in backtest_source, f"the backtester has no {smell}")
# Shuffling a time series split leaks the future through autocorrelation:
# tomorrow's bar ends up in-sample while today's is held out. Checked on the
# parsed source, since the module docstring explains the rule in words and a
# grep would fail on the explanation.


def _names(source: str) -> set[str]:
    """Imported modules, attribute names and called names in a module."""
    found: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
    return found


backtest_names: set[str] = set()
for path in (SRC / "gtcc" / "backtest").glob("*.py"):
    backtest_names |= _names(path.read_text(encoding="utf-8"))
for forbidden in ("random", "shuffle", "sample", "choice"):
    check(
        forbidden not in backtest_names,
        f"no split in the backtester calls {forbidden}",
    )

# Every way out of submit() writes a journal row. Section 24 keeps the
# refusals, and a path that returns without journalling would silently
# make the journal a record of only the trades that worked — which is the
# shape of journal that cannot answer whether the refusals were right.
submit_source = runtime_source.split("def submit(")[1].split("\n    def ")[0]
returns = submit_source.count("return SubmissionResult(")
journals = submit_source.count("self._journal(")
check(
    returns == journals and returns >= 4,
    f"every exit from submit journals ({returns} returns, {journals} journal writes)",
)

# The scanner analyses and cannot trade. It is handed a market-data
# adapter and the strategy registry; giving it the runtime, a broker or the
# risk engine would create a second route to a venue, and the whole design
# rests on there being exactly one.
scanner_sources = "\n".join(
    path.read_text(encoding="utf-8") for path in (SRC / "gtcc" / "scanner").glob("*.py")
)
for forbidden in (
    "place_order", "submit", "BrokerAdapter", "gtcc.runtime", "RiskEngine",
):
    check(
        forbidden not in scanner_sources,
        f"the scanner package never references {forbidden}",
    )

# The scan route may read the runtime, but must not submit through it.
scan_route = (SRC / "gtcc" / "api" / "routes" / "scan.py").read_text(encoding="utf-8")
check(".submit(" not in scan_route, "the scan route never submits an order")

# The suite must stand alone. A test that reads the deployment's own risk
# file passes on the machine where that file exists and fails everywhere
# else, and couples its assertions to numbers the owner is free to change.
# This was a real failure: seven tests went red in CI and green locally,
# and the local pass was the misleading one.
#
# Checked on the parsed source rather than by grepping, so that writing
# about the rule in a docstring does not break it. Comments never reach
# the AST; docstrings are skipped explicitly.


def _path_literals(source: str) -> list[str]:
    docstrings = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    docstrings.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


for path in sorted((SRC.parent / "tests").glob("*.py")):
    literals = _path_literals(path.read_text(encoding="utf-8"))
    check(
        not any("config/risk" in literal for literal in literals),
        f"{path.name} does not read the deployment's risk configuration",
    )

print()
if failures:
    print(f"{len(failures)} invariant(s) broken:")
    for failure in failures:
        print(f"  - {failure}")
    raise SystemExit(1)
print("all invariants hold")
