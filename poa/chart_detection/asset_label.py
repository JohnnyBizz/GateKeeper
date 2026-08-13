"""Reading the traded pair's name off the screen.

The candles tell GateKeeper everything about *price*, but nothing about which
instrument it is looking at — the pair is text drawn by the platform. Without
reading it, switching charts leaves the panel labelled with the previous pair
and the journal filing new signals under the wrong name.

Deliberately conservative: a misread name is worse than no name, because it
silently splits the journal into two instruments, so anything that does not
look like an instrument symbol is rejected outright.
"""

from __future__ import annotations

import re

from .label_reader import LabelReader, LabelReading, ocr_text

# Instrument names on these platforms look like EUR/USD, EURUSD, AED/CNY OTC,
# BTC/USD, or an equity ticker. Anything else is a misread.
_SYMBOL = re.compile(
    r"^([A-Z]{3,6})\s*[/\-]?\s*([A-Z]{3,6})?(\s+OTC)?$", re.IGNORECASE
)

# OCR reliably confuses these reading short uppercase text. Currency codes
# contain no digits, so correcting them beats rejecting the whole read.
_CONFUSIONS = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B"})

# Words that appear near the pair label and must never be taken for a symbol.
_NOT_SYMBOLS = {"OTC", "TIME", "AMOUNT", "PAYOUT", "BUY", "SELL", "DEMO", "USD"}


def normalise(text: str) -> str | None:
    """Turn raw OCR output into a canonical pair name, or None if implausible."""
    cleaned = " ".join(text.split()).strip().upper()
    if not cleaned:
        return None

    cleaned = cleaned.replace("▾", "").replace("▼", "").replace("|", "").strip(" .,:")
    cleaned = cleaned.translate(_CONFUSIONS)

    match = _SYMBOL.match(cleaned)
    if not match:
        return None

    base = match.group(1).upper()
    quote = (match.group(2) or "").upper()
    otc = " OTC" if match.group(3) else ""

    if quote:
        return f"{base}/{quote}{otc}"
    if len(base) == 6:
        # A pair written without a separator, e.g. EURUSD.
        return f"{base[:3]}/{base[3:]}{otc}"
    # A bare word with no counter-currency is far more likely to be a stray
    # interface label than a single-name instrument.
    if base in _NOT_SYMBOLS:
        return None
    return f"{base}{otc}"


def read_asset_label(image) -> LabelReading[str]:
    """OCR an image of the platform's pair label."""
    raw, confidence = ocr_text(
        image, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz/-0123456789 "
    )
    if not raw:
        return LabelReading(value=None, confidence=0.0)
    return LabelReading(value=normalise(raw), confidence=confidence, raw=raw)


class AssetLabelReader(LabelReader[str]):
    """Reads the pair name badge, e.g. ``EUR/USD``."""

    def __init__(self, region: dict[str, int] | None, confirmations: int = 2) -> None:
        super().__init__(region, parse=normalise, confirmations=confirmations)


# Kept for callers that used the previous name of the reading type.
AssetReading = LabelReading
