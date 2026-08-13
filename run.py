#!/usr/bin/env python3
"""Start the assistant.

    python run.py                  # use config.yaml, or config.example.yaml
    python run.py -c my-config.yaml
"""

import sys

from poa.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
