#!/usr/bin/env python3
"""Does any part of the score actually predict the outcome?

    python tools/components.py --live               # the calls it really made
    python tools/components.py                      # every recorded market
    python tools/components.py --timeframe 5 --duration 30

``--live`` is the one that matters day to day. It reads the app's own journal,
where every call is already stored with its component breakdown, and groups
them into sessions — so the answer accumulates while the app is simply used,
with nothing to record or send.

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
script, and the fix is more market rather than a cleverer statistic — about
465 calls per group before an edge worth having would clear fifty. The app
produces roughly ninety an hour with several pairs watched, so the question
answers itself in a few hours of ordinary use.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poa.backtesting.paper import Backtester  # noqa: E402
from poa.backtesting.stats import auc  # noqa: E402,F401  (re-exported)
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


# A gap this long between calls starts a new session. Sessions are the unit
# of replication here for the same reason recordings are: a component that
# only works in one sitting has told you about that sitting.
SESSION_GAP_SECONDS = 30 * 60


def from_journal(path: Path, limit: int = 100_000) -> dict[str, list]:
    """Every settled call the tool has really made, grouped into sessions.

    The journal already stores each signal's full component breakdown in its
    payload, so this needs nothing collected specially — every session that
    has ever run is already in here, and every future one adds to it. That is
    the whole point: the question of whether any component predicts anything
    needs a few hundred calls per group, and the app produces them by itself
    while being used normally.
    """
    from poa.storage.journal import Journal

    rows = [
        row for row in Journal(path).recent(limit=limit)
        if row.get("outcome") in ("win", "loss")
    ]
    rows.sort(key=lambda r: str(r.get("timestamp") or ""))

    groups: dict[str, list] = {}
    current: list = []
    label = ""
    previous: datetime | None = None
    for row in rows:
        try:
            when = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        payload = row.get("payload") or {}
        score = payload.get("score") if isinstance(payload, dict) else None
        parts = (score or {}).get("components") if isinstance(score, dict) else None
        if not parts:
            continue
        components = {
            str(c.get("name")): float(c.get("score", 0.0))
            for c in parts if c.get("name")
        }
        if previous is None or (when - previous).total_seconds() > SESSION_GAP_SECONDS:
            if current:
                groups[label] = current
            current = []
            label = f"{when:%Y-%m-%d %H:%M}"
        previous = when
        current.append((components, row["outcome"] == "win"))
    if current:
        groups[label] = current
    return groups


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


def _report_live(path: Path | None) -> int:
    """The same question, asked of calls the tool actually made.

    Replaying a recording measures the engine against a market. This measures
    it against its own live behaviour, which is the thing being complained
    about — and needs nothing collected specially, because every call is
    already journalled with its component breakdown.
    """
    if path is None:
        from poa.config import data_root

        path = data_root() / "storage" / "journal.db"
    if not path.exists():
        print(f"No journal at {path}. Run the app first.")
        return 1

    data = {k: v for k, v in from_journal(path).items() if v}
    total = sum(len(v) for v in data.values())
    print(f"WHICH PARTS OF THE SCORE PREDICT, ON CALLS ACTUALLY MADE")
    print(f"{path}\n{total} settled calls across {len(data)} session(s)\n")
    if len(data) < 2:
        print(
            "At least two sessions are needed before anything can be said to "
            "replicate.\nOne session cannot tell a finding from a coincidence — "
            "that is the whole\nlesson of this file. Keep the app running and "
            "come back."
        )
        return 1
    _print_table(data)
    print(_how_much_more(data))
    return 0


def _print_table(data: dict) -> None:
    """One row per component, one column per independent group."""
    names = sorted({k for rows in data.values() for comp, _ in rows for k in comp})
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
        if sides and all(s == sides[0] != 0 for s in sides):
            verdict = "YES — every group, same direction"
        elif any(sides):
            verdict = "one group only — not evidence"
        else:
            verdict = "no"
        print(f"{component:20} " + "  ".join(cells) + f"   {verdict}")
    print()
    for name, rows in data.items():
        wins = sum(1 for _, won in rows if won)
        print(f"  {name}: {len(rows)} calls, base rate {wins / len(rows) * 100:.1f}%")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframe", type=int, default=5)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--window", type=int, default=120)
    parser.add_argument(
        "--journal", type=Path, default=None,
        help="read the calls the tool really made instead of replaying "
             "recordings — defaults to the app's own journal",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="shorthand for --journal pointing at the app's journal",
    )
    args = parser.parse_args()

    if args.live or args.journal is not None:
        return _report_live(args.journal)

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
