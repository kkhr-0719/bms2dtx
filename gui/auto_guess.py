"""Filename-based drum lane guessing ("按文件名猜轨").

Pure helpers (no tkinter): the GUI only calls these when the user explicitly
clicks the button, and only for WAVs that are not yet assigned to any Tab.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

from core import TabId

# Whole-token abbreviations (matched against tokens split on non-alnum / digit edges).
_TOKEN_MAP: Dict[str, TabId] = {
    "bd": TabId.BD,
    "kick": TabId.BD,
    "bassdrum": TabId.BD,
    "sd": TabId.SD,
    "snare": TabId.SD,
    "hh": TabId.HH_CLOSE,
    "hhc": TabId.HH_CLOSE,
    "chh": TabId.HH_CLOSE,
    "hihat": TabId.HH_CLOSE,
    "hhclose": TabId.HH_CLOSE,
    "hhclosed": TabId.HH_CLOSE,
    "hho": TabId.HH_OPEN,
    "ohh": TabId.HH_OPEN,
    "hhopen": TabId.HH_OPEN,
    "ht": TabId.HT,
    "hitom": TabId.HT,
    "hightom": TabId.HT,
    "lt": TabId.LT,
    "lowtom": TabId.LT,
    "ft": TabId.FT,
    "floortom": TabId.FT,
    "floor": TabId.FT,
    "cy": TabId.CY,
    "crash": TabId.CY,
    "cymbal": TabId.CY,
    "rd": TabId.RD,
    "ride": TabId.RD,
    "lc": TabId.LC,
    "leftcrash": TabId.LC,
    "leftcymbal": TabId.LC,
    "lp": TabId.LP,
    "leftpedal": TabId.LP,
    "lbd": TabId.LBD,
    "leftbass": TabId.LBD,
    "leftbassdrum": TabId.LBD,
}

_SPLIT_RE = re.compile(r"[^A-Za-z]+")


def _tokens(stem: str) -> List[str]:
    return [t.lower() for t in _SPLIT_RE.split(stem) if t]


def guess_tab_for_filename(filename: str) -> Optional[TabId]:
    """Return a drum/Tab guess for a sample filename, or None when uncertain."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    toks = _tokens(stem)
    if not toks:
        return None
    joined = "".join(toks)

    # Hi-hat open/close disambiguation across tokens (e.g. "hihat_open", "open hh")
    has_hh = any(t in ("hh", "hihat", "hat") for t in toks) or "hihat" in joined
    if has_hh:
        if "open" in toks or "opened" in toks or any(t in ("hho", "ohh") for t in toks):
            return TabId.HH_OPEN
        return TabId.HH_CLOSE

    for tok in toks:
        tab = _TOKEN_MAP.get(tok)
        if tab is not None:
            return tab
    # "bass drum" split across tokens
    if "bass" in toks and "drum" in toks:
        return TabId.BD
    for key in ("snare", "kick", "crash", "cymbal", "ride"):
        if key in joined:
            return _TOKEN_MAP[key]
    return None


def guess_assignments(
    candidates: Iterable[tuple[str, str]],
) -> Dict[TabId, List[str]]:
    """Guess Tab assignments for ``(wav_id, filename)`` pairs (in given order)."""
    result: Dict[TabId, List[str]] = {}
    for wav_id, name in candidates:
        tab = guess_tab_for_filename(name)
        if tab is None:
            continue
        result.setdefault(tab, []).append(wav_id)
    return result
