#!/usr/bin/env python3
"""Select the chart region to analyse.

Drag a box over your Pocket Option chart. The tool prints the YAML to paste
into ``config.yaml``, and offers to calibrate the price scale by clicking two
points whose prices you can read off the chart's axis.

    python tools/select_region.py

Select the plot area only — candles plus the price axis, and none of the
platform's own buttons, order panels or the asset list. Anything else in the
region gets mistaken for candles and pushes recognition confidence down.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import cv2
    import numpy as np
except ImportError:
    sys.exit(
        "This tool needs OpenCV. Install it with:\n"
        "    pip install opencv-python"
    )

try:
    import mss
except ImportError:
    sys.exit("This tool needs mss. Install it with:\n    pip install mss")

from poa.chart_detection.candles import ColorProfile, extract_pixel_candles


def grab_screen(monitor_index: int = 1) -> tuple[np.ndarray, dict]:
    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor_index >= len(monitors):
            print(f"Monitor {monitor_index} not found; using the primary monitor.")
            monitor_index = 1 if len(monitors) > 1 else 0
        monitor = monitors[monitor_index]
        raw = sct.grab(monitor)
        frame = cv2.cvtColor(np.asarray(raw), cv2.COLOR_BGRA2BGR)
    return frame, monitor


def main() -> int:
    monitor_index = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    print("Bring your chart into view. Capturing the screen in 3 seconds…")
    cv2.waitKey(1)
    import time

    time.sleep(3)

    screen, monitor = grab_screen(monitor_index)
    print(f"Captured {screen.shape[1]}x{screen.shape[0]} from monitor {monitor_index}.")
    print("\nDrag a box around the chart, then press ENTER. Press C to cancel.")

    window = "Select your chart area — ENTER to confirm, C to cancel"
    box = cv2.selectROI(window, screen, showCrosshair=True, fromCenter=False)
    cv2.destroyWindow(window)

    x, y, width, height = (int(v) for v in box)
    if width < 50 or height < 50:
        print("Selection too small — nothing saved.")
        return 1

    region = {
        "left": monitor["left"] + x,
        "top": monitor["top"] + y,
        "width": width,
        "height": height,
    }

    crop = screen[y : y + height, x : x + width]

    # Immediate feedback: can the extractor actually read this region?
    try:
        result = extract_pixel_candles(crop, ColorProfile())
    except Exception as exc:
        print(f"\nCandle recognition failed on this region: {exc}")
        result = None

    print("\n" + "=" * 66)
    print("Paste this into config.yaml under `capture:`")
    print("=" * 66)
    print("  region:")
    for key in ("left", "top", "width", "height"):
        print(f"    {key}: {region[key]}")
    print("=" * 66)

    if result is not None:
        print(
            f"\nRecognition check: {len(result.candles)} candles found, "
            f"confidence {result.confidence:.0f}%."
        )
        for issue in result.issues:
            print(f"  - {issue}")
        if len(result.candles) < 30:
            print(
                "\n  Fewer than 30 candles were found. Widen the selection, or "
                "zoom the chart out so more candles are visible."
            )

    _offer_calibration(crop, height)
    return 0


def _offer_calibration(crop: np.ndarray, height: int) -> None:
    """Walk the user through clicking two points to calibrate the price scale."""
    print(
        "\nCalibrate the price scale? Click two points on the chart whose prices\n"
        "you can read from the price axis (two gridlines work well).\n"
        "Press Y to calibrate, any other key to skip."
    )

    preview = crop.copy()
    cv2.imshow("Chart region — press Y to calibrate, any other key to finish", preview)
    key = cv2.waitKey(0) & 0xFF
    cv2.destroyAllWindows()
    if key not in (ord("y"), ord("Y")):
        print("\nSkipped calibration. Without it, the dashboard shows a relative")
        print("price scale and reports lower data confidence.")
        return

    clicks: list[int] = []

    def on_click(event, _x, y_pos, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN and len(clicks) < 2:
            clicks.append(int(y_pos))
            cv2.line(preview, (0, y_pos), (preview.shape[1], y_pos), (0, 255, 255), 1)
            cv2.imshow("Click two known price levels", preview)

    cv2.imshow("Click two known price levels", preview)
    cv2.setMouseCallback("Click two known price levels", on_click)
    while len(clicks) < 2:
        if cv2.waitKey(50) & 0xFF == 27:  # Esc
            cv2.destroyAllWindows()
            print("Calibration cancelled.")
            return
    cv2.destroyAllWindows()

    top_row, bottom_row = sorted(clicks)
    try:
        top_price = float(input(f"Price at the upper line (row {top_row}): ").strip())
        bottom_price = float(
            input(f"Price at the lower line (row {bottom_row}): ").strip()
        )
    except ValueError:
        print("Those were not numbers — calibration skipped.")
        return

    if top_price <= bottom_price:
        print(
            "The upper line must have the higher price (price increases upward "
            "on a chart). Calibration skipped."
        )
        return

    print("\n" + "=" * 66)
    print("Add this to config.yaml under `capture:` as well")
    print("=" * 66)
    print("  calibration:")
    print("    enabled: true")
    print(f"    top_pixel: {top_row}")
    print(f"    top_price: {top_price}")
    print(f"    bottom_pixel: {bottom_row}")
    print(f"    bottom_price: {bottom_price}")
    print("=" * 66)
    print("\nThen set `capture.source: screen` and run: python run.py")


if __name__ == "__main__":
    sys.exit(main())
