# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for bms2dtx.exe  (onefile, windowed).
# ffmpeg.exe is intentionally NOT bundled (3-tier lookup: exe dir / bin -> PATH -> GUI picker).
# numpy IS bundled so offline bgm.ogg mixing is fast in the standalone exe.

from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

_ICON_FILE = Path("assets/icon.ico")
_EXE_ICON = str(_ICON_FILE) if _ICON_FILE.is_file() else None
_EXTRA_DATAS = []
_EXTRA_BINS = []
_EXTRA_HIDDEN = []

if Path("assets/icon.ico").is_file():
    _EXTRA_DATAS.append(("assets/icon.ico", "assets"))
if Path("assets/icon.png").is_file():
    _EXTRA_DATAS.append(("assets/icon.png", "assets"))

# Bundle lightweight in-process audio libraries (libsndfile, PortAudio 64-bit, soxr)
_sf_datas, _sf_bins, _sf_hi = collect_all("_soundfile_data")
_EXTRA_DATAS.extend(_sf_datas)
_EXTRA_BINS.extend(_sf_bins)
_EXTRA_HIDDEN.extend(_sf_hi)

import _sounddevice_data  # noqa: E402

_sd_dir = Path(_sounddevice_data.__file__).resolve().parent
_EXTRA_DATAS.append((str(_sd_dir / "__init__.py"), "_sounddevice_data"))
for _dll_name in ("libportaudio64bit.dll", "libportaudio64bit-asio.dll"):
    _dll_path = _sd_dir / "portaudio-binaries" / _dll_name
    if _dll_path.is_file():
        _EXTRA_BINS.append((str(_dll_path), "_sounddevice_data/portaudio-binaries"))

_soxr_datas, _soxr_bins, _soxr_hi = collect_all("soxr")
_EXTRA_DATAS.extend(_soxr_datas)
_EXTRA_BINS.extend(_soxr_bins)
_EXTRA_HIDDEN.extend(_soxr_hi)
_EXTRA_HIDDEN.extend(collect_submodules("soxr"))

EXCLUDES = [
    "matplotlib", "scipy", "pandas", "PIL", "Pillow", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "jupyter", "notebook", "pytest", "setuptools", "pip", "wheel", "sphinx",
    "numpy.testing", "numpy.f2py", "unittest.mock", "pydoc_data", "test", "tkinter.test",
]

a = Analysis(
    ["bms2dtx_gui.py"],
    pathex=["."],
    binaries=_EXTRA_BINS,
    datas=_EXTRA_DATAS,
    hiddenimports=[
        "core", "core.audio_resolver", "core.constants", "core.converter", "core.dtx_writer",
        "core.encoding", "core.mapping", "core.mixer", "core.models", "core.parser",
        "core.se_allocator", "core.timeline",
        "gui", "gui.app", "gui.auto_guess", "gui.helpers", "gui.i18n", "gui.preview_player",
        "winsound", "numpy", "soundfile", "sounddevice", "soxr", "_cffi_backend",
        *_EXTRA_HIDDEN,
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
