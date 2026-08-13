"""Screenshot storage for the journal.

Signals are far easier to review with the chart image that produced them, so
captures are saved alongside the journal row. Old images are pruned to keep the
directory from growing without bound.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..logging_setup import get_logger
from ..models import utcnow

log = get_logger(__name__)


class ScreenshotStore:
    def __init__(self, directory: str | Path, retain: int = 500) -> None:
        self.directory = Path(directory)
        self.retain = max(0, int(retain))
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            log.warning("screenshot directory unavailable (%s)", exc)

    def save(
        self, png_bytes: bytes | None, signal_id: str, when: datetime | None = None
    ) -> str | None:
        """Write a PNG and return its path, or None when there is nothing to save."""
        if not png_bytes:
            return None
        when = when or utcnow()
        name = f"{when.strftime('%Y%m%d-%H%M%S')}-{signal_id}.png"
        target = self.directory / name
        try:
            target.write_bytes(png_bytes)
        except OSError as exc:
            log.warning("could not save screenshot: %s", exc)
            return None
        self._prune()
        return str(target)

    def _prune(self) -> None:
        if self.retain <= 0:
            return
        try:
            files = sorted(
                self.directory.glob("*.png"), key=lambda p: p.stat().st_mtime
            )
        except OSError:  # pragma: no cover - filesystem dependent
            return
        excess = len(files) - self.retain
        for path in files[:excess]:
            try:
                path.unlink()
            except OSError:  # pragma: no cover
                pass
