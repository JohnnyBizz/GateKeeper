#!/usr/bin/env python3
"""Does any part of the score actually predict the outcome?

    python tools/components.py                      # every recorded market
    python tools/components.py --timeframe 5 --duration 30

The engine scores a setup from ten weighted components. This asks, of each
one separately: given a winning call and a losing call picked at random, how
often did this component argue harder for the winner? Fifty per cent means it
said nothing. Below fifty means it argued for the wrong side, which is worse
than nothing, because its weight is actively pulling the score around.

**Gates off, deliberately.** Judging a component only on the calls the gates
allowed would judge it on survivors — the sample would already be filtered by
the thing being measured.

The important column is the last one. Every measurement here is repeated
independently per recording, and only a component that holds *the same
direction in every one of them* is reported as replicating. Pooling first is
how this analysis lies: run over two recordings together, ema_alignment came
out at 41.6% with an interval clear of fifty, which reads as a component
wired backwards and worth flipping. Split apart it was 35.9% in one recording
and 48.1% in the other — the whole effect was one half-hour of one market,
and acting on it would have been fitting noise into the engine and shipping
it as an improvement.

Nothing has replicated yet. That is the honest state, not a bug in this
script, and the fix is more recorded market rather than a cleverer statistic.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poa.backtesting.paper import Backtester  # noqa: E402
from poa.chart_detection.csv_source import load_csv  # noqa: E402
from poa.signals import GateSettings  # noqa: E402

RECORDED = Path(__file__).resolve().parent.parent / "data/recorded"

# Everything open. See the module docstring: a component judged only on the
# calls that passed the gates is judged on a sample the gates already chose.
GATES_OFF = GateSettings(
    min_confidence=0.0,
    min_shown_confidence=0.0,
    min_duration_compatibility=0.0,
    min_data_confidence=0.0,
    min_component_agreement=0.0,
    max_atr_percentile=100.0,
    require_multi_timeframe_agreement=False,
    require_heikin_ashi_confirmation=False,
    require_measured_edge=False,
    avoid_weak_regimes=False,
)


def auc(pairs: list[tuple[float, bool]]) -> tuple[float, float] | None:
    """Ranking power and its standard error, by Hanley–McNeil.

    An AUC without an interval is a number pretending to be a fact, and at
    these sample sizes the interval is usually wide enough to contain fifty.
    """
    wins = [value for value, won in pairs if won]
    losses = [value for value, won in pairs if not won]
    if not wins or not losses:
        return None
    better = sum(
        1.0 if a > b else 0.5 if a == b else 0.0 for a in wins for b in losses
    )
    a = better / (len(wins) * len(losses))
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    variance = (
        a * (1 - a)
        + (len(wins) - 1) * (q1 - a * a)
        + (len(losses) - 1) * (q2 - a * a)
    ) / (len(wins) * len(losses))
    return a * 100, math.sqrt(variance) * 100


def _calls(paths: list[Path], timeframe: int, duration: int, window: int):
    out = []
    for path in paths:
        series = load_csv(path, timeframe_seconds=timeframe)
        if len(series) < window + 2:
            continue
        result = Backtester(window=window, payout=0.92, settings=GATES_OFF).run(
            series, trade_duration=duration, asset=path.stem, step=1,
            realistic_entry=True,
        )
        out += [
            (trade.components, trade.outcome == "win")
            for trade in result.trades
            if trade.outcome in ("win", "loss") and trade.components
        ]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframe", type=int, default=5)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--window", type=int, default=120)
    args = parser.parse_args()

    groups: dict[str, list[Path]] = {}
    for folder in sorted(p for p in RECORDED.iterdir() if p.is_dir()):
        found = sorted(folder.glob(f"*-{args.timeframe}s.csv"))
        if found:
            groups[folder.name] = found
    if len(groups) < 2:
        print(
            f"Need at least two recordings with {args.timeframe}s charts to say "
            "whether anything replicates. A single one cannot tell a finding "
            "apart from a coincidence."
        )
        return 1

    data = {
        name: _calls(paths, args.timeframe, args.duration, args.window)
        for name, paths in groups.items()
    }
    data = {name: rows for name, rows in data.items() if rows}
    names = sorted({key for rows in data.values() for comp, _ in rows for key in comp})

    print(f"WHICH PARTS OF THE SCORE PREDICT, AT {args.timeframe}s -> "
          f"{args.duration}s, GATES OFF\n")
    header = f"{'component':20} " + "  ".join(f"{n[:20]:>21}" for n in data)
    print(header + "   replicates?")
    print("-" * len(header + "   replicates?"))

    for component in names:
        cells, sides = [], []
        for rows in data.values():
            measured = auc([(c[component], won) for c, won in rows if component in c])
            if measured is None:
                cells.append(f"{'—':>21}")
                sides.append(0)
                continue
            value, error = measured
            low, high = value - 1.96 * error, value + 1.96 * error
            cells.append(f"{value:5.1f}% [{low:5.1f},{high:5.1f}]")
            sides.append(1 if low > 50 else (-1 if high < 50 else 0))
        # Replicating means every recording agreed, and none of them was
        # merely inconclusive. One significant result out of ten components
        # is roughly what chance produces.
        if sides and all(s == sides[0] != 0 for s in sides):
            verdict = "YES — every recording, same direction"
        elif any(sides):
            verdict = "one recording only — not evidence"
        else:
            verdict = "no"
        print(f"{component:20} " + "  ".join(cells) + f"   {verdict}")

    print()
    for name, rows in data.items():
        wins = sum(1 for _, won in rows if won)
        print(f"  {name}: {len(rows)} calls, base rate {wins / len(rows) * 100:.1f}%")
    print(
        "\nA component only counts if it holds the same direction in every "
        "recording.\nPooling them first is how this analysis lies; see the "
        "docstring."
    )
    print(_how_much_more(data))
    return 0


# The effect worth finding. A component ranking winners above losers 55% of
# the time is small, and small is what an honest edge looks like here — 60%
# would already be extraordinary. Sizing for 55% means the answer arrives
# whether it is good news or bad.
WORTH_FINDING = 55.0


def _how_much_more(data: dict) -> str:
    """What it would take to answer this, rather than "collect more".

    "More data" is not an instruction anybody can act on. This turns the
    intervals above into a number of calls and, at the rate the app actually
    produces them, an amount of time.
    """
    smallest = min((len(rows) for rows in data.values()), default=0)
    if smallest < 10:
        return ""
    # Half-widths shrink as 1/sqrt(n), so scale the observed one down to what
    # would put a 55% result clear of 50%.
    sample = next(iter(data.values()))
    component = sorted({k for c, _ in sample for k in c})[0]
    measured = auc([(c[component], won) for c, won in sample if component in c])
    if measured is None:
        return ""
    _value, error = measured
    target = (WORTH_FINDING - 50.0) / 1.96
    if error <= target:
        return ""
    needed = int(smallest * (error / target) ** 2)

    # Measured from the sessions: about 90 calls an hour with five pairs
    # watched, and the rate scales with how many are open.
    hours = needed / 90.0
    return (
        f"\nTo settle it: each recording holds about {smallest} calls, and an "
        f"edge worth\nfinding ({WORTH_FINDING:.0f}%) needs roughly {needed:,} "
        f"per recording before the interval\nclears 50%. At the ~90 calls an "
        f"hour five watched pairs produce, that is about\n{hours:.0f} hours of "
        "the app simply running — and it now collects them by itself."
    )


if __name__ == "__main__":
    raise SystemExit(main())
