"""Single-slice audio preview for the bms2dtx GUI.

Design (v0.4.0):
* One audio slice only (no timeline mixing).
* In-process decoding via ``soundfile`` (libsndfile: .wav/.ogg/.flac/.mp3) with
  in-memory NumPy L/R swap (``arr[:, [1, 0]]``) and zero temp files.
* Direct playback via ``sounddevice`` (PortAudio) explicitly preferring the
  ``Windows WASAPI`` Host API default output device in Shared mode
  (``WasapiSettings(exclusive=False)``) so other apps are never locked out.
* Optional ``ffmpeg`` fallback only when ``libsndfile`` cannot decode an exotic
  container (e.g. .m4a / .wma).
* Thread-safe generation counter discards stale decodes on rapid clicks/keys.
"""

from __future__ import annotations

import atexit
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from .i18n import tr

try:  # pragma: no cover - platform dependent
    import winsound as _winsound
except ImportError:  # non-Windows
    _winsound = None  # type: ignore[assignment]

try:  # pragma: no cover - optional in pure stdlib test env
    import numpy as _np  # type: ignore
except ImportError:
    _np = None  # type: ignore[assignment]

try:  # pragma: no cover
    import soundfile as _sf  # type: ignore
except ImportError:
    _sf = None  # type: ignore[assignment]

try:  # pragma: no cover
    import soxr as _soxr  # type: ignore
except ImportError:
    _soxr = None  # type: ignore[assignment]

try:  # pragma: no cover
    os.environ.setdefault("PATH", "")
    import sounddevice as _sd  # type: ignore
except Exception:
    _sd = None  # type: ignore[assignment]

StatusCallback = Callable[[str], None]
FfmpegGetter = Callable[[], Optional[str]]
SwapLrGetter = Callable[[], bool]
Decoder = Callable[..., None]

MAX_PREVIEW_SECONDS = 180


def _select_wasapi_shared_device(sd_mod: Any) -> Tuple[Optional[int], Any]:
    """Select Windows WASAPI default output device in Shared mode (exclusive=False).

    Falls back to the PortAudio default output device when WASAPI is unavailable.
    Raises RuntimeError if no output audio device exists at all.
    """
    hostapis = sd_mod.query_hostapis()
    for host in hostapis:
        name = str(host.get("name", "")).lower()
        if "wasapi" in name:
            dev_idx = int(host.get("default_output_device", -1))
            if dev_idx >= 0:
                dev_info = sd_mod.query_devices(dev_idx)
                if int(dev_info.get("max_output_channels", 0)) > 0:
                    extra = None
                    wasapi_cls = getattr(sd_mod, "WasapiSettings", None)
                    if wasapi_cls is not None:
                        try:
                            extra = wasapi_cls(exclusive=False, auto_convert=True)
                        except TypeError:
                            try:
                                extra = wasapi_cls(exclusive=False)
                            except Exception:
                                extra = None
                        except Exception:
                            extra = None
                    return dev_idx, extra

    # Fallback to system default output device (non-WASAPI or non-Windows)
    try:
        default_dev = sd_mod.default.device
        out_idx = int(default_dev[1]) if isinstance(default_dev, (list, tuple)) else int(default_dev)
    except Exception:
        out_idx = -1
    if out_idx >= 0:
        dev_info = sd_mod.query_devices(out_idx)
        if int(dev_info.get("max_output_channels", 0)) > 0:
            return out_idx, None

    # Last scan for any output-capable device
    for idx, dev in enumerate(sd_mod.query_devices()):
        if int(dev.get("max_output_channels", 0)) > 0:
            return idx, None

    raise RuntimeError("No audio output device available")


def _adapt_buffer_for_device(
    arr: Any,
    sr: int,
    dev_info: Any,
    *,
    np_mod: Any,
    soxr_mod: Any = _soxr,
) -> Tuple[Any, int]:
    """Fallback adapter: expand mono to stereo and resample to device default_samplerate."""
    if np_mod is None or arr is None or len(arr) == 0:
        return arr, sr
    out = arr
    max_ch = int((dev_info.get("max_output_channels", 0) if isinstance(dev_info, dict) else 0) or 0)
    if max_ch >= 2 and getattr(out, "ndim", 0) == 2 and out.shape[1] == 1:
        out = np_mod.ascontiguousarray(np_mod.repeat(out, 2, axis=1), dtype=np_mod.float32)

    raw_dev_sr = dev_info.get("default_samplerate", 0) if isinstance(dev_info, dict) else 0
    target_sr = int(round(float(raw_dev_sr or 0)))
    if target_sr <= 0 or target_sr == sr:
        return out, sr

    if soxr_mod is not None:
        try:
            resampled = soxr_mod.resample(out, sr, target_sr, quality="HQ")
            return np_mod.ascontiguousarray(resampled, dtype=np_mod.float32), target_sr
        except Exception:
            pass

    out_len = max(1, int(round(len(out) * (target_sr / float(sr)))))
    if len(out) == 1:
        return np_mod.repeat(out.astype(np_mod.float32, copy=False), out_len, axis=0), target_sr
    x_old = np_mod.linspace(0.0, 1.0, num=len(out), endpoint=False)
    x_new = np_mod.linspace(0.0, 1.0, num=out_len, endpoint=False)
    cols = [np_mod.interp(x_new, x_old, out[:, ch]) for ch in range(out.shape[1])]
    return np_mod.ascontiguousarray(np_mod.column_stack(cols), dtype=np_mod.float32), target_sr


