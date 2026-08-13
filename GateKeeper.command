#!/bin/bash
# Double-click launcher for GateKeeper on macOS and Linux.
# (On macOS you may need to run: chmod +x GateKeeper.command  once.)
cd "$(dirname "$0")" || exit 1

PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
    echo "Python 3 was not found. Install it from https://www.python.org/downloads/"
    read -r -p "Press Enter to close..."
    exit 1
fi

if ! "$PY" -c "import numpy, yaml, cv2, mss, PIL" >/dev/null 2>&1; then
    echo "Setting up GateKeeper for the first time. This takes a minute..."
    "$PY" -m pip install --quiet --upgrade pip
    if ! "$PY" -m pip install --quiet -r requirements.txt; then
        echo "Could not install requirements. Check your internet connection."
        read -r -p "Press Enter to close..."
        exit 1
    fi
fi

exec "$PY" -m poa.overlay
