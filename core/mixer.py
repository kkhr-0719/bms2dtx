"""Offline audio mixer (bgm.ogg) and retained slice materializer/transcoder."""

from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from typing import Dict, List, Optional, Sequence, Tuple, Union
import wave

from .constants import (
    AUTO_LP_RESONANT_WAV_ID,
    DRUMS_STEM_FILENAME,
    DRUMS_STEM_SOURCE_CHANNELS,
    DTX_MAX_WAV_SLOTS,
    IMAGE_MODE_KEEP,
    IMAGE_MODE_NONE,
    IMAGE_MODE_RESIZE,
    SUPPORTED_VIDEO_EXTENSIONS,
    int_to_base36,
)
from .models import BmsChart, ConversionWarning, MergedSliceSpec, MixStats, NoteEvent, PlacedDtxNote

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - tested via explicit fallback helper
    np = None  # type: ignore

try:
    import soundfile as sf  # type: ignore
except ImportError:  # pragma: no cover
    sf = None  # type: ignore

try:
    import soxr  # type: ignore
except ImportError:  # pragma: no cover
    soxr = None  # type: ignore

AudioData = Union["np.ndarray", array.array]

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_PEAK_CEILING = 0.99


class FfmpegNotFoundError(RuntimeError):
    """Raised when ffmpeg is required for audio decoding/encoding but not found."""


def find_ffmpeg(explicit_path: Optional[str] = None) -> str:
    """Locate ffmpeg executable via 3-tier lookup: local app/bin dir -> PATH -> error."""
    if explicit_path:
        p = Path(explicit_path)
        if p.is_file():
            return str(p)
        found = shutil.which(explicit_path)
        if found:
            return found
        raise FfmpegNotFoundError(f"Specified ffmpeg executable not found: {explicit_path}")

    # Tier 1: Application / executable directory and its bin/ subdirectory (checked before PATH)
    candidate_dirs: List[Path] = []
    seen_dirs: set[Path] = set()

    def _add_dir(d: Path) -> None:
        resolved = d.resolve()
        if resolved not in seen_dirs:
            seen_dirs.add(resolved)
            candidate_dirs.append(resolved)

    _add_dir(Path(sys.executable).resolve().parent)
    _add_dir(Path(__file__).resolve().parent.parent)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        _add_dir(Path(meipass))

    for d in candidate_dirs:
        for cand in (
            d / "ffmpeg.exe",
            d / "bin" / "ffmpeg.exe",
            d / "ffmpeg",
            d / "bin" / "ffmpeg",
        ):
            if cand.is_file():
                return str(cand)

    # Tier 2: System PATH
    found_on_path = shutil.which("ffmpeg")
    if found_on_path:
        return found_on_path

    # Tier 3: Clear actionable error
    raise FfmpegNotFoundError(
        "ffmpeg executable not found. Place ffmpeg.exe beside bms2dtx.exe (or in bin/), "
        "add ffmpeg to system PATH, or specify its path in the GUI / --ffmpeg."
    )


def _resolve_ffmpeg_optional(explicit_path: Optional[str] = None) -> Optional[str]:
    """Best-effort ffmpeg lookup that returns None instead of raising when absent."""
    try:
        return find_ffmpeg(explicit_path)
    except FfmpegNotFoundError:
        return None


def _resample_stereo_f32(arr: "np.ndarray", src_sr: int, dst_sr: int) -> "np.ndarray":
    """Resample (N, 2) float32 audio via soxr (HQ) or pure-NumPy linear fallback."""
    assert np is not None
    if src_sr <= 0 or dst_sr <= 0 or src_sr == dst_sr or len(arr) == 0:
        return arr.astype(np.float32, copy=False)
    if soxr is not None:
        try:
            out = soxr.resample(arr, src_sr, dst_sr, quality="HQ")
            return np.ascontiguousarray(out, dtype=np.float32)
        except Exception:
            pass
    out_len = max(1, int(round(len(arr) * (dst_sr / float(src_sr)))))
    if len(arr) == 1:
        return np.repeat(arr.astype(np.float32, copy=False), out_len, axis=0)
    x_old = np.linspace(0.0, 1.0, num=len(arr), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=out_len, endpoint=False)
    ch0 = np.interp(x_new, x_old, arr[:, 0])
    ch1 = np.interp(x_new, x_old, arr[:, 1])
    return np.column_stack([ch0, ch1]).astype(np.float32)


