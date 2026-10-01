#!/usr/bin/env python
"""Invariants CI asserts outside the test suite.

These are the properties whose failure would be worst and whose cause
would be least obvious from a unit test name: a shipped example risk file
becoming loadable, or the safe defaults quietly flipping. Kept as a
script so the failure message says what broke in plain language.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gtcc.config import LIVE_CONFIRMATION_PHRASE, Settings  # noqa: E402
from gtcc.domain.enums import TradingMode  # noqa: E402
from gtcc.risk.engine import RiskEngine  # noqa: E402
from gtcc.risk.limits import RiskConfigError, load_limits  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok    {message}")
    else:
        print(f"  FAIL  {message}")
        failures.append(message)


print("Safety invariants")

try:
    load_limits(ROOT / "config" / "risk.example.yaml")
    check(False, "the shipped example risk file is refused")
except RiskConfigError:
    check(True, "the shipped example risk file is refused")

settings = Settings()
check(settings.mode is TradingMode.PAPER, "mode defaults to PAPER")
check(settings.live_trading is False, "live trading defaults to off")
check(settings.automatic_execution is False, "automatic execution defaults to off")
check(settings.grok_model == "", "no Grok model name is assumed")

for kwargs, label in (
    ({"mode": TradingMode.LIVE}, "LIVE without the live flag is refused"),
    (
        {"mode": TradingMode.LIVE, "live_trading": True},
        "LIVE without the confirmation phrase is refused",
    ),
):
    try:
        Settings(**kwargs)
        check(False, label)
    except ValueError:
        check(True, label)

reachable = Settings(
    mode=TradingMode.LIVE,
    live_trading=True,
    live_confirmation=LIVE_CONFIRMATION_PHRASE,
)
check(reachable.is_live, "LIVE is reachable when both gates are satisfied")

# The risk engine must not be able to see the AI layer at all.
engine_source = (ROOT / "src" / "gtcc" / "risk" / "engine.py").read_text(encoding="utf-8")
check("gtcc.ai" not in engine_source, "the risk engine imports nothing from ai/")
check(
    "import httpx" not in engine_source and "requests" not in engine_source,
    "the risk engine performs no network I/O",
)

runtime_source = (ROOT / "src" / "gtcc" / "runtime.py").read_text(encoding="utf-8")
check(
    runtime_source.count("self.broker.place_order") == 1,
    "there is exactly one call site that places an order",
)
check(
    "self.engine.evaluate" in runtime_source,
    "the submission path calls the risk engine",
)

print()
if failures:
    print(f"{len(failures)} invariant(s) broken:")
    for failure in failures:
        print(f"  - {failure}")
    raise SystemExit(1)
print("all invariants hold")
