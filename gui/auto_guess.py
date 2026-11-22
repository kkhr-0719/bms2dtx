"""Filename-based drum lane guessing ("按文件名猜轨") and cymbal panning routing.

Pure helpers (no tkinter): the GUI only calls these when the user explicitly
clicks the button, and only for WAVs that are not yet assigned to any Tab.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Dict, Iterable, List, Optional, Tuple

from core import TabId

# Whole-token abbreviations (strictly matched against tokens split on non-letter boundaries
# so e.g. 'sn' matches 'sn01.wav' / 'sn_hard.wav' and NEVER 'snow.wav' / 'synth.wav').
# Note: bare "bell" is intentionally omitted so melody bell slices are not misclassified as RD;
# Ride/Cymbal + Bell compounds (e.g. ride_bell, cym_bell) are handled explicitly below.
_TOKEN_MAP: Dict[str, TabId] = {
    # BD / Kick
    "bd": TabId.BD,
    "kick": TabId.BD,
    "kck": TabId.BD,
    "bassdrum": TabId.BD,
    "bdrum": TabId.BD,
    "bdrm": TabId.BD,
    # SD / Snare / Rim / Clap
    "sd": TabId.SD,
    "sn": TabId.SD,
    "snr": TabId.SD,
    "snare": TabId.SD,
    "rim": TabId.SD,
    "rimshot": TabId.SD,
    "sidestick": TabId.SD,
    "clap": TabId.SD,
    "handclap": TabId.SD,
    # HH Close / Open
    "hh": TabId.HH_CLOSE,
    "hhc": TabId.HH_CLOSE,
    "chh": TabId.HH_CLOSE,
    "hihat": TabId.HH_CLOSE,
    "hhclose": TabId.HH_CLOSE,
    "hhclosed": TabId.HH_CLOSE,
    "closehat": TabId.HH_CLOSE,
    "closedhat": TabId.HH_CLOSE,
    "hho": TabId.HH_OPEN,
    "ohh": TabId.HH_OPEN,
    "hhopen": TabId.HH_OPEN,
    "openhat": TabId.HH_OPEN,
    "openhihat": TabId.HH_OPEN,
    "ophat": TabId.HH_OPEN,
    "opendhat": TabId.HH_OPEN,
    # Toms (explicit pitch/position)
    "ht": TabId.HT,
    "hitom": TabId.HT,
    "hightom": TabId.HT,
    "lt": TabId.LT,
    "lowtom": TabId.LT,
    "midtom": TabId.LT,
    "ft": TabId.FT,
    "floortom": TabId.FT,
    "floor": TabId.FT,
    # Cymbals / Ride
    "cy": TabId.CY,
    "cym": TabId.CY,
    "crash": TabId.CY,
    "cymbal": TabId.CY,
    "cr": TabId.CY,
    "crs": TabId.CY,
    "splash": TabId.CY,
    "splt": TabId.CY,
    "china": TabId.CY,
    "rd": TabId.RD,
    "ride": TabId.RD,
    "ridecup": TabId.RD,
    "ridebell": TabId.RD,
    "rdbell": TabId.RD,
    "cymbell": TabId.RD,
    "cymbalbell": TabId.RD,
    # Left Cymbal / Pedal / Left Bass
    "lc": TabId.LC,
    "leftcrash": TabId.LC,
    "leftcymbal": TabId.LC,
    "lp": TabId.LP,
    "leftpedal": TabId.LP,
    "hhpedal": TabId.LP,
    "lbd": TabId.LBD,
    "leftbass": TabId.LBD,
    "leftbassdrum": TabId.LBD,
}

# Exact and ordered compound Katakana keywords (checked on Katakana runs)
_KATAKANA_RULES: Tuple[Tuple[str, TabId], ...] = (
    ("オープンハイハット", TabId.HH_OPEN),
    ("ハイハットオープン", TabId.HH_OPEN),
    ("オープンハット", TabId.HH_OPEN),
    ("クローズハイハット", TabId.HH_CLOSE),
    ("ハイハットクローズ", TabId.HH_CLOSE),
    ("クローズハット", TabId.HH_CLOSE),
    ("ハイタム", TabId.HT),
    ("ロータム", TabId.LT),
    ("ミッドタム", TabId.LT),
    ("フロアタム", TabId.FT),
    ("バスドラム", TabId.BD),
    ("バスドラ", TabId.BD),
    ("キック", TabId.BD),
    ("スネア", TabId.SD),
    ("リムショット", TabId.SD),
    ("リム", TabId.SD),
    ("クラップ", TabId.SD),
    ("ハンドクラップ", TabId.SD),
    ("クラッシュ", TabId.CY),
    ("スプラッシュ", TabId.CY),
    ("チャイナ", TabId.CY),
    ("シンバル", TabId.CY),
    ("ライド", TabId.RD),
    ("フロア", TabId.FT),
)

_SPLIT_LATIN_RE = re.compile(r"[^A-Za-z]+")
_KATAKANA_RUN_RE = re.compile(r"[\u30A0-\u30FFー]+")
_TOM_AFTER_NUM_RE = re.compile(r"(?:^|[^A-Za-z])(?:tom|タム)[_\-\s]*0*(\d+)", re.IGNORECASE)
_TOM_BEFORE_NUM_RE = re.compile(r"(?:^|\D)0*(\d+)[_\-\s]*(?:tom|タム)(?:$|[^A-Za-z])", re.IGNORECASE)

_CYMBAL_ALL_TOKENS = (
    "cymbal",
    "crash",
    "cy",
    "cym",
    "cr",
    "crs",
    "china",
    "splash",
    "splt",
)
_HIHAT_TOKENS = (
    "hh",
    "hihat",
    "hat",
    "hhc",
    "chh",
    "hho",
    "ohh",
    "hhopen",
    "openhat",
    "openhihat",
    "ophat",
    "opendhat",
    "hhclose",
    "hhclosed",
    "closehat",
    "closedhat",
)


def _tokens(stem: str) -> List[str]:
    return [t.lower() for t in _SPLIT_LATIN_RE.split(stem) if t]


def _katakana_runs(stem: str) -> List[str]:
    return _KATAKANA_RUN_RE.findall(stem)


def _is_cymbal_bell(toks: List[str], joined: str) -> bool:
    if "bell" in toks and any(t in ("ride", "rd", "cym", "cymbal") for t in toks):
        return True
    return any(k in joined for k in ("ridebell", "rdbell", "cymbell", "cymbalbell"))


def _is_hihat_context(toks: List[str], joined: str, kata_joined: str) -> bool:
    if "ハイハット" in kata_joined or "ハット" in kata_joined:
        return True
    if any(t in _HIHAT_TOKENS for t in toks):
        return True
    return "hihat" in joined


def _classify_cymbal_kind(filename: str) -> Optional[str]:
    """Classify a cymbal filename for context isolation and L/R panning disambiguation.

    Returns one of ``"ride"``, ``"lc"``, ``"rc"``, ``"crash"``, ``"china_splash"``,
    ``"generic_cymbal"``, or ``None``.
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    toks = _tokens(stem)
    kata_runs = _katakana_runs(stem)
    if not toks and not kata_runs:
        return None
    joined = "".join(toks)
    kata_joined = "".join(kata_runs)

    # Hi-hats are handled separately and must never enter cymbal panning routing.
    if _is_hihat_context(toks, joined, kata_joined):
        return None

    # Explicit Ride (including Ride/Cymbal + Bell compounds).
    if (
        _is_cymbal_bell(toks, joined)
        or any(t in ("rd", "ride", "ridecup", "ridecym", "ridecymbal") for t in toks)
        or "ride" in joined
        or "ライド" in kata_joined
    ):
        return "ride"

    # Explicit Left / Right Cymbal markers.
    if (
        "lc" in toks
        or "leftcrash" in joined
        or "leftcymbal" in joined
        or ("left" in toks and any(t in _CYMBAL_ALL_TOKENS for t in toks))
    ):
        return "lc"
    if (
        "rc" in toks
        or "rightcrash" in joined
        or "rightcymbal" in joined
        or ("right" in toks and any(t in _CYMBAL_ALL_TOKENS for t in toks))
    ):
        return "rc"

    # Explicit Crash keywords.
    if (
        any(t in ("crash", "cr", "crs", "cymcrash", "crashcym", "crashcymbal") for t in toks)
        or "crash" in joined
        or "クラッシュ" in kata_joined
    ):
        return "crash"

    # China / Splash cymbals.
    if (
        any(t in ("china", "splash", "splt") for t in toks)
        or "china" in joined
        or "splash" in joined
        or "チャイナ" in kata_joined
        or "スプラッシュ" in kata_joined
    ):
        return "china_splash"

    # Generic Cymbal keywords (cym / cy / cymbal / シンバル).
    if (
        any(t in ("cy", "cym", "cymbal") for t in toks)
        or "cymbal" in joined
        or "シンバル" in kata_joined
    ):
        return "generic_cymbal"

    return None


