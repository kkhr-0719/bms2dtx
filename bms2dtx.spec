# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for bms2dtx.exe  (onefile, windowed).
# ffmpeg.exe is intentionally NOT bundled (3-tier lookup: exe dir / bin -> PATH -> GUI picker).
# numpy IS bundled so offline bgm.ogg mixing is fast in the standalone exe.

from pathlib import Path

_ICON_FILE = Path("assets/icon.ico")
_EXE_ICON = str(_ICON_FILE) if _ICON_FILE.is_file() else None
_EXTRA_DATAS = []
if Path("assets/icon.ico").is_file():
    _EXTRA_DATAS.append(("assets/icon.ico", "assets"))
if Path("assets/icon.png").is_file():
    _EXTRA_DATAS.append(("assets/icon.png", "assets"))

EXCLUDES = [
    "matplotlib", "scipy", "pandas", "PIL", "Pillow", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "jupyter", "notebook", "pytest", "setuptools", "pip", "wheel", "sphinx",
    "numpy.testing", "numpy.f2py", "unittest.mock", "pydoc_data", "test", "tkinter.test",
]

a = Analysis(
    ["bms2dtx_gui.py"],
    pathex=["."],
    binaries=[],
    datas=_EXTRA_DATAS,
    hiddenimports=[
        "core", "core.audio_resolver", "core.constants", "core.converter", "core.dtx_writer",
        "core.encoding", "core.mapping", "core.mixer", "core.models", "core.parser",
        "core.se_allocator", "core.timeline",
        "gui", "gui.app", "gui.auto_guess", "gui.helpers", "gui.i18n", "gui.preview_player",
        "winsound", "numpy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="bms2dtx",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,  # --windowed
    disable_windowed_traceback=False,
    icon=_EXE_ICON,
)
