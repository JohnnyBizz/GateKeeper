#!/usr/bin/env python3
"""Generate sample candle data for testing and backtesting.

    python tools/make_sample_data.py
    python tools/make_sample_data.py --candles 5000 --out data/big.csv

The output is synthetic. It contains trends, ranges, breakouts and volatility
clusters so the analysis layer has something to react to, but it is not market
data and results measured on it say nothing about live performance.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poa.chart_detection import generate_series, write_csv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles", type=int, default=2000)
    parser.add_argument("--symbol", default="EUR/USD")
    parser.add_argument("--timeframe", type=int, default=60, help="seconds per candle")
    parser.add_argument("--price", type=float, default=1.08)
    parser.add_argument("--volatility", type=float, default=0.00022)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="data/sample_eurusd_m1.csv")
    args = parser.parse_args()

    series = generate_series(
        args.candles,
        symbol=args.symbol,
        timeframe_seconds=args.timeframe,
        seed=args.seed,
        start_price=args.price,
        base_volatility=args.volatility,
    )

    root = Path(__file__).resolve().parent.parent
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = root / out_path

    written = write_csv(series, out_path)
    print(f"Wrote {len(series)} candles to {written}")
    print(
        f"  symbol {series.symbol} | timeframe {series.timeframe_seconds}s | "
        f"range {series.low.min():.5f} – {series.high.max():.5f}"
    )
    print("\n  This is synthetic data, not a market recording.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
