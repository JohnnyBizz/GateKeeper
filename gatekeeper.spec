# PyInstaller build spec — produces a single standalone GateKeeper executable
# that needs no Python installed on the target machine.
#
#   pip install pyinstaller
#   pyinstaller gatekeeper.spec
#
# The result lands in dist/GateKeeper.exe (Windows) or dist/GateKeeper.

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Tesseract, when the build vendored it. Without OCR the app still reads
# candles, but not the pair name, the timeframe badge or the price axis — on a
# machine with nothing installed it would come up half blind. Kept optional so
# a plain local `pyinstaller gatekeeper.spec` still builds.
tesseract_datas = []
if os.path.isdir("vendor/tesseract"):
    tesseract_datas.append(("vendor/tesseract", "vendor/tesseract"))

analysis = Analysis(
    ["gatekeeper_main.py"],
    pathex=["."],
    binaries=[],
    # The dashboard assets and the sample config are read at runtime, so they
    # have to travel inside the bundle rather than being left on disk.
    datas=[
        ("poa/dashboard", "poa/dashboard"),
        ("config.example.yaml", "."),
        ("data/sample_eurusd_m1.csv", "data"),
    ] + tesseract_datas,
    # Every poa module, less the dashboard: collecting them all is what finds
    # the ones imported inside functions, but it would also drag the web
    # server in, and this executable has no way to start it.
    #
    # PIL.ImageTk is named for the opposite reason. It too is imported inside
    # a function — so a headless run never pays for it, and an install without
    # tkinter still starts — but a bundler trusting top-level imports would
    # leave it out, and the overlay would discover that on its very first
    # repaint, as a crash on the user's machine and nowhere else.
    hiddenimports=(
        [m for m in collect_submodules("poa") if not m.startswith("poa.server")]
        + ["PIL.ImageTk", "PIL._tkinter_finder"]
    ),
    hookspath=[],
    runtime_hooks=[],
    # Everything the packaged app cannot reach. It is the overlay: the
    # dashboard has its own entry point that a double-clicked executable never
    # runs, so the whole web stack rode along unused — as did pandas, which
    # nothing in the project imports at all. Without this the bundle roughly
    # doubles for no benefit, on a file downloaded by hand after every change.
    excludes=[
        "matplotlib", "scipy", "notebook", "IPython",
        "pandas", "fastapi", "uvicorn", "starlette", "httpx", "httpcore",
        "poa.server",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="GateKeeper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Deliberately off. Compressing the executable saves a few megabytes on a
    # file that is downloaded once, and costs the download itself: a binary
    # that has to unpack itself in memory before it runs is the classic shape
    # of something hiding what it does, and browsers and antivirus refuse it
    # on that shape alone. RecordFeed.exe was refused outright.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # A name, a publisher and a version, so this is not an anonymous binary.
    version="packaging/gatekeeper_version.txt",
    # No console window: this is a desktop app, not a script.
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
