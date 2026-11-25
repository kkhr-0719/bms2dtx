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


def _is_wdmks_host(name: Any) -> bool:
    """Return True if host API name refers to Windows WDM-KS (explicitly blacklisted)."""
    low = str(name or "").lower()
    return "wdm-ks" in low or "wdmks" in low


def _build_wasapi_settings(sd_mod: Any) -> Any:
    """Construct WasapiSettings(exclusive=False, auto_convert=True) when supported."""
    wasapi_cls = getattr(sd_mod, "WasapiSettings", None)
    if wasapi_cls is None:
        return None
    try:
        return wasapi_cls(exclusive=False, auto_convert=True)
    except TypeError:
        try:
            return wasapi_cls(exclusive=False)
        except Exception:
            return None
    except Exception:
        return None


def _get_candidate_output_devices(
    sd_mod: Any,
) -> list[Tuple[str, int, dict[str, Any], Any]]:
    """Build prioritized Windows output candidates: WASAPI Shared -> DirectSound -> MME.

    Strictly excludes any ``Windows WDM-KS`` Host API or device.
    """
    hostapis = list(sd_mod.query_hostapis())
    try:
        all_devs_raw = list(sd_mod.query_devices())
    except Exception:
        all_devs_raw = []

    def _safe_dev_info(idx: int) -> dict[str, Any]:
        info = sd_mod.query_devices(idx)
        return dict(info) if isinstance(info, dict) else {}

    def _host_name_for_dev(dev_info: dict[str, Any]) -> str:
        hidx = dev_info.get("hostapi")
        if isinstance(hidx, int) and 0 <= hidx < len(hostapis):
            return str(hostapis[hidx].get("name", ""))
        return ""

    candidates: list[Tuple[str, int, dict[str, Any], Any]] = []
    seen_devs: set[int] = set()

    # Priority 1: Windows WASAPI -> Priority 2: Windows DirectSound -> Priority 3: MME
    priority_specs = (
        ("wasapi", True),
        ("directsound", False),
        ("mme", False),
    )
    for token, is_wasapi in priority_specs:
        for host in hostapis:
            hname = str(host.get("name", ""))
            if _is_wdmks_host(hname):
                continue
            if token not in hname.lower():
                continue
            dev_idx = int(host.get("default_output_device", -1))
            if dev_idx < 0 or dev_idx in seen_devs:
                continue
            dev_info = _safe_dev_info(dev_idx)
            if int(dev_info.get("max_output_channels", 0)) <= 0:
                continue
            dev_hname = _host_name_for_dev(dev_info) or hname
            if _is_wdmks_host(dev_hname):
                continue
            extra = _build_wasapi_settings(sd_mod) if is_wasapi else None
            seen_devs.add(dev_idx)
            candidates.append((dev_hname, dev_idx, dev_info, extra))

    if not candidates:
        # Fallback for non-Windows or custom devices: still strictly skip WDM-KS
        try:
            default_dev = sd_mod.default.device
            out_idx = (
                int(default_dev[1])
                if isinstance(default_dev, (list, tuple))
                else int(default_dev)
            )
        except Exception:
            out_idx = -1
        if out_idx >= 0 and out_idx not in seen_devs:
            dev_info = _safe_dev_info(out_idx)
            hname = _host_name_for_dev(dev_info) or "Default"
            if int(dev_info.get("max_output_channels", 0)) > 0 and not _is_wdmks_host(hname):
                seen_devs.add(out_idx)
                candidates.append((hname, out_idx, dev_info, None))

        for idx, raw_dev in enumerate(all_devs_raw):
            if idx in seen_devs or not isinstance(raw_dev, dict):
                continue
            hname = _host_name_for_dev(raw_dev) or f"Device {idx}"
            if _is_wdmks_host(hname):
                continue
            if int(raw_dev.get("max_output_channels", 0)) > 0:
                seen_devs.add(idx)
                candidates.append((hname, idx, dict(raw_dev), None))

    if not candidates:
        raise RuntimeError(
            "No audio output device available "
            "(tried: Windows WASAPI, Windows DirectSound, MME; WDM-KS blacklisted)"
        )
    return candidates


