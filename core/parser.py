"""BMS / BME parser strictly obeying AGENTS.md Rules 1-6."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .audio_resolver import ParentFolderAudioIndex
from .constants import (
    BMS_BGA_CHANNELS,
    BMS_INVISIBLE_CHANNELS,
    BMS_LN_CHANNELS,
    BMS_MINE_CHANNELS,
    BMS_PLAYABLE_NORMAL_CHANNELS,
    DTX_MAX_WAV_SLOTS,
    SUPPORTED_BGA_EXTENSIONS,
    SUPPORTED_IMAGE_EXTENSIONS,
)
from .encoding import read_bms_text
from .models import (
    BgaEvent,
    BmsChart,
    BpmChange,
    ConversionWarning,
    NoteEvent,
    TrackSummary,
    WavEntry,
)
from .timeline import build_timeline_for_chart

_DATA_LINE_RE = re.compile(r"^#(\d{3})([0-9A-Za-z]{2})\s*:(.*)$")
_WAV_HEADER_RE = re.compile(r"^#WAV([0-9A-Za-z]{2})\s+(.+)$", re.IGNORECASE)
_BMP_HEADER_RE = re.compile(r"^#(?:BMP|AVI)([0-9A-Za-z]{2})\s+(.+)$", re.IGNORECASE)
_BPM_EXT_RE = re.compile(r"^#(?:EX)?BPM([0-9A-Za-z]{2})\s+(.+)$", re.IGNORECASE)
_STOP_HEADER_RE = re.compile(r"^#STOP([0-9A-Za-z]{2})\s+(.+)$", re.IGNORECASE)
_GENERIC_HEADER_RE = re.compile(r"^#([A-Za-z0-9_]+)(?:\s+(.*))?$")
_VALID_TOKEN_RE = re.compile(r"^[0-9A-Za-z]{2}$")


@dataclass
class _ControlFrame:
    kind: str  # "if" or "switch"
    rand_value: int
    active: bool
    matched_any: bool
    parent_active: bool


def _parse_float(raw: str) -> Optional[float]:
    cleaned = raw.strip().replace(",", ".")
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(val):
        return None
    return val


def _parse_int(raw: str) -> Optional[int]:
    cleaned = raw.strip()
    try:
        return int(cleaned)
    except ValueError:
        try:
            fval = float(cleaned.replace(",", "."))
            if not math.isfinite(fval):
                return None
            return int(fval)
        except (ValueError, OverflowError):
            return None


def _filter_conditional_lines(
    lines: List[str], warnings: List[ConversionWarning]
) -> List[Tuple[int, str]]:
    """Deterministically evaluate #RANDOM/#IF/#SWITCH blocks while recording warnings."""
    active_lines: List[Tuple[int, str]] = []
    block_stack: List[_ControlFrame] = []
    random_stack: List[Tuple[int, int]] = []  # (block_depth, selected_value)
    conditional_directives: List[str] = []

    def is_currently_active() -> bool:
        return all(frame.active for frame in block_stack)

    def set_random_for_depth(val: int) -> None:
        depth = len(block_stack)
        if random_stack and random_stack[-1][0] == depth:
            random_stack[-1] = (depth, val)
        else:
            random_stack.append((depth, val))

    def prune_random_stack() -> None:
        depth = len(block_stack)
        while random_stack and random_stack[-1][0] > depth:
            random_stack.pop()

    for line_no, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped.startswith("#"):
            continue

        # Check control flow commands (case-insensitive)
        parts = stripped.split(None, 1)
        cmd = parts[0].upper()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "#RANDOM":
            conditional_directives.append(f"L{line_no}:{cmd}")
            if is_currently_active():
                # Deterministic branch selection: pick 1
                set_random_for_depth(1)
            continue

        if cmd == "#SETRANDOM":
            conditional_directives.append(f"L{line_no}:{cmd}")
            if is_currently_active():
                parsed = _parse_int(arg)
                set_random_for_depth(parsed if parsed is not None else 1)
            continue

        if cmd == "#ENDRANDOM":
            conditional_directives.append(f"L{line_no}:{cmd}")
            if (
                is_currently_active()
                and random_stack
                and random_stack[-1][0] == len(block_stack)
            ):
                random_stack.pop()
            continue

        if cmd == "#IF":
            conditional_directives.append(f"L{line_no}:{cmd}")
            parent_ok = is_currently_active()
            cur_rand = random_stack[-1][1] if random_stack else 1
            target = _parse_int(arg)
            matched = parent_ok and (target == cur_rand)
            block_stack.append(
                _ControlFrame(
                    kind="if",
                    rand_value=cur_rand,
                    active=matched,
                    matched_any=matched,
                    parent_active=parent_ok,
                )
            )
            continue

        if cmd == "#ELSEIF":
            conditional_directives.append(f"L{line_no}:{cmd}")
            target = _parse_int(arg)
            if block_stack and block_stack[-1].kind == "if":
                frame = block_stack[-1]
                matched = (
                    frame.parent_active
                    and not frame.matched_any
                    and (target == frame.rand_value)
                )
                frame.active = matched
                if matched:
                    frame.matched_any = True
            continue

        if cmd == "#ELSE":
            conditional_directives.append(f"L{line_no}:{cmd}")
            if block_stack and block_stack[-1].kind == "if":
                frame = block_stack[-1]
                matched = frame.parent_active and not frame.matched_any
                frame.active = matched
                if matched:
                    frame.matched_any = True
            continue

        if cmd == "#ENDIF":
            conditional_directives.append(f"L{line_no}:{cmd}")
            if block_stack and block_stack[-1].kind == "if":
                block_stack.pop()
                prune_random_stack()
            continue

        if cmd in ("#SWITCH", "#SETSWITCH"):
            conditional_directives.append(f"L{line_no}:{cmd}")
            parent_ok = is_currently_active()
            if cmd == "#SETSWITCH":
                parsed = _parse_int(arg)
                sw_val = parsed if parsed is not None else 1
            else:
                sw_val = 1
            block_stack.append(
                _ControlFrame(
                    kind="switch",
                    rand_value=sw_val,
                    active=False,
                    matched_any=False,
                    parent_active=parent_ok,
                )
            )
            continue

        if cmd == "#CASE":
            conditional_directives.append(f"L{line_no}:{cmd}")
            target = _parse_int(arg)
            if block_stack and block_stack[-1].kind == "switch":
                frame = block_stack[-1]
                matched = (
                    frame.parent_active
                    and not frame.matched_any
                    and (target == frame.rand_value)
                )
                frame.active = matched
                if matched:
                    frame.matched_any = True
            continue

        if cmd == "#SKIP":
            conditional_directives.append(f"L{line_no}:{cmd}")
            if block_stack and block_stack[-1].kind == "switch":
                block_stack[-1].active = False
            continue

        if cmd == "#DEF":
            conditional_directives.append(f"L{line_no}:{cmd}")
            if block_stack and block_stack[-1].kind == "switch":
                frame = block_stack[-1]
                matched = frame.parent_active and not frame.matched_any
                frame.active = matched
                if matched:
                    frame.matched_any = True
            continue

        if cmd == "#ENDSW":
            conditional_directives.append(f"L{line_no}:{cmd}")
            if block_stack and block_stack[-1].kind == "switch":
                block_stack.pop()
                prune_random_stack()
            continue

        if is_currently_active():
            active_lines.append((line_no, stripped))

    if conditional_directives:
        sample = ", ".join(conditional_directives[:8])
        more = f" (+{len(conditional_directives) - 8} more)" if len(conditional_directives) > 8 else ""
        warnings.append(
            ConversionWarning(
                code="UNEXPANDED_RANDOM_BRANCH",
                message=(
                    f"Detected unexpanded #RANDOM/#IF/#SWITCH conditional directives "
                    f"({sample}{more}). DTX does not support runtime random branching; "
                    f"deterministically evaluated branch 1."
                ),
            )
        )

    return active_lines