def detect_stereo_pan_cymbal_tab(
    audio_path: Optional[Path | str],
    *,
    effective_swap: bool = False,
    default_tab: TabId = TabId.CY,
    db_threshold: float = 1.5,
) -> TabId:
    """Detect LC (left-panned) vs CY (right-panned) from a cymbal sample's stereo RMS balance.

    Honors ``effective_swap`` (global Swap L/R XOR per-slice L/R override).
    Falls back to ``default_tab`` for centered, mono, silent, missing, or unreadable files.
    """
    if audio_path is None:
        return default_tab
    p = Path(audio_path)
    if not p.is_file():
        return default_tab
    try:
        import numpy as np
        import soundfile as sf

        data, _sr = sf.read(str(p), dtype="float32", always_2d=True)
        if data.ndim != 2 or data.shape[0] == 0 or data.shape[1] < 2:
            return default_tab
        if effective_swap:
            left = data[:, 1]
            right = data[:, 0]
        else:
            left = data[:, 0]
            right = data[:, 1]
        rms_l = float(np.sqrt(np.mean(np.square(left, dtype=np.float64))))
        rms_r = float(np.sqrt(np.mean(np.square(right, dtype=np.float64))))
        if max(rms_l, rms_r) <= 1e-7:
            return default_tab
        db_diff = 20.0 * math.log10((rms_l + 1e-12) / (rms_r + 1e-12))
        if db_diff >= db_threshold:
            return TabId.LC
        if db_diff <= -db_threshold:
            return TabId.CY
        return default_tab
    except Exception:
        return default_tab