def _decode_with_soundfile(
    src: Path,
    *,
    swap_lr: bool,
    sf_mod: Any,
    np_mod: Any,
) -> Tuple[Any, int]:
    """Decode ``src`` in-process via soundfile and optionally swap stereo L/R."""
    if sf_mod is None or np_mod is None:
        raise RuntimeError("soundfile/numpy unavailable")
    data, sr = sf_mod.read(str(src), dtype="float32", always_2d=True)
    sr_int = int(sr)
    if sr_int <= 0:
        raise RuntimeError(f"Invalid sample rate: {sr_int}")
    max_frames = int(MAX_PREVIEW_SECONDS * sr_int)
    if max_frames > 0 and len(data) > max_frames:
        data = data[:max_frames]
    if len(data) > 0 and not np_mod.all(np_mod.isfinite(data)):
        data = np_mod.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    if swap_lr and data.shape[1] >= 2:
        data = np_mod.ascontiguousarray(data[:, :2][:, [1, 0]], dtype=np_mod.float32)
    elif data.shape[1] > 2:
        data = np_mod.ascontiguousarray(data[:, :2], dtype=np_mod.float32)
    else:
        data = np_mod.ascontiguousarray(data, dtype=np_mod.float32)
    return data, sr_int


def decode_with_ffmpeg(
    ffmpeg: str, src: Path, dst: Path, *, swap_lr: bool = False
) -> None:
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
    ]
    if swap_lr:
        cmd.extend(["-af", "aformat=channel_layouts=stereo,pan=stereo|c0=c1|c1=c0"])
    cmd.extend([
        "-acodec",
        "pcm_s16le",
        "-ar",
        "44100",
        "-ac",
        "2",
        str(dst),
    ])
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
    """Thread-safe single-slice preview player (soundfile + WASAPI Shared, ffmpeg fallback)."""

    def __init__(
        self,
        ffmpeg_getter: FfmpegGetter,
        on_status: Optional[StatusCallback] = None,
        *,
        winsound_module=_winsound,
        decoder: Optional[Decoder] = None,
        temp_root: Optional[Path] = None,
        swap_lr_getter: Optional[SwapLrGetter] = None,
        sounddevice_module: Any = None,
        soundfile_module: Any = None,
        numpy_module: Any = None,
    ) -> None:
        self._ffmpeg_getter = ffmpeg_getter
        self._on_status = on_status or (lambda _msg: None)
        self._ws = winsound_module
        self._sd = _sd if sounddevice_module is None else sounddevice_module
        self._sf = _sf if soundfile_module is None else soundfile_module
        self._np = _np if numpy_module is None else numpy_module
        self._use_legacy_decoder = decoder is not None
        self._decoder = decoder or decode_with_ffmpeg
        try:
            self._decoder_accepts_swap_lr = "swap_lr" in inspect.signature(self._decoder).parameters
        except (TypeError, ValueError):
            self._decoder_accepts_swap_lr = False
        self._swap_lr_getter = swap_lr_getter or (lambda: False)
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
        return self._sd is not None or self._ws is not None

    @property
    def temp_dir(self) -> Optional[Path]:
        return self._temp_dir

    def _status(self, msg: str) -> None:
        try:
            self._on_status(msg)
        except Exception:  # pragma: no cover - UI callback must never kill the thread
            pass

    def _purge(self) -> None:
        if self._sd is not None:
            try:
                self._sd.stop()
            except Exception:
                pass
        if self._ws is not None:
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
        if not self.available:
            self._status(tr("preview_no_winsound"))
            return False
        if not src_path.is_file():
            self._status(tr("preview_file_missing", name=src_path.name))
            return False

        with self._lock:
            self._generation += 1
            gen = self._generation
        self._purge()

        swap_lr = bool(self._swap_lr_getter())

        if self._use_legacy_decoder:
            ffmpeg = self._ffmpeg_getter()
            if not ffmpeg:
                if src_path.suffix.lower() == ".wav" and not swap_lr and self._ws is not None:
                    self._play_file(src_path, gen, direct=True)
                    return True
                self._status(tr("preview_no_ffmpeg"))
                return False
            with self._lock:
                slot = self._slot
                self._slot = 1 - self._slot
            self._status(tr("preview_decoding", name=src_path.name))
            thread = threading.Thread(
                target=self._decode_and_play_legacy,
                args=(ffmpeg, src_path, slot, gen, swap_lr),
                name="bms2dtx-preview",
                daemon=True,
            )
            thread.start()
            self._last_thread = thread
            return True

        with self._lock:
            slot = self._slot
            self._slot = 1 - self._slot
        self._status(tr("preview_decoding", name=src_path.name))
        thread = threading.Thread(
            target=self._decode_and_play_inprocess,
            args=(src_path, slot, gen, swap_lr),
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

    def _decode_and_play_inprocess(
        self, src: Path, slot: int, gen: int, swap_lr: bool = False
    ) -> None:
        arr: Any = None
        sr: int = 44100
        sf_exc: Optional[Exception] = None
        try:
            arr, sr = _decode_with_soundfile(
                src, swap_lr=swap_lr, sf_mod=self._sf, np_mod=self._np
            )
        except Exception as exc:
            sf_exc = exc

        if not self._is_current(gen):
            return

        # Fallback to ffmpeg only when soundfile cannot handle an exotic format
        if arr is None:
            ffmpeg = self._ffmpeg_getter()
            if not ffmpeg:
                if src.suffix.lower() == ".wav" and not swap_lr and self._ws is not None:
                    self._play_file(src, gen, direct=True)
                    return
                if sf_exc is not None:
                    self._status(tr("preview_decode_failed", exc=sf_exc))
                else:
                    self._status(tr("preview_no_ffmpeg"))
                return
            try:
                tmp_dir = self._ensure_temp_dir()
                dst = tmp_dir / f"slot_{slot}.wav"
                self._purge()
                if self._decoder_accepts_swap_lr:
                    self._decoder(ffmpeg, src, dst, swap_lr=swap_lr)
                else:
                    self._decoder(ffmpeg, src, dst)
            except Exception as exc:
                if self._is_current(gen):
                    self._status(tr("preview_decode_failed", exc=exc))
                return
            if not self._is_current(gen):
                return
            if self._sd is not None and self._sf is not None and self._np is not None:
                try:
                    arr, sr = _decode_with_soundfile(
                        dst, swap_lr=False, sf_mod=self._sf, np_mod=self._np
                    )
                except Exception:
                    self._play_file(dst, gen, direct=False, label=src.name)
                    return
            else:
                self._play_file(dst, gen, direct=False, label=src.name)
                return

        if not self._is_current(gen):
            return

        if self._sd is not None:
            try:
                dev_idx, extra = _select_wasapi_shared_device(self._sd)
                if not self._is_current(gen):
                    return
                self._sd.stop()
                kwargs: dict[str, Any] = {
                    "samplerate": sr,
                    "device": dev_idx,
                    "blocking": False,
                }
                if extra is not None:
                    kwargs["extra_settings"] = extra
                try:
                    self._sd.play(arr, **kwargs)
                except Exception:
                    dev_info = self._sd.query_devices(dev_idx) if dev_idx is not None else {}
                    adapted_arr, adapted_sr = _adapt_buffer_for_device(
                        arr, sr, dev_info, np_mod=self._np
                    )
                    if not self._is_current(gen):
                        return
                    kwargs["samplerate"] = adapted_sr
                    self._sd.play(adapted_arr, **kwargs)
                self._status(tr("preview_playing", name=src.name))
                return
            except Exception as exc:
                # Safe degradation when no audio device exists or PortAudio errors
                if self._ws is not None and src.suffix.lower() == ".wav" and not swap_lr:
                    self._play_file(src, gen, direct=True)
                    return
                if self._is_current(gen):
                    self._status(tr("preview_play_failed", name=src.name, exc=exc))
                return

        if self._ws is not None and src.suffix.lower() == ".wav" and not swap_lr:
            self._play_file(src, gen, direct=True)
        else:
            self._status(tr("preview_no_winsound"))

    def _decode_and_play_legacy(
        self, ffmpeg: str, src: Path, slot: int, gen: int, swap_lr: bool = False
    ) -> None:
        try:
            tmp_dir = self._ensure_temp_dir()
            dst = tmp_dir / f"slot_{slot}.wav"
            # Make sure nothing still holds the slot file before overwriting it.
            self._purge()
            if self._decoder_accepts_swap_lr:
                self._decoder(ffmpeg, src, dst, swap_lr=swap_lr)
            else:
                self._decoder(ffmpeg, src, dst)
        except Exception as exc:
            if self._is_current(gen):
                if src.suffix.lower() == ".wav" and not swap_lr and self._ws is not None:
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
