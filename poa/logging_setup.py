"""Logging configuration for the assistant.

One thing here is not obvious: a packaged windowed build has **no console**.
PyInstaller sets ``sys.stderr`` to None, so a ``StreamHandler`` silently
discards everything. That is the build where a log matters most — there is no
terminal to read a traceback in — so the console handler is skipped when there
is nothing to write to, and the file handler is the one that has to work.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False
_LOG_FILE: Path | None = None

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"


def log_file() -> Path | None:
    """Where the log is being written, for telling the user."""
    return _LOG_FILE


def setup_logging(level: str = "INFO", file: str | Path | None = None) -> None:
    """Install console (and optionally rotating file) handlers exactly once."""
    global _CONFIGURED, _LOG_FILE
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    formatter = logging.Formatter(_FORMAT, datefmt="%H:%M:%S")

    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    if file:
        target = Path(file)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                target, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(formatter)
            root.addHandler(handler)
            _LOG_FILE = target
        except OSError as exc:  # pragma: no cover - depends on filesystem
            root.warning("file logging disabled (%s)", exc)

    # uvicorn is chatty about every websocket frame at INFO.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def install_crash_handlers() -> None:
    """Route every uncaught exception into the log, from any thread.

    Without this, a failure inside a Tk callback or a worker thread in a
    windowed build goes nowhere at all: Tk prints its traceback to the missing
    stderr and carries on, the window stops responding, and there is nothing
    afterwards to say why. Logging them is the difference between a bug report
    and a guess.
    """
    root = logging.getLogger()

    def on_exception(kind, value, traceback) -> None:
        if issubclass(kind, KeyboardInterrupt):  # pragma: no cover
            return
        root.critical("unhandled exception", exc_info=(kind, value, traceback))

    sys.excepthook = on_exception

    def on_thread_exception(args) -> None:
        if issubclass(args.exc_type, SystemExit):  # pragma: no cover
            return
        root.critical(
            "unhandled exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    import threading

    threading.excepthook = on_thread_exception
