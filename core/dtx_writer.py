"""DTXMania / DTXCreator .dtx file serializer with strict channel & encoding invariants."""

from __future__ import annotations

import math
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .constants import (
    DTX_MAX_BPM_SLOTS,
    LEGAL_DTX_NOTE_CHANNELS,
    MAX_MEASURE_DIVISIONS,
    QUANTIZE_FALLBACK_DIVISIONS,
    format_dtx_number,
    int_to_base36,
)
from .encoding import encode_dtx_text
from .models import BmsChart, BpmChange, ConversionWarning, PlacedDtxNote


def _lcm(a: int, b: int) -> int:
    return math.lcm(a, b)


def _serialize_measure_channel(
    measure: int,
    channel: str,
    slot_map: Dict[Fraction, str],
    warnings: List[ConversionWarning],
) -> str:
    """Serialize {Fraction(p, q): 'XX'} into a minimal-LCM DTX channel string."""
    if not slot_map:
        return ""

    denom = 1
    for pos in slot_map.keys():
        denom = _lcm(denom, pos.denominator)

    if denom > MAX_MEASURE_DIVISIONS:
        # Quantize onto 192-division DTXCreator grid to avoid pathological LCM strings
        quantized: Dict[Fraction, str] = {}
        dropped = 0
        for pos, token in sorted(slot_map.items()):
            idx = int(round(float(pos) * QUANTIZE_FALLBACK_DIVISIONS))
            if idx >= QUANTIZE_FALLBACK_DIVISIONS:
                idx = QUANTIZE_FALLBACK_DIVISIONS - 1
            elif idx < 0:
                idx = 0
            qpos = Fraction(idx, QUANTIZE_FALLBACK_DIVISIONS)
            if qpos in quantized and quantized[qpos] != token:
                dropped += 1
                continue
            quantized[qpos] = token

        warnings.append(
            ConversionWarning(
                code="MEASURE_GRID_QUANTIZED",
                message=(
                    f"Measure {measure:03d} channel {channel} had LCM={denom} > {MAX_MEASURE_DIVISIONS}; "
                    f"quantized to {QUANTIZE_FALLBACK_DIVISIONS}-division grid"
                    + (f" ({dropped} collided slot(s) dropped)." if dropped else ".")
                ),
            )
        )
        slot_map = quantized
        denom = 1
        for pos in slot_map.keys():
            denom = _lcm(denom, pos.denominator)

    cells = ["00"] * denom
    for pos, token in slot_map.items():
        idx = int(pos * denom)
        if 0 <= idx < denom:
            cells[idx] = token
    return "".join(cells)


def _build_dtx_bpm_table(
    bpm_changes: Sequence[BpmChange], warnings: List[ConversionWarning]
) -> Tuple[List[Tuple[str, float]], Dict[int, Dict[Fraction, str]]]:
    """Normalize all BMS channel 03 + 08 BPM changes into Base-36 #BPMxx + channel 08."""
    # Deduplicate by exact (measure, pos), preferring channel 08 over 03
    resolved: Dict[Tuple[int, Fraction], float] = {}
    ordered = sorted(
        bpm_changes,
        key=lambda bc: (bc.measure, bc.pos, 0 if bc.source_channel == "03" else 1),
    )
    for bc in ordered:
        if bc.bpm > 0 and bc.measure >= 0:
            resolved[(bc.measure, bc.pos)] = bc.bpm

    unique_bpms: Dict[float, str] = {}
    bpm_defs: List[Tuple[str, float]] = []
    by_measure: Dict[int, Dict[Fraction, str]] = defaultdict(dict)
    quantized_bpm_warned = False

    for (measure, pos), bpm_val in sorted(resolved.items()):
        key = round(bpm_val, 6)
        if key not in unique_bpms:
            if len(bpm_defs) >= DTX_MAX_BPM_SLOTS:
                # Fallback: round to 0.1 BPM if exceeding 1295 unique BPMs
                key = round(bpm_val, 1)
                if not quantized_bpm_warned:
                    quantized_bpm_warned = True
                    warnings.append(
                        ConversionWarning(
                            code="BPM_TABLE_LIMIT_QUANTIZED",
                            message="Exceeded 1295 unique BPM definitions; rounded additional BPMs to 0.1.",
                        )
                    )
                if key not in unique_bpms:
                    # Pick nearest existing BPM
                    key = min(unique_bpms.keys(), key=lambda existing: abs(existing - bpm_val))
            else:
                bid = int_to_base36(len(bpm_defs) + 1)
                unique_bpms[key] = bid
                bpm_defs.append((bid, key))
        by_measure[measure][pos] = unique_bpms[key]

    return bpm_defs, by_measure


