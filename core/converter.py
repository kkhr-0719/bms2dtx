"""High-level conversion facade orchestrating parse -> map -> mix/allocate -> write .dtx."""

from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .audio_resolver import ParentFolderAudioIndex
from .constants import (
    AUTO_LP_CHIP_NONE,
    AUTO_LP_RESONANT_WAV_ID,
    AUTO_LP_SILENT_FILENAME,
    AUTO_LP_SILENT_WAV_ID,
    AUTO_LP_THRESHOLD_MS,
    AUTO_LP_THRESHOLD_NOTE,
    AUTO_LP_VALID_DENOMINATORS,
    PROJECT_MAPPING_FILENAME,
    TabId,
)
from .dtx_writer import generate_dtx_text, write_dtx_file
from .mapping import build_mapping_plan
from .mixer import (
    materialize_bga,
    materialize_images,
    materialize_preview_audio,
    materialize_retained_slices,
    render_bgm_ogg,
    write_silent_wav,
)
from .models import (
    AutoLpConfig,
    BmsChart,
    ConversionOptions,
    ConversionResult,
    ConversionWarning,
    MappingConfig,
    MixStats,
    PlacedDtxNote,
)
from .parser import parse_bms
from .se_allocator import allocate_se_channels


def parse_auto_lp_config(raw: Any) -> AutoLpConfig:
    """Safely parse serialized auto_lp dictionary into an AutoLpConfig."""
    if not isinstance(raw, dict):
        return AutoLpConfig()
    chip_mode = str(raw.get("chip_mode", AUTO_LP_CHIP_NONE)).strip() or AUTO_LP_CHIP_NONE
    try:
        volume = max(0, min(100, int(raw.get("volume", 50))))
    except (TypeError, ValueError):
        volume = 50
    thresh_mode = str(raw.get("threshold_mode", AUTO_LP_THRESHOLD_NOTE)).strip().lower()
    if thresh_mode not in (AUTO_LP_THRESHOLD_NOTE, AUTO_LP_THRESHOLD_MS):
        thresh_mode = AUTO_LP_THRESHOLD_NOTE
    try:
        note_num = max(1, int(raw.get("note_num", 1)))
    except (TypeError, ValueError):
        note_num = 1
    try:
        raw_den = int(raw.get("note_den", 4))
    except (TypeError, ValueError):
        raw_den = 4
    note_den = raw_den if raw_den in AUTO_LP_VALID_DENOMINATORS else 4
    try:
        threshold_ms = max(0.0, float(raw.get("threshold_ms", 250.0)))
    except (TypeError, ValueError):
        threshold_ms = 250.0
    return AutoLpConfig(
        chip_mode=chip_mode,
        volume=volume,
        threshold_mode=thresh_mode,
        note_num=note_num,
        note_den=note_den,
        threshold_ms=threshold_ms,
    )


def build_project_mapping_dict(
    config: MappingConfig,
    *,
    swap_lr: bool = False,
    tab_wavs_override: Optional[Dict[TabId, List[str]]] = None,
    default_binds_override: Optional[Dict[TabId, str]] = None,
) -> Dict[str, Any]:
    """Serialize full mapping configuration (for __bms2dtx.json auto-save & Save JSON)."""
    src_tabs = tab_wavs_override if tab_wavs_override is not None else config.tab_wavs
    src_binds = (
        default_binds_override
        if default_binds_override is not None
        else config.default_lane_binds
    )
    alp = config.auto_lp
    data: Dict[str, Any] = {
        "version": "0.3.0",
        "tab_wavs": {t.value: list(ids) for t, ids in src_tabs.items() if ids},
        "default_lane_binds": {t.value: w for t, w in src_binds.items() if w},
        "default_bind_mode": config.default_bind_mode,
        "toggle_on": False,
        "separate_lbd": bool(config.separate_lbd),
        "swap_lr": bool(swap_lr),
        "auto_lp": {
            "chip_mode": str(alp.chip_mode or AUTO_LP_CHIP_NONE),
            "volume": max(0, min(100, int(alp.volume))),
            "threshold_mode": str(alp.threshold_mode or AUTO_LP_THRESHOLD_NOTE),
            "note_num": max(1, int(alp.note_num)),
            "note_den": int(alp.note_den) if int(alp.note_den) in AUTO_LP_VALID_DENOMINATORS else 4,
            "threshold_ms": max(0.0, float(alp.threshold_ms)),
        },
    }
    return data


