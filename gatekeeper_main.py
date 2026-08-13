#!/usr/bin/env python3
"""Entry point for the packaged GateKeeper application.

Kept separate from ``overlay.py`` because a frozen build has no console: any
error before the window opens would vanish silently, so this shows it in a
dialog instead.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

# A PyInstaller bundle unpacks to a temporary directory; make sure that is on
# the path so `poa` imports the bundled copy.
if getattr(sys, "frozen", False):  # pragma: no cover - only in a frozen build
    sys.path.insert(0, str(Path(sys._MEIPASS)))  # type: ignore[attr-defined]


def _show_error(message: str) -> None:
    """Surface a startup failure, since a windowed build has no console."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("GateKeeper could not start", message)
        root.destroy()
    except Exception:  # pragma: no cover - last resort
        print(message, file=sys.stderr)


def main() -> int:
    try:
        from poa.config import ensure_config_file
        from poa.overlay.app import run

        ensure_config_file()
        run()
        return 0
    except Exception:
        _show_error(
            "GateKeeper hit an error while starting.\n\n"
            + traceback.format_exc(limit=6)
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
