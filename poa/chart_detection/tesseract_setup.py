"""Finding the Tesseract binary, including one bundled inside the app.

``pytesseract`` is a thin wrapper: it shells out to a ``tesseract`` executable
and does nothing at all if that executable is not on PATH. On a machine where
the user just double-clicked GateKeeper.exe there is no PATH entry for it, so
the Windows build ships its own copy and this points the wrapper at it.

Called once at startup. Safe to call when nothing is bundled and safe to call
when Tesseract is already installed — an existing working install wins, since
it is likely newer than the vendored one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger(__name__)

try:  # pragma: no cover - optional
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None  # type: ignore[assignment]


def _bundle_root() -> Path:
    """Where the app's data files live — the unpack dir in a frozen build."""
    base = getattr(sys, "_MEIPASS", None)
    if base:  # pragma: no cover - only in a frozen build
        return Path(base)
    return Path(__file__).resolve().parents[2]


def _works() -> bool:
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return False
    return True


def configure() -> bool:
    """Make OCR usable if it can be. Returns whether it ended up working."""
    if pytesseract is None:
        return False
    if _works():
        return True

    vendored = _bundle_root() / "vendor" / "tesseract"
    binary = vendored / ("tesseract.exe" if os.name == "nt" else "tesseract")
    if not binary.exists():
        log.info(
            "Tesseract is not installed and none is bundled; the pair name, "
            "chart timeframe and price axis cannot be read from the screen"
        )
        return False

    pytesseract.pytesseract.tesseract_cmd = str(binary)
    tessdata = vendored / "tessdata"
    if tessdata.is_dir():
        # Tesseract finds its language data relative to this, not to the binary.
        os.environ.setdefault("TESSDATA_PREFIX", str(tessdata))

    if _works():
        log.info("using the bundled Tesseract at %s", binary)
        return True

    log.warning("the bundled Tesseract at %s could not be run", binary)
    return False