def guess_tab_for_filename(filename: str) -> Optional[TabId]:
    """Return a drum/Tab guess for a sample filename, or None when uncertain."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    toks = _tokens(stem)
    kata_runs = _katakana_runs(stem)
    if not toks and not kata_runs:
        return None
    joined = "".join(toks)
    kata_joined = "".join(kata_runs)

    # 1. Katakana hi-hat open/close + multi-word rules
    if "ハイハット" in kata_joined or "ハット" in kata_joined:
        if (
            "オープン" in kata_joined
            or any(t.startswith("open") or t in ("hho", "ohh", "op") for t in toks)
            or "opend" in joined
        ):
            return TabId.HH_OPEN
        if "クローズ" in kata_joined or any(t.startswith("clos") or t in ("hhc", "chh") for t in toks):
            return TabId.HH_CLOSE
        if "ハイハット" in kata_joined:
            return TabId.HH_CLOSE

    for kw, tab in _KATAKANA_RULES:
        if kw in kata_joined:
            return tab

    # 2. Hi-hat open/close disambiguation across Latin tokens (including "opend" typo tolerance)
    if _is_hihat_context(toks, joined, kata_joined):
        if (
            any(
                t.startswith("open")
                or t
                in (
                    "hho",
                    "ohh",
                    "op",
                    "hhopen",
                    "openhat",
                    "openhihat",
                    "ophat",
                    "opendhat",
                )
                for t in toks
            )
            or "hhopen" in joined
            or "openhat" in joined
            or "opendhat" in joined
            or "openhihat" in joined
            or "ophat" in joined
        ):
            return TabId.HH_OPEN
        return TabId.HH_CLOSE

    # 3. Ride / Cymbal + Bell compounds (bare 'bell' intentionally excluded)
    if _is_cymbal_bell(toks, joined):
        return TabId.RD

    # 4. Left-crash / left-cymbal multi-token disambiguation before generic CY
    if "left" in toks and any(t in _CYMBAL_ALL_TOKENS for t in toks):
        return TabId.LC
    if "left" in toks and "pedal" in toks:
        return TabId.LP

    # 5. Exact token dictionary lookup (boundary-safe!)
    for tok in toks:
        tab = _TOKEN_MAP.get(tok)
        if tab is not None:
            return tab

    # 6. Explicit tom qualifiers split across tokens (e.g. "high_tom", "mid_tom", "floor_tom")
    if "tom" in toks:
        if any(t in ("hi", "high") for t in toks):
            return TabId.HT
        if any(t in ("mid", "middle", "lo", "low") for t in toks):
            return TabId.LT
        if "floor" in toks:
            return TabId.FT

    # 7. Multi-token "bass drum"
    if "bass" in toks and "drum" in toks:
        return TabId.BD

    # 8. Unambiguous longer substrings on joined ASCII stem (note: short 2-3 char tokens
    #    like 'sn'/'cr'/'bd'/'rd' and bare 'bell' are intentionally excluded here to avoid false positives).
    for key in ("snare", "kick", "crash", "cymbal", "ride", "hihat"):
        if key in joined:
            return _TOKEN_MAP[key]

    return None


def _extract_tom_sequence_index(filename: str) -> Optional[int]:
    """Extract numeric index for generic numbered toms (e.g. tom1/tom2/tom3, タム1/2/3)."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    m = _TOM_AFTER_NUM_RE.search(stem)
    if m:
        return int(m.group(1))
    m_pre = _TOM_BEFORE_NUM_RE.search(stem)
    if m_pre:
        return int(m_pre.group(1))
    return None