def _decode_audio_to_f32_bytes(
    audio_path: Path, ffmpeg_exe: Optional[str], sample_rate: int
) -> Tuple[Optional[bytes], Optional[str]]:
    """Decode audio via built-in soundfile (+ soxr/NumPy resample), falling back to ffmpeg."""
    sf_err: Optional[str] = None
    if sf is not None and np is not None:
        try:
            data, src_sr = sf.read(str(audio_path), dtype="float32", always_2d=True)
            if len(data) == 0:
                return b"", None
            if data.shape[1] == 1:
                stereo = np.repeat(data, 2, axis=1)
            else:
                stereo = data[:, :2]
            if not np.all(np.isfinite(stereo)):
                stereo = np.nan_to_num(stereo, nan=0.0, posinf=0.0, neginf=0.0)
            if int(src_sr) != int(sample_rate):
                stereo = _resample_stereo_f32(stereo, int(src_sr), int(sample_rate))
            return np.ascontiguousarray(stereo, dtype="<f4").tobytes(), None
        except Exception as exc:  # pragma: no cover - fallback to ffmpeg for exotic codecs
            sf_err = str(exc)

    if not ffmpeg_exe:
        return None, sf_err or "soundfile/numpy unavailable and ffmpeg not found"

    cmd = [
        ffmpeg_exe,
        "-v",
        "error",
        "-nostdin",
        "-i",
        str(audio_path),
        "-f",
        "f32le",
        "-ac",
        "2",
        "-ar",
        str(sample_rate),
        "pipe:1",
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        return None, err or f"ffmpeg exit code {proc.returncode}"
    raw = proc.stdout
    # Trim any stray non-frame-aligned trailing bytes (8 bytes = 2 channels * 4 bytes)
    usable = (len(raw) // 8) * 8
    if usable == 0:
        return b"", None
    return raw[:usable], None


def _encode_f32_to_ogg(
    pcm_chunks: Sequence[bytes],
    output_path: Path,
    ffmpeg_exe: Optional[str],
    sample_rate: int,
    ogg_quality: float,
) -> None:
    """Encode stereo float32le PCM chunks to OGG Vorbis via soundfile (ffmpeg fallback)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf_err: Optional[Exception] = None
    if sf is not None and np is not None:
        try:
            with sf.SoundFile(
                str(output_path),
                mode="w",
                samplerate=int(sample_rate),
                channels=2,
                format="OGG",
                subtype="VORBIS",
            ) as out_file:
                for chunk in pcm_chunks:
                    if not chunk:
                        continue
                    arr = np.frombuffer(chunk, dtype="<f4").reshape(-1, 2)
                    out_file.write(arr)
            return
        except Exception as exc:  # pragma: no cover
            sf_err = exc
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass

    if not ffmpeg_exe:
        if sf_err is not None:
            raise RuntimeError(f"Failed encoding {output_path.name} with soundfile: {sf_err}") from sf_err
        raise FfmpegNotFoundError(
            "Cannot encode OGG Vorbis: soundfile unavailable and ffmpeg not found."
        )

    cmd = [
        ffmpeg_exe,
        "-v",
        "error",
        "-y",
        "-f",
        "f32le",
        "-ar",
        str(sample_rate),
        "-ac",
        "2",
        "-i",
        "pipe:0",
        "-c:a",
        "libvorbis",
        "-q:a",
        f"{ogg_quality:g}",
        str(output_path),
    ]
    stderr_chunks: List[bytes] = []
    with subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
    ) as proc:
        def _drain_stderr() -> None:
            assert proc.stderr is not None
            try:
                stderr_chunks.append(proc.stderr.read())
            finally:
                proc.stderr.close()

        drain_thread = threading.Thread(target=_drain_stderr, daemon=True)
        drain_thread.start()

        assert proc.stdin is not None
        try:
            for chunk in pcm_chunks:
                if chunk:
                    proc.stdin.write(chunk)
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

        returncode = proc.wait()
        drain_thread.join()

    if returncode != 0:
        err = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed encoding {output_path.name}: {err}")


def _mix_with_numpy(
    decoded_raw: Dict[Path, bytes],
    scheduled: List[Tuple[int, Path]],
    total_frames: int,
    *,
    swap_lr: bool = False,
) -> Tuple[List[bytes], float, float, float, float]:
    """Fast vectorized stereo float32 mixer using numpy."""
    assert np is not None
    decoded_arrs: Dict[Path, "np.ndarray"] = {}
    for path, raw in decoded_raw.items():
        if not raw:
            continue
        arr = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
        if not np.all(np.isfinite(arr)):
            arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        decoded_arrs[path] = arr

    mix = np.zeros((total_frames, 2), dtype=np.float32)
    for start_frame, path in scheduled:
        buf = decoded_arrs.get(path)
        if buf is None or len(buf) == 0:
            continue
        end_frame = min(total_frames, start_frame + len(buf))
        if end_frame <= start_frame:
            continue
        mix[start_frame:end_frame] += buf[: end_frame - start_frame]

    peak_before = float(np.max(np.abs(mix))) if total_frames > 0 else 0.0
    gain = 1.0
    if peak_before > _PEAK_CEILING:
        gain = _PEAK_CEILING / peak_before
        mix *= np.float32(gain)
    peak_after = float(np.max(np.abs(mix))) if total_frames > 0 else 0.0

    # Compute RMS in float64 to avoid precision loss over long songs
    rms = float(np.sqrt(np.mean(mix.astype(np.float64) ** 2))) if total_frames > 0 else 0.0

    if swap_lr and total_frames > 0:
        mix = mix[:, [1, 0]]

    # Yield ~2 MB chunks to ffmpeg without doubling memory
    chunk_frames = 262_144  # 262144 frames * 8 bytes = 2 MiB
    chunks: List[bytes] = []
    for offset in range(0, total_frames, chunk_frames):
        sub = mix[offset : offset + chunk_frames]
        chunks.append(np.ascontiguousarray(sub, dtype="<f4").tobytes())

    return chunks, peak_before, peak_after, rms, gain


def _mix_pure_python(
    decoded_raw: Dict[Path, bytes],
    scheduled: List[Tuple[int, Path]],
    total_frames: int,
    *,
    swap_lr: bool = False,
) -> Tuple[List[bytes], float, float, float, float]:
    """Pure-stdlib stereo float32 mixer fallback when numpy is unavailable."""
    decoded_arrs: Dict[Path, array.array] = {}
    need_swap = sys.byteorder != "little"
    for path, raw in decoded_raw.items():
        if not raw:
            continue
        arr = array.array("f")
        arr.frombytes(raw)
        if need_swap:
            arr.byteswap()
        for idx in range(len(arr)):
            if not math.isfinite(arr[idx]):
                arr[idx] = 0.0
        decoded_arrs[path] = arr

    total_samples = total_frames * 2
    mix = array.array("f", [0.0]) * total_samples

    for start_frame, path in scheduled:
        buf = decoded_arrs.get(path)
        if buf is None:
            continue
        start_idx = start_frame * 2
        count = min(len(buf), total_samples - start_idx)
        for i in range(count):
            mix[start_idx + i] += buf[i]

    peak_before = 0.0
    for val in mix:
        aval = abs(val)
        if aval > peak_before:
            peak_before = aval

    gain = 1.0
    if peak_before > _PEAK_CEILING:
        gain = _PEAK_CEILING / peak_before
        for i in range(total_samples):
            mix[i] *= gain

    peak_after = peak_before * gain
    sq_sum = 0.0
    for val in mix:
        sq_sum += val * val
    rms = math.sqrt(sq_sum / total_samples) if total_samples > 0 else 0.0

    if swap_lr and total_frames > 0:
        for i in range(0, total_samples, 2):
            mix[i], mix[i + 1] = mix[i + 1], mix[i]

    if need_swap:
        mix.byteswap()

    chunk_samples = 524_288  # 2 MiB
    chunks: List[bytes] = []
    for offset in range(0, total_samples, chunk_samples):
        chunks.append(mix[offset : offset + chunk_samples].tobytes())

    return chunks, peak_before, peak_after, rms, gain


def _swap_f32_stereo_bytes(raw: bytes) -> bytes:
    """Swap Left and Right float32 channels in a stereo f32le byte buffer."""
    if not raw:
        return raw
    if np is not None:
        arr = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
        return np.ascontiguousarray(arr[:, [1, 0]], dtype="<f4").tobytes()
    need_swap = sys.byteorder != "little"
    arr_py = array.array("f")
    arr_py.frombytes(raw)
    if need_swap:
        arr_py.byteswap()
    for i in range(0, len(arr_py) - 1, 2):
        arr_py[i], arr_py[i + 1] = arr_py[i + 1], arr_py[i]
    if need_swap:
        arr_py.byteswap()
    return arr_py.tobytes()


def _normalize_swapped_wav_set(
    chart: BmsChart, swapped_wav_ids: Optional[Sequence[str]]
) -> set[str]:
    """Normalize per-slice L/R swap override WAV IDs and propagate across shared physical files."""
    if not swapped_wav_ids:
        return set()
    norm_ids: set[str] = set()
    for raw_wid in swapped_wav_ids:
        wid = str(raw_wid).strip()
        if not wid:
            continue
        norm_ids.add(wid if chart.is_base62 else wid.upper())
    if not norm_ids:
        return set()
    swapped_paths: set[Path] = set()
    for wid in list(norm_ids):
        entry = chart.wavs.get(wid)
        if entry is not None and entry.exists and entry.resolved_path is not None:
            try:
                swapped_paths.add(entry.resolved_path.resolve())
            except OSError:
                swapped_paths.add(entry.resolved_path)
    if swapped_paths:
        for wid, entry in chart.wavs.items():
            if entry.exists and entry.resolved_path is not None:
                try:
                    rp = entry.resolved_path.resolve()
                except OSError:
                    rp = entry.resolved_path
                if rp in swapped_paths:
                    norm_ids.add(wid)
    return norm_ids


def render_bgm_ogg(
    chart: BmsChart,
    unmapped_events: Sequence[NoteEvent],
    output_path: Path,
    *,
    sample_rate: int = 44100,
    ogg_quality: float = 6.0,
    ffmpeg_path: Optional[str] = None,
    use_numpy: Optional[bool] = None,
    decode_cache: Optional[Dict[Tuple[Path, int], bytes]] = None,
    cut_same_wav: bool = False,
    swap_lr: bool = False,
    swapped_wav_ids: Optional[Sequence[str]] = None,
) -> Tuple[MixStats, List[ConversionWarning]]:
    """Offline-mix all unmapped BMS events along the exact timeline and export bgm.ogg.

    Deduplicates identical (start_frame, wav_id) triggers to prevent +6 dB phase spikes,
    and applies strictly downward-only peak limiting above 0.99 (never boosts quiet audio).
    Per-slice L/R channel swap is applied at the decoded slice level via XOR:
    ``effective_swap = bool(swap_lr) ^ (wav_id in swapped_wav_ids)``.
    """
    _ = cut_same_wav
    ffmpeg_exe = _resolve_ffmpeg_optional(ffmpeg_path)
    warnings: List[ConversionWarning] = []
    swapped_set = _normalize_swapped_wav_set(chart, swapped_wav_ids)

    # Collect unique audio paths needed for unmapped events and their per-slice effective_swap
    unique_paths: List[Path] = []
    seen_paths: set[Path] = set()
    path_effective_swap: Dict[Path, bool] = {}
    seen_frame_wav: set[Tuple[int, str]] = set()
    missing_event_count = 0
    playable_pairs: List[Tuple[int, Path]] = []

    for ev in unmapped_events:
        entry = chart.wavs.get(ev.wav_id)
        if entry is None or not entry.exists or entry.resolved_path is None:
            missing_event_count += 1
            continue
        start_frame = max(0, int(round(ev.time_sec * sample_rate)))
        dedup_key = (start_frame, ev.wav_id)
        if dedup_key in seen_frame_wav:
            continue
        seen_frame_wav.add(dedup_key)
        playable_pairs.append((start_frame, entry.resolved_path))
        eff_swap = bool(swap_lr) ^ (ev.wav_id in swapped_set)
        if entry.resolved_path not in seen_paths:
            seen_paths.add(entry.resolved_path)
            unique_paths.append(entry.resolved_path)
            path_effective_swap[entry.resolved_path] = eff_swap

    # Decode each unique file once in parallel (reusing unswapped decode_cache if provided)
    unswapped_raw: Dict[Path, bytes] = {}
    paths_to_decode: List[Path] = []
    for p in unique_paths:
        cache_key = (p, sample_rate)
        if decode_cache is not None and cache_key in decode_cache:
            unswapped_raw[p] = decode_cache[cache_key]
        else:
            paths_to_decode.append(p)

    decode_failures: List[str] = []

    if paths_to_decode:
        max_workers = min(8, os.cpu_count() or 4, len(paths_to_decode))

        def _worker(p: Path) -> Tuple[Path, Optional[bytes], Optional[str]]:
            raw, err = _decode_audio_to_f32_bytes(p, ffmpeg_exe, sample_rate)
            return p, raw, err

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for path, raw, err in executor.map(_worker, paths_to_decode):
                if err is not None or raw is None:
                    decode_failures.append(f"{path.name}: {err}")
                else:
                    unswapped_raw[path] = raw
                    if decode_cache is not None:
                        decode_cache[(path, sample_rate)] = raw

    # Apply per-slice effective_swap at the decoded slice stage
    decoded_raw: Dict[Path, bytes] = {}
    for p, raw in unswapped_raw.items():
        if path_effective_swap.get(p, False):
            decoded_raw[p] = _swap_f32_stereo_bytes(raw)
        else:
            decoded_raw[p] = raw

    if decode_failures:
        sample = "; ".join(decode_failures[:4])
        more = f" (+{len(decode_failures) - 4} more)" if len(decode_failures) > 4 else ""
        warnings.append(
            ConversionWarning(
                code="AUDIO_DECODE_FAILED",
                message=f"Failed to decode {len(decode_failures)} audio slice(s) ({sample}{more}).",
            )
        )

    # Determine total mix frames
    frame_lengths: Dict[Path, int] = {
        path: len(raw) // 8 for path, raw in decoded_raw.items()
    }
    max_end_frame = int(math.ceil(max(chart.song_duration_sec, 0.5) * sample_rate))
    mixed_event_count = 0
    scheduled: List[Tuple[int, Path]] = []

    for start_frame, path in playable_pairs:
        flen = frame_lengths.get(path, 0)
        if flen <= 0:
            continue
        scheduled.append((start_frame, path))
        mixed_event_count += 1
        end_frame = start_frame + flen
        if end_frame > max_end_frame:
            max_end_frame = end_frame

    total_frames = max(max_end_frame, sample_rate // 2)

    should_use_numpy = (np is not None) if use_numpy is None else (use_numpy and np is not None)
    if should_use_numpy:
        pcm_chunks, peak_before, peak_after, rms, gain = _mix_with_numpy(
            decoded_raw, scheduled, total_frames, swap_lr=False
        )
    else:
        pcm_chunks, peak_before, peak_after, rms, gain = _mix_pure_python(
            decoded_raw, scheduled, total_frames, swap_lr=False
        )

    if mixed_event_count == 0:
        warnings.append(
            ConversionWarning(
                code="BGM_MIX_SILENT_NO_EVENTS",
                message=(
                    "No unmapped audio events remained to mix into bgm.ogg; "
                    "exported silent bgm.ogg."
                ),
            )
        )
    elif peak_after == 0.0:
        warnings.append(
            ConversionWarning(
                code="BGM_MIX_SILENT_ZERO_ENERGY",
                message="Rendered bgm.ogg has zero audio energy (all mixed slices were silent).",
            )
        )

    _encode_f32_to_ogg(
        pcm_chunks,
        output_path,
        ffmpeg_exe=ffmpeg_exe,
        sample_rate=sample_rate,
        ogg_quality=ogg_quality,
    )

    stats = MixStats(
        output_path=output_path,
        sample_rate=sample_rate,
        total_frames=total_frames,
        duration_sec=total_frames / float(sample_rate),
        mixed_event_count=mixed_event_count,
        unique_files_decoded=len(decoded_raw),
        missing_event_count=missing_event_count,
        peak_before_norm=peak_before,
        peak_after_norm=peak_after,
        rms=rms,
        gain_applied=gain,
    )
    return stats, warnings


_FFMPEG_AUDIO_STREAM_RE = re.compile(r"Audio:.*?,\s*\d+\s*Hz,\s*([^,\r\n]+)", re.IGNORECASE)


def _get_audio_channels(path: Path, ffmpeg_exe: Optional[str] = None) -> Optional[int]:
    """Detect audio channel count (1 for mono, 2 for stereo, or None if unknown)."""
    if sf is not None:
        try:
            return int(sf.info(str(path)).channels)
        except Exception:
            pass
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                return int(wf.getnchannels())
        except Exception:
            pass
    if ffmpeg_exe:
        proc = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-nostdin", "-i", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
        stderr_txt = proc.stderr.decode("utf-8", errors="replace")
        m = _FFMPEG_AUDIO_STREAM_RE.search(stderr_txt)
        if m:
            layout = m.group(1).strip().lower()
            if "mono" in layout or layout.startswith("1 channels") or layout == "1.0":
                return 1
            if "stereo" in layout or layout.startswith("2 channels") or layout == "2.0":
                return 2
            m_cnt = re.search(r"(\d+)\s*channels?", layout)
            if m_cnt:
                return int(m_cnt.group(1))
    return None


def _write_audio_via_soundfile(src: Path, dst: Path, *, swap_lr: bool) -> None:
    """Read audio with soundfile, optionally swap L/R channels, and write to dst."""
    if sf is None or np is None:
        raise RuntimeError("soundfile/numpy unavailable")
    info = sf.info(str(src))
    data, sr = sf.read(str(src), dtype="float32", always_2d=True)
    if swap_lr and data.shape[1] >= 2:
        data = np.array(data, copy=True)
        data[:, [0, 1]] = data[:, [1, 0]]
    ext = dst.suffix.lower()
    if ext == ".ogg":
        sf.write(str(dst), data, int(sr), format="OGG", subtype="VORBIS")
    elif ext == ".flac":
        sf.write(str(dst), data, int(sr), format="FLAC")
    elif ext == ".mp3":
        sf.write(str(dst), data, int(sr), format="MP3")
    elif ext == ".wav":
        subtype = info.subtype if sf.check_format("WAV", info.subtype) else "PCM_16"
        sf.write(str(dst), data, int(sr), format="WAV", subtype=subtype)
    else:
        raise RuntimeError(f"Unsupported output extension for soundfile: {ext}")


def _codec_args_for_audio(dst: Path, ogg_quality: float) -> List[str]:
    ext = dst.suffix.lower()
    if ext == ".wav":
        return ["-c:a", "pcm_s16le"]
    if ext == ".ogg":
        return ["-c:a", "libvorbis", "-q:a", f"{ogg_quality:g}"]
    if ext == ".flac":
        return ["-c:a", "flac"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-q:a", "2"]
    return []


def _copy_or_swap_lr_audio(
    src: Path,
    dst: Path,
    *,
    swap_lr: bool,
    ffmpeg_exe: Optional[str],
    ogg_quality: float = 6.0,
) -> Optional[str]:
    """Copy audio file or swap L/R stereo channels via soundfile (ffmpeg fallback); mono always copies."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not swap_lr:
        shutil.copy2(src, dst)
        return None
    channels = _get_audio_channels(src, ffmpeg_exe)
    if channels == 1:
        shutil.copy2(src, dst)
        return None

    sf_err: Optional[str] = None
    try:
        _write_audio_via_soundfile(src, dst, swap_lr=True)
        return None
    except Exception as exc:
        sf_err = str(exc)
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass

    if not ffmpeg_exe:
        shutil.copy2(src, dst)
        return sf_err or "ffmpeg not found"

    cmd = [
        ffmpeg_exe,
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-af",
        "pan=stereo|c0=c1|c1=c0",
        *_codec_args_for_audio(dst, ogg_quality),
        str(dst),
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
        check=False,
    )
    if proc.returncode != 0 or not dst.is_file():
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        shutil.copy2(src, dst)
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        return err or f"ffmpeg exit code {proc.returncode}"
    return None


def _sanitize_rel_candidate(
    resolved_name: str, src_path: Path, forced_suffix: Optional[str] = None
) -> Path:
    rel_candidate = Path(resolved_name.replace("\\", "/"))
    if (
        rel_candidate.is_absolute()
        or bool(rel_candidate.drive)
        or bool(rel_candidate.root)
        or ".." in rel_candidate.parts
        or str(rel_candidate) in ("", ".")
    ):
        rel_candidate = Path(src_path.name)
    sanitized_parts = [part.replace(";", "_") for part in rel_candidate.parts]
    rel_candidate = (
        Path(*sanitized_parts)
        if sanitized_parts
        else Path(src_path.name.replace(";", "_"))
    )
    if forced_suffix is not None:
        rel_candidate = rel_candidate.with_suffix(forced_suffix)
    return rel_candidate


def _disambiguate_rel(
    rel_candidate: Path,
    src_path: Optional[Path],
    claimed_rel_lower: Dict[str, Optional[Path]],
    *,
    allow_reuse_same_src: bool = True,
) -> Path:
    rel_str = rel_candidate.as_posix()
    key = rel_str.lower()
    if key in claimed_rel_lower:
        if allow_reuse_same_src and src_path is not None and claimed_rel_lower[key] == src_path:
            return rel_candidate
        stem = rel_candidate.stem
        suffix = rel_candidate.suffix
        parent = rel_candidate.parent
        counter = 1
        while True:
            alt = (
                (parent / f"{stem}_{counter}{suffix}")
                if str(parent) not in ("", ".")
                else Path(f"{stem}_{counter}{suffix}")
            )
            if alt.as_posix().lower() not in claimed_rel_lower:
                return alt
            counter += 1
    return rel_candidate


def _safe_output_dst(output_dir: Path, rel_candidate: Path, src_path: Path) -> Path:
    resolved_out_root = output_dir.resolve()
    dst_path = (resolved_out_root / rel_candidate).resolve()
    if not dst_path.is_relative_to(resolved_out_root):
        raise ValueError(
            f"Refusing to write artifact outside output directory: {dst_path}"
        )
    if dst_path == src_path:
        raise ValueError(
            f"Refusing to overwrite original source file: {src_path}"
        )
    return dst_path


def materialize_retained_slices(
    chart: BmsChart,
    ordered_retained_orig_ids: Sequence[str],
    output_dir: Path,
    *,
    transcode_wav_to_ogg: bool = False,
    copy_files: bool = True,
    ogg_quality: float = 6.0,
    ffmpeg_path: Optional[str] = None,
    reserve_bgm_ogg: bool = True,
    reserve_drums_ogg: bool = False,
    swap_lr: bool = False,
    swapped_wav_ids: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, str], List[Path], List[Path], List[ConversionWarning]]:
    """Copy or transcode retained slice files into output_dir (NEVER mutating source_dir).

    Returns:
        (orig_wav_id -> output_relative_filename, copied_paths, transcoded_paths, warnings)
    """
    warnings: List[ConversionWarning] = []
    copied_files: List[Path] = []
    transcoded_files: List[Path] = []
    orig_to_out_name: Dict[str, str] = {}
    swapped_set = _normalize_swapped_wav_set(chart, swapped_wav_ids)

    # Track source_path -> assigned relative output path (deduplicate shared files)
    src_to_rel_out: Dict[Path, str] = {}
    claimed_rel_lower: Dict[str, Optional[Path]] = {}

    # Reserve bgm.ogg / drums.ogg in Toggle=OFF so no retained slice accidentally overwrites them
    if reserve_bgm_ogg:
        claimed_rel_lower["bgm.ogg"] = (output_dir / "bgm.ogg").resolve()
    if reserve_drums_ogg:
        claimed_rel_lower[DRUMS_STEM_FILENAME.lower()] = (
            output_dir / DRUMS_STEM_FILENAME
        ).resolve()

    ffmpeg_exe: Optional[str] = None
    transcode_jobs: List[Tuple[Path, Path, bool]] = []
    copy_jobs: List[Tuple[Path, Path, bool]] = []

    for orig_wid in ordered_retained_orig_ids:
        entry = chart.wavs.get(orig_wid)
        if entry is None:
            orig_to_out_name[orig_wid] = f"missing_{orig_wid}.wav"
            continue

        if not entry.exists or entry.resolved_path is None:
            orig_to_out_name[orig_wid] = entry.resolved_name.replace("/", "\\").replace(";", "_")
            continue

        src_path = entry.resolved_path.resolve()
        if src_path in src_to_rel_out:
            orig_to_out_name[orig_wid] = src_to_rel_out[src_path]
            continue

        is_wav_transcode = transcode_wav_to_ogg and src_path.suffix.lower() == ".wav"
        rel_candidate = _sanitize_rel_candidate(
            entry.resolved_name,
            src_path,
            forced_suffix=".ogg" if is_wav_transcode else None,
        )
        rel_candidate = _disambiguate_rel(rel_candidate, src_path, claimed_rel_lower)
        rel_str = rel_candidate.as_posix()

        dtx_rel = rel_str.replace("/", "\\")
        claimed_rel_lower[rel_str.lower()] = src_path
        src_to_rel_out[src_path] = dtx_rel
        orig_to_out_name[orig_wid] = dtx_rel
        eff_swap = bool(swap_lr) ^ (orig_wid in swapped_set)

        if copy_files:
            dst_path = _safe_output_dst(output_dir, rel_candidate, src_path)
            if is_wav_transcode:
                transcode_jobs.append((src_path, dst_path, eff_swap))
            else:
                copy_jobs.append((src_path, dst_path, eff_swap))

    any_copy_swap = any(eff for _, _, eff in copy_jobs)
    if any_copy_swap or transcode_jobs:
        ffmpeg_exe = _resolve_ffmpeg_optional(ffmpeg_path)

    # Execute copies (or L/R swap on stereo copies with effective_swap=True)
    if copy_jobs:
        if any_copy_swap:
            max_workers = min(8, os.cpu_count() or 4, len(copy_jobs))

            def _copy_or_swap_one(job: Tuple[Path, Path, bool]) -> Tuple[Path, Optional[str]]:
                src, dst, eff_swap = job
                err = _copy_or_swap_lr_audio(
                    src,
                    dst,
                    swap_lr=eff_swap,
                    ffmpeg_exe=ffmpeg_exe,
                    ogg_quality=ogg_quality,
                )
                return dst, err

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for dst, err in executor.map(_copy_or_swap_one, copy_jobs):
                    copied_files.append(dst)
                    if err is not None:
                        warnings.append(
                            ConversionWarning(
                                code="SWAP_LR_SLICE_FALLBACK",
                                message=f"Copied {dst.name} without L/R swap ({err}).",
                            )
                        )
        else:
            for src, dst, _eff_swap in copy_jobs:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied_files.append(dst)

    # Execute .wav -> .ogg transcodes in parallel (built-in soundfile first, ffmpeg fallback)
    if transcode_jobs:
        max_workers = min(8, os.cpu_count() or 4, len(transcode_jobs))

        def _transcode_one(job: Tuple[Path, Path, bool]) -> Tuple[Path, Optional[str]]:
            src, dst, eff_swap = job
            dst.parent.mkdir(parents=True, exist_ok=True)
            do_swap = bool(eff_swap and _get_audio_channels(src, ffmpeg_exe) != 1)
            sf_err: Optional[str] = None
            try:
                _write_audio_via_soundfile(src, dst, swap_lr=do_swap)
                return dst, None
            except Exception as exc:
                sf_err = str(exc)
                try:
                    dst.unlink(missing_ok=True)
                except OSError:
                    pass

            if not ffmpeg_exe:
                return dst, sf_err or "ffmpeg not found"

            cmd = [
                ffmpeg_exe,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(src),
            ]
            if do_swap:
                cmd.extend(["-af", "pan=stereo|c0=c1|c1=c0"])
            cmd.extend([
                "-c:a",
                "libvorbis",
                "-q:a",
                f"{ogg_quality:g}",
                str(dst),
            ])
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW,
                check=False,
            )
            if proc.returncode != 0:
                try:
                    dst.unlink(missing_ok=True)
                except OSError:
                    pass
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                return dst, err or f"ffmpeg exit code {proc.returncode}"
            return dst, None

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for dst, err in executor.map(_transcode_one, transcode_jobs):
                if err is not None:
                    warnings.append(
                        ConversionWarning(
                            code="WAV_TRANSCODE_FAILED",
                            message=f"Failed to transcode to {dst.name}: {err}",
                        )
                    )
                else:
                    transcoded_files.append(dst)

    return orig_to_out_name, copied_files, transcoded_files, warnings


