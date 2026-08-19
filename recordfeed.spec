# PyInstaller spec for the feed recorder — a small console tool that records
# what the platform's socket is sending.
#
# GateKeeper itself does this now, from the RECORD button on the panel, using
# the browser it is already attached to. That is the path that matters: this
# executable is the one Windows refused to download, and a capability behind a
# refused download is a capability nobody has. What is left here is the
# scriptable way in — a capture without the app, or with flags.

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

analysis = Analysis(
    ["tools/record_feed.py"],
    pathex=["."],
    binaries=[],
    datas=[("config.example.yaml", ".")],
    hiddenimports=collect_submodules("poa.feed") + ["websockets"],
    hookspath=[],
    runtime_hooks=[],
    # None of the analysis stack is needed to listen to a socket.
    excludes=[
        "matplotlib", "scipy", "pandas", "notebook", "IPython", "cv2",
        "pytesseract", "PIL", "fastapi", "uvicorn", "mss", "tkinter",
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
    name="RecordFeed",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Off on purpose — see gatekeeper.spec. A self-unpacking executable is the
    # shape a download check refuses, and this is the file that was refused:
    # "RecordFeed.exe — couldn't download, virus detected".
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # A name, a publisher and a version, so this is not an anonymous binary.
    version="packaging/recordfeed_version.txt",
    # A console, deliberately: the whole output of this tool is text to read.
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
