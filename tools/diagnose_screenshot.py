#!/usr/bin/env python3
"""Run the whole capture pipeline over a real screenshot and report what broke.

Point this at a PNG of an actual trading screen and it walks every stage the
app walks — find the chart, measure the axis, read the labels, extract the
candles, calibrate the prices, score the data quality — printing what each one
produced and where the chain first went wrong.

    python tools/diagnose_screenshot.py screen.png [--out debug/]

``--out`` writes the cropped region and a mask overlay next to the report,
which is usually the fastest way to see that a region is right or wrong.

This exists because photographs of a monitor are not the pixels the app sees.
Diagnosing from them means guessing, and guessing meant shipping fixes for the
wrong problem several times over.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from poa.chart_detection.autodetect import detect_layout, ocr_available  # noqa: E402
from poa.chart_detection.calibration import resolve_calibration  # noqa: E402
from poa.chart_detection.candles import (  # noqa: E402
    ColorProfile,
    build_masks,
    extract_pixel_candles,
    pixel_candles_to_series,
)
from poa.chart_detection.quality import validate_series  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    image = cv2.imread(str(args.image))
    if image is None:
        print(f"Could not read {args.image}")
        return 1

    height, width = image.shape[:2]
    rule("SCREEN")
    print(f"{args.image.name}: {width} x {height}")
    print(f"Tesseract available: {ocr_available()}")

    # --- how much of the screen even looks like candles --------------------
    profile = ColorProfile()
    bull, bear = build_masks(image, profile)
    print(
        f"Candle-coloured pixels: green {int(bull.sum() / 255):,}  "
        f"red {int(bear.sum() / 255):,}"
    )
    if int((bull | bear).sum() / 255) < 500:
        print(
            "\nAlmost nothing matched the candle colours. This chart's palette "
            "is outside the default green/red ranges — that alone would explain "
            "every downstream failure."
        )

    # --- stage 1: the layout ----------------------------------------------
    rule("LAYOUT DETECTION")
    layout = detect_layout(image)
    print(json.dumps(layout.to_dict(), indent=2))

    if layout.chart is None:
        print("\nSTOPPED: no chart region found; nothing downstream can run.")
        return 0

    box = layout.chart
    crop = image[box.top : box.bottom, box.left : box.right]

    # --- stage 2: the candles ---------------------------------------------
    rule("CANDLE EXTRACTION (on the detected region)")
    extraction = extract_pixel_candles(crop)
    print(f"candles     : {len(extraction.candles)}")
    print(f"confidence  : {extraction.confidence:.0f}%")
    print(f"pitch       : {extraction.pitch:.1f}px")
    widths = [c.width for c in extraction.candles]
    if widths:
        print(
            f"body widths : median {np.median(widths):.0f}px  "
            f"min {min(widths)}  max {max(widths)}"
        )
    for issue in extraction.issues:
        print(f"  ! {issue}")

    crop_bull, crop_bear = build_masks(crop, profile)
    band = (crop_bull | crop_bear)
    x0, y0, x1, y1 = extraction.plot_bounds
    if x1 > x0 and y1 > y0:
        fill = float(band[y0:y1, x0:x1].any(axis=0).mean())
        print(f"column fill : {fill:.2f}  (>=0.94 means candles are touching)")

    # --- stage 3: the price scale -----------------------------------------
    rule("PRICE CALIBRATION")
    calibration = resolve_calibration(crop, None, use_ocr=True)
    print(json.dumps(calibration.to_dict(), indent=2, default=str))

    # --- stage 4: the series and its quality ------------------------------
    rule("SERIES AND DATA QUALITY")
    series = pixel_candles_to_series(
        extraction.candles,
        calibration.price_at_row,
        timeframe_seconds=layout.timeframe_seconds or 60,
        symbol=layout.asset_name or "UNKNOWN",
    )
    print(f"candles     : {len(series)}")
    if len(series):
        print(f"last price  : {series.last_price:.6f}")
        print(
            f"range       : {float(series.low.min()):.6f} .. "
            f"{float(series.high.max()):.6f}"
        )
    quality = validate_series(
        series,
        source="screen",
        recognition_confidence=min(extraction.confidence, calibration.confidence),
        expected_timeframe=layout.timeframe_seconds or 60,
    )
    print(f"usable      : {quality.ok}   confidence {quality.confidence:.0f}%")
    for issue in quality.issues:
        print(f"  ! {issue}")

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(args.out / "region.png"), crop)
        overlay = crop.copy()
        overlay[band > 0] = (0, 255, 255)
        cv2.imwrite(str(args.out / "mask.png"), overlay)
        marked = image.copy()
        cv2.rectangle(
            marked, (box.left, box.top), (box.right, box.bottom), (0, 255, 255), 3
        )
        for label, found in (("asset", layout.asset), ("timeframe", layout.timeframe)):
            if found:
                cv2.rectangle(
                    marked,
                    (found.left, found.top),
                    (found.right, found.bottom),
                    (255, 0, 255),
                    2,
                )
        cv2.imwrite(str(args.out / "detected.png"), marked)
        print(f"\nWrote region.png, mask.png and detected.png to {args.out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
