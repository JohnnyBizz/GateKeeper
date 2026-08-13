"""``python -m poa.overlay`` — start the overlay panel."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="poa.overlay",
        description=(
            "Always-on-top technical-analysis overlay. Analysis and alerts "
            "only — this tool never places a trade."
        ),
    )
    parser.add_argument("-c", "--config", default=None, help="path to a config YAML file")
    args = parser.parse_args(argv)

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(
            "The overlay needs Tkinter, which is not available in this Python.\n"
            "  Linux:   sudo apt install python3-tk   (or your distro's equivalent)\n"
            "  macOS:   brew install python-tk\n"
            "  Windows: re-run the Python installer and tick 'tcl/tk and IDLE'\n"
            "\nThe web dashboard needs none of this — run 'python run.py' instead.",
            file=sys.stderr,
        )
        return 1

    from .app import run

    try:
        run(args.config)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