def materialize_preview_audio(
    chart: BmsChart,
    output_dir: Path,
    claimed_rel_names: Sequence[str],
    *,
    copy_files: bool = True,
    reserve_bgm_ogg: bool = True,
    reserve_drums_ogg: bool = False,
    swap_lr: bool = False,
    ffmpeg_path: Optional[str] = None,
    ogg_quality: float = 6.0,
) -> Tuple[Optional[str], Optional[Path]]:
    """Copy (or L/R swap) #PREVIEW audio into output_dir without colliding with slices or bgm.ogg.

    Returns (dtx_relative_preview_filename_with_backslashes, copied_dst_path_or_None).
    """
    entry = chart.preview
    if entry is None or not entry.exists or entry.resolved_path is None:
        return None, None

    src_path = entry.resolved_path.resolve()
    claimed_lower: Dict[str, Optional[Path]] = {
        name.replace("\\", "/").lower(): None for name in claimed_rel_names
    }
    if reserve_bgm_ogg:
        claimed_lower["bgm.ogg"] = None
    if reserve_drums_ogg:
        claimed_lower[DRUMS_STEM_FILENAME.lower()] = None

    rel_candidate = _sanitize_rel_candidate(entry.resolved_name, src_path)
    rel_candidate = _disambiguate_rel(
        rel_candidate, None, claimed_lower, allow_reuse_same_src=False
    )
    rel_str = rel_candidate.as_posix()

    dtx_rel = rel_str.replace("/", "\\")
    dst_path: Optional[Path] = None
    if copy_files:
        dst_path = _safe_output_dst(output_dir, rel_candidate, src_path)
        ffmpeg_exe = _resolve_ffmpeg_optional(ffmpeg_path) if swap_lr else None
        _copy_or_swap_lr_audio(
            src_path,
            dst_path,
            swap_lr=swap_lr,
            ffmpeg_exe=ffmpeg_exe,
            ogg_quality=ogg_quality,
        )

    return dtx_rel, dst_path


