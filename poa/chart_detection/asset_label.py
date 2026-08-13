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

# What can legitimately sit on either side of the slash. Without this list any
# two adjacent words on screen can be welded into an instrument: the platform's
# own "Profile" menu item, read as the tokens PROF and ILE, becomes the pair
# PROF/ILE, and the app then confidently labels the chart with it and files
# journal entries under an instrument that does not exist.
_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY", "CNH",
    "HKD", "SGD", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RUB", "TRY",
    "ZAR", "MXN", "BRL", "ARS", "CLP", "COP", "PEN", "INR", "IDR", "MYR",
    "PHP", "THB", "VND", "KRW", "TWD", "ILS", "AED", "SAR", "QAR", "KWD",
    "BHD", "OMR", "JOD", "EGP", "NGN", "KES", "GHS", "MAD", "TND", "DZD",
    "UAH", "RON", "BGN", "HRK", "ISK", "PKR", "BDT", "LKR", "NPR",
}
_CRYPTO = {
    "BTC", "ETH", "LTC", "XRP", "BCH", "ADA", "DOT", "SOL", "BNB", "DOGE",
    "TRX", "AVAX", "LINK", "MATIC", "XLM", "ETC", "USDT", "USDC", "TON",
    "SHIB", "PEPE", "NEAR", "ATOM", "UNI", "FIL", "APT", "ARB", "OP",
}
_COMMODITIES = {"XAU", "XAG", "XPT", "XPD", "GOLD", "SILVER", "OIL", "BRENT", "WTI", "GAS"}

KNOWN_CODES = frozenset(_CURRENCIES | _CRYPTO | _COMMODITIES)


def is_known_pair(name: str | None) -> bool:
    """Whether both halves of ``name`` are codes an instrument is made of.

    Used where a symbol is being *discovered* rather than read from a box the
    user pointed at — searching a whole screen turns up plenty of text that
    parses as a pair and is not one.
    """
    if not name or "/" not in name:
        return False
    base, _, rest = name.partition("/")
    quote = rest.replace(" OTC", "").strip()
    return base.strip() in KNOWN_CODES and quote in KNOWN_CODES


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

    # OCR drops the space before the OTC suffix often enough that "CAD/JPYOTC"
    # is a normal read. Left alone it becomes an instrument named JPYOTC, which
    # then files journal entries under a pair that does not exist.
    if not otc and quote.endswith("OTC") and len(quote) > 3:
        quote, otc = quote[:-3], " OTC"
    elif not otc and not quote and base.endswith("OTC") and len(base) == 6:
        base, otc = base[:-3], " OTC"

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
