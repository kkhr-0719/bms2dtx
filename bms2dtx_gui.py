"""bms2dtx GUI launcher (also the PyInstaller entry point for ``bms2dtx.exe``).

Usage:
    bms2dtx_gui.py [chart.bms|chart.bme]   open the GUI (optionally auto-load a chart)
    bms2dtx_gui.py --version               print version and exit
    bms2dtx_gui.py --smoke-test [OUTFILE]  headless import check (core, numpy, tkinter, gui)
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

os.environ.setdefault("PATH", "")


def _emit(text: str, outfile: Optional[str] = None) -> None:
    if outfile:
        try:
            with open(outfile, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass
    stream = getattr(sys, "stdout", None)  # None in --windowed PyInstaller builds
    if stream is not None:
        try:
            print(text, file=stream)
        except Exception:
            pass


def smoke_test(outfile: Optional[str] = None) -> int:
    lines: List[str] = []
    ok = True
    try:
        import numpy

        lines.append(f"numpy {numpy.__version__}")
        import soundfile

        lines.append(f"soundfile {soundfile.__version__}")
        import sounddevice

        lines.append(f"sounddevice {sounddevice.__version__}")
        import soxr

        lines.append(f"soxr {soxr.__version__}")
        import tkinter

        import _tkinter  # noqa: F401

        lines.append(f"tkinter TkVersion {tkinter.TkVersion}")
        import tkinterdnd2  # noqa: F401
        from tkinterdnd2 import DND_FILES, TkinterDnD

        root = TkinterDnD.Tk()
        root.withdraw()
        root.drop_target_register(DND_FILES)
        tkdnd_ver = getattr(root, "TkdndVersion", "ok")
        root.destroy()
        lines.append(f"tkdnd OK ({tkdnd_ver})")
        import core

        lines.append(f"core tabs={len(core.TAB_SPECS)}")
        import gui
        import gui.app  # noqa: F401
        import gui.auto_guess  # noqa: F401
        import gui.crash_log  # noqa: F401
        import gui.helpers  # noqa: F401
        import gui.i18n  # noqa: F401
        import gui.preview_player  # noqa: F401

        lines.append(f"gui {gui.APP_VERSION}")
        try:
            lines.append(f"ffmpeg {core.find_ffmpeg()}")
        except core.FfmpegNotFoundError:
            lines.append("ffmpeg not found (optional)")
        assert len(core.TAB_SPECS) == 15
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(f"FAIL {type(exc).__name__}: {exc}")
    lines.append("SMOKE OK" if ok else "SMOKE FAILED")
    _emit("\n".join(lines), outfile)
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        from gui import APP_NAME, APP_VERSION

        _emit(f"{APP_NAME} {APP_VERSION}")
        return 0
    if "--smoke-test" in args:
        idx = args.index("--smoke-test")
        outfile = args[idx + 1] if idx + 1 < len(args) and not args[idx + 1].startswith("-") else None
        return smoke_test(outfile)

    from gui.crash_log import install_crash_handlers
    from gui.app import run

    install_crash_handlers()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
