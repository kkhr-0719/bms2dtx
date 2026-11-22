"""Dual-layer persistent crash logging (%APPDATA%\\bms2dtx\\crash.log).

Layer 1: ``faulthandler.enable(file=..., all_threads=True)`` captures C-level
         segmentation faults / Access Violations before process exit.
Layer 2: ``sys.excepthook``, ``threading.excepthook``, and Tkinter's
         ``root.report_callback_exception`` record timestamped tracebacks with
         application version and optionally notify the user when the GUI is alive.
"""

from __future__ import annotations

from datetime import datetime
import faulthandler
import os
from pathlib import Path
import sys
import tempfile
import threading
import traceback
from types import TracebackType
from typing import Any, Callable, Optional, Type

from . import APP_NAME, APP_VERSION

MAX_CRASH_LOG_BYTES = 1_048_576  # 1 MiB
KEEP_CRASH_LOG_BYTES = 524_288   # Keep tail 512 KiB on truncation

_LOCK = threading.Lock()
_FAULT_STREAM: Optional[Any] = None
_ACTIVE_LOG_PATH: Optional[Path] = None
_ORIG_SYS_EXCEPTHOOK = sys.excepthook
_ORIG_THREADING_EXCEPTHOOK = getattr(threading, "excepthook", None)


def get_crash_log_path(custom_path: Optional[Path | str] = None) -> Path:
    """Return ``%APPDATA%\\bms2dtx\\crash.log`` (falling back to temp dir if unavailable)."""
    if custom_path is not None:
        return Path(custom_path)
    if _ACTIVE_LOG_PATH is not None:
        return _ACTIVE_LOG_PATH
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        candidate_dir = Path(appdata) / APP_NAME
        try:
            candidate_dir.mkdir(parents=True, exist_ok=True)
            return candidate_dir / "crash.log"
        except OSError:
            pass
    fallback_dir = Path(tempfile.gettempdir()) / APP_NAME
    try:
        fallback_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return fallback_dir / "crash.log"


def get_crash_log_dir(custom_path: Optional[Path | str] = None) -> Path:
    """Return directory containing ``crash.log``."""
    return get_crash_log_path(custom_path).parent