def _resize_image_ffmpeg(
    src: Path, dst: Path, width: int, height: int, ffmpeg_exe: str
) -> Optional[str]:
    """Scale image preserving aspect ratio and pad with black bars to exact width x height."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )
    cmd = [
        ffmpeg_exe,
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-vf",
        vf,
        "-frames:v",
        "1",
    ]
    if dst.suffix.lower() in (".jpg", ".jpeg"):
        cmd.extend(["-q:v", "2"])
    cmd.append(str(dst))
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
        check=False,
    )
    if proc.returncode != 0 or not dst.is_file():
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        return err or f"ffmpeg exit code {proc.returncode}"
    return None


def materialize_images(
    chart: BmsChart,
    output_dir: Path,
    claimed_rel_names: Sequence[str],
    *,
    image_mode: str = IMAGE_MODE_KEEP,
    copy_files: bool = True,
    reserve_bgm_ogg: bool = True,
    reserve_drums_ogg: bool = False,
    ffmpeg_path: Optional[str] = None,
) -> Tuple[
    Optional[str],
    Optional[str],
    Optional[str],
    Optional[Path],
    Optional[Path],
    Optional[Path],
    List[ConversionWarning],
]:
    """Materialize #STAGEFILE -> #STAGEFILE/#PREIMAGE and #BACKBMP -> #BACKGROUND."""
    mode = (image_mode or IMAGE_MODE_KEEP).strip().lower()
    if mode == IMAGE_MODE_NONE:
        return None, None, None, None, None, None, []

    warnings: List[ConversionWarning] = []
    claimed_lower: Dict[str, Optional[Path]] = {
        name.replace("\\", "/").lower(): None for name in claimed_rel_names
    }
    if reserve_bgm_ogg:
        claimed_lower["bgm.ogg"] = None
    if reserve_drums_ogg:
        claimed_lower[DRUMS_STEM_FILENAME.lower()] = None

    ffmpeg_exe: Optional[str] = None
    if mode == IMAGE_MODE_RESIZE and copy_files:
        try:
            ffmpeg_exe = find_ffmpeg(ffmpeg_path)
        except FfmpegNotFoundError:
            warnings.append(
                ConversionWarning(
                    code="IMAGE_RESIZE_NO_FFMPEG",
                    message="ffmpeg not found; copied original images without resizing.",
                )
            )
            mode = IMAGE_MODE_KEEP

    stage_rel: Optional[str] = None
    pre_rel: Optional[str] = None
    bg_rel: Optional[str] = None
    stage_dst: Optional[Path] = None
    pre_dst: Optional[Path] = None
    bg_dst: Optional[Path] = None

    def _copy_raw_image(entry, missing_code: str, label: str) -> Tuple[Optional[str], Optional[Path]]:
        if entry is None:
            return None, None
        if not entry.exists or entry.resolved_path is None:
            warnings.append(
                ConversionWarning(
                    code=missing_code,
                    message=f"{label} image file {entry.declared_name!r} could not be found.",
                )
            )
            return None, None
        src_path = entry.resolved_path.resolve()
        rel_cand = _disambiguate_rel(
            _sanitize_rel_candidate(entry.resolved_name, src_path),
            src_path,
            claimed_lower,
        )
        rel_str = rel_cand.as_posix()
        claimed_lower[rel_str.lower()] = src_path
        dst_path: Optional[Path] = None
        if copy_files:
            dst_path = _safe_output_dst(output_dir, rel_cand, src_path)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if not dst_path.exists():
                shutil.copy2(src_path, dst_path)
        return rel_str.replace("/", "\\"), dst_path

    if mode == IMAGE_MODE_KEEP:
        stage_rel, stage_dst = _copy_raw_image(chart.stagefile, "MISSING_STAGEFILE", "#STAGEFILE")
        if stage_rel is not None:
            pre_rel = stage_rel
            pre_dst = stage_dst
        bg_rel, bg_dst = _copy_raw_image(chart.backbmp, "MISSING_BACKBMP", "#BACKBMP")
        return stage_rel, pre_rel, bg_rel, stage_dst, pre_dst, bg_dst, warnings

    # IMAGE_MODE_RESIZE: #STAGEFILE -> 640x480, #PREIMAGE -> 204x269, #BACKGROUND -> 640x480
    def _resize_one(entry, missing_code: str, label: str, width: int, height: int, suffix_tag: str) -> Tuple[Optional[str], Optional[Path]]:
        if entry is None:
            return None, None
        if not entry.exists or entry.resolved_path is None:
            warnings.append(
                ConversionWarning(
                    code=missing_code,
                    message=f"{label} image file {entry.declared_name!r} could not be found.",
                )
            )
            return None, None
        src_path = entry.resolved_path.resolve()
        rel_cand = _sanitize_rel_candidate(entry.resolved_name, src_path)
        if suffix_tag:
            rel_cand = rel_cand.with_name(f"{rel_cand.stem}{suffix_tag}{rel_cand.suffix}")
        rel_cand = _disambiguate_rel(
            rel_cand, None, claimed_lower, allow_reuse_same_src=False
        )
        rel_str = rel_cand.as_posix()
        claimed_lower[rel_str.lower()] = src_path
        dst_path: Optional[Path] = None
        if copy_files and ffmpeg_exe:
            dst_path = _safe_output_dst(output_dir, rel_cand, src_path)
            err = _resize_image_ffmpeg(src_path, dst_path, width, height, ffmpeg_exe)
            if err is not None:
                warnings.append(
                    ConversionWarning(
                        code="IMAGE_RESIZE_FAILED",
                        message=f"Failed to resize {src_path.name} ({width}x{height}); copied original instead ({err}).",
                    )
                )
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst_path)
        return rel_str.replace("/", "\\"), dst_path

    if chart.stagefile is not None:
        stage_rel, stage_dst = _resize_one(chart.stagefile, "MISSING_STAGEFILE", "#STAGEFILE", 640, 480, "")
        if chart.stagefile.exists and chart.stagefile.resolved_path is not None:
            pre_rel, pre_dst = _resize_one(chart.stagefile, "MISSING_STAGEFILE", "#PREIMAGE", 204, 269, "_preimage")
    bg_rel, bg_dst = _resize_one(chart.backbmp, "MISSING_BACKBMP", "#BACKBMP", 640, 480, "")
    return stage_rel, pre_rel, bg_rel, stage_dst, pre_dst, bg_dst, warnings


