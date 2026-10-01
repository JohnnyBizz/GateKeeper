"""The market scanner — specification section 20.

Reads many symbols, analyses each one, and reports what it found and
what it could not look at. It holds no broker and no runtime, so it is
structurally incapable of placing an order; CI asserts that.
"""

from gtcc.scanner.engine import (
    ScanResult,
    ScanRow,
    ScanSettings,
    ScanStatus,
    Scanner,
    SortKey,
)

__all__ = [
    "ScanResult",
    "ScanRow",
    "ScanSettings",
    "ScanStatus",
    "Scanner",
    "SortKey",
]
