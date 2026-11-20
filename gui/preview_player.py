"""Single-slice audio preview for the bms2dtx GUI.

Design (see Phase 3 ruling):
* One audio slice only (no timeline mixing).
* Decode with ffmpeg into a real temp ``.wav`` (16-bit / 44.1 kHz / stereo PCM)
  and play it with ``winsound.PlaySound(path, SND_FILENAME | SND_ASYNC)``.
  ``SND_MEMORY | SND_ASYNC`` is NEVER used (it raises RuntimeError).
* Decoding runs in a background thread; a generation counter discards stale
  decodes after rapid clicks.
* ``PlaySound(None, SND_PURGE)`` is always issued before switching/stopping.
* Two rotating temp files (``slot_0.wav`` / ``slot_1.wav``) avoid Windows file
  lock races.
* Without ffmpeg, a ``.wav`` source can still be played directly.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from .i18n import tr

try:  # pragma: no cover - platform dependent
    import winsound as _winsound
except ImportError:  # non-Windows
    _winsound = None  # type: ignore[assignment]

StatusCallback = Callable[[str], None]
FfmpegGetter = Callable[[], Optional[str]]
Decoder = Callable[[str, Path, Path], None]

MAX_PREVIEW_SECONDS = 180


def decode_with_ffmpeg(ffmpeg: str, src: Path, dst: Path) -> None:
    """Decode ``src`` to 16-bit 44.1 kHz stereo PCM wav at ``dst`` (raises on failure)."""
    cmd = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-t",
        str(MAX_PREVIEW_SECONDS),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "44100",
        "-ac",
        "2",
        str(dst),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        timeout=60,
    )
    if proc.returncode != 0 or not dst.is_file():
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError(err[-1] if err else f"ffmpeg exit code {proc.returncode}")


class PreviewPlayer:
    """Thread-safe single-slice preview player."""

    def __init__(
        self,
        ffmpeg_getter: FfmpegGetter,
        on_status: Optional[StatusCallback] = None,
        *,
        winsound_module=_winsound,
        decoder: Optional[Decoder] = None,
        temp_root: Optional[Path] = None,
    ) -> None:
        self._ffmpeg_getter = ffmpeg_getter
        self._on_status = on_status or (lambda _msg: None)
        self._ws = winsound_module
        self._decoder = decoder or decode_with_ffmpeg
        self._lock = threading.Lock()
        self._generation = 0
        self._slot = 0
        self._temp_dir: Optional[Path] = None
        self._temp_root = temp_root
        self._closed = False
        self._last_thread: Optional[threading.Thread] = None
        atexit.register(self.shutdown)

    # ------------------------------------------------------------------ helpers
    @property
    def available(self) -> bool:
        return self._ws is not None

    @property
    def temp_dir(self) -> Optional[Path]:
        return self._temp_dir

    def _status(self, msg: str) -> None:
        try:
            self._on_status(msg)
        except Exception:  # pragma: no cover - UI callback must never kill the thread
            pass

    def _purge(self) -> None:
        if self._ws is None:
            return
        try:
            self._ws.PlaySound(None, self._ws.SND_PURGE)
        except Exception:
            pass

    def _ensure_temp_dir(self) -> Path:
        if self._temp_dir is None or not self._temp_dir.is_dir():
            self._temp_dir = Path(
                tempfile.mkdtemp(
                    prefix="bms2dtx_preview_",
                    dir=str(self._temp_root) if self._temp_root else None,
                )
            )
        return self._temp_dir

    # --------------------------------------------------------------------- API
    def play(self, src: Path | str) -> bool:
        """Start previewing ``src``; returns False if it cannot even be attempted."""
        src_path = Path(src)
        if self._closed:
            return False
        if self._ws is None:
            self._status(tr("preview_no_winsound"))
            return False
        if not src_path.is_file():
            self._status(tr("preview_file_missing", name=src_path.name))
            return False

        with self._lock:
            self._generation += 1
            gen = self._generation
        self._purge()

        ffmpeg = self._ffmpeg_getter()
        if not ffmpeg:
            if src_path.suffix.lower() == ".wav":
                self._play_file(src_path, gen, direct=True)
                return True
            self._status(tr("preview_no_ffmpeg"))
            return False

        with self._lock:
            slot = self._slot
            self._slot = 1 - self._slot
        self._status(tr("preview_decoding", name=src_path.name))
        thread = threading.Thread(
            target=self._decode_and_play,
            args=(ffmpeg, src_path, slot, gen),
            name="bms2dtx-preview",
            daemon=True,
        )
        thread.start()
        self._last_thread = thread
        return True

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
        self._purge()
        self._status(tr("preview_stopped"))

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self._generation += 1
        self._purge()
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
        try:
            atexit.unregister(self.shutdown)
        except Exception:  # pragma: no cover
            pass

    # ---------------------------------------------------------------- internals
    def _is_current(self, gen: int) -> bool:
        with self._lock:
            return gen == self._generation and not self._closed

    def _decode_and_play(self, ffmpeg: str, src: Path, slot: int, gen: int) -> None:
        try:
            tmp_dir = self._ensure_temp_dir()
            dst = tmp_dir / f"slot_{slot}.wav"
            # Make sure nothing still holds the slot file before overwriting it.
            self._purge()
            self._decoder(ffmpeg, src, dst)
        except Exception as exc:
            if self._is_current(gen):
                if src.suffix.lower() == ".wav":
                    self._play_file(src, gen, direct=True)
                else:
                    self._status(tr("preview_decode_failed", exc=exc))
            return
        if not self._is_current(gen):
            return  # stale decode: a newer click / stop superseded it
        self._play_file(dst, gen, direct=False, label=src.name)

    def _play_file(self, path: Path, gen: int, *, direct: bool, label: Optional[str] = None) -> None:
        if not self._is_current(gen) or self._ws is None:
            return
        try:
            self._ws.PlaySound(str(path), self._ws.SND_FILENAME | self._ws.SND_ASYNC)
            self._status(tr("preview_playing", name=label or path.name))
        except Exception as exc:  # RuntimeError for unsupported encodings, etc.
            self._status(tr("preview_play_failed", name=path.name, exc=exc))