def _detect_radix(active_lines: List[Tuple[int, str]]) -> Tuple[int, bool, Optional[int]]:
    """Pass 1: detect Base-36 vs Base-62 from #BASE header or case-sensitive indices.

    Respects explicit #BASE 36 / #BASE 62 headers. When #BASE is omitted, infers
    Base-62 only from genuine case-sensitive evidence (case collisions among #WAVxx,
    #BPMxx/#EXBPMxx, or #STOPxx definitions, >1295 distinct #WAVxx definitions, or
    note-token case collisions when no #WAVxx definitions exist). Standard Base-36
    charts that mix header/data case (e.g. #WAV0A with #00101:0a) stay Base-36.
    """
    declared_base: Optional[int] = None
    wav_raw_ids: Set[str] = set()
    wav_upper_to_raw: Dict[str, Set[str]] = defaultdict(set)
    bpm_upper_to_raw: Dict[str, Set[str]] = defaultdict(set)
    stop_upper_to_raw: Dict[str, Set[str]] = defaultdict(set)
    note_upper_to_raw: Dict[str, Set[str]] = defaultdict(set)

    for _line_no, line in active_lines:
        m_wav = _WAV_HEADER_RE.match(line)
        if m_wav:
            tok = m_wav.group(1)
            if tok != "00":
                wav_raw_ids.add(tok)
                wav_upper_to_raw[tok.upper()].add(tok)
            continue

        m_bpm = _BPM_EXT_RE.match(line)
        if m_bpm:
            tok = m_bpm.group(1)
            if tok != "00":
                bpm_upper_to_raw[tok.upper()].add(tok)
            continue

        m_stop = _STOP_HEADER_RE.match(line)
        if m_stop:
            tok = m_stop.group(1)
            if tok != "00":
                stop_upper_to_raw[tok.upper()].add(tok)
            continue

        m_data = _DATA_LINE_RE.match(line)
        if m_data:
            ch = m_data.group(2).upper()
            if ch in ("02", "03"):
                continue
            if (
                ch in ("01", "08", "09")
                or ch in BMS_PLAYABLE_NORMAL_CHANNELS
                or ch in BMS_LN_CHANNELS
            ):
                data = m_data.group(3).strip()
                n_pairs = len(data) // 2
                for i in range(n_pairs):
                    tok = data[2 * i : 2 * i + 2]
                    if tok != "00" and _VALID_TOKEN_RE.match(tok):
                        note_upper_to_raw[tok.upper()].add(tok)
            continue

        m_hdr = _GENERIC_HEADER_RE.match(line)
        if m_hdr and m_hdr.group(1).upper() == "BASE":
            val = _parse_int(m_hdr.group(2) or "")
            if val in (36, 62):
                declared_base = val

    if declared_base == 62:
        return 62, True, declared_base
    if declared_base == 36:
        return 36, False, declared_base

    has_header_case_collision = (
        any(len(variants) > 1 for variants in wav_upper_to_raw.values())
        or any(len(variants) > 1 for variants in bpm_upper_to_raw.values())
        or any(len(variants) > 1 for variants in stop_upper_to_raw.values())
    )
    has_data_only_case_collision = (
        not wav_raw_ids
        and any(len(variants) > 1 for variants in note_upper_to_raw.values())
    )

    if (
        has_header_case_collision
        or len(wav_raw_ids) > DTX_MAX_WAV_SLOTS
        or has_data_only_case_collision
    ):
        return 62, True, declared_base

    return 36, False, declared_base


