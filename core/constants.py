"""Constants, channel tables, and radix helpers for bms2dtx."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

BASE36_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BASE62_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

_BASE36_MAP: Dict[str, int] = {ch: idx for idx, ch in enumerate(BASE36_DIGITS)}
_BASE62_MAP: Dict[str, int] = {ch: idx for idx, ch in enumerate(BASE62_DIGITS)}

AUTO_LP_RESONANT_WAV_ID = "ZY"
AUTO_LP_SILENT_WAV_ID = "ZZ"
AUTO_LP_SILENT_FILENAME = "_silent_zz.wav"
RESERVED_AUTO_LP_WAV_IDS: frozenset[str] = frozenset(
    {AUTO_LP_RESONANT_WAV_ID, AUTO_LP_SILENT_WAV_ID}
)

AUTO_LP_CHIP_NONE = "none"
AUTO_LP_CHIP_AUTO = "auto"
AUTO_LP_CHIP_EMPTY_ZZ = "empty_zz"
AUTO_LP_THRESHOLD_NOTE = "note_value"
AUTO_LP_THRESHOLD_MS = "ms"
AUTO_LP_VALID_DENOMINATORS: Tuple[int, ...] = (2, 4, 6, 8, 12, 16)

PROJECT_MAPPING_FILENAME = "__bms2dtx.json"
CONVERSION_LOG_FILENAME = "__bms2dtx_log.txt"
DRUMS_STEM_FILENAME = "drums.ogg"
DRUMS_STEM_SE_CHANNEL = "68"  # SE8 (strictly isolated from SE1..5, SE6, SE7, SE11..14)
DRUMS_STEM_VOLUME = 10
SET_DEF_FILENAME = "set.def"
FOUR_DIFF_ENTRIES: Tuple[Tuple[str, str], ...] = (
    ("__bsc.dtx", "BASIC"),
    ("__adv.dtx", "ADVANCED"),
    ("__extr.dtx", "EXTREME"),
    ("__mstr.dtx", "MASTER"),
)

DTX_MAX_BASE36_ID = 1295  # 00..ZZ
DTX_MAX_WAV_SLOTS = 1293  # 01..ZX (ZY & ZZ permanently reserved for Auto-LP)
DTX_MAX_MAPPED_SLOTS_TOGGLE_OFF = 1292  # 02..ZX (01 reserved for bgm.ogg; ZY/ZZ reserved)
DTX_MAX_BPM_SLOTS = 1295  # #BPM01..#BPMZZ
MAX_SLICER_COLUMNS = 5
MAX_MEASURE_DIVISIONS = 1536
QUANTIZE_FALLBACK_DIVISIONS = 192

SUPPORTED_AUDIO_EXTENSIONS: Tuple[str, ...] = (
    ".ogg",
    ".wav",
    ".mp3",
    ".xa",
    ".flac",
    ".m4a",
    ".aac",
    ".opus",
    ".wma",
)

AUTO_PREVIEW_AUDIO_EXTENSIONS: Tuple[str, ...] = (
    ".ogg",
    ".wav",
    ".mp3",
    ".flac",
)

SUPPORTED_IMAGE_EXTENSIONS: Tuple[str, ...] = (
    ".bmp",
    ".png",
    ".jpg",
    ".jpeg",
)

SUPPORTED_VIDEO_EXTENSIONS: Tuple[str, ...] = (
    ".mp4",
    ".avi",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".webm",
    ".mkv",
)

SUPPORTED_BGA_EXTENSIONS: Tuple[str, ...] = (
    SUPPORTED_IMAGE_EXTENSIONS + SUPPORTED_VIDEO_EXTENSIONS
)

IMAGE_MODE_KEEP = "keep"
IMAGE_MODE_RESIZE = "resize"
IMAGE_MODE_NONE = "none"
VALID_IMAGE_MODES: Tuple[str, ...] = (
    IMAGE_MODE_KEEP,
    IMAGE_MODE_RESIZE,
    IMAGE_MODE_NONE,
)


class TabId(str, Enum):
    """The 15 mapping tabs defined in AGENTS.md, in canonical WAV renumbering order."""

    LC = "LC"
    HH_CLOSE = "HH_CLOSE"
    HH_OPEN = "HH_OPEN"
    LP = "LP"
    LBD = "LBD"
    SD = "SD"
    HT = "HT"
    BD = "BD"
    LT = "LT"
    FT = "FT"
    CY = "CY"
    RD = "RD"
    OTHER = "OTHER"
    GUITAR = "GUITAR"
    BASS = "BASS"


@dataclass(frozen=True)
class TabSpec:
    """Metadata and DTX channel mapping for one of the 15 tabs."""

    tab_id: TabId
    label: str
    dtx_channels: Tuple[str, ...]
    is_drum: bool
    bind_lane: Optional[str] = None
    bind_slot: Optional[str] = None  # "main" or "sub"


TAB_SPECS: Tuple[TabSpec, ...] = (
    TabSpec(TabId.LC, "LC", ("1A",), True, "01", "main"),
    TabSpec(TabId.HH_CLOSE, "HH Close", ("11",), True, "02", "main"),
    TabSpec(TabId.HH_OPEN, "HH Open", ("18",), True, "02", "sub"),
    TabSpec(TabId.LP, "LP", ("1B",), True, "03", "main"),
    TabSpec(TabId.LBD, "LBD", ("1C",), True, "03", "sub"),
    TabSpec(TabId.SD, "SD", ("12",), True, "04", "main"),
    TabSpec(TabId.HT, "HT", ("14",), True, "05", "main"),
    TabSpec(TabId.BD, "BD", ("13",), True, "06", "main"),
    TabSpec(TabId.LT, "LT", ("15",), True, "07", "main"),
    TabSpec(TabId.FT, "FT", ("17",), True, "08", "main"),
    TabSpec(TabId.CY, "CY", ("16",), True, "09", "main"),
    TabSpec(TabId.RD, "RD", ("19",), True, "09", "sub"),
    TabSpec(TabId.OTHER, "Other", ("66",), False),
    TabSpec(TabId.GUITAR, "Guitar", ("71", "72"), False),
    TabSpec(TabId.BASS, "Bass", ("73", "74"), False),
)

TAB_SPEC_MAP: Dict[TabId, TabSpec] = {spec.tab_id: spec for spec in TAB_SPECS}
TAB_ORDER: Tuple[TabId, ...] = tuple(spec.tab_id for spec in TAB_SPECS)
DRUM_TABS: Tuple[TabId, ...] = tuple(spec.tab_id for spec in TAB_SPECS if spec.is_drum)
TOM_TABS: frozenset[TabId] = frozenset({TabId.HT, TabId.LT, TabId.FT})

# DTXCreator #DTXC_LANEBINDEDCHIP lane -> (main_tab, sub_tab)
DTXC_BIND_LANES: Tuple[Tuple[str, TabId, Optional[TabId]], ...] = (
    ("01", TabId.LC, None),
    ("02", TabId.HH_CLOSE, TabId.HH_OPEN),
    ("03", TabId.LP, TabId.LBD),
    ("04", TabId.SD, None),
    ("05", TabId.HT, None),
    ("06", TabId.BD, None),
    ("07", TabId.LT, None),
    ("08", TabId.FT, None),
    ("09", TabId.CY, TabId.RD),
)

# DTX SE channels: 32 legal channels (SE1..SE32)
ALL_SE_CHANNELS: Tuple[str, ...] = (
    "61", "62", "63", "64", "65", "66", "67", "68", "69",  # SE1..SE9
    "70", "71", "72", "73", "74", "75", "76", "77", "78", "79",  # SE10..SE19
    "80", "81", "82", "83", "84", "85", "86", "87", "88", "89",  # SE20..SE29
    "90", "91", "92",  # SE30..SE32
)

SLICER_SE_CHANNELS: Tuple[str, ...] = ("61", "62", "63", "64", "65")  # SE1..SE5
OTHER_SE_CHANNEL = "66"  # SE6
TOM_OVERFLOW_SE_CHANNEL = "67"  # SE7 (dedicated 1-voice overflow lane for HT/LT/FT in Toggle=OFF)
GUITAR_SE_CHANNELS: Tuple[str, ...] = ("71", "72")  # SE11..SE12
BASS_SE_CHANNELS: Tuple[str, ...] = ("73", "74")  # SE13..SE14

# Non-interrupting Collapse pool priority
COLLAPSE_PRIMARY_PRE: Tuple[str, ...] = ("67", "68", "69", "70")  # SE7..SE10
COLLAPSE_PRIMARY_MID: Tuple[str, ...] = (
    "75", "76", "77", "78", "79", "80", "81", "82", "83",  # SE15..SE23
)
COLLAPSE_PRIMARY_TAIL: Tuple[str, ...] = ("90", "91", "92")  # SE30..SE32
COLLAPSE_BACKUP_CHANNELS: Tuple[str, ...] = (
    "84", "85", "86", "87", "88", "89",  # SE24..SE29
)

DRUM_DTX_CHANNELS: Tuple[str, ...] = (
    "11", "12", "13", "14", "15", "16", "17", "18", "19", "1A", "1B", "1C",
)

DRUMS_STEM_SOURCE_CHANNELS: frozenset[str] = frozenset(
    DRUM_DTX_CHANNELS + (OTHER_SE_CHANNEL, TOM_OVERFLOW_SE_CHANNEL)
)

LEGAL_DTX_NOTE_CHANNELS: frozenset[str] = frozenset(
    ("01",) + DRUM_DTX_CHANNELS + ALL_SE_CHANNELS
)

# BMS channel classifications
BMS_PLAYABLE_NORMAL_CHANNELS: frozenset[str] = frozenset(
    {f"1{i}" for i in range(1, 10)} | {f"2{i}" for i in range(1, 10)}
)
BMS_LN_CHANNELS: frozenset[str] = frozenset(
    {f"5{i}" for i in range(1, 10)} | {f"6{i}" for i in range(1, 10)}
)
BMS_INVISIBLE_CHANNELS: frozenset[str] = frozenset(
    {f"3{i}" for i in range(1, 10)} | {f"4{i}" for i in range(1, 10)}
)
BMS_MINE_CHANNELS: frozenset[str] = frozenset(
    {f"D{i}" for i in range(1, 10)} | {f"E{i}" for i in range(1, 10)}
)
BMS_BGA_CHANNELS: frozenset[str] = frozenset({"04", "06", "07", "0A"})

DEFAULT_WAV_LABELS: Dict[str, str] = {
    "BGM": "BGM",
    TabId.LC.value: "LC",
    TabId.HH_CLOSE.value: "HHC",
    TabId.HH_OPEN.value: "HHO",
    TabId.LP.value: "LP",
    TabId.LBD.value: "LBD",
    TabId.SD.value: "SD",
    TabId.HT.value: "HT",
    TabId.BD.value: "BD",
    TabId.LT.value: "LT",
    TabId.FT.value: "FT",
    TabId.CY.value: "CY",
    TabId.RD.value: "RD",
    TabId.OTHER.value: "Other",
    TabId.GUITAR.value: "GT",
    TabId.BASS.value: "BS",
}

WAV_LABEL_KEYS: Tuple[str, ...] = ("BGM",) + tuple(spec.tab_id.value for spec in TAB_SPECS)


def sanitize_wav_label(label: str) -> str:
    """Strip newlines, tabs, and semicolons from a custom #WAVxx inline label."""
    return (
        label.replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
        .replace(";", "")
        .strip()
    )


