#!/usr/bin/env python3
"""Start the always-on-top overlay panel.

    python overlay.py                  # use config.yaml, or config.example.yaml
    python overlay.py -c my-config.yaml

Drag it by its header to sit beside your chart. For the browser dashboard
instead, run `python run.py`.
"""

import sys

from poa.overlay.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
