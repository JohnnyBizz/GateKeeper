#!/usr/bin/env python3
"""What actually happens over a trade horizon, measured rather than assumed.

    python tools/horizon.py                    # every committed recording
    python tools/horizon.py --bars 6           # a 30 SEC trade on a 5 SEC chart

The engine scores "is this a good CALL". The trade asks something narrower:
will price be above *this* price in thirty seconds. On a 5 SEC chart that is
six bars, and six bars is not a horizon a trend has much to say about — which
is the standing explanation for why a score built almost entirely out of
trend-continuation reads worse than nothing when it is live.

This asks the narrower question directly, and it asks it of every bar rather
than only the ones a gate let through. That matters more than it sounds: at
the gate the four committed recordings between them produce thirty-three
calls, which is too few to measure anything. Ungated they produce thousands,
and the question here does not need the gate — it is about the market, not
about the tool's opinion of it.

The measurement: bucket each bar by how far price has stretched from its own
recent centre, in ATR units and *with a sign*, then report how often price was
higher N bars later. If a stretched market reverts, the buckets slope
downwards — high positive stretch, price falls. If it continues, they slope up.
Fifty per cent everywhere means extension says nothing and this line of enquiry
is finished, which is a result worth having too.

The replication rule applies here as everywhere: a slope that appears in one
recording and not the others is one market sampled once. The per-recording
columns are printed for that reason and the pooled figure is the least
interesting number on the page.

Nothing here predicts the future. It describes recorded markets.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poa.analysis.extension import LOOKBACK  # noqa: E402
from poa.backtesting.stats import wilson_interval  # noqa: E402
from poa.chart_detection.csv_source import load_csv  # noqa: E402
from poa.indicators.core import atr as atr_of  # noqa: E402
from poa.indicators.core import sma  # noqa: E402

RECORDED = Path(__file__).resolve().parent.parent / "data/recorded"

#: Fixed in advance. A bucket edge chosen after seeing the outcomes finds an
#: edge in noise, which is the mistake FINDINGS.md exists to prevent.
BUCKETS: tuple[tuple[float, float, str], ...] = (
    (-99.0, -2.0, "below centre by 2+ ATR"),
    (-2.0, -1.0, "below by 1-2"),
    (-1.0, -0.35, "below by 0.35-1"),
    (-0.35, 0.35, "near its centre"),
    (0.35, 1.0, "above by 0.35-1"),
    (1.0, 2.0, "above by 1-2"),
    (2.0, 99.0, "above centre by 2+ ATR"),
)


def stretch_series(series, lookback: int = LOOKBACK) -> np.ndarray:
    """Signed distance from the rolling centre, in ATR units, per bar.

    NaN wherever there is not enough history behind the bar to say.
    """
    closes = np.asarray(series.close, dtype=np.float64)
    centre = sma(closes, lookback)
    average_range = atr_of(series.high, series.low, series.close, period=14)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (closes - centre) / average_range
    out[~np.isfinite(out)] = np.nan
    return out


def measure(series, bars: int) -> dict[str, tuple[int, int]]:
    """Per bucket: how many bars, and how many finished higher `bars` later."""
    closes = np.asarray(series.close, dtype=np.float64)
    stretch = stretch_series(series)
    counts: dict[str, tuple[int, int]] = {label: (0, 0) for _, _, label in BUCKETS}
    for i in range(len(closes) - bars):
        value = stretch[i]
        if not np.isfinite(value):
            continue
        later = closes[i + bars]
        if later == closes[i]:  # a tie decides nothing either way
            continue
        rose = later > closes[i]
        for low, high, label in BUCKETS:
            if low <= value < high:
                total, ups = counts[label]
                counts[label] = (total + 1, ups + int(rose))
                break
    return counts


def _charts(paths: list[Path], timeframe: int) -> list[tuple[str, object]]:
    found = []
    for directory in sorted(paths):
        for csv in sorted(directory.glob(f"*-{timeframe}s.csv")):
            try:
                series = load_csv(str(csv), timeframe, None)
            except Exception:
                continue
            if len(series) > LOOKBACK + 30:
                found.append((f"{directory.name[:10]} {csv.stem[:-3]}", series))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=int, default=6,
                        help="how many bars ahead the trade settles (default 6)")
    parser.add_argument("--timeframe", type=int, default=5,
                        help="chart timeframe in seconds (default 5)")
    args = parser.parse_args()

    directories = [p for p in RECORDED.iterdir() if p.is_dir()] if RECORDED.is_dir() else []
    charts = _charts(directories, args.timeframe)
    if not charts:
        print(f"No {args.timeframe}s recording found under {RECORDED}.")
        return 1

    print("=" * 78)
    print(f"WHAT HAPPENS {args.bars} BARS LATER, BY HOW STRETCHED PRICE ALREADY WAS")
    print("=" * 78)
    print(f"{len(charts)} charts at {args.timeframe}s. Each row is a state of the")
    print("market; the number is how often price was HIGHER after "
          f"{args.bars} bars.")
    print("50% means the state says nothing. The column has to lean the same")
    print("way in every recording independently before it is a finding.")
    print("-" * 78)

    per_chart = {label: measure(series, args.bars) for label, series in charts}

    # The number every row has to be read against. A bucket at 45% is not a
    # bearish state if every bar in the recording finished lower — it is the
    # drift, and reading it as information is how a market's mood gets
    # mistaken for a signal.
    base_total = sum(t for c in per_chart for t, _ in per_chart[c].values())
    base_ups = sum(u for c in per_chart for _, u in per_chart[c].values())
    base = base_ups / base_total * 100 if base_total else 50.0
    print(f"{'ANY bar (the base rate)':<26}{f'{base:.1f}% of {base_total}':>16}")
    print("Everything below is measured against that, not against 50%.")
    print("-" * 78)

    header = (
        f"{'state':<26}{'pooled':>16}{'95% interval':>18}"
        f"{'vs base':>9}   per chart"
    )
    print(header)
    print("-" * 78)
    for _, _, label in BUCKETS:
        total = sum(per_chart[c][label][0] for c in per_chart)
        ups = sum(per_chart[c][label][1] for c in per_chart)
        if total < 30:
            print(f"{label:<26}{'too few':>16}{'':>18}   ({total} bars)")
            continue
        rate = ups / total * 100
        interval = wilson_interval(ups, total)
        span = f"{interval[0]:.1f} .. {interval[1]:.1f}" if interval else "--"
        each = []
        for chart in per_chart:
            t, u = per_chart[chart][label]
            each.append(f"{u / t * 100:.0f}%" if t >= 20 else "·")
        print(
            f"{label:<26}{f'{rate:.1f}% of {total}':>16}{span:>18}"
            f"{rate - base:>+8.1f}   {' '.join(each)}"
        )

    print("-" * 78)
    print("The column that matters is `vs base`. A state only tells you"
          " something")
    print("if it differs from what an arbitrary bar was doing anyway.")
    print()
    print("A column that slopes DOWN is a market that reverts: the further")
    print("price has already gone, the less likely it is to keep going over")
    print("this horizon. That would be an entry rule the score cannot express,")
    print("because nothing in it measures distance travelled.")
    print()
    print("These results describe recorded markets. They do not predict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