def int_to_base36(value: int) -> str:
    """Convert an integer in [0, 1295] to a 2-character uppercase base-36 ID."""
    if not (0 <= value <= DTX_MAX_BASE36_ID):
        raise ValueError(f"Base-36 2-char ID out of range [0, 1295]: {value}")
    hi, lo = divmod(value, 36)
    return BASE36_DIGITS[hi] + BASE36_DIGITS[lo]


def base36_to_int(token: str) -> int:
    """Convert a 2-character case-insensitive base-36 ID to an integer."""
    if len(token) != 2:
        raise ValueError(f"Expected 2-character base-36 token, got {token!r}")
    upper = token.upper()
    if upper[0] not in _BASE36_MAP or upper[1] not in _BASE36_MAP:
        raise ValueError(f"Invalid base-36 token: {token!r}")
    return _BASE36_MAP[upper[0]] * 36 + _BASE36_MAP[upper[1]]


def base62_to_int(token: str) -> int:
    """Convert a 2-character case-sensitive base-62 ID to an integer."""
    if len(token) != 2:
        raise ValueError(f"Expected 2-character base-62 token, got {token!r}")
    if token[0] not in _BASE62_MAP or token[1] not in _BASE62_MAP:
        raise ValueError(f"Invalid base-62 token: {token!r}")
    return _BASE62_MAP[token[0]] * 62 + _BASE62_MAP[token[1]]


def format_dtx_number(value: float) -> str:
    """Format a float cleanly for DTX tags (e.g. 140.0 -> '140', 0.75 -> '0.75')."""
    rounded = round(value, 6)
    if abs(rounded - round(rounded)) < 1e-6:
        return str(int(round(rounded)))
    return f"{rounded:.6f}".rstrip("0").rstrip(".")
