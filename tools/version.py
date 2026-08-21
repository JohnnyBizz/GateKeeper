#!/usr/bin/env python3
"""The version the executables carry, read from the resource they carry it in.

    python tools/version.py        ->  1.0.0

There is one version in this project and it lives in
``packaging/gatekeeper_version.txt``, because that is the copy that ends up
inside the .exe where a user can right-click and read it. Anything else naming
a version would be a second answer to the same question, free to disagree with
the file that ships.

Windows version resources are four-part by requirement; the release tag is the
first three, which is the number people say out loud. ``1.0.0.0`` is ``v1.0.0``.

The resource is Python that PyInstaller ``eval()``s, but importing its
``versioninfo`` module needs ``win32api`` and therefore Windows. The classes
are stubbed instead, so this reads the same on the Linux runner that publishes
the release as on the Windows one that built the file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESOURCE = ROOT / "packaging" / "gatekeeper_version.txt"

#: Constructor signatures from PyInstaller's ``versioninfo``. Only enough to
#: evaluate the file — the fields are read back out of StringStruct.
_SIGNATURES = {
    "VSVersionInfo": ("ffi", "kids"),
    "FixedFileInfo": (
        "filevers", "prodvers", "mask", "flags", "OS", "fileType",
        "subtype", "date",
    ),
    "StringFileInfo": ("kids",),
    "StringTable": ("name", "kids"),
    "StringStruct": ("name", "val"),
    "VarFileInfo": ("kids",),
    "VarStruct": ("name", "kids"),
}


def _fields(path: Path) -> dict[str, str]:
    """Every StringStruct name/value pair in a version resource."""
    found: dict[str, str] = {}

    def make(parameters):
        def build(*args, **kwargs):
            bound = dict(zip(parameters, args))
            bound.update(kwargs)
            if parameters == _SIGNATURES["StringStruct"]:
                found[bound["name"]] = bound["val"]
            return bound
        return build

    namespace = {name: make(params) for name, params in _SIGNATURES.items()}
    eval(compile(path.read_text(encoding="utf-8"), str(path), "eval"), namespace)
    return found


def product_version(path: Path | None = None) -> str:
    """The three-part version, e.g. ``1.0.0``.

    Raises rather than guessing: a release tagged from a version this could
    not read would be worse than a build that stopped and said so.
    """
    resource = path or RESOURCE
    raw = _fields(resource).get("ProductVersion")
    if not raw:
        raise ValueError(f"{resource} does not set ProductVersion")

    parts = raw.strip().split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise ValueError(
            f"{resource} has ProductVersion {raw!r}; expected four numbers"
        )
    return ".".join(parts[:3])


def main() -> int:
    print(product_version())
    return 0


if __name__ == "__main__":
    sys.exit(main())