def inspect_bms(path: Path | str) -> BmsChart:
    """Parse and inspect a BMS/BME chart in strictly read-only mode."""
    return parse_bms(path)


def convert_bms(
    source_path: Path | str,
    options: ConversionOptions,
    mapping: Optional[MappingConfig] = None,
    *,
    parsed_chart: Optional[BmsChart] = None,
    decode_cache: Optional[Dict[Tuple[Path, int], bytes]] = None,
) -> ConversionResult:
    """Convert a BMS/BME file to DTX (+ bgm.ogg in Toggle=OFF) into options.output_dir.

    Strictly enforces AGENTS.md Rule 1: never writes to or mutates the BMS source folder.
    """
    chart = parsed_chart if parsed_chart is not None else parse_bms(source_path)
    config = mapping if mapping is not None else MappingConfig(toggle_on=False)

    output_dir = Path(options.output_dir)
    resolved_out = output_dir.resolve()
    resolved_src_dir = chart.source_dir.resolve()
    if resolved_out == resolved_src_dir:
        raise ValueError(
            "Output directory must be different from the BMS source directory "
            "to guarantee original files remain read-only (AGENTS.md Rule 1)."
        )

    all_warnings: List[ConversionWarning] = list(chart.warnings)

    if chart.preview is None and chart.headers.get("PREVIEW", "").strip():
        raw_prev = chart.headers["PREVIEW"].strip()
        prev_entry = ParentFolderAudioIndex(chart.source_dir).resolve_wav("PREVIEW", raw_prev)
        chart.preview = prev_entry
        if not prev_entry.exists or prev_entry.resolved_path is None:
            all_warnings.append(
                ConversionWarning(
                    code="MISSING_PREVIEW_FILE",
                    message=(
                        f"#PREVIEW audio file {raw_prev!r} could not be found in "
                        f"{chart.source_dir} even after suffix fallback."
                    ),
                )
            )

    referenced_src_files = {
        entry.resolved_path.resolve()
        for entry in chart.wavs.values()
        if entry.exists and entry.resolved_path is not None
    }
    if chart.preview is not None and chart.preview.exists and chart.preview.resolved_path is not None:
        referenced_src_files.add(chart.preview.resolved_path.resolve())
    referenced_src_dirs = {p.parent for p in referenced_src_files}
    if resolved_out in referenced_src_dirs:
        raise ValueError(
            "Output directory must not target an existing BMS audio subdirectory "
            "to guarantee original files remain read-only (AGENTS.md Rule 1)."
        )

    plan = build_mapping_plan(chart, config)

    effective_toggle_on = plan.effective_toggle_on
    toggle_off_forced = plan.toggle_off_forced
    placed_notes: List[PlacedDtxNote] = list(plan.tab_placed_notes)
    nudged_overflow_count = 0

    # If Toggle=ON is active, allocate remaining events via Slicer + Collapse
    if effective_toggle_on:
        se_result = allocate_se_channels(
            plan.unmapped_events,
            plan.wav_id_map,
            slicer_bgm_columns=config.slicer_bgm_columns,
            guitar_enabled=plan.guitar_enabled,
            bass_enabled=plan.bass_enabled,
            overflow_strategy=config.overflow_strategy,
            extra_collapse_events=plan.tab_collision_events,
        )
        if se_result.fallback_to_toggle_off:
            # Re-plan cleanly under Toggle=OFF (reserving 01 for bgm.ogg)
            # Drop stale Toggle=ON plan warnings (e.g. UNDEFINED_WAV_SKIPPED_TOGGLE_ON)
            fallback_cfg = MappingConfig(
                tab_wavs=config.tab_wavs,
                default_lane_binds=config.default_lane_binds,
                toggle_on=False,
                slicer_bgm_columns=[],
                overflow_strategy="fail",
                separate_lbd=config.separate_lbd,
                default_bind_mode=config.default_bind_mode,
                auto_lp=config.auto_lp,
            )
            plan = build_mapping_plan(chart, fallback_cfg, auto_merge_duplicates=False)
            all_warnings.extend(plan.warnings)
            all_warnings.extend(se_result.warnings)
            effective_toggle_on = False
            toggle_off_forced = True
            placed_notes = list(plan.tab_placed_notes)
        else:
            all_warnings.extend(plan.warnings)
            all_warnings.extend(se_result.warnings)
            placed_notes.extend(se_result.placed_notes)
            nudged_overflow_count = se_result.nudged_count
    else:
        all_warnings.extend(plan.warnings)

    dtx_name = options.output_dtx_name or f"{chart.source_path.stem}.dtx"
    if not dtx_name.lower().endswith(".dtx"):
        dtx_name += ".dtx"
    dtx_path = output_dir / dtx_name
    if dtx_path.resolve() == chart.source_path.resolve() or dtx_path.resolve() in referenced_src_files:
        raise ValueError(
            f"Refusing to overwrite original BMS source file: {dtx_path}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    # Materialize retained slice files (copy or transcode .wav -> .ogg)
    orig_to_out_name, copied_files, transcoded_files, mat_warnings = (
        materialize_retained_slices(
            chart,
            plan.ordered_retained_orig_ids,
            output_dir,
            transcode_wav_to_ogg=options.transcode_wav_to_ogg,
            copy_files=options.copy_retained_audio,
            ogg_quality=options.ogg_quality,
            ffmpeg_path=options.ffmpeg_path,
            reserve_bgm_ogg=not effective_toggle_on,
            swap_lr=options.swap_lr,
        )
    )
    all_warnings.extend(mat_warnings)

    claimed_names: List[str] = list(orig_to_out_name.values())

    # Materialize #PREVIEW audio file (copy/swap, avoiding name collision with slices or bgm.ogg)
    preview_dtx_rel, preview_dst_path = materialize_preview_audio(
        chart,
        output_dir,
        claimed_names,
        copy_files=options.copy_retained_audio,
        reserve_bgm_ogg=not effective_toggle_on,
        swap_lr=options.swap_lr,
        ffmpeg_path=options.ffmpeg_path,
        ogg_quality=options.ogg_quality,
    )
    if preview_dtx_rel:
        claimed_names.append(preview_dtx_rel)
    if preview_dst_path is not None and preview_dst_path not in copied_files:
        copied_files.append(preview_dst_path)

    # Materialize #STAGEFILE / #PREIMAGE / #BACKGROUND images
    (
        stagefile_rel,
        preimage_rel,
        background_rel,
        stagefile_dst,
        preimage_dst,
        background_dst,
        img_warnings,
    ) = materialize_images(
        chart,
        output_dir,
        claimed_names,
        image_mode=options.image_mode,
        copy_files=options.copy_retained_audio,
        reserve_bgm_ogg=not effective_toggle_on,
        ffmpeg_path=options.ffmpeg_path,
    )
    all_warnings.extend(img_warnings)
    for rel_item in (stagefile_rel, preimage_rel, background_rel):
        if rel_item:
            claimed_names.append(rel_item)
    for dst_item in (stagefile_dst, preimage_dst, background_dst):
        if dst_item is not None and dst_item not in copied_files:
            copied_files.append(dst_item)

    # Experimental BGA passthrough (#BMPxx/#AVIxx + channels 04/07/54)
    bga_headers: List[Tuple[str, str, str]] = []
    bga_placed_events: List[Tuple[int, Fraction, str, str]] = []
    bga_copied_files: List[Path] = []
    if options.export_bga:
        bga_headers, bga_placed_events, bga_copied_files, bga_warnings = materialize_bga(
            chart,
            output_dir,
            claimed_names,
            copy_files=options.copy_retained_audio,
            reserve_bgm_ogg=not effective_toggle_on,
        )
        all_warnings.extend(bga_warnings)

    # Build final DTX #WAVxx table in exact sequential Base-36 order (01..ZX),
    # then append reserved Auto-LP ZY / ZZ entries if enabled.
    dtx_wav_table: List[Tuple[str, str]] = []
    if not effective_toggle_on:
        dtx_wav_table.append(("01", "bgm.ogg"))
    slot_to_out_name: Dict[str, str] = {}
    for orig_wid in plan.ordered_retained_orig_ids:
        dtx_wid = plan.wav_id_map[orig_wid]
        out_name = orig_to_out_name.get(orig_wid)
        if out_name is None:
            entry = chart.wavs.get(orig_wid)
            out_name = entry.resolved_name if entry else f"missing_{orig_wid}.wav"
        dtx_wav_table.append((dtx_wid, out_name))
        slot_to_out_name[dtx_wid] = out_name

    if plan.auto_lp_orig_wav_id is not None:
        zy_out = orig_to_out_name.get(plan.auto_lp_orig_wav_id)
        if zy_out is None:
            mapped_slot = plan.wav_id_map.get(plan.auto_lp_orig_wav_id)
            if mapped_slot is not None:
                zy_out = slot_to_out_name.get(mapped_slot)
        if zy_out is not None:
            dtx_wav_table.append((AUTO_LP_RESONANT_WAV_ID, zy_out))

    if plan.auto_lp_use_silent_zz:
        claimed_lower = {name.replace("\\", "/").lower() for name in claimed_names}
        if not effective_toggle_on:
            claimed_lower.add("bgm.ogg")
        silent_name = AUTO_LP_SILENT_FILENAME
        if silent_name.lower() in claimed_lower:
            stem = Path(AUTO_LP_SILENT_FILENAME).stem
            suffix = Path(AUTO_LP_SILENT_FILENAME).suffix
            counter = 1
            while f"{stem}_{counter}{suffix}".lower() in claimed_lower:
                counter += 1
            silent_name = f"{stem}_{counter}{suffix}"
        claimed_names.append(silent_name)
        if options.copy_retained_audio:
            silent_path = write_silent_wav(output_dir / silent_name)
            copied_files.append(silent_path)
        dtx_wav_table.append((AUTO_LP_SILENT_WAV_ID, silent_name))

    # Render bgm.ogg in Toggle=OFF mode
    bgm_path: Optional[Path] = None
    mix_stats: Optional[MixStats] = None
    if not effective_toggle_on and options.render_bgm:
        bgm_path = output_dir / "bgm.ogg"
        if bgm_path.resolve() in referenced_src_files:
            raise ValueError(
                f"Refusing to overwrite original BMS audio file with bgm.ogg: {bgm_path}"
            )
        mix_stats, mix_warnings = render_bgm_ogg(
            chart,
            plan.unmapped_events,
            bgm_path,
            sample_rate=options.sample_rate,
            ogg_quality=options.ogg_quality,
            ffmpeg_path=options.ffmpeg_path,
            decode_cache=decode_cache,
            cut_same_wav=options.cut_same_wav,
            swap_lr=options.swap_lr,
        )
        all_warnings.extend(mix_warnings)

    # Generate and write .dtx file
    dtx_text, writer_warnings = generate_dtx_text(
        chart,
        dtx_wav_table,
        placed_notes,
        plan.lane_bind_lines,
        effective_toggle_on=effective_toggle_on,
        dlevel=options.dlevel,
        comment=options.comment,
        preview_filename=preview_dtx_rel,
        stagefile_filename=stagefile_rel,
        preimage_filename=preimage_rel,
        background_filename=background_rel,
        dtx_wav_labels=plan.dtx_wav_labels,
        wav_labels=options.wav_labels,
        wav_volumes=plan.wav_volumes,
        bga_headers=bga_headers,
        bga_placed_events=bga_placed_events,
    )
    all_warnings.extend(writer_warnings)

    dtx_encoding, enc_warnings = write_dtx_file(
        dtx_text, dtx_path, utf8=options.utf8
    )
    all_warnings.extend(enc_warnings)

    # Auto-save reproducible project mapping JSON (__bms2dtx.json) inside output_dir
    project_json_path = output_dir / PROJECT_MAPPING_FILENAME
    if project_json_path.resolve() not in referenced_src_files:
        project_dict = build_project_mapping_dict(config, swap_lr=options.swap_lr)
        project_json_path.write_text(
            json.dumps(project_dict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return ConversionResult(
        chart=chart,
        output_dir=output_dir,
        dtx_path=dtx_path,
        bgm_path=bgm_path,
        effective_toggle_on=effective_toggle_on,
        toggle_off_forced=toggle_off_forced,
        dtx_encoding=dtx_encoding,
        wav_id_map=plan.wav_id_map,
        dtx_wav_table=dtx_wav_table,
        lane_bind_lines=plan.lane_bind_lines,
        placed_note_count=len(placed_notes),
        mix_stats=mix_stats,
        copied_files=copied_files,
        transcoded_files=transcoded_files,
        warnings=all_warnings,
        nudged_overflow_count=nudged_overflow_count,
        preview_path=preview_dst_path,
        stagefile_path=stagefile_dst,
        preimage_path=preimage_dst,
        background_path=background_dst,
        bga_copied_files=bga_copied_files,
        auto_lp_placed_count=plan.auto_lp_placed_count,
        project_json_path=project_json_path,
    )


def batch_convert(
    bms_paths: Sequence[Path | str],
    output_root: Path | str,
    *,
    mapping: Optional[MappingConfig] = None,
    utf8: bool = False,
    transcode_wav_to_ogg: bool = False,
    dlevel: Optional[int] = None,
    ffmpeg_path: Optional[str] = None,
    render_bgm: bool = True,
) -> Dict[Path, ConversionResult]:
    """Batch-convert multiple BMS/BME files into isolated subdirectories under output_root."""
    out_root = Path(output_root)
    resolved_out_root = out_root.resolve()
    resolved_sources = [Path(raw_path) for raw_path in bms_paths]

    # Rule 1 guard: refuse writing batch outputs into any BMS source folder
    for src in resolved_sources:
        src_dir = src.parent.resolve()
        if resolved_out_root == src_dir or resolved_out_root.is_relative_to(src_dir):
            raise ValueError(
                f"Batch output root ({out_root}) must not be inside BMS source directory "
                f"({src.parent}) to guarantee original files remain read-only (AGENTS.md Rule 1)."
            )

    results: Dict[Path, ConversionResult] = {}
    shared_decode_cache: Dict[Tuple[Path, int], bytes] = {}
    claimed_subdirs: set[str] = set()
    try:
        for src in resolved_sources:
            base_name = src.stem
            sub_name = base_name
            if sub_name.lower() in claimed_subdirs:
                ext_tag = src.suffix.lstrip(".").lower()
                candidate = f"{base_name}_{ext_tag}" if ext_tag else f"{base_name}_1"
                counter = 1
                while candidate.lower() in claimed_subdirs:
                    counter += 1
                    candidate = f"{base_name}_{counter}"
                sub_name = candidate
            claimed_subdirs.add(sub_name.lower())

            sub_out = out_root / sub_name
            opts = ConversionOptions(
                output_dir=sub_out,
                output_dtx_name=f"{sub_name}.dtx",
                utf8=utf8,
                transcode_wav_to_ogg=transcode_wav_to_ogg,
                dlevel=dlevel,
                ffmpeg_path=ffmpeg_path,
                render_bgm=render_bgm,
            )
            results[src] = convert_bms(
                src, opts, mapping=mapping, decode_cache=shared_decode_cache
            )
    finally:
        shared_decode_cache.clear()
    return results
