# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Phantom Lyrics
====================================
Build a standalone Windows .exe with:

    pip install pyinstaller
    pyinstaller phantom_lyrics.spec

The resulting executable is in dist/PhantomLyrics/ (folder mode, recommended
for PySide6 — onefile mode has slower startup and occasional plugin issues).

Run it directly:

    dist/PhantomLyrics/PhantomLyrics.exe
"""

import sys
from PyInstaller.utils.hooks import collect_submodules

# PySide6 has many Qt plugins (platforms, styles, image formats) that must
# be collected explicitly so the packaged app can create a QApplication.
hiddenimports = []
hiddenimports += collect_submodules("pynput") if "pynput" in sys.modules else []
hiddenimports += ["websockets"]

datas = []
binaries = []

a = Analysis(
    ["phantom_lyrics.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Only exclude truly safe, heavy modules we know aren't needed.
        # Many stdlib modules have non-obvious dependencies (e.g. websockets
        # → importlib.metadata → email; shiboken6 → argparse), so be conservative.
        "tkinter",
        "unittest",
        "pydoc",
        "pdb",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhantomLyrics",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # No terminal window — tray icon is the UI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # Uses the bundled Qt icon; add an .ico path here if desired
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PhantomLyrics",
)
