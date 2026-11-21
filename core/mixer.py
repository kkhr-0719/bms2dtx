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
    DTX_MAX_WAV_SLOTS,
    IMAGE_MODE_KEEP,
    IMAGE_MODE_NONE,
    IMAGE_MODE_RESIZE,
    SUPPORTED_VIDEO_EXTENSIONS,
    int_to_base36,
)
from .models import BmsChart, ConversionWarning, MixStats, NoteEvent

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - tested via explicit fallback helper
    np = None  # type: ignore

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


def _decode_audio_to_f32_bytes(
    audio_path: Path, ffmpeg_exe: str, sample_rate: int
) -> Tuple[Optional[bytes], Optional[str]]:
    """Decode any audio file supported by ffmpeg to stereo float32le PCM bytes."""
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
    ffmpeg_exe: str,
    sample_rate: int,
    ogg_quality: float,
) -> None:
    """Encode stereo float32le PCM chunks to an OGG Vorbis file via ffmpeg."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
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
) -> Tuple[MixStats, List[ConversionWarning]]:
    """Offline-mix all unmapped BMS events along the exact timeline and export bgm.ogg.

    Deduplicates identical (start_frame, wav_id) triggers to prevent +6 dB phase spikes,
    and applies strictly downward-only peak limiting above 0.99 (never boosts quiet audio).
    cut_same_wav is reserved for future mono voice-cut mode; v1 default is polyphonic.
    """
    _ = cut_same_wav
    ffmpeg_exe = find_ffmpeg(ffmpeg_path)
    warnings: List[ConversionWarning] = []

    # Collect unique audio paths needed for unmapped events
    unique_paths: List[Path] = []
    seen_paths: set[Path] = set()
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
        if entry.resolved_path not in seen_paths:
            seen_paths.add(entry.resolved_path)
            unique_paths.append(entry.resolved_path)

    # Decode each unique file once in parallel (reusing decode_cache if provided)
    decoded_raw: Dict[Path, bytes] = {}
    paths_to_decode: List[Path] = []
    for p in unique_paths:
        cache_key = (p, sample_rate)
        if decode_cache is not None and cache_key in decode_cache:
            decoded_raw[p] = decode_cache[cache_key]
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
                    decoded_raw[path] = raw
                    if decode_cache is not None:
                        decode_cache[(path, sample_rate)] = raw

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
            decoded_raw, scheduled, total_frames, swap_lr=swap_lr
        )
    else:
        pcm_chunks, peak_before, peak_after, rms, gain = _mix_pure_python(
            decoded_raw, scheduled, total_frames, swap_lr=swap_lr
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
    """Copy audio file or swap L/R stereo channels via ffmpeg; mono files always plain-copy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not swap_lr or not ffmpeg_exe:
        shutil.copy2(src, dst)
        return None
    channels = _get_audio_channels(src, ffmpeg_exe)
    if channels == 1:
        shutil.copy2(src, dst)
        return None

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
    swap_lr: bool = False,
) -> Tuple[Dict[str, str], List[Path], List[Path], List[ConversionWarning]]:
    """Copy or transcode retained slice files into output_dir (NEVER mutating source_dir).

    Returns:
        (orig_wav_id -> output_relative_filename, copied_paths, transcoded_paths, warnings)
    """
    warnings: List[ConversionWarning] = []
    copied_files: List[Path] = []
    transcoded_files: List[Path] = []
    orig_to_out_name: Dict[str, str] = {}

    # Track source_path -> assigned relative output path (deduplicate shared files)
    src_to_rel_out: Dict[Path, str] = {}
    claimed_rel_lower: Dict[str, Optional[Path]] = {}

    # Reserve bgm.ogg in Toggle=OFF so no retained slice accidentally overwrites it
    if reserve_bgm_ogg:
        claimed_rel_lower["bgm.ogg"] = (output_dir / "bgm.ogg").resolve()

    ffmpeg_exe: Optional[str] = None
    transcode_jobs: List[Tuple[Path, Path]] = []
    copy_jobs: List[Tuple[Path, Path]] = []

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

        if copy_files:
            dst_path = _safe_output_dst(output_dir, rel_candidate, src_path)
            if is_wav_transcode:
                transcode_jobs.append((src_path, dst_path))
            else:
                copy_jobs.append((src_path, dst_path))

    if swap_lr and (copy_jobs or transcode_jobs):
        try:
            ffmpeg_exe = find_ffmpeg(ffmpeg_path)
        except FfmpegNotFoundError:
            if transcode_jobs:
                raise
            warnings.append(
                ConversionWarning(
                    code="SWAP_LR_NO_FFMPEG",
                    message="ffmpeg not found; retained slices copied without L/R channel swap.",
                )
            )
            ffmpeg_exe = None

    # Execute copies (or L/R swap on stereo copies)
    if copy_jobs:
        if swap_lr and ffmpeg_exe:
            max_workers = min(8, os.cpu_count() or 4, len(copy_jobs))

            def _copy_or_swap_one(pair: Tuple[Path, Path]) -> Tuple[Path, Optional[str]]:
                src, dst = pair
                err = _copy_or_swap_lr_audio(
                    src,
                    dst,
                    swap_lr=True,
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
            for src, dst in copy_jobs:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied_files.append(dst)

    # Execute .wav -> .ogg transcodes in parallel
    if transcode_jobs:
        if ffmpeg_exe is None:
            ffmpeg_exe = find_ffmpeg(ffmpeg_path)
        max_workers = min(8, os.cpu_count() or 4, len(transcode_jobs))

        def _transcode_one(pair: Tuple[Path, Path]) -> Tuple[Path, Optional[str]]:
            src, dst = pair
            dst.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                ffmpeg_exe,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(src),
            ]
            if swap_lr and _get_audio_channels(src, ffmpeg_exe) != 1:
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

    rel_candidate = _sanitize_rel_candidate(entry.resolved_name, src_path)
    rel_candidate = _disambiguate_rel(
        rel_candidate, None, claimed_lower, allow_reuse_same_src=False
    )
    rel_str = rel_candidate.as_posix()

    dtx_rel = rel_str.replace("/", "\\")
    dst_path: Optional[Path] = None
    if copy_files:
        dst_path = _safe_output_dst(output_dir, rel_candidate, src_path)
        ffmpeg_exe: Optional[str] = None
        if swap_lr:
            try:
                ffmpeg_exe = find_ffmpeg(ffmpeg_path)
            except FfmpegNotFoundError:
                ffmpeg_exe = None
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
