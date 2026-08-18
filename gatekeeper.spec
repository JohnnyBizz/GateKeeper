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
    # PIL.ImageTk is imported inside a function, so that a headless run never
    # pays for it and an install without tkinter still starts. A bundler that
    # trusts top-level imports would leave it out — which the overlay would
    # discover on its very first repaint, as a crash on the user's machine and
    # nowhere else. Named here so that cannot happen.
    hiddenimports=(
        collect_submodules("poa") + ["PIL.ImageTk", "PIL._tkinter_finder"]
    ),
    hookspath=[],
    runtime_hooks=[],
    # Trim the parts of the scientific stack that are never imported; without
    # this the bundle roughly doubles in size for no benefit.
    excludes=["matplotlib", "scipy", "pandas.tests", "notebook", "IPython"],
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
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # No console window: this is a desktop app, not a script.
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
