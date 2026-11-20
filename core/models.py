"""Data models for BMS parsing, timeline computation, mapping, and DTX export."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .constants import TabId


@dataclass(frozen=True)
class ConversionWarning:
    """Structured warning produced during parsing, mapping, mixing, or writing."""

    code: str
    message: str


@dataclass
class WavEntry:
    """Represents one #WAVxx declaration and its in-memory disk resolution."""

    wav_id: str
    declared_name: str
    resolved_name: str
    resolved_path: Optional[Path]
    exists: bool
    suffix_corrected: bool = False
    case_corrected: bool = False
    use_count: int = 0


@dataclass
class NoteEvent:
    """A single BMS note event on a BGM column, play lane, or LN channel."""

    measure: int
    pos: Fraction
    channel: str  # "01", "11"-"29", "51"-"69"
    wav_id: str
    bgm_column: Optional[int] = None  # 1-based for channel "01" (B1, B2, ...)
    track_label: str = ""  # "B1", "B2", ..., "11", "54", etc.
    is_ln_end: bool = False
    time_sec: float = 0.0
    order_index: int = 0


@dataclass(frozen=True)
class BpmChange:
    """A BPM change event at an exact fractional measure position."""

    measure: int
    pos: Fraction
    bpm: float
    source_channel: str  # "03" or "08"
    time_sec: float = 0.0


@dataclass
class TrackSummary:
    """Summary statistics for one non-empty BMS track (B1..Bn, 11..29, 51..69)."""

    track_label: str
    channel: str
    bgm_column: Optional[int]
    note_count: int
    unique_wav_ids: List[str]
    sample_filenames: List[str]


@dataclass
class BmsChart:
    """Parsed BMS/BME chart ready for inspection, mapping, mixing, and DTX export."""

    source_path: Path
    source_dir: Path
    encoding_used: str
    base: int  # 36 or 62
    is_base62: bool
    declared_base: Optional[int]
    title: str
    subtitle: str
    artist: str
    subartist: str
    genre: str
    initial_bpm: float
    playlevel: Optional[int]
    difficulty: Optional[int]
    lntype: int
    lnobj: Optional[str]
    headers: Dict[str, str]
    wavs: Dict[str, WavEntry]
    ex_bpms: Dict[str, float]
    stops: Dict[str, float]
    measure_lengths: Dict[int, float]  # measure -> BMS per-measure length multiplier
    bpm_changes: List[BpmChange]
    events: List[NoteEvent]  # playable events (excluding LN end markers)
    ln_end_events: List[NoteEvent]
    tracks: List[TrackSummary]  # non-empty tracks only, preserving true B1..Bn labels
    bgm_tracks: List[TrackSummary]  # non-empty B1..Bn tracks only
    pruned_empty_tracks: List[str]  # e.g. ["B20", "B23"]
    max_bgm_columns_raw: int
    max_measure: int
    song_duration_sec: float
    warnings: List[ConversionWarning] = field(default_factory=list)
    preview: Optional[WavEntry] = None

    @property
    def full_title(self) -> str:
        """Combine #TITLE and #SUBTITLE cleanly without duplication."""
        if self.subtitle and self.subtitle not in self.title:
            return f"{self.title} {self.subtitle}".strip()
        return self.title or self.source_path.stem

    @property
    def referenced_wav_ids(self) -> List[str]:
        """Ordered list of unique WAV IDs actually referenced by playable notes."""
        seen: set[str] = set()
        result: List[str] = []
        for ev in self.events:
            if ev.wav_id not in seen:
                seen.add(ev.wav_id)
                result.append(ev.wav_id)
        return result

    @property
    def exceeds_dtx_wav_limit(self) -> bool:
        """True if retaining all slices in Toggle=ON would exceed DTX 36-base limit (1295)."""
        defined_or_used = set(self.wavs.keys()) | set(self.referenced_wav_ids)
        return len(defined_or_used) > 1295

    @property
    def toggle_off_locked(self) -> bool:
        """True when Toggle MUST be locked to OFF per AGENTS.md Rule 5."""
        return self.exceeds_dtx_wav_limit


@dataclass
class MappingConfig:
    """User configuration for the 15-Tab mapping, default bound chips, and Toggle mode."""

    tab_wavs: Dict[TabId, List[str]] = field(default_factory=dict)
    default_lane_binds: Dict[TabId, str] = field(default_factory=dict)
    toggle_on: bool = False  # v1 default: Toggle = OFF
    slicer_bgm_columns: List[int] = field(default_factory=list)  # up to 5 1-based B-column indices
    overflow_strategy: str = "fail"  # "fail" | "nudge_192" | "fallback_toggle_off"
    separate_lbd: bool = False  # False => sync BD default bind to lane 03 LBD sub-slot when LBD unbound


@dataclass
class ConversionOptions:
    """Export options for convert_bms()."""

    output_dir: Path
    output_dtx_name: Optional[str] = None
    utf8: bool = False  # False => cp932 (default); True => utf-8
    transcode_wav_to_ogg: bool = False
    dlevel: Optional[int] = None
    comment: Optional[str] = None
    sample_rate: int = 44100
    ogg_quality: float = 6.0
    ffmpeg_path: Optional[str] = None
    copy_retained_audio: bool = True
    render_bgm: bool = True  # only used in Toggle=OFF
    cut_same_wav: bool = False  # Reserved for optional mono voice-cut; v1 default is polyphonic


@dataclass
class PlacedDtxNote:
    """A note placed on a specific legal DTX channel."""

    measure: int
    pos: Fraction
    channel: str
    dtx_wav_id: str
    original_wav_id: str
    time_sec: float = 0.0
    nudged_192: int = 0  # -1, 0, or +1


@dataclass
class MixStats:
    """Diagnostics from offline BGM audio rendering."""

    output_path: Optional[Path]
    sample_rate: int
    total_frames: int
    duration_sec: float
    mixed_event_count: int
    unique_files_decoded: int
    missing_event_count: int
    peak_before_norm: float
    peak_after_norm: float
    rms: float
    gain_applied: float


@dataclass
class ConversionResult:
    """Full result of a BMS -> DTX conversion."""

    chart: BmsChart
    output_dir: Path
    dtx_path: Path
    bgm_path: Optional[Path]
    effective_toggle_on: bool
    toggle_off_forced: bool
    dtx_encoding: str
    wav_id_map: Dict[str, str]  # original BMS wav_id -> renumbered DTX base-36 wav_id
    dtx_wav_table: List[Tuple[str, str]]  # [(dtx_wav_id, output_filename), ...]
    lane_bind_lines: List[str]
    placed_note_count: int
    mix_stats: Optional[MixStats]
    copied_files: List[Path]
    transcoded_files: List[Path]
    warnings: List[ConversionWarning] = field(default_factory=list)
    nudged_overflow_count: int = 0
    preview_path: Optional[Path] = None
