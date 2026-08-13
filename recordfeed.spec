# PyInstaller spec for the feed recorder — a small console tool, separate from
# the main app on purpose. It runs once, prints what the platform's socket is
# sending, and is thrown away as soon as that format is known.

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
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # A console, deliberately: the whole output of this tool is text to read.
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