def _truncate_if_oversized(log_path: Path, max_bytes: int = MAX_CRASH_LOG_BYTES) -> None:
    """Truncate ``log_path`` in-place keeping the most recent tail when exceeding ``max_bytes``."""
    try:
        if not log_path.is_file():
            return
        size = log_path.stat().st_size
        if size <= max_bytes:
            return
        keep = min(KEEP_CRASH_LOG_BYTES, max(4096, max_bytes // 2))
        raw = log_path.read_bytes()
        tail = raw[-keep:]
        nl = tail.find(b"\n")
        if 0 <= nl < len(tail) - 1:
            tail = tail[nl + 1 :]
        header = (
            f"=== [truncated crash.log from {size} bytes at "
            f"{datetime.now().isoformat(timespec='seconds')}] ===\n"
        ).encode("utf-8", errors="replace")
        log_path.write_bytes(header + tail)
    except OSError:
        pass


def write_crash_entry(
    exc_type: Type[BaseException],
    exc_value: BaseException,
    exc_tb: Optional[TracebackType],
    *,
    context: str = "unhandled",
    log_path: Optional[Path | str] = None,
    max_bytes: int = MAX_CRASH_LOG_BYTES,
) -> Path:
    """Append a timestamped traceback entry to ``crash.log`` (thread-safe)."""
    target = get_crash_log_path(log_path)
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        return target
    ts = datetime.now().isoformat(timespec="seconds")
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    entry = (
        f"\n{'=' * 72}\n"
        f"[{ts}] {APP_NAME} {APP_VERSION} | context={context} | platform={sys.platform}\n"
        f"{'-' * 72}\n"
        f"{tb_text}"
    )
    with _LOCK:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _truncate_if_oversized(target, max_bytes=max_bytes)
            with open(target, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(entry)
                fh.flush()
        except OSError:
            pass
    return target


def install_crash_handlers(
    log_path: Optional[Path | str] = None,
    *,
    max_bytes: int = MAX_CRASH_LOG_BYTES,
) -> Path:
    """Enable ``faulthandler`` and install ``sys.excepthook`` / ``threading.excepthook``."""
    global _FAULT_STREAM, _ACTIVE_LOG_PATH
    target = get_crash_log_path(log_path)
    with _LOCK:
        _ACTIVE_LOG_PATH = target
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _truncate_if_oversized(target, max_bytes=max_bytes)
            if _FAULT_STREAM is not None:
                try:
                    faulthandler.disable()
                except Exception:
                    pass
                try:
                    _FAULT_STREAM.close()
                except Exception:
                    pass
                _FAULT_STREAM = None
            _FAULT_STREAM = open(target, "a", encoding="utf-8", errors="replace", buffering=1)
            faulthandler.enable(file=_FAULT_STREAM, all_threads=True)
        except Exception:
            pass

    def _sys_hook(
        exc_type: Type[BaseException],
        exc_value: BaseException,
        exc_tb: Optional[TracebackType],
    ) -> None:
        write_crash_entry(
            exc_type, exc_value, exc_tb, context="sys.excepthook", log_path=target
        )
        if _ORIG_SYS_EXCEPTHOOK is not None:
            try:
                _ORIG_SYS_EXCEPTHOOK(exc_type, exc_value, exc_tb)
            except Exception:
                pass

    def _thread_hook(args: Any) -> None:
        exc_type = getattr(args, "exc_type", Exception)
        exc_value = getattr(args, "exc_value", None)
        exc_tb = getattr(args, "exc_traceback", None)
        t_name = getattr(getattr(args, "thread", None), "name", "thread")
        if exc_value is not None:
            write_crash_entry(
                exc_type,
                exc_value,
                exc_tb,
                context=f"threading.excepthook({t_name})",
                log_path=target,
            )

    sys.excepthook = _sys_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook  # type: ignore[assignment]
    return target


def close_crash_handlers() -> None:
    """Disable faulthandler stream and close active crash log file handle (for cleanup/tests)."""
    global _FAULT_STREAM, _ACTIVE_LOG_PATH
    with _LOCK:
        _ACTIVE_LOG_PATH = None
        if _FAULT_STREAM is not None:
            try:
                faulthandler.disable()
            except Exception:
                pass
            try:
                _FAULT_STREAM.close()
            except Exception:
                pass
            _FAULT_STREAM = None


def attach_tk_crash_handler(
    root: Any,
    *,
    show_dialog: bool = True,
    log_path: Optional[Path | str] = None,
    dialog_callback: Optional[Callable[[Path, BaseException], None]] = None,
) -> None:
    """Bind ``root.report_callback_exception`` to log tracebacks and optionally alert user."""
    target = get_crash_log_path(log_path)

    def _tk_callback_exc(
        exc_type: Type[BaseException],
        exc_value: BaseException,
        exc_tb: Optional[TracebackType],
    ) -> None:
        written = write_crash_entry(
            exc_type,
            exc_value,
            exc_tb,
            context="tk.report_callback_exception",
            log_path=target,
        )
        if not show_dialog:
            return
        if dialog_callback is not None:
            try:
                dialog_callback(written, exc_value)
            except Exception:
                pass
            return
        try:
            from tkinter import messagebox
            from .i18n import tr

            if root is not None and bool(root.winfo_exists()):
                messagebox.showerror(
                    tr("dialog_crash_logged_title"),
                    tr("dialog_crash_logged_body", exc=exc_value, path=written),
                    parent=root,
                )
        except Exception:
            pass

    try:
        root.report_callback_exception = _tk_callback_exc
    except Exception:
        pass
