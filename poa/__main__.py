"""``python -m poa`` — start the assistant."""

from __future__ import annotations

import argparse
import sys

from .server import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="poa",
        description=(
            "Pocket Option technical-analysis assistant. Analysis and alerts "
            "only — this tool never places a trade."
        ),
    )
    parser.add_argument(
        "-c", "--config", default=None, help="path to a config YAML file"
    )
    args = parser.parse_args(argv)

    try:
        run(args.config)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
