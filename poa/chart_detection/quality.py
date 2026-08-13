"""Data validation.

Runs before any signal is generated. Its job is to make "we cannot read the
chart" and "the chart says wait" two clearly different outcomes, so a confident
signal is never produced from incomplete or nonsensical candles.
"""

from __future__ import annotations

import math
from collections import Counter

from ..models import DataQuality, Series


def validate_series(
    series: Series | None,
    *,
    min_candles: int = 60,
    source: str = "unknown",
    recognition_confidence: float = 100.0,
    timeframe_detected: bool = True,
    expected_timeframe: int | None = None,
) -> DataQuality:
    """Score how usable a candle series is, from 0 to 100.

    ``recognition_confidence`` is the vision layer's own estimate of how well
    it read the chart; it caps the final score, because clean-looking candles
    extracted from a badly-read chart are still wrong.
    """
    issues: list[str] = []

    if series is None or len(series) == 0:
        return DataQuality(
            ok=False,
            confidence=0.0,
            candle_count=0,
            issues=["No chart data is available."],
            source=source,
            timeframe_detected=False,
        )

    count = len(series)
    confidence = 100.0

    # --- 1. enough history ------------------------------------------------
    if count < min_candles:
        shortfall = 1.0 - (count / max(min_candles, 1))
        confidence -= 60.0 * shortfall
        issues.append(
            f"Only {count} candles available; {min_candles} are needed for a full read."
        )
    if count < 25:
        issues.append("Too few candles for market structure or indicators.")
        return DataQuality(
            ok=False,
            confidence=max(0.0, min(confidence, 35.0)),
            candle_count=count,
            issues=issues,
            source=source,
            timeframe_detected=timeframe_detected,
        )

    # --- 2. sane OHLC -----------------------------------------------------
    bad_ohlc = 0
    flat = 0
    for candle in series:
        if not all(
            math.isfinite(v) for v in (candle.open, candle.high, candle.low, candle.close)
        ):
            bad_ohlc += 1
            continue
        if candle.high < max(candle.open, candle.close) - 1e-12:
            bad_ohlc += 1
        elif candle.low > min(candle.open, candle.close) + 1e-12:
            bad_ohlc += 1
        if candle.range <= 0:
            flat += 1

    if bad_ohlc:
        share = bad_ohlc / count
        confidence -= min(60.0, share * 200.0)
        issues.append(f"{bad_ohlc} candles have inconsistent OHLC values.")
    if flat / count > 0.25:
        confidence -= 25.0
        issues.append(
            f"{flat} of {count} candles have no range — the chart may be frozen "
            "or partially obstructed."
        )

    # --- 3. timeframe consistency -----------------------------------------
    if count >= 3:
        gaps = [
            int(
                (series[i + 1].timestamp - series[i].timestamp).total_seconds()
            )
            for i in range(count - 1)
        ]
        gaps = [g for g in gaps if g > 0]
        if gaps:
            common, occurrences = Counter(gaps).most_common(1)[0]
            regularity = occurrences / len(gaps)
            if regularity < 0.7:
                confidence -= 20.0
                issues.append(
                    "Candle spacing is irregular; some candles may be missing."
                )
            if expected_timeframe and common != expected_timeframe:
                confidence -= 15.0
                issues.append(
                    f"Detected candle spacing is {common}s but the configured "
                    f"chart timeframe is {expected_timeframe}s."
                )
        else:
            confidence -= 25.0
            issues.append("Candle timestamps do not advance.")

    if not timeframe_detected:
        confidence -= 15.0
        issues.append("Chart timeframe could not be detected automatically.")

    # --- 4. price sanity ---------------------------------------------------
    closes = series.close
    if closes.size >= 2:
        if float(closes.max() - closes.min()) <= 0:
            confidence -= 40.0
            issues.append("Price has not moved across the whole window.")
        # A single implausible jump usually means a misread candle rather than
        # a real gap on a 60-second forex chart.
        spans = abs(closes[1:] - closes[:-1])
        typical = float(spans.mean()) if spans.size else 0.0
        if typical > 0:
            outliers = int((spans > typical * 25).sum())
            if outliers:
                confidence -= min(30.0, outliers * 10.0)
                issues.append(
                    f"{outliers} implausible price jumps detected; candles may be misread."
                )

    # --- 5. cap by recognition confidence ----------------------------------
    if recognition_confidence < 100.0:
        confidence = min(confidence, recognition_confidence)
        if recognition_confidence < 75.0:
            issues.append(
                f"Chart recognition confidence is only {recognition_confidence:.0f}%."
            )

    confidence = max(0.0, min(100.0, confidence))
    ok = confidence >= 50.0 and count >= 25 and bad_ohlc / count < 0.1

    return DataQuality(
        ok=ok,
        confidence=confidence,
        candle_count=count,
        issues=issues,
        source=source,
        timeframe_detected=timeframe_detected,
    )