def parse_bms(path: Path | str) -> BmsChart:
    """Parse a .bms / .bme file in read-only mode, returning a full BmsChart."""
    source_path = Path(path)
    if not source_path.is_file():
        raise FileNotFoundError(f"BMS file not found: {source_path}")
    source_dir = source_path.parent

    text, encoding_used, warnings = read_bms_text(source_path)
    raw_lines = text.splitlines()

    active_lines = _filter_conditional_lines(raw_lines, warnings)
    base, is_base62, declared_base = _detect_radix(active_lines)

    def norm_id(tok: str) -> str:
        return tok if is_base62 else tok.upper()

    audio_index = ParentFolderAudioIndex(source_dir)

    headers: Dict[str, str] = {}
    wavs: Dict[str, WavEntry] = {}
    bmps: Dict[str, WavEntry] = {}
    ex_bpms: Dict[str, float] = {}
    stops: Dict[str, float] = {}
    measure_lengths: Dict[int, float] = {}

    title = ""
    subtitle = ""
    artist = ""
    subartist = ""
    genre = ""
    initial_bpm: Optional[float] = None
    playlevel: Optional[int] = None
    difficulty: Optional[int] = None
    lntype = 1
    lnobj: Optional[str] = None
    lnobjs: Set[str] = set()
    preview_raw: Optional[str] = None
    stagefile_raw: Optional[str] = None
    backbmp_raw: Optional[str] = None
    reserved_00_headers: List[str] = []

    # First parse headers (so #BPMxx, #WAVxx, #LNOBJ are all known even if interleaved)
    data_lines: List[Tuple[int, int, str, str]] = []
    stop_defs_found: List[str] = []

    for line_no, line in active_lines:
        m_data = _DATA_LINE_RE.match(line)
        if m_data:
            measure = int(m_data.group(1))
            channel = m_data.group(2).upper()
            payload = m_data.group(3).strip()
            data_lines.append((line_no, measure, channel, payload))
            continue

        m_wav = _WAV_HEADER_RE.match(line)
        if m_wav:
            wid = norm_id(m_wav.group(1))
            if wid == "00":
                reserved_00_headers.append(f"L{line_no}:#WAV00")
                continue
            fname = m_wav.group(2).strip()
            wavs[wid] = audio_index.resolve_wav(wid, fname)
            continue

        m_bmp = _BMP_HEADER_RE.match(line)
        if m_bmp:
            bid = norm_id(m_bmp.group(1))
            if bid != "00":
                fname = m_bmp.group(2).strip()
                bmps[bid] = audio_index.resolve_media(
                    bid, fname, SUPPORTED_BGA_EXTENSIONS
                )
            continue

        m_bpm = _BPM_EXT_RE.match(line)
        if m_bpm:
            bid = norm_id(m_bpm.group(1))
            if bid == "00":
                reserved_00_headers.append(f"L{line_no}:#BPM00")
                continue
            val = _parse_float(m_bpm.group(2))
            if val is not None and val > 0:
                ex_bpms[bid] = val
            else:
                warnings.append(
                    ConversionWarning(
                        code="INVALID_EXBPM",
                        message=f"Line {line_no}: invalid #BPM{bid} value {m_bpm.group(2)!r}; ignored.",
                    )
                )
            continue

        m_stop = _STOP_HEADER_RE.match(line)
        if m_stop:
            sid = norm_id(m_stop.group(1))
            if sid == "00":
                reserved_00_headers.append(f"L{line_no}:#STOP00")
                continue
            val = _parse_float(m_stop.group(2))
            stops[sid] = val if val is not None else 0.0
            stop_defs_found.append(sid)
            continue

        m_hdr = _GENERIC_HEADER_RE.match(line)
        if m_hdr:
            key = m_hdr.group(1).upper()
            val_str = (m_hdr.group(2) or "").strip()
            headers[key] = val_str
            if key == "TITLE":
                title = val_str
            elif key == "SUBTITLE":
                subtitle = val_str
            elif key == "ARTIST":
                artist = val_str
            elif key == "SUBARTIST":
                subartist = val_str
            elif key == "GENRE":
                genre = val_str
            elif key == "BPM":
                parsed_bpm = _parse_float(val_str)
                if parsed_bpm is not None and parsed_bpm > 0:
                    initial_bpm = parsed_bpm
                else:
                    warnings.append(
                        ConversionWarning(
                            code="INVALID_INITIAL_BPM",
                            message=f"Line {line_no}: invalid #BPM value {val_str!r}; will fall back to 130.",
                        )
                    )
            elif key == "PLAYLEVEL":
                playlevel = _parse_int(val_str)
            elif key == "DIFFICULTY":
                difficulty = _parse_int(val_str)
            elif key == "LNTYPE":
                parsed_ln = _parse_int(val_str)
                if parsed_ln in (1, 2):
                    lntype = parsed_ln
            elif key == "LNOBJ":
                if len(val_str) >= 2 and _VALID_TOKEN_RE.match(val_str[:2]):
                    obj_id = norm_id(val_str[:2])
                    if obj_id != "00":
                        lnobj = obj_id
                        lnobjs.add(obj_id)
            elif key == "PREVIEW":
                if val_str:
                    preview_raw = val_str
            elif key == "STAGEFILE":
                if val_str:
                    stagefile_raw = val_str
            elif key == "BACKBMP":
                if val_str:
                    backbmp_raw = val_str

    if reserved_00_headers:
        warnings.append(
            ConversionWarning(
                code="RESERVED_ID_00_IGNORED",
                message=(
                    f"Ignored reserved '00' header declaration(s): "
                    f"{', '.join(reserved_00_headers[:6])}."
                ),
            )
        )

    preview_entry: Optional[WavEntry] = None
    if preview_raw:
        preview_entry = audio_index.resolve_wav("PREVIEW", preview_raw)
        if not preview_entry.exists or preview_entry.resolved_path is None:
            warnings.append(
                ConversionWarning(
                    code="MISSING_PREVIEW_FILE",
                    message=(
                        f"#PREVIEW audio file {preview_raw!r} could not be found in "
                        f"{source_dir} even after suffix fallback."
                    ),
                )
            )
    else:
        preview_entry = audio_index.discover_auto_preview(
            excluded_paths=[
                w.resolved_path
                for w in wavs.values()
                if w.exists and w.resolved_path is not None
            ]
        )

    stagefile_entry: Optional[WavEntry] = None
    if stagefile_raw:
        stagefile_entry = audio_index.resolve_media(
            "STAGEFILE", stagefile_raw, SUPPORTED_IMAGE_EXTENSIONS
        )

    backbmp_entry: Optional[WavEntry] = None
    if backbmp_raw:
        backbmp_entry = audio_index.resolve_media(
            "BACKBMP", backbmp_raw, SUPPORTED_IMAGE_EXTENSIONS
        )

    if initial_bpm is None:
        initial_bpm = 130.0
        warnings.append(
            ConversionWarning(
                code="MISSING_INITIAL_BPM",
                message="No valid #BPM header found; defaulting to 130 BPM.",
            )
        )

    # Parse main data lines
    bgm_col_counter: Dict[int, int] = defaultdict(int)
    max_bgm_columns_raw = 0

    bgm_events: List[NoteEvent] = []
    # For non-01 channels, duplicate lines in same (measure, channel) overlay by Fraction pos
    normal_slots: Dict[Tuple[int, str], Dict[Fraction, Tuple[str, int]]] = defaultdict(dict)
    ln_slots: Dict[Tuple[int, str], Dict[Fraction, Tuple[str, int]]] = defaultdict(dict)
    ln_raw_lines: Dict[Tuple[int, str], List[Tuple[List[str], int]]] = defaultdict(list)
    bga_slots: Dict[Tuple[int, str], Dict[Fraction, Tuple[str, int]]] = defaultdict(dict)
    seen_non_bgm_channels: Set[str] = set()
    bpm_slots: Dict[Tuple[int, Fraction, str], float] = {}

    stop_measures: List[int] = []
    undefined_exbpms: Set[str] = set()
    odd_length_lines: List[int] = []
    invalid_data_tokens: List[str] = []
    order_seq = 0

    for line_no, measure, channel, payload in data_lines:
        if channel == "01":
            # RULE 4: NEVER discard :00 placeholder lines during parsing!
            bgm_col_counter[measure] += 1
            col_idx = bgm_col_counter[measure]
            if col_idx > max_bgm_columns_raw:
                max_bgm_columns_raw = col_idx

            if not payload:
                continue
            if len(payload) % 2 != 0:
                odd_length_lines.append(line_no)
            n_slots = len(payload) // 2
            if n_slots == 0:
                continue
            for idx in range(n_slots):
                tok = payload[2 * idx : 2 * idx + 2]
                if tok != "00":
                    if not _VALID_TOKEN_RE.match(tok):
                        invalid_data_tokens.append(f"L{line_no}:{tok!r}")
                        continue
                    order_seq += 1
                    bgm_events.append(
                        NoteEvent(
                            measure=measure,
                            pos=Fraction(idx, n_slots),
                            channel="01",
                            wav_id=norm_id(tok),
                            bgm_column=col_idx,
                            track_label=f"B{col_idx}",
                            order_index=order_seq,
                        )
                    )
            continue

        if channel == "02":
            length_val = _parse_float(payload)
            if length_val is not None and length_val > 0:
                measure_lengths[measure] = length_val
            else:
                warnings.append(
                    ConversionWarning(
                        code="INVALID_MEASURE_LENGTH",
                        message=f"Line {line_no}: invalid measure {measure:03d} length {payload!r}; ignored.",
                    )
                )
            continue

        if channel == "03":
            if len(payload) % 2 != 0:
                odd_length_lines.append(line_no)
            n_slots = len(payload) // 2
            for idx in range(n_slots):
                tok = payload[2 * idx : 2 * idx + 2]
                if tok != "00":
                    try:
                        bpm_val = int(tok, 16)
                    except ValueError:
                        warnings.append(
                            ConversionWarning(
                                code="INVALID_HEX_BPM",
                                message=f"Line {line_no}: non-hex BPM token {tok!r} on channel 03; skipped.",
                            )
                        )
                        continue
                    if bpm_val > 0:
                        bpm_slots[(measure, Fraction(idx, n_slots), "03")] = float(bpm_val)
            continue

        if channel == "08":
            if len(payload) % 2 != 0:
                odd_length_lines.append(line_no)
            n_slots = len(payload) // 2
            for idx in range(n_slots):
                tok = payload[2 * idx : 2 * idx + 2]
                if tok != "00":
                    if not _VALID_TOKEN_RE.match(tok):
                        invalid_data_tokens.append(f"L{line_no}:{tok!r}")
                        continue
                    bid = norm_id(tok)
                    if bid in ex_bpms:
                        bpm_slots[(measure, Fraction(idx, n_slots), "08")] = ex_bpms[bid]
                    else:
                        undefined_exbpms.add(bid)
            continue

        if channel == "09":
            n_slots = len(payload) // 2
            has_stop = any(
                payload[2 * idx : 2 * idx + 2] != "00" for idx in range(n_slots)
            )
            if has_stop:
                stop_measures.append(measure)
            continue

        if channel in BMS_PLAYABLE_NORMAL_CHANNELS:
            seen_non_bgm_channels.add(channel)
            if len(payload) % 2 != 0:
                odd_length_lines.append(line_no)
            n_slots = len(payload) // 2
            slot_map = normal_slots[(measure, channel)]
            for idx in range(n_slots):
                tok = payload[2 * idx : 2 * idx + 2]
                if tok != "00":
                    if not _VALID_TOKEN_RE.match(tok):
                        invalid_data_tokens.append(f"L{line_no}:{tok!r}")
                        continue
                    order_seq += 1
                    slot_map[Fraction(idx, n_slots)] = (norm_id(tok), order_seq)
            continue

        if channel in BMS_LN_CHANNELS:
            seen_non_bgm_channels.add(channel)
            if len(payload) % 2 != 0:
                odd_length_lines.append(line_no)
            n_slots = len(payload) // 2
            slot_map = ln_slots[(measure, channel)]
            row_tokens: List[str] = []
            row_base_seq = order_seq + 1
            for idx in range(n_slots):
                tok = payload[2 * idx : 2 * idx + 2]
                if tok != "00":
                    if not _VALID_TOKEN_RE.match(tok):
                        invalid_data_tokens.append(f"L{line_no}:{tok!r}")
                        row_tokens.append("00")
                        continue
                    normed = norm_id(tok)
                    order_seq += 1
                    slot_map[Fraction(idx, n_slots)] = (normed, order_seq)
                    row_tokens.append(normed)
                else:
                    row_tokens.append("00")
            if row_tokens:
                ln_raw_lines[(measure, channel)].append((row_tokens, row_base_seq))
            continue

        if channel in ("04", "07"):
            n_slots = len(payload) // 2
            slot_map = bga_slots[(measure, channel)]
            for idx in range(n_slots):
                tok = payload[2 * idx : 2 * idx + 2]
                if tok != "00" and _VALID_TOKEN_RE.match(tok):
                    order_seq += 1
                    slot_map[Fraction(idx, n_slots)] = (norm_id(tok), order_seq)
            continue

        # Ignore invisible (3x/4x), mines (Dx/Ex), BGA (06/0A), etc.
        if (
            channel in BMS_INVISIBLE_CHANNELS
            or channel in BMS_MINE_CHANNELS
            or channel in BMS_BGA_CHANNELS
        ):
            continue

    # Emit parsing diagnostics / warnings
    if odd_length_lines:
        sample = ", ".join(str(x) for x in odd_length_lines[:5])
        warnings.append(
            ConversionWarning(
                code="ODD_DATA_LENGTH",
                message=f"Truncated trailing odd character on BMS data line(s): {sample}.",
            )
        )

    if invalid_data_tokens:
        sample = ", ".join(invalid_data_tokens[:6])
        more = f" (+{len(invalid_data_tokens) - 6} more)" if len(invalid_data_tokens) > 6 else ""
        warnings.append(
            ConversionWarning(
                code="INVALID_DATA_TOKEN",
                message=f"Skipped malformed non-alphanumeric data token(s): {sample}{more}.",
            )
        )

    if undefined_exbpms:
        sample = ", ".join(sorted(undefined_exbpms)[:8])
        warnings.append(
            ConversionWarning(
                code="UNDEFINED_EXBPM",
                message=f"Channel 08 referenced undefined #BPMxx ID(s): {sample}.",
            )
        )

    if stop_defs_found or stop_measures:
        unique_stop_measures = sorted(set(stop_measures))
        m_preview = ", ".join(f"{m:03d}" for m in unique_stop_measures[:10])
        more = f" (+{len(unique_stop_measures) - 10} more)" if len(unique_stop_measures) > 10 else ""
        warnings.append(
            ConversionWarning(
                code="STOP_UNSUPPORTED",
                message=(
                    f"DTX does not support BMS #STOP / channel 09 "
                    f"({len(stop_defs_found)} #STOP definitions, "
                    f"{len(unique_stop_measures)} channel 09 measure(s): {m_preview}{more}). "
                    f"Stops are ignored so bgm.ogg and DTX notes stay synchronized."
                ),
            )
        )

    # Assemble normal play-lane events (handling #LNOBJ end markers)
    playable_events: List[NoteEvent] = list(bgm_events)
    ln_end_events: List[NoteEvent] = []

    for (measure, channel), slot_map in sorted(normal_slots.items()):
        for pos, (wid, seq) in sorted(slot_map.items()):
            if wid in lnobjs:
                ln_end_events.append(
                    NoteEvent(
                        measure=measure,
                        pos=pos,
                        channel=channel,
                        wav_id=wid,
                        track_label=channel,
                        is_ln_end=True,
                        order_index=seq,
                    )
                )
            else:
                playable_events.append(
                    NoteEvent(
                        measure=measure,
                        pos=pos,
                        channel=channel,
                        wav_id=wid,
                        track_label=channel,
                        is_ln_end=False,
                        order_index=seq,
                    )
                )

    # Assemble Long Note channel events (51-59, 61-69)
    ln_by_channel: Dict[str, List[Tuple[int, Fraction, str, int]]] = defaultdict(list)
    for (measure, channel), slot_map in ln_slots.items():
        for pos, (wid, seq) in slot_map.items():
            ln_by_channel[channel].append((measure, pos, wid, seq))

    if lntype == 2 and ln_by_channel:
        warnings.append(
            ConversionWarning(
                code="LNTYPE_2_APPROXIMATED",
                message="#LNTYPE 2 detected; only the start of each contiguous long-note run is triggered.",
            )
        )

    unclosed_ln_channels: List[str] = []
    if lntype == 2:
        # Preserve 00 gaps and empty measures so consecutive holds of the same WAV trigger properly
        ln2_measures_by_ch: Dict[str, List[int]] = defaultdict(list)
        for (measure, channel) in sorted(ln_raw_lines.keys()):
            ln2_measures_by_ch[channel].append(measure)

        for channel, measures in sorted(ln2_measures_by_ch.items()):
            active_wid: Optional[str] = None
            prev_measure: Optional[int] = None
            for measure in measures:
                if prev_measure is not None and measure != prev_measure + 1:
                    active_wid = None
                rows = ln_raw_lines[(measure, channel)]
                grid_len = 1
                for row_tokens, _ in rows:
                    grid_len = math.lcm(grid_len, len(row_tokens))
                    if grid_len > 1536:
                        break
                if grid_len <= 1536:
                    grid: List[Tuple[str, int]] = [("00", 0)] * grid_len
                    for row_tokens, base_seq in rows:
                        step = grid_len // len(row_tokens)
                        for idx, tok in enumerate(row_tokens):
                            if tok != "00":
                                for sub in range(step):
                                    grid[idx * step + sub] = (tok, base_seq + idx)
                    for idx, (tok, seq) in enumerate(grid):
                        if tok == "00":
                            active_wid = None
                            continue
                        pos = Fraction(idx, grid_len)
                        if tok != active_wid:
                            playable_events.append(
                                NoteEvent(
                                    measure=measure,
                                    pos=pos,
                                    channel=channel,
                                    wav_id=tok,
                                    track_label=channel,
                                    is_ln_end=False,
                                    order_index=seq,
                                )
                            )
                            active_wid = tok
                        else:
                            ln_end_events.append(
                                NoteEvent(
                                    measure=measure,
                                    pos=pos,
                                    channel=channel,
                                    wav_id=tok,
                                    track_label=channel,
                                    is_ln_end=True,
                                    order_index=seq,
                                )
                            )
                else:
                    # Fallback for pathological multi-line LCM: iterate last row's slots
                    row_tokens, base_seq = rows[-1]
                    n_toks = len(row_tokens)
                    for idx, tok in enumerate(row_tokens):
                        if tok == "00":
                            active_wid = None
                            continue
                        pos = Fraction(idx, n_toks)
                        if tok != active_wid:
                            playable_events.append(
                                NoteEvent(
                                    measure=measure,
                                    pos=pos,
                                    channel=channel,
                                    wav_id=tok,
                                    track_label=channel,
                                    is_ln_end=False,
                                    order_index=base_seq + idx,
                                )
                            )
                            active_wid = tok
                        else:
                            ln_end_events.append(
                                NoteEvent(
                                    measure=measure,
                                    pos=pos,
                                    channel=channel,
                                    wav_id=tok,
                                    track_label=channel,
                                    is_ln_end=True,
                                    order_index=base_seq + idx,
                                )
                            )
                prev_measure = measure
    else:
        for channel, items in sorted(ln_by_channel.items()):
            items.sort(key=lambda t: (t[0], t[1]))
            # Standard #LNTYPE 1: alternating START (even), END (odd)
            if len(items) % 2 != 0:
                unclosed_ln_channels.append(channel)
            for idx, (measure, pos, wid, seq) in enumerate(items):
                if idx % 2 == 0:
                    playable_events.append(
                        NoteEvent(
                            measure=measure,
                            pos=pos,
                            channel=channel,
                            wav_id=wid,
                            track_label=channel,
                            is_ln_end=False,
                            order_index=seq,
                        )
                    )
                else:
                    ln_end_events.append(
                        NoteEvent(
                            measure=measure,
                            pos=pos,
                            channel=channel,
                            wav_id=wid,
                            track_label=channel,
                            is_ln_end=True,
                            order_index=seq,
                        )
                    )

    if unclosed_ln_channels:
        warnings.append(
            ConversionWarning(
                code="UNCLOSED_LONG_NOTE",
                message=(
                    f"Odd number of LN markers on channel(s) {', '.join(unclosed_ln_channels)}; "
                    f"treated trailing marker as LN start."
                ),
            )
        )

    # Sort playable events chronologically (preserving deterministic order_index tie-breaker)
    playable_events.sort(key=lambda e: (e.measure, e.pos, e.order_index))
    ln_end_events.sort(key=lambda e: (e.measure, e.pos, e.order_index))

    # Count WAV usage and check undefined / missing audio files
    undefined_wav_ids: Set[str] = set()
    for ev in playable_events:
        entry = wavs.get(ev.wav_id)
        if entry is not None:
            entry.use_count += 1
        else:
            undefined_wav_ids.add(ev.wav_id)

    if undefined_wav_ids:
        sample = ", ".join(sorted(undefined_wav_ids)[:8])
        warnings.append(
            ConversionWarning(
                code="UNDEFINED_WAV_REFERENCE",
                message=(
                    f"{len(undefined_wav_ids)} note WAV ID(s) have no #WAVxx declaration "
                    f"(e.g. {sample})."
                ),
            )
        )

    missing_used_wavs = [
        w for w in wavs.values() if w.use_count > 0 and not w.exists
    ]
    if missing_used_wavs:
        sample = ", ".join(f"{w.wav_id}:{w.declared_name}" for w in missing_used_wavs[:6])
        more = f" (+{len(missing_used_wavs) - 6} more)" if len(missing_used_wavs) > 6 else ""
        warnings.append(
            ConversionWarning(
                code="MISSING_AUDIO_FILES",
                message=(
                    f"{len(missing_used_wavs)} referenced audio file(s) could not be found "
                    f"in {source_dir} even after suffix fallback ({sample}{more})."
                ),
            )
        )

    # Build Track Summaries and prune empty tracks across the entire song (RULE 4)
    bgm_events_by_col: Dict[int, List[NoteEvent]] = defaultdict(list)
    other_events_by_ch: Dict[str, List[NoteEvent]] = defaultdict(list)
    for ev in playable_events:
        if ev.channel == "01" and ev.bgm_column is not None:
            bgm_events_by_col[ev.bgm_column].append(ev)
        else:
            other_events_by_ch[ev.channel].append(ev)

    bgm_tracks: List[TrackSummary] = []
    tracks: List[TrackSummary] = []
    pruned_empty_tracks: List[str] = []

    def _make_summary(
        label: str, ch: str, col: Optional[int], evs: List[NoteEvent]
    ) -> TrackSummary:
        uniq_ids: List[str] = []
        seen_ids: Set[str] = set()
        sample_names: List[str] = []
        seen_names: Set[str] = set()
        for e in evs:
            if e.wav_id not in seen_ids:
                seen_ids.add(e.wav_id)
                uniq_ids.append(e.wav_id)
                entry = wavs.get(e.wav_id)
                name = entry.resolved_name if entry else f"(undefined:{e.wav_id})"
                if name not in seen_names and len(sample_names) < 5:
                    seen_names.add(name)
                    sample_names.append(name)
        return TrackSummary(
            track_label=label,
            channel=ch,
            bgm_column=col,
            note_count=len(evs),
            unique_wav_ids=uniq_ids,
            sample_filenames=sample_names,
        )

    for col in range(1, max_bgm_columns_raw + 1):
        evs = bgm_events_by_col.get(col, [])
        label = f"B{col}"
        if not evs:
            pruned_empty_tracks.append(label)
        else:
            summary = _make_summary(label, "01", col, evs)
            bgm_tracks.append(summary)
            tracks.append(summary)

    for ch in sorted(seen_non_bgm_channels | set(other_events_by_ch.keys())):
        evs = other_events_by_ch.get(ch, [])
        if evs:
            tracks.append(_make_summary(ch, ch, None, evs))
        else:
            pruned_empty_tracks.append(ch)

    # Build BPM change list
    bpm_changes = [
        BpmChange(measure=m, pos=pos, bpm=bpm_val, source_channel=src_ch)
        for (m, pos, src_ch), bpm_val in sorted(
            bpm_slots.items(),
            key=lambda item: (item[0][0], item[0][1], 0 if item[0][2] == "03" else 1),
        )
    ]

    # Compute musical max_measure (ignore trailing BGA/invisible/mine/empty-00 lines)
    max_measure = 0
    if playable_events:
        max_measure = max(max_measure, max(e.measure for e in playable_events))
    if ln_end_events:
        max_measure = max(max_measure, max(e.measure for e in ln_end_events))
    if bpm_changes:
        max_measure = max(max_measure, max(bc.measure for bc in bpm_changes))

    # Radix / WAV limit warnings (RULE 5)
    total_distinct_wavs = len(set(wavs.keys()) | {e.wav_id for e in playable_events})
    if is_base62:
        warnings.append(
            ConversionWarning(
                code="BASE62_DETECTED",
                message=(
                    f"Base-62 chart detected ({total_distinct_wavs} distinct slices). "
                    f"DTX natively supports only Base-36 (01-ZZ, max {DTX_MAX_WAV_SLOTS} slices)."
                ),
            )
        )
    if total_distinct_wavs > DTX_MAX_WAV_SLOTS:
        warnings.append(
            ConversionWarning(
                code="WAV_LIMIT_EXCEEDED_LOCK_TOGGLE_OFF",
                message=(
                    f"Chart has {total_distinct_wavs} slices, exceeding DTX Base-36 limit "
                    f"({DTX_MAX_WAV_SLOTS}). Toggle is force-locked to OFF (offline bgm.ogg mix); "
                    f"up to 1294 slices mapped into Tabs 1-15 will be renumbered to 02-ZZ."
                ),
            )
        )

    bga_events: List[BgaEvent] = []
    for (measure, channel), slot_map in sorted(bga_slots.items()):
        for pos, (bid, seq) in sorted(slot_map.items()):
            bga_events.append(
                BgaEvent(
                    measure=measure,
                    pos=pos,
                    channel=channel,
                    bga_id=bid,
                    order_index=seq,
                )
            )
    bga_events.sort(key=lambda e: (e.measure, e.pos, e.order_index))

    chart = BmsChart(
        source_path=source_path,
        source_dir=source_dir,
        encoding_used=encoding_used,
        base=base,
        is_base62=is_base62,
        declared_base=declared_base,
        title=title,
        subtitle=subtitle,
        artist=artist,
        subartist=subartist,
        genre=genre,
        initial_bpm=initial_bpm,
        playlevel=playlevel,
        difficulty=difficulty,
        lntype=lntype,
        lnobj=lnobj,
        headers=headers,
        wavs=wavs,
        ex_bpms=ex_bpms,
        stops=stops,
        measure_lengths=measure_lengths,
        bpm_changes=bpm_changes,
        events=playable_events,
        ln_end_events=ln_end_events,
        tracks=tracks,
        bgm_tracks=bgm_tracks,
        pruned_empty_tracks=pruned_empty_tracks,
        max_bgm_columns_raw=max_bgm_columns_raw,
        max_measure=max_measure,
        song_duration_sec=0.0,
        warnings=warnings,
        preview=preview_entry,
        stagefile=stagefile_entry,
        backbmp=backbmp_entry,
        bmps=bmps,
        bga_events=bga_events,
    )
    build_timeline_for_chart(chart)
    return chart