def guess_assignments(
    candidates: Iterable[tuple[str, str]],
    *,
    audio_paths: Optional[Dict[str, Optional[Path | str]]] = None,
    effective_swaps: Optional[Dict[str, bool]] = None,
) -> Dict[TabId, List[str]]:
    """Guess Tab assignments for ``(wav_id, filename)`` pairs with context & cymbal panning."""
    cand_list = list(candidates)
    result: Dict[TabId, List[str]] = {}
    pending_toms: List[Tuple[str, int]] = []

    cymbal_kinds: Dict[str, Optional[str]] = {
        wav_id: _classify_cymbal_kind(name) for wav_id, name in cand_list
    }
    has_explicit_crash = any(k in ("crash", "lc", "rc") for k in cymbal_kinds.values())
    has_explicit_ride = any(k == "ride" for k in cymbal_kinds.values())

    for wav_id, name in cand_list:
        ckind = cymbal_kinds.get(wav_id)
        if ckind == "ride":
            tab: Optional[TabId] = TabId.RD
        elif ckind == "lc":
            tab = TabId.LC
        elif ckind == "rc":
            tab = TabId.CY
        elif ckind in ("crash", "china_splash", "generic_cymbal"):
            if ckind == "generic_cymbal" and has_explicit_crash and not has_explicit_ride:
                tab = TabId.RD
            else:
                path = audio_paths.get(wav_id) if audio_paths is not None else None
                eff_swap = bool(effective_swaps.get(wav_id, False)) if effective_swaps is not None else False
                default_tab = TabId.LC if ckind == "crash" else TabId.CY
                tab = detect_stereo_pan_cymbal_tab(
                    path,
                    effective_swap=eff_swap,
                    default_tab=default_tab,
                )
        else:
            tab = guess_tab_for_filename(name)

        if tab is not None:
            result.setdefault(tab, []).append(wav_id)
            continue
        tom_idx = _extract_tom_sequence_index(name)
        if tom_idx is not None:
            pending_toms.append((wav_id, tom_idx))

    if pending_toms:
        distinct_indices = sorted({idx for _wid, idx in pending_toms})
        n = len(distinct_indices)
        rank_map: Dict[int, TabId] = {}
        if n == 1:
            rank_map[distinct_indices[0]] = TabId.HT
        elif n == 2:
            rank_map[distinct_indices[0]] = TabId.HT
            rank_map[distinct_indices[1]] = TabId.LT
        else:
            rank_map[distinct_indices[0]] = TabId.HT
            for mid in distinct_indices[1:-1]:
                rank_map[mid] = TabId.LT
            rank_map[distinct_indices[-1]] = TabId.FT
        for wav_id, tom_idx in pending_toms:
            result.setdefault(rank_map[tom_idx], []).append(wav_id)

    return result