def materialize_bga(
    chart: BmsChart,
    output_dir: Path,
    claimed_rel_names: Sequence[str],
    *,
    copy_files: bool = True,
    reserve_bgm_ogg: bool = True,
    reserve_drums_ogg: bool = False,
) -> Tuple[
    List[Tuple[str, str, str]],
    List[Tuple[int, Fraction, str, str]],
    List[Path],
    List[ConversionWarning],
]:
    """Materialize #BMPxx/#AVIxx media files and map BMS 04/07 events to DTX 04/07/54."""
    warnings: List[ConversionWarning] = []
    if not chart.bmps:
        return [], [], [], warnings

    claimed_lower: Dict[str, Optional[Path]] = {
        name.replace("\\", "/").lower(): None for name in claimed_rel_names
    }
    if reserve_bgm_ogg:
        claimed_lower["bgm.ogg"] = None
    if reserve_drums_ogg:
        claimed_lower[DRUMS_STEM_FILENAME.lower()] = None

    bga_headers: List[Tuple[str, str, str]] = []
    id_map: Dict[str, Tuple[str, str]] = {}  # orig_bid -> (tag "BMP"|"AVI", dtx_bid)
    src_to_rel_out: Dict[Path, str] = {}
    copied_files: List[Path] = []
    missing_used: List[str] = []

    referenced_ids = {ev.bga_id for ev in chart.bga_events}

    # Preserve declaration order, re-indexed sequentially to legal Base-36 01..ZZ
    slot_num = 1
    for orig_bid, entry in chart.bmps.items():
        if slot_num > DTX_MAX_WAV_SLOTS:
            warnings.append(
                ConversionWarning(
                    code="BGA_LIMIT_EXCEEDED",
                    message=f"BMS BGA declarations exceed DTX Base-36 limit ({DTX_MAX_WAV_SLOTS}); truncated.",
                )
            )
            break
        dtx_bid = int_to_base36(slot_num)
        slot_num += 1

        ext = (
            entry.resolved_path.suffix.lower()
            if entry.resolved_path is not None
            else Path(entry.resolved_name).suffix.lower()
        )
        tag = "AVI" if ext in SUPPORTED_VIDEO_EXTENSIONS else "BMP"

        if not entry.exists or entry.resolved_path is None:
            dtx_rel = entry.resolved_name.replace("/", "\\").replace(";", "_")
            if orig_bid in referenced_ids:
                missing_used.append(f"{orig_bid}:{entry.declared_name}")
        else:
            src_path = entry.resolved_path.resolve()
            if src_path in src_to_rel_out:
                dtx_rel = src_to_rel_out[src_path]
            else:
                rel_cand = _disambiguate_rel(
                    _sanitize_rel_candidate(entry.resolved_name, src_path),
                    src_path,
                    claimed_lower,
                )
                rel_str = rel_cand.as_posix()
                dtx_rel = rel_str.replace("/", "\\")
                claimed_lower[rel_str.lower()] = src_path
                src_to_rel_out[src_path] = dtx_rel
                if copy_files:
                    dst_path = _safe_output_dst(output_dir, rel_cand, src_path)
                    dst_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, dst_path)
                    copied_files.append(dst_path)

        bga_headers.append((tag, dtx_bid, dtx_rel))
        id_map[orig_bid] = (tag, dtx_bid)

    if missing_used:
        sample = ", ".join(missing_used[:6])
        more = f" (+{len(missing_used) - 6} more)" if len(missing_used) > 6 else ""
        warnings.append(
            ConversionWarning(
                code="MISSING_BGA_FILES",
                message=f"{len(missing_used)} BGA file(s) could not be found ({sample}{more}).",
            )
        )

    bga_placed_events: List[Tuple[int, Fraction, str, str]] = []
    for ev in chart.bga_events:
        mapped = id_map.get(ev.bga_id)
        if mapped is None:
            continue
        tag, dtx_bid = mapped
        dtx_ch = "54" if tag == "AVI" else ev.channel
        bga_placed_events.append((ev.measure, ev.pos, dtx_ch, dtx_bid))

    return bga_headers, bga_placed_events, copied_files, warnings