def _select_wasapi_shared_device(sd_mod: Any) -> Tuple[Optional[int], Any]:
    """Return highest-priority safe output device (WASAPI Shared -> DirectSound -> MME)."""
    candidates = _get_candidate_output_devices(sd_mod)
    _host_name, dev_idx, _dev_info, extra = candidates[0]
    return dev_idx, extra


def _adapt_buffer_for_device(
    arr: Any,
    sr: int,
    dev_info: Any,
    *,
    np_mod: Any,
    soxr_mod: Any = _soxr,
) -> Tuple[Any, int]:
    """Expand mono to stereo (float32) and resample to device default_samplerate via soxr."""
    if np_mod is None or arr is None or len(arr) == 0:
        return arr, sr
    out = arr
    raw_max_ch = dev_info.get("max_output_channels", 2) if isinstance(dev_info, dict) else 2
    max_ch = int(raw_max_ch if raw_max_ch is not None else 2)
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
    def play(
        self,
        src: Path | str,
        *,
        swap_lr: Optional[bool] = None,
        gain: float = 1.0,
    ) -> bool:
        """Start previewing ``src`` with optional linear ``gain`` (0.0..1.0); returns False if impossible."""
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

        swap_lr = bool(self._swap_lr_getter()) if swap_lr is None else bool(swap_lr)
        gain_val = max(0.0, min(1.0, float(gain)))

        if self._use_legacy_decoder:
            ffmpeg = self._ffmpeg_getter()
            if not ffmpeg:
                if (
                    src_path.suffix.lower() == ".wav"
                    and not swap_lr
                    and abs(gain_val - 1.0) < 1e-6
                    and self._ws is not None
                ):
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
            args=(src_path, slot, gen, swap_lr, gain_val),
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
        self, src: Path, slot: int, gen: int, swap_lr: bool = False, gain: float = 1.0
    ) -> None:
        arr: Any = None
        sr: int = 44100
        sf_exc: Optional[Exception] = None
        gain_val = max(0.0, min(1.0, float(gain)))
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
                if (
                    src.suffix.lower() == ".wav"
                    and not swap_lr
                    and abs(gain_val - 1.0) < 1e-6
                    and self._ws is not None
                ):
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

        if arr is not None and abs(gain_val - 1.0) > 1e-6 and self._np is not None:
            arr = self._np.ascontiguousarray(arr * self._np.float32(gain_val), dtype=self._np.float32)

        if self._sd is not None:
            try:
                candidates = _get_candidate_output_devices(self._sd)
                errors: list[str] = []
                for host_name, dev_idx, dev_info, extra in candidates:
                    if not self._is_current(gen):
                        return
                    play_arr, play_sr = _adapt_buffer_for_device(
                        arr, sr, dev_info, np_mod=self._np
                    )
                    kwargs: dict[str, Any] = {
                        "samplerate": play_sr,
                        "device": dev_idx,
                        "blocking": False,
                    }
                    if extra is not None:
                        kwargs["extra_settings"] = extra
                    try:
                        self._sd.stop()
                        self._sd.play(play_arr, **kwargs)
                        self._status(tr("preview_playing", name=src.name))
                        return
                    except Exception as play_exc:
                        errors.append(f"{host_name}: {play_exc}")
                raise RuntimeError(f"All host APIs failed ({'; '.join(errors)})")
            except Exception as exc:
                # Safe degradation when no audio device exists or PortAudio errors
                if (
                    self._ws is not None
                    and src.suffix.lower() == ".wav"
                    and not swap_lr
                    and abs(gain_val - 1.0) < 1e-6
                ):
                    self._play_file(src, gen, direct=True)
                    return
                if self._is_current(gen):
                    self._status(tr("preview_play_failed", name=src.name, exc=exc))
                return

        if (
            self._ws is not None
            and src.suffix.lower() == ".wav"
            and not swap_lr
            and abs(gain_val - 1.0) < 1e-6
        ):
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
