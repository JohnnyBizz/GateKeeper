#!/usr/bin/env python3
"""Run the signal engine over historical candles.

    python tools/backtest.py                                  # sample data
    python tools/backtest.py --csv data/my-session.csv --duration 300
    python tools/backtest.py --compare-durations              # every expiration

Results describe how the engine behaved on the data supplied. They do not
predict future performance, and on synthetic data they measure the generator as
much as the engine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poa.backtesting import Backtester
from poa.chart_detection import generate_series, load_csv
from poa.config import TRADE_DURATIONS, load_config
from poa.models import format_duration
from poa.signals import GateSettings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=None, help="candle CSV to replay")
    parser.add_argument("--synthetic", type=int, default=None, help="generate N candles instead")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--timeframe", type=int, default=None, help="seconds per candle")
    parser.add_argument("--duration", type=int, default=180, help="trade duration in seconds")
    parser.add_argument("--window", type=int, default=250, help="candles visible to the engine")
    parser.add_argument("--step", type=int, default=1, help="evaluate every Nth candle")
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument("--payout", type=float, default=0.80, help="broker payout, for break-even")
    parser.add_argument(
        "--use-recommended-duration",
        action="store_true",
        help="settle at the engine's preferred expiration instead of --duration",
    )
    parser.add_argument(
        "--compare-durations",
        action="store_true",
        help="run once per available expiration and compare",
    )
    args = parser.parse_args()

    config = load_config()

    if args.synthetic:
        series = generate_series(
            args.synthetic,
            symbol=args.asset or "SYNTHETIC",
            timeframe_seconds=args.timeframe or 60,
        )
        source_label = f"synthetic ({args.synthetic} candles)"
    else:
        path = args.csv or str(config.resolve_path("capture.csv_path"))
        try:
            series = load_csv(path, args.timeframe, args.asset or config.get("market.asset"))
        except Exception as exc:
            print(f"Could not load {path}: {exc}")
            return 1
        source_label = path

    settings = GateSettings.from_config(config.section("signals"))
    if args.min_confidence is not None:
        settings.min_confidence = args.min_confidence

    print("=" * 72)
    print("BACKTEST")
    print("=" * 72)
    print(f"Data          {source_label}")
    print(f"Candles       {len(series)} at {format_duration(series.timeframe_seconds)}")
    print(f"Min confidence {settings.min_confidence:.0f} | min duration fit "
          f"{settings.min_duration_compatibility:.0f}")
    print("-" * 72)

    backtester = Backtester(settings=settings, window=args.window, payout=args.payout)

    durations = list(TRADE_DURATIONS) if args.compare_durations else [args.duration]
    rows = []
    for duration in durations:
        result = backtester.run(
            series,
            trade_duration=duration,
            asset=args.asset or series.symbol,
            step=args.step,
            use_recommended_duration=args.use_recommended_duration and not args.compare_durations,
        )
        stats = result.statistics
        rows.append((duration, result, stats))
        if not args.compare_durations:
            _print_detail(result, stats)

    if args.compare_durations:
        print(f"{'DURATION':>10} {'SIGNALS':>8} {'WIN%':>7} {'BREAK-EVEN':>11} {'EV':>8} {'SAMPLE':>8}")
        print("-" * 72)
        for duration, result, stats in rows:
            win_rate = stats["win_rate"]
            expected = stats["expected_value"]
            rate_text = "--" if win_rate is None else f"{win_rate:.1f}"
            ev_text = "--" if expected is None else f"{expected:+.3f}"
            sample_text = "ok" if stats["sufficient_sample"] else "small"
            print(
                f"{format_duration(duration):>10} {len(result.trades):>8} "
                f"{rate_text:>7} {stats['breakeven_rate']:>11.1f} "
                f"{ev_text:>8} {sample_text:>8}"
            )
        print("-" * 72)

    print(
        "\nThese results describe past behaviour on this data only. They do not\n"
        "predict future performance."
    )
    return 0


def _print_detail(result, stats) -> None:
    print(f"Bars evaluated   {result.evaluated_bars}")
    print(f"Signals          {len(result.trades)}  ({result.signal_rate}% of bars)")
    print(f"Waits            {result.wait_count}")
    print(f"No-trade         {result.no_trade_count}")
    print("-" * 72)
    print(f"Wins             {stats['wins']}")
    print(f"Losses           {stats['losses']}")
    print(f"Flat             {stats['flat']}")
    win_rate = stats["win_rate"]
    print(f"Win rate         {'--' if win_rate is None else f'{win_rate:.1f}%'}")
    print(f"Break-even       {stats['breakeven_rate']:.1f}%  (at {stats['payout_assumed']:.0%} payout)")
    expected = stats["expected_value"]
    print(f"Expected value   {'--' if expected is None else f'{expected:+.4f} per unit staked'}")
    print(f"Avg confidence   {stats['average_confidence']}")
    print(
        f"Streaks          {stats['streaks']['max_winning_streak']} wins / "
        f"{stats['streaks']['max_losing_streak']} losses"
    )
    if stats["sample_warning"]:
        print(f"\n  ! {stats['sample_warning']}")

    for title, key in (
        ("BY DIRECTION", "by_direction"),
        ("BY SETUP QUALITY", "by_setup_quality"),
        ("BY REGIME", "by_regime"),
    ):
        groups = stats.get(key) or []
        if not groups:
            continue
        print(f"\n{title}")
        for group in groups:
            rate = group["win_rate"]
            print(
                f"  {group['group']:<20} {group['signals']:>4} signals  "
                f"{('--' if rate is None else f'{rate:.1f}%'):>7}"
                + ("" if group["sufficient_sample"] else "   (small sample)")
            )
    print("-" * 72)


if __name__ == "__main__":
    sys.exit(main())
