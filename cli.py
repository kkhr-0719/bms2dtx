"""Command-line entrypoint for bms2dtx core engine (v1 Toggle=OFF default)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import (
    ConversionOptions,
    MappingConfig,
    TabId,
    batch_convert,
    convert_bms,
    inspect_bms,
    parse_auto_lp_config,
)


def _safe_print(text: str, *, stream: Any = None) -> None:
    target = sys.stdout if stream is None else stream
    try:
        print(text, file=target)
    except UnicodeEncodeError:
        enc = getattr(target, "encoding", None) or "utf-8"
        safe_text = text.encode(enc, errors="replace").decode(enc, errors="replace")
        print(safe_text, file=target)


def _print_json(obj: Any) -> None:
    pretty = json.dumps(obj, ensure_ascii=False, indent=2)
    try:
        print(pretty)
    except UnicodeEncodeError:
        # Preserve all Unicode codepoints losslessly as \uXXXX on legacy Windows consoles
        print(json.dumps(obj, ensure_ascii=True, indent=2))


def _load_mapping_json(path: Path) -> MappingConfig:
    data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    tab_wavs: Dict[TabId, List[str]] = {}
    for k, v in data.get("tab_wavs", {}).items():
        tab = TabId(k.strip().upper().replace(" ", "_"))
        tab_wavs[tab] = [str(item) for item in v]

    default_binds: Dict[TabId, str] = {}
    for k, v in data.get("default_lane_binds", {}).items():
        tab = TabId(k.strip().upper().replace(" ", "_"))
        default_binds[tab] = str(v)

    return MappingConfig(
        tab_wavs=tab_wavs,
        default_lane_binds=default_binds,
        toggle_on=bool(data.get("toggle_on", False)),
        slicer_bgm_columns=[int(x) for x in data.get("slicer_bgm_columns", [])],
        overflow_strategy=str(data.get("overflow_strategy", "fail")),
        separate_lbd=bool(data.get("separate_lbd", False)),
        default_bind_mode=str(data.get("default_bind_mode", "manual")),
        auto_lp=parse_auto_lp_config(data.get("auto_lp")),
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bms2dtx",
        description="Convert BMS/BME charts to DTXMania/DTXCreator .dtx (v1 Toggle=OFF default).",
    )
    parser.add_argument("input", type=Path, help="Input .bms/.bme file (or directory with --batch)")
    parser.add_argument("--inspect", action="store_true", help="Inspect BMS file and print JSON summary")
    parser.add_argument("--out", "-o", type=Path, help="Output directory (must differ from BMS folder)")
    parser.add_argument("--map-json", type=Path, help="Optional JSON file specifying 15-Tab mapping")
    parser.add_argument("--utf8", action="store_true", help="Export .dtx in UTF-8 instead of CP932")
    parser.add_argument(
        "--transcode-ogg",
        action="store_true",
        help="Transcode retained .wav slices to .ogg in output directory",
    )
    parser.add_argument("--no-bgm", action="store_true", help="Skip rendering bgm.ogg (dry/fast .dtx test)")
    parser.add_argument("--dlevel", type=int, default=None, help="Override DTX #DLEVEL")
    parser.add_argument("--ffmpeg", type=str, default=None, help="Explicit path to ffmpeg executable")
    parser.add_argument("--batch", action="store_true", help="Convert all .bms/.bme files in input directory")
    args = parser.parse_args(argv)

    if args.inspect:
        chart = inspect_bms(args.input)
        suffix_corrected = sum(1 for w in chart.wavs.values() if w.suffix_corrected)
        missing = sum(1 for w in chart.wavs.values() if not w.exists)
        summary = {
            "source": str(chart.source_path),
            "encoding": chart.encoding_used,
            "title": chart.full_title,
            "artist": chart.artist,
            "genre": chart.genre,
            "initial_bpm": chart.initial_bpm,
            "base": chart.base,
            "is_base62": chart.is_base62,
            "toggle_off_locked": chart.toggle_off_locked,
            "wav_count": len(chart.wavs),
            "suffix_corrected_count": suffix_corrected,
            "missing_wav_count": missing,
            "playable_events": len(chart.events),
            "ln_end_events": len(chart.ln_end_events),
            "max_measure": chart.max_measure,
            "duration_sec": round(chart.song_duration_sec, 3),
            "raw_bgm_columns": chart.max_bgm_columns_raw,
            "non_empty_bgm_columns": [t.track_label for t in chart.bgm_tracks],
            "pruned_empty_tracks": chart.pruned_empty_tracks,
            "warnings": [{"code": w.code, "message": w.message} for w in chart.warnings],
        }
        _print_json(summary)
        return 0

    if not args.out:
        parser.error("--out is required unless --inspect is specified.")

    mapping = _load_mapping_json(args.map_json) if args.map_json else MappingConfig(toggle_on=False)

    if args.batch:
        if not args.input.is_dir():
            parser.error("--batch requires input to be a directory.")
        bms_files = sorted(
            [p for p in args.input.iterdir() if p.is_file() and p.suffix.lower() in (".bms", ".bme")]
        )
        if not bms_files:
            _safe_print(f"No .bms/.bme files found in {args.input}", stream=sys.stderr)
            return 1
        results = batch_convert(
            bms_files,
            args.out,
            mapping=mapping,
            utf8=args.utf8,
            transcode_wav_to_ogg=args.transcode_ogg,
            dlevel=args.dlevel,
            ffmpeg_path=args.ffmpeg,
            render_bgm=not args.no_bgm,
        )
        for src, res in results.items():
            rms_info = (
                f"rms={res.mix_stats.rms:.4f}, peak={res.mix_stats.peak_after_norm:.4f}"
                if res.mix_stats
                else "no-bgm"
            )
            _safe_print(
                f"[OK] {src.name} -> {res.dtx_path} ({res.dtx_encoding}, "
                f"notes={res.placed_note_count}, {rms_info}, warnings={len(res.warnings)})"
            )
        return 0

    opts = ConversionOptions(
        output_dir=args.out,
        utf8=args.utf8,
        transcode_wav_to_ogg=args.transcode_ogg,
        dlevel=args.dlevel,
        ffmpeg_path=args.ffmpeg,
        render_bgm=not args.no_bgm,
    )
    res = convert_bms(args.input, opts, mapping=mapping)
    _safe_print(f"Converted: {res.dtx_path} (encoding={res.dtx_encoding})")
    if res.bgm_path and res.mix_stats:
        _safe_print(
            f"BGM: {res.bgm_path} (duration={res.mix_stats.duration_sec:.2f}s, "
            f"events={res.mix_stats.mixed_event_count}, rms={res.mix_stats.rms:.4f}, "
            f"peak={res.mix_stats.peak_after_norm:.4f})"
        )
    if res.warnings:
        _safe_print("Warnings:")
        for w in res.warnings:
            _safe_print(f"  [{w.code}] {w.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
