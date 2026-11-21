"""Filename-based drum lane guessing ("按文件名猜轨").

Pure helpers (no tkinter): the GUI only calls these when the user explicitly
clicks the button, and only for WAVs that are not yet assigned to any Tab.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from core import TabId

# Whole-token abbreviations (strictly matched against tokens split on non-letter boundaries
# so e.g. 'sn' matches 'sn01.wav' / 'sn_hard.wav' and NEVER 'snow.wav' / 'synth.wav').
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
    "bell": TabId.RD,
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


def _tokens(stem: str) -> List[str]:
    return [t.lower() for t in _SPLIT_LATIN_RE.split(stem) if t]


def _katakana_runs(stem: str) -> List[str]:
    return _KATAKANA_RUN_RE.findall(stem)


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
        if "オープン" in kata_joined or "open" in toks or "opened" in toks:
            return TabId.HH_OPEN
        if "クローズ" in kata_joined or "close" in toks or "closed" in toks:
            return TabId.HH_CLOSE
        if "ハイハット" in kata_joined:
            return TabId.HH_CLOSE

    for kw, tab in _KATAKANA_RULES:
        if kw in kata_joined:
            return tab

    # 2. Hi-hat open/close disambiguation across Latin tokens (e.g. "hihat_open", "open hh")
    has_hh = any(t in ("hh", "hihat", "hat", "hhc", "chh", "hho", "ohh") for t in toks) or "hihat" in joined
    if has_hh:
        if "open" in toks or "opened" in toks or any(t in ("hho", "ohh", "hhopen", "openhat") for t in toks):
            return TabId.HH_OPEN
        return TabId.HH_CLOSE

    # 3. Left-crash / left-cymbal multi-token disambiguation before generic CY
    if "left" in toks and any(t in ("cymbal", "crash", "cy", "cym", "cr") for t in toks):
        return TabId.LC
    if "left" in toks and "pedal" in toks:
        return TabId.LP

    # 4. Exact token dictionary lookup (boundary-safe!)
    for tok in toks:
        tab = _TOKEN_MAP.get(tok)
        if tab is not None:
            return tab

    # 5. Explicit tom qualifiers split across tokens (e.g. "high_tom", "mid_tom", "floor_tom")
    if "tom" in toks:
        if any(t in ("hi", "high") for t in toks):
            return TabId.HT
        if any(t in ("mid", "middle", "lo", "low") for t in toks):
            return TabId.LT
        if "floor" in toks:
            return TabId.FT

    # 6. Multi-token "bass drum"
    if "bass" in toks and "drum" in toks:
        return TabId.BD

    # 7. Unambiguous longer substrings on joined ASCII stem (note: short 2-3 char tokens
    #    like 'sn'/'cr'/'bd'/'rd' are intentionally excluded here to avoid false positives).
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
) -> Dict[TabId, List[str]]:
    """Guess Tab assignments for ``(wav_id, filename)`` pairs (with tom1/2/3 context)."""
    cand_list = list(candidates)
    result: Dict[TabId, List[str]] = {}
    pending_toms: List[Tuple[str, int]] = []

    for wav_id, name in cand_list:
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