def _build_sticky_measure_lengths(
    measure_lengths: Dict[int, float], max_measure: int
) -> List[Tuple[int, float]]:
    """Convert BMS single-measure channel 02 into DTX sticky channel 02 lines.

    In BMS, #01002:0.75 affects only measure 10 (measure 11 returns to 1.0).
    In DTX, #01002: 0.75 persists for all subsequent measures until explicitly reset!
    """
    emitted: List[Tuple[int, float]] = []
    current_dtx_len = 1.0
    end_m = max(max_measure, max(measure_lengths.keys(), default=0))

    for m in range(0, end_m + 1):
        desired = measure_lengths.get(m, 1.0)
        if desired <= 0:
            desired = 1.0
        if abs(desired - current_dtx_len) > 1e-9:
            emitted.append((m, desired))
            current_dtx_len = desired

    # Reset trailing non-4/4 measure length on the following bar (within 000-999)
    if abs(current_dtx_len - 1.0) > 1e-9 and end_m + 1 <= 999:
        emitted.append((end_m + 1, 1.0))

    return emitted


def _sanitize_header_value(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace(";", "；").strip()


def generate_dtx_text(
    chart: BmsChart,
    dtx_wav_table: Sequence[Tuple[str, str]],
    placed_notes: Sequence[PlacedDtxNote],
    lane_bind_lines: Sequence[str],
    *,
    effective_toggle_on: bool,
    dlevel: Optional[int] = None,
    comment: Optional[str] = None,
    preview_filename: Optional[str] = None,
) -> Tuple[str, List[ConversionWarning]]:
    """Generate complete DTX text and enforce Rule 6 channel safety invariants."""
    warnings: List[ConversionWarning] = []

    # Compute effective max measure across chart, notes, and BPM changes
    max_measure = chart.max_measure
    for note in placed_notes:
        if note.measure > max_measure:
            max_measure = note.measure
    for bc in chart.bpm_changes:
        if bc.measure > max_measure:
            max_measure = bc.measure
    if max_measure > 999:
        warnings.append(
            ConversionWarning(
                code="MEASURE_OVER_999",
                message=(
                    f"Chart reaches measure {max_measure}, exceeding standard 3-digit "
                    f"DTX measure range 000-999."
                ),
            )
        )

    # Resolve header fields
    title = _sanitize_header_value(chart.full_title)
    artist = _sanitize_header_value(chart.artist)
    if comment is not None:
        final_comment = _sanitize_header_value(comment)
    else:
        genre_clean = chart.genre.strip()
        if genre_clean:
            final_comment = _sanitize_header_value(f"{genre_clean} (w/ bms2dtx)")
        else:
            final_comment = "prepped w/ bms2dtx"

    if dlevel is not None:
        final_dlevel = max(1, min(999, int(dlevel)))
    elif chart.playlevel is not None:
        final_dlevel = max(1, min(999, int(chart.playlevel)))
    else:
        final_dlevel = 50

    lines: List[str] = [
        "; Created by bms2dtx",
        "",
        f"#TITLE: {title}",
        f"#ARTIST: {artist}",
        f"#COMMENT: {final_comment}",
        f"#BPM: {format_dtx_number(chart.initial_bpm)}",
        f"#DLEVEL: {final_dlevel}",
    ]
    if preview_filename:
        dtx_preview = preview_filename.replace("/", "\\").replace(";", "_")
        lines.append(f"#PREVIEW: {dtx_preview}")
    lines.append("")

    # WAV declarations + #BGMWAV (Toggle=OFF reserves 01 for bgm.ogg)
    for wid, filename in dtx_wav_table:
        dtx_filename = filename.replace("/", "\\").replace(";", "_")
        lines.append(f"#WAV{wid}: {dtx_filename}")
        if not effective_toggle_on and wid == "01":
            lines.append("#BGMWAV: 01")
    lines.append("")

    # Sticky channel 02 (measure length) declarations
    sticky_lengths = _build_sticky_measure_lengths(chart.measure_lengths, max_measure)
    if sticky_lengths:
        for m, length_val in sticky_lengths:
            lines.append(f"#{m:03d}02: {format_dtx_number(length_val)}")
        lines.append("")

    # Extended BPM definitions (#BPM01..#BPMZZ)
    bpm_defs, bpm_by_measure = _build_dtx_bpm_table(chart.bpm_changes, warnings)
    if bpm_defs:
        for bid, bpm_val in bpm_defs:
            lines.append(f"#BPM{bid}: {format_dtx_number(bpm_val)}")
        lines.append("")

    # Organize note events by measure -> channel -> {Fraction pos: dtx_wav_id}
    # STRICT INVARIANT (AGENTS.md Rule 6):
    # - NEVER allow out-of-bounds channels (e.g. SE33+ / 93+)
    # - NEVER allow multi-line stacking on channel 01 (BGM)
    notes_by_measure: Dict[int, Dict[str, Dict[Fraction, str]]] = defaultdict(
        lambda: defaultdict(dict)
    )

    if not effective_toggle_on:
        # Place bgm.ogg (01) once at measure 000 beat 0 (t = 0.0)
        notes_by_measure[0]["01"][Fraction(0)] = "01"

    for note in placed_notes:
        ch = note.channel.upper()
        if ch not in LEGAL_DTX_NOTE_CHANNELS:
            raise ValueError(
                f"Illegal DTX note channel {ch!r} rejected (Rule 6: no SE33+ or invalid channels)."
            )
        if ch == "01":
            raise ValueError(
                "Illegal placement on channel 01 (Rule 6: never stack arbitrary tracks onto BGM 01)."
            )
        slot_dict = notes_by_measure[note.measure][ch]
        if note.pos in slot_dict and slot_dict[note.pos] != note.dtx_wav_id:
            warnings.append(
                ConversionWarning(
                    code="DTX_CHANNEL_SLOT_OVERWRITE",
                    message=(
                        f"Measure {note.measure:03d} channel {ch} slot {note.pos} had "
                        f"duplicate placement; kept {slot_dict[note.pos]}."
                    ),
                )
            )
            continue
        slot_dict[note.pos] = note.dtx_wav_id

    # Emit main measure data sorted by measure index
    all_data_measures = sorted(set(notes_by_measure.keys()) | set(bpm_by_measure.keys()))
    channel_01_count_per_measure: Dict[int, int] = defaultdict(int)

    for m in all_data_measures:
        if m in bpm_by_measure:
            payload = _serialize_measure_channel(m, "08", bpm_by_measure[m], warnings)
            if payload:
                lines.append(f"#{m:03d}08: {payload}")

        ch_map = notes_by_measure.get(m, {})
        for ch in sorted(ch_map.keys()):
            if ch == "01":
                channel_01_count_per_measure[m] += 1
                if channel_01_count_per_measure[m] > 1:
                    raise RuntimeError(
                        f"Rule 6 violation: attempted multiple channel 01 lines in measure {m:03d}."
                    )
            payload = _serialize_measure_channel(m, ch, ch_map[ch], warnings)
            if payload:
                lines.append(f"#{m:03d}{ch}: {payload}")

    if lane_bind_lines:
        lines.append("")
        lines.extend(lane_bind_lines)

    lines.append("")
    return "\r\n".join(lines), warnings


def write_dtx_file(
    dtx_text: str, output_path: Path, *, utf8: bool = False
) -> Tuple[str, List[ConversionWarning]]:
    """Encode and write .dtx text to disk using cp932 (default) or utf-8 (--utf8)."""
    raw_bytes, encoding_used, warnings = encode_dtx_text(dtx_text, utf8=utf8)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(raw_bytes)
    return encoding_used, warnings