def _write_f32_stereo_wav(
    pcm_bytes: bytes,
    output_path: Path,
    *,
    sample_rate: int,
) -> None:
    """Write interleaved stereo float32le PCM bytes to a standard 16-bit PCM WAV file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if sf is not None and np is not None:
        arr = (
            np.frombuffer(pcm_bytes, dtype="<f4").reshape(-1, 2)
            if pcm_bytes
            else np.zeros((0, 2), dtype=np.float32)
        )
        sf.write(str(output_path), arr, int(sample_rate), format="WAV", subtype="PCM_16")
        return

    arr_f = array.array("f")
    arr_f.frombytes(pcm_bytes)
    if sys.byteorder != "little":
        arr_f.byteswap()
    pcm16 = array.array("h", [0]) * len(arr_f)
    for idx, sample in enumerate(arr_f):
        clamped = max(-1.0, min(1.0, sample))
        pcm16[idx] = max(-32768, min(32767, int(round(clamped * 32767.0))))
    if sys.byteorder != "little":
        pcm16.byteswap()
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm16.tobytes())


def materialize_merged_slices(
    chart: BmsChart,
    merged_slices: Sequence[MergedSliceSpec],
    output_dir: Path,
    claimed_rel_names: Sequence[str],
    *,
    sample_rate: int = 44100,
    transcode_wav_to_ogg: bool = False,
    copy_files: bool = True,
    ogg_quality: float = 6.0,
    ffmpeg_path: Optional[str] = None,
    reserve_bgm_ogg: bool = True,
    reserve_drums_ogg: bool = False,
    decode_cache: Optional[Dict[Tuple[Path, int], bytes]] = None,
    out_pcm_by_id: Optional[Dict[str, bytes]] = None,
) -> Tuple[Dict[str, str], List[Path], List[Path], List[ConversionWarning]]:
    """Render deduplicated composite WAV/OGG slices for simultaneous multi-hit drum collisions.

    For each unique component set, decodes each slice once (shared cache), applies per-slice
    effective L/R swap BEFORE mixing, sums linearly in float32, normalizes downward if
    peak > 0.99, and writes one file per unique combo.
    """
    warnings: List[ConversionWarning] = []
    merged_id_to_out_name: Dict[str, str] = {}
    written_wav_paths: List[Path] = []
    written_ogg_paths: List[Path] = []

    if not merged_slices:
        return merged_id_to_out_name, written_wav_paths, written_ogg_paths, warnings

    claimed_lower: Dict[str, Optional[Path]] = {
        name.replace("\\", "/").lower(): None for name in claimed_rel_names
    }
    if reserve_bgm_ogg:
        claimed_lower["bgm.ogg"] = None
    if reserve_drums_ogg:
        claimed_lower[DRUMS_STEM_FILENAME.lower()] = None

    ffmpeg_exe = _resolve_ffmpeg_optional(ffmpeg_path)
    local_cache: Dict[Tuple[Path, int], bytes] = (
        decode_cache if decode_cache is not None else {}
    )

    # Pre-decode all unique component files
    paths_needed: List[Path] = []
    seen_paths: set[Path] = set()
    for spec in merged_slices:
        for orig_wid, _eff_swap, _fname in spec.components:
            entry = chart.wavs.get(orig_wid)
            if entry is None or not entry.exists or entry.resolved_path is None:
                continue
            rp = entry.resolved_path.resolve()
            if (rp, sample_rate) not in local_cache and rp not in seen_paths:
                seen_paths.add(rp)
                paths_needed.append(rp)

    if paths_needed:
        max_workers = min(8, os.cpu_count() or 4, len(paths_needed))

        def _decode_worker(p: Path) -> Tuple[Path, Optional[bytes], Optional[str]]:
            raw, err = _decode_audio_to_f32_bytes(p, ffmpeg_exe, sample_rate)
            return p, raw, err

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for path, raw, err in executor.map(_decode_worker, paths_needed):
                if err is not None or raw is None:
                    warnings.append(
                        ConversionWarning(
                            code="AUDIO_DECODE_FAILED",
                            message=f"Failed to decode merged slice component {path.name}: {err}",
                        )
                    )
                else:
                    local_cache[(path, sample_rate)] = raw

    ext = ".ogg" if transcode_wav_to_ogg else ".wav"
    for spec in merged_slices:
        comp_ids = [c[0] for c in spec.components[:3]]
        id_tag = "_".join(re.sub(r"[^0-9A-Za-z_-]", "_", cid) for cid in comp_ids)
        if len(spec.components) > 3:
            id_tag += f"_etc{len(spec.components) - 3}"
        rel_candidate = _disambiguate_rel(
            Path(f"_mix_{spec.dtx_wav_id}_{id_tag}{ext}"),
            None,
            claimed_lower,
            allow_reuse_same_src=False,
        )
        rel_str = rel_candidate.as_posix()
        claimed_lower[rel_str.lower()] = None
        dtx_rel = rel_str.replace("/", "\\")
        spec.output_filename = dtx_rel
        merged_id_to_out_name[spec.dtx_wav_id] = dtx_rel

        # Mix components in float32 stereo after per-slice L/R swap
        comp_buffers: List[bytes] = []
        for orig_wid, eff_swap, _fname in spec.components:
            entry = chart.wavs.get(orig_wid)
            if entry is None or not entry.exists or entry.resolved_path is None:
                continue
            raw = local_cache.get((entry.resolved_path.resolve(), sample_rate))
            if not raw:
                continue
            comp_buffers.append(_swap_f32_stereo_bytes(raw) if eff_swap else raw)

        if not comp_buffers:
            total_frames = max(1, sample_rate // 100)
            mix_bytes = b"\x00" * (total_frames * 8)
            spec.peak_before_norm = 0.0
            spec.gain_applied = 1.0
        elif np is not None:
            arrays = [
                np.frombuffer(buf, dtype="<f4").reshape(-1, 2) for buf in comp_buffers
            ]
            max_frames = max(len(a) for a in arrays)
            mix = np.zeros((max_frames, 2), dtype=np.float32)
            for a in arrays:
                if not np.all(np.isfinite(a)):
                    a = np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
                mix[: len(a)] += a
            peak_before = float(np.max(np.abs(mix))) if max_frames > 0 else 0.0
            gain = 1.0
            if peak_before > _PEAK_CEILING:
                gain = _PEAK_CEILING / peak_before
                mix *= np.float32(gain)
            spec.peak_before_norm = peak_before
            spec.gain_applied = gain
            mix_bytes = np.ascontiguousarray(mix, dtype="<f4").tobytes()
        else:
            need_swap = sys.byteorder != "little"
            py_arrays: List[array.array] = []
            for buf in comp_buffers:
                a_py = array.array("f")
                a_py.frombytes(buf)
                if need_swap:
                    a_py.byteswap()
                py_arrays.append(a_py)
            total_samples = max((len(a) for a in py_arrays), default=0)
            mix_py = array.array("f", [0.0]) * total_samples
            for a_py in py_arrays:
                for idx, val in enumerate(a_py):
                    if math.isfinite(val):
                        mix_py[idx] += val
            peak_before = max((abs(v) for v in mix_py), default=0.0)
            gain = 1.0
            if peak_before > _PEAK_CEILING:
                gain = _PEAK_CEILING / peak_before
                for idx in range(total_samples):
                    mix_py[idx] *= gain
            spec.peak_before_norm = peak_before
            spec.gain_applied = gain
            if need_swap:
                mix_py.byteswap()
            mix_bytes = mix_py.tobytes()

        if out_pcm_by_id is not None:
            out_pcm_by_id[spec.dtx_wav_id] = mix_bytes

        if copy_files:
            dst_path = (output_dir.resolve() / rel_candidate).resolve()
            if transcode_wav_to_ogg:
                _encode_f32_to_ogg(
                    [mix_bytes],
                    dst_path,
                    ffmpeg_exe=ffmpeg_exe,
                    sample_rate=sample_rate,
                    ogg_quality=ogg_quality,
                )
                written_ogg_paths.append(dst_path)
            else:
                _write_f32_stereo_wav(mix_bytes, dst_path, sample_rate=sample_rate)
                written_wav_paths.append(dst_path)

    return merged_id_to_out_name, written_wav_paths, written_ogg_paths, warnings


def render_drums_stem_ogg(
    chart: BmsChart,
    placed_notes: Sequence[PlacedDtxNote],
    output_path: Path,
    *,
    slot_to_canonical_orig: Optional[Dict[str, str]] = None,
    auto_lp_orig_wav_id: Optional[str] = None,
    merged_pcm_by_id: Optional[Dict[str, bytes]] = None,
    wav_volumes: Optional[Dict[str, int]] = None,
    sample_rate: int = 44100,
    ogg_quality: float = 6.0,
    ffmpeg_path: Optional[str] = None,
    decode_cache: Optional[Dict[Tuple[Path, int], bytes]] = None,
    swap_lr: bool = False,
    swapped_wav_ids: Optional[Sequence[str]] = None,
) -> Tuple[Optional[PlacedDtxNote], Optional[MixStats], List[ConversionWarning]]:
    """Render drums.ogg stem from Drum (Tab 1-12, incl. Tom SE7) + Other (SE6) notes.

    Sample frame 0 aligns strictly with ``t_first`` of the earliest valid drum/Other note.
    Returns ``(first_note, MixStats, warnings)`` or ``(None, None, warnings)`` when empty.
    """
    warnings: List[ConversionWarning] = []
    ffmpeg_exe = _resolve_ffmpeg_optional(ffmpeg_path)
    swapped_set = _normalize_swapped_wav_set(chart, swapped_wav_ids)
    slot_canon = slot_to_canonical_orig or {}
    merged_pcm = merged_pcm_by_id or {}
    vols = wav_volumes or {}
    local_cache: Dict[Tuple[Path, int], bytes] = (
        decode_cache if decode_cache is not None else {}
    )

    stem_notes = [n for n in placed_notes if n.channel in DRUMS_STEM_SOURCE_CHANNELS]
    if not stem_notes:
        return None, None, warnings

    # Resolve valid audio sources and per-note amplitude gain
    # Item: (note, buf_key, raw_pcm_or_path, eff_swap, gain_float)
    candidate_items: List[Tuple[PlacedDtxNote, str, Union[bytes, Path], bool, float]] = []
    paths_needed: List[Path] = []
    seen_paths: set[Path] = set()
    missing_event_count = 0

    for note in stem_notes:
        vol_pct = vols.get(note.dtx_wav_id, 100)
        if vol_pct <= 0:
            continue
        vol_gain = max(0.0, min(1.0, float(vol_pct) / 100.0))

        if note.dtx_wav_id in merged_pcm:
            raw_mix = merged_pcm[note.dtx_wav_id]
            if not raw_mix:
                continue
            candidate_items.append((note, f"M:{note.dtx_wav_id}", raw_mix, False, vol_gain))
            continue

        if note.dtx_wav_id == AUTO_LP_RESONANT_WAV_ID:
            orig_wid = auto_lp_orig_wav_id or note.original_wav_id
        else:
            orig_wid = slot_canon.get(note.dtx_wav_id, note.original_wav_id)

        entry = chart.wavs.get(orig_wid) if orig_wid else None
        if entry is None or not entry.exists or entry.resolved_path is None:
            missing_event_count += 1
            continue
        rp = entry.resolved_path.resolve()
        eff_swap = bool(swap_lr) ^ (orig_wid in swapped_set)
        buf_key = f"F:{rp}:S{int(eff_swap)}"
        candidate_items.append((note, buf_key, rp, eff_swap, vol_gain))
        if (rp, sample_rate) not in local_cache and rp not in seen_paths:
            seen_paths.add(rp)
            paths_needed.append(rp)

    if not candidate_items:
        return None, None, warnings

    if paths_needed:
        max_workers = min(8, os.cpu_count() or 4, len(paths_needed))

        def _decode_worker(p: Path) -> Tuple[Path, Optional[bytes], Optional[str]]:
            raw, err = _decode_audio_to_f32_bytes(p, ffmpeg_exe, sample_rate)
            return p, raw, err

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for path, raw, err in executor.map(_decode_worker, paths_needed):
                if err is not None or raw is None:
                    warnings.append(
                        ConversionWarning(
                            code="AUDIO_DECODE_FAILED",
                            message=f"Failed to decode drums stem slice {path.name}: {err}",
                        )
                    )
                else:
                    local_cache[(path, sample_rate)] = raw

    buf_map: Dict[str, bytes] = {}
    playable_items: List[Tuple[PlacedDtxNote, str, float]] = []
    for note, buf_key, src_obj, eff_swap, vol_gain in candidate_items:
        if isinstance(src_obj, bytes):
            raw = src_obj
        else:
            raw_cached = local_cache.get((src_obj, sample_rate))
            if raw_cached is None:
                continue
            if buf_key not in buf_map:
                buf_map[buf_key] = (
                    _swap_f32_stereo_bytes(raw_cached) if eff_swap else raw_cached
                )
            raw = buf_map[buf_key]
        if buf_key not in buf_map:
            buf_map[buf_key] = raw
        if len(raw) >= 8:
            playable_items.append((note, buf_key, vol_gain))

    if not playable_items:
        return None, None, warnings

    first_note = min(
        playable_items,
        key=lambda item: (item[0].time_sec, item[0].measure, item[0].pos, item[0].channel),
    )[0]
    t_first = first_note.time_sec

    seen_frame_buf: set[Tuple[int, str, float]] = set()
    scheduled: List[Tuple[int, str, float]] = []
    max_end_frame = 0
    for note, buf_key, vol_gain in playable_items:
        start_frame = max(0, int(round((note.time_sec - t_first) * sample_rate)))
        dedup_key = (start_frame, buf_key, round(vol_gain, 4))
        if dedup_key in seen_frame_buf:
            continue
        seen_frame_buf.add(dedup_key)
        flen = len(buf_map[buf_key]) // 8
        scheduled.append((start_frame, buf_key, vol_gain))
        end_frame = start_frame + flen
        if end_frame > max_end_frame:
            max_end_frame = end_frame

    total_frames = max(max_end_frame, sample_rate // 2)
    mixed_event_count = len(scheduled)

    if np is not None:
        arr_map: Dict[str, "np.ndarray"] = {}
        for bk, raw in buf_map.items():
            arr = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
            if not np.all(np.isfinite(arr)):
                arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
            arr_map[bk] = arr
        mix = np.zeros((total_frames, 2), dtype=np.float32)
        for start_frame, bk, vol_gain in scheduled:
            buf = arr_map[bk]
            end_frame = min(total_frames, start_frame + len(buf))
            if end_frame <= start_frame:
                continue
            seg = buf[: end_frame - start_frame]
            if vol_gain < 0.9999:
                mix[start_frame:end_frame] += seg * np.float32(vol_gain)
            else:
                mix[start_frame:end_frame] += seg
        peak_before = float(np.max(np.abs(mix))) if total_frames > 0 else 0.0
        gain = 1.0
        if peak_before > _PEAK_CEILING:
            gain = _PEAK_CEILING / peak_before
            mix *= np.float32(gain)
        peak_after = float(np.max(np.abs(mix))) if total_frames > 0 else 0.0
        rms = float(np.sqrt(np.mean(mix.astype(np.float64) ** 2))) if total_frames > 0 else 0.0
        chunk_frames = 262_144
        pcm_chunks = [
            np.ascontiguousarray(mix[off : off + chunk_frames], dtype="<f4").tobytes()
            for off in range(0, total_frames, chunk_frames)
        ]
    else:
        need_swap = sys.byteorder != "little"
        py_map: Dict[str, array.array] = {}
        for bk, raw in buf_map.items():
            a_py = array.array("f")
            a_py.frombytes(raw)
            if need_swap:
                a_py.byteswap()
            py_map[bk] = a_py
        total_samples = total_frames * 2
        mix_py = array.array("f", [0.0]) * total_samples
        for start_frame, bk, vol_gain in scheduled:
            buf_py = py_map[bk]
            start_idx = start_frame * 2
            count = min(len(buf_py), total_samples - start_idx)
            for idx in range(count):
                val = buf_py[idx]
                if math.isfinite(val):
                    mix_py[start_idx + idx] += val * vol_gain
        peak_before = max((abs(v) for v in mix_py), default=0.0)
        gain = 1.0
        if peak_before > _PEAK_CEILING:
            gain = _PEAK_CEILING / peak_before
            for idx in range(total_samples):
                mix_py[idx] *= gain
        peak_after = peak_before * gain
        rms = (
            math.sqrt(sum(v * v for v in mix_py) / total_samples)
            if total_samples > 0
            else 0.0
        )
        if need_swap:
            mix_py.byteswap()
        chunk_samples = 524_288
        pcm_chunks = [
            mix_py[off : off + chunk_samples].tobytes()
            for off in range(0, total_samples, chunk_samples)
        ]

    _encode_f32_to_ogg(
        pcm_chunks,
        output_path,
        ffmpeg_exe=ffmpeg_exe,
        sample_rate=sample_rate,
        ogg_quality=ogg_quality,
    )

    stats = MixStats(
        output_path=output_path,
        sample_rate=sample_rate,
        total_frames=total_frames,
        duration_sec=total_frames / float(sample_rate),
        mixed_event_count=mixed_event_count,
        unique_files_decoded=len(buf_map),
        missing_event_count=missing_event_count,
        peak_before_norm=peak_before,
        peak_after_norm=peak_after,
        rms=rms,
        gain_applied=gain,
    )
    return first_note, stats, warnings


def write_silent_wav(
    output_path: Path,
    *,
    duration_ms: float = 10.0,
    sample_rate: int = 44100,
) -> Path:
    """Write a minimal silent 16-bit mono PCM WAV file (e.g. for #WAVZZ ghost LP slot)."""
    n_frames = max(1, int(round(sample_rate * duration_ms / 1000.0)))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return output_path
