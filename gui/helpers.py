"""Pure (tkinter-free) GUI logic: assignment model, list formatting, summaries, JSON I/O.

Everything here is unit-tested in ``tests/test_gui_helpers.py`` without a display.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core import (
    AUTO_LP_CHIP_AUTO,
    AUTO_LP_CHIP_EMPTY_ZZ,
    AUTO_LP_CHIP_NONE,
    DRUM_TABS,
    PROJECT_MAPPING_FILENAME,
    TAB_SPEC_MAP,
    TAB_SPECS,
    AutoLpConfig,
    BmsChart,
    ConversionResult,
    MappingConfig,
    TabId,
    WavEntry,
    base36_to_int,
    base62_to_int,
    build_project_mapping_dict,
    parse_auto_lp_config,
    resolve_auto_bind_wav_id,
)

from .i18n import tr

GRAY_FOREGROUND = "#888888"
NO_BIND_LABEL = "(未指定 / 00)"
AUTO_BIND_SENTINEL = "__AUTO__"
TAB_NOTES: Dict[TabId, str] = {
    TabId.OTHER: "固定分配至 SE6 (通道 66)",
    TabId.GUITAR: "可选：留空即禁用并自动释放 SE11–SE12",
    TabId.BASS: "可选：留空即禁用并自动释放 SE13–SE14",
}

_TAB_NOTE_KEYS: Dict[TabId, str] = {
    TabId.OTHER: "tab_note_other",
    TabId.GUITAR: "tab_note_guitar",
    TabId.BASS: "tab_note_bass",
}


def get_no_bind_label() -> str:
    return tr("no_bind_label")


def get_tab_note(tab: TabId) -> Optional[str]:
    key = _TAB_NOTE_KEYS.get(tab)
    return tr(key) if key else None


# --------------------------------------------------------------------------- tabs
def tab_label(tab: TabId) -> str:
    return TAB_SPEC_MAP[tab].label


def tab_title(tab: TabId, count: int) -> str:
    """Tab button title with live count badge, e.g. ``SD (6)``."""
    label = tab_label(tab)
    return f"{label} ({count})" if count else label


def tab_has_bind_combobox(tab: TabId) -> bool:
    return tab in DRUM_TABS


def visible_tab_specs(separate_lbd: bool = False) -> List[Any]:
    """Return TAB_SPECS filtered by whether LBD is separately defined."""
    if separate_lbd:
        return list(TAB_SPECS)
    return [spec for spec in TAB_SPECS if spec.tab_id != TabId.LBD]


# ------------------------------------------------ physical file grouping helpers
def wav_sort_key(chart: BmsChart, wav_id: str) -> Tuple[int, str]:
    try:
        value = base62_to_int(wav_id) if chart.is_base62 else base36_to_int(wav_id)
    except ValueError:
        value = 10**9
    return (value, wav_id)


def sorted_wav_ids(chart: BmsChart) -> List[str]:
    return sorted(chart.wavs.keys(), key=lambda w: wav_sort_key(chart, w))


def wav_physical_groups(
    chart: Optional[BmsChart],
) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Group #WAVxx entries that resolve to the exact same physical file on disk.

    Cached on ``chart`` so repeated UI refreshes and row formatting run in O(1) without
    triggering O(N^2) Win32 Path.resolve() filesystem calls.

    Returns:
        (rep_id -> [all_group_wav_ids_in_order], any_wav_id -> rep_id)
    """
    if chart is None:
        return {}, {}

    cache_sig = (
        chart.is_base62,
        len(chart.wavs),
        tuple((wid, entry.exists, entry.resolved_path) for wid, entry in chart.wavs.items()),
    )
    cached = getattr(chart, "_wav_phys_groups_cache", None)
    if cached is not None and cached[0] == cache_sig:
        return cached[1], cached[2]

    rep_to_group: Dict[str, List[str]] = {}
    wid_to_rep: Dict[str, str] = {}
    phys_to_rep: Dict[str, str] = {}

    for wid in sorted_wav_ids(chart):
        entry = chart.wavs[wid]
        if entry.exists and entry.resolved_path is not None:
            phys = os.path.normcase(os.path.abspath(str(entry.resolved_path)))
            if phys in phys_to_rep:
                rep = phys_to_rep[phys]
                rep_to_group[rep].append(wid)
                wid_to_rep[wid] = rep
            else:
                phys_to_rep[phys] = wid
                rep_to_group[wid] = [wid]
                wid_to_rep[wid] = wid
        else:
            rep_to_group[wid] = [wid]
            wid_to_rep[wid] = wid

    try:
        setattr(chart, "_wav_phys_groups_cache", (cache_sig, rep_to_group, wid_to_rep))
    except Exception:
        pass
    return rep_to_group, wid_to_rep


def wav_group_for(chart: Optional[BmsChart], wav_id: str) -> List[str]:
    """Return all #WAVxx IDs sharing the same physical file as ``wav_id``."""
    if chart is None:
        return [wav_id]
    rep_to_group, wid_to_rep = wav_physical_groups(chart)
    rep = wid_to_rep.get(wav_id)
    if rep is None:
        return [wav_id]
    return list(rep_to_group[rep])


def expand_wav_ids(chart: Optional[BmsChart], wav_ids: Iterable[str]) -> List[str]:
    """Expand any WAV ID to include all sibling IDs pointing to the same physical file."""
    if chart is None:
        seen: set[str] = set()
        out: List[str] = []
        for wid in wav_ids:
            if wid not in seen:
                seen.add(wid)
                out.append(wid)
        return out

    rep_to_group, wid_to_rep = wav_physical_groups(chart)
    seen = set()
    out = []
    for wid in wav_ids:
        rep = wid_to_rep.get(wid)
        members = rep_to_group[rep] if rep is not None else [wid]
        for m in members:
            if m not in seen:
                seen.add(m)
                out.append(m)
    return out


# --------------------------------------------------------------- assignment model
@dataclass
class AssignmentModel:
    """Tracks which ``#WAVxx`` is assigned to which of the 15 Tabs (no double assignment)."""

    tab_wavs: Dict[TabId, List[str]] = field(
        default_factory=lambda: {spec.tab_id: [] for spec in TAB_SPECS}
    )
    default_binds: Dict[TabId, str] = field(default_factory=dict)
    explicit_unbinds: set[TabId] = field(default_factory=set)
    separate_lbd: bool = False
    default_bind_mode: str = "auto"
    auto_lp: AutoLpConfig = field(default_factory=AutoLpConfig)
    swap_lr: bool = False
    chart: Optional[BmsChart] = field(default=None, repr=False, compare=False)

    def set_chart(self, chart: Optional[BmsChart]) -> None:
        self.chart = chart

    def set_default_bind_mode(self, mode: str) -> None:
        self.default_bind_mode = "auto" if str(mode).strip().lower() == "auto" else "manual"

    @staticmethod
    def _copy_auto_lp(cfg: AutoLpConfig) -> AutoLpConfig:
        return AutoLpConfig(
            chip_mode=cfg.chip_mode,
            volume=int(cfg.volume),
            threshold_mode=cfg.threshold_mode,
            note_num=int(cfg.note_num),
            note_den=int(cfg.note_den),
            threshold_ms=float(cfg.threshold_ms),
        )

    def clear(self) -> None:
        for tab in self.tab_wavs:
            self.tab_wavs[tab] = []
        self.default_binds.clear()
        self.explicit_unbinds.clear()
        self.auto_lp = AutoLpConfig()

    def snapshot(self) -> Dict[str, Any]:
        """Return a detached copy of mapping state for Undo/Redo."""
        return {
            "tab_wavs": {tab: list(ids) for tab, ids in self.tab_wavs.items()},
            "default_binds": dict(self.default_binds),
            "explicit_unbinds": set(self.explicit_unbinds),
            "separate_lbd": bool(self.separate_lbd),
            "default_bind_mode": self.default_bind_mode,
            "auto_lp": self._copy_auto_lp(self.auto_lp),
            "swap_lr": bool(self.swap_lr),
        }

    def restore(self, snap: Dict[str, Any]) -> None:
        """Restore mapping state from a snapshot produced by :meth:`snapshot`."""
        raw_tabs = snap.get("tab_wavs", {})
        for spec in TAB_SPECS:
            self.tab_wavs[spec.tab_id] = list(raw_tabs.get(spec.tab_id, ()))
        self.default_binds = dict(snap.get("default_binds", {}))
        self.explicit_unbinds = set(snap.get("explicit_unbinds", ()))
        self.separate_lbd = bool(snap.get("separate_lbd", False))
        if "default_bind_mode" in snap:
            self.set_default_bind_mode(str(snap["default_bind_mode"]))
        if "auto_lp" in snap and isinstance(snap["auto_lp"], AutoLpConfig):
            self.auto_lp = self._copy_auto_lp(snap["auto_lp"])
        if "swap_lr" in snap:
            self.swap_lr = bool(snap["swap_lr"])

    def set_separate_lbd(self, enabled: bool) -> None:
        self.separate_lbd = bool(enabled)
        if not self.separate_lbd:
            self.tab_wavs[TabId.LBD] = []
            self.default_binds.pop(TabId.LBD, None)
            self.explicit_unbinds.discard(TabId.LBD)

    def tab_of(self, wav_id: str) -> Optional[TabId]:
        for tab, ids in self.tab_wavs.items():
            if wav_id in ids:
                return tab
        return None

    @property
    def assigned_ids(self) -> set[str]:
        return {wid for ids in self.tab_wavs.values() for wid in ids}

    @property
    def total_assigned(self) -> int:
        return sum(len(ids) for ids in self.tab_wavs.values())

    @property
    def has_assignments(self) -> bool:
        return self.total_assigned > 0

    def display_ids_for_tab(
        self, tab: TabId, chart: Optional[BmsChart] = None
    ) -> List[str]:
        """Return representative WAV IDs for ``tab`` (folding same-physical-file siblings into 1 row)."""
        active_chart = chart if chart is not None else self.chart
        raw_ids = self.tab_wavs[tab]
        if active_chart is None:
            return list(raw_ids)
        _rep_to_group, wid_to_rep = wav_physical_groups(active_chart)
        seen_reps: set[str] = set()
        result: List[str] = []
        for wid in raw_ids:
            rep = wid_to_rep.get(wid, wid)
            if rep not in seen_reps:
                seen_reps.add(rep)
                result.append(rep)
        return result

    def count(self, tab: TabId) -> int:
        return len(self.display_ids_for_tab(tab))

    def assign(
        self,
        tab: TabId,
        wav_ids: Iterable[str],
        *,
        chart: Optional[BmsChart] = None,
    ) -> Tuple[List[str], List[str]]:
        """Add unassigned ids (and their same-physical-file siblings) to ``tab``.

        Returns ``(added, skipped_already_assigned)``.
        """
        active_chart = chart if chart is not None else self.chart
        expanded_ids = expand_wav_ids(active_chart, wav_ids)
        added: List[str] = []
        skipped: List[str] = []
        owner = {wid: t for t, ids in self.tab_wavs.items() for wid in ids}
        for wid in expanded_ids:
            if wid in owner:
                skipped.append(wid)
                continue
            self.tab_wavs[tab].append(wid)
            owner[wid] = tab
            added.append(wid)
        return added, skipped

    def remove(
        self,
        tab: TabId,
        wav_ids: Iterable[str],
        *,
        chart: Optional[BmsChart] = None,
    ) -> List[str]:
        """Remove ids (and their same-physical-file siblings) from ``tab``; resets default bind if removed."""
        active_chart = chart if chart is not None else self.chart
        wanted = set(expand_wav_ids(active_chart, wav_ids))
        removed: List[str] = []
        kept: List[str] = []
        for wid in self.tab_wavs[tab]:
            if wid in wanted:
                removed.append(wid)
            else:
                kept.append(wid)
        self.tab_wavs[tab] = kept
        if self.default_binds.get(tab) in removed:
            del self.default_binds[tab]
        if tab == TabId.LP and self.auto_lp.chip_mode in removed:
            self.auto_lp.chip_mode = AUTO_LP_CHIP_NONE
        if not kept:
            self.explicit_unbinds.discard(tab)
        return removed

    def auto_bind_for_tab(
        self, tab: TabId, chart: Optional[BmsChart] = None
    ) -> Optional[str]:
        """Return the highest-frequency representative WAV ID currently in ``tab``."""
        if tab not in DRUM_TABS:
            return None
        active_chart = chart if chart is not None else self.chart
        display_ids = self.display_ids_for_tab(tab, active_chart)
        if not display_ids:
            return None
        return resolve_auto_bind_wav_id(active_chart, display_ids)

    def effective_bind(
        self, tab: TabId, chart: Optional[BmsChart] = None
    ) -> Tuple[Optional[str], bool]:
        """Return ``(effective_wav_id, is_auto_selected)`` for ``tab``."""
        if tab not in DRUM_TABS or not self.tab_wavs[tab]:
            return None, False
        manual = self.default_binds.get(tab)
        if manual and manual in self.tab_wavs[tab]:
            return manual, False
        if tab in self.explicit_unbinds:
            return None, False
        if self.default_bind_mode == "auto":
            return self.auto_bind_for_tab(tab, chart), True
        return None, False

    def set_bind(self, tab: TabId, wav_id: Optional[str]) -> None:
        if tab not in DRUM_TABS:
            raise ValueError(f"Tab {tab.value} does not support default binding")
        if wav_id == AUTO_BIND_SENTINEL:
            self.default_binds.pop(tab, None)
            self.explicit_unbinds.discard(tab)
            return
        if not wav_id or wav_id == "00":
            self.default_binds.pop(tab, None)
            if self.default_bind_mode == "auto":
                self.explicit_unbinds.add(tab)
            else:
                self.explicit_unbinds.discard(tab)
            return
        if wav_id not in self.tab_wavs[tab]:
            raise ValueError(f"{wav_id} is not in tab {tab.value}")
        self.default_binds[tab] = wav_id
        self.explicit_unbinds.discard(tab)

    def get_bind(self, tab: TabId) -> Optional[str]:
        return self.default_binds.get(tab)

    def to_mapping_config(self) -> MappingConfig:
        """Build a v1/v1.2 ``MappingConfig`` (Toggle = OFF only)."""
        tab_wavs = {
            t: list(ids)
            for t, ids in self.tab_wavs.items()
            if ids and (self.separate_lbd or t != TabId.LBD)
        }
        # Extra guard: if residual LBD items somehow exist when LBD is hidden, fold into BD
        if not self.separate_lbd and self.tab_wavs.get(TabId.LBD):
            bd_list = tab_wavs.setdefault(TabId.BD, [])
            bd_seen = set(bd_list)
            for wid in self.tab_wavs[TabId.LBD]:
                if wid not in bd_seen:
                    bd_list.append(wid)
                    bd_seen.add(wid)

        default_binds = {
            t: w
            for t, w in self.default_binds.items()
            if w and (self.separate_lbd or t != TabId.LBD)
        }
        if not self.separate_lbd and TabId.LBD in self.default_binds and TabId.BD not in default_binds:
            lbd_bind = self.default_binds[TabId.LBD]
            if lbd_bind:
                default_binds[TabId.BD] = lbd_bind

        if self.default_bind_mode == "auto":
            for tab in self.explicit_unbinds:
                if (
                    tab in DRUM_TABS
                    and (self.separate_lbd or tab != TabId.LBD)
                    and tab not in default_binds
                    and tab in tab_wavs
                ):
                    default_binds[tab] = "00"

        return MappingConfig(
            tab_wavs=tab_wavs,
            default_lane_binds=default_binds,
            toggle_on=False,
            separate_lbd=self.separate_lbd,
            default_bind_mode=self.default_bind_mode,
            auto_lp=self._copy_auto_lp(self.auto_lp),
        )

    # ----- JSON (matches cli.py --map-json & __bms2dtx.json) -----
    def to_json_dict(self) -> Dict[str, Any]:
        return build_project_mapping_dict(
            self.to_mapping_config(),
            swap_lr=self.swap_lr,
            tab_wavs_override=self.tab_wavs,
            default_binds_override=self.default_binds,
        )

    def load_json_dict(
        self, data: Dict[str, Any], chart: Optional[BmsChart]
    ) -> List[str]:
        """Replace assignments from a mapping dict. Returns human-readable notes/warnings."""
        notes: List[str] = []
        active_chart = chart if chart is not None else self.chart
        new_model = AssignmentModel(
            chart=active_chart,
            default_bind_mode=self.default_bind_mode,
        )
        new_model.separate_lbd = bool(data.get("separate_lbd", self.separate_lbd))
        valid = set(active_chart.wavs.keys()) if active_chart is not None else None
        for raw_tab, raw_ids in dict(data.get("tab_wavs", {})).items():
            try:
                tab = TabId(str(raw_tab).strip().upper().replace(" ", "_"))
            except ValueError:
                notes.append(tr("map_note_unknown_tab", tab=raw_tab))
                continue
            for raw_id in raw_ids:
                wid = normalize_wav_id(active_chart, str(raw_id))
                if valid is not None and wid not in valid:
                    notes.append(tr("map_note_undeclared_slice", wav_id=raw_id))
                    continue
                if wid in new_model.tab_wavs[tab]:
                    # Already expanded via a same-physical-file sibling in the same tab
                    if list(raw_ids).count(raw_id) > 1:
                        notes.append(tr("map_note_duplicate_slice", wav_id=raw_id))
                    continue
                _added, skipped = new_model.assign(tab, [wid], chart=active_chart)
                if skipped:
                    notes.append(tr("map_note_duplicate_slice", wav_id=raw_id))
        for raw_tab, raw_wid in dict(data.get("default_lane_binds", {})).items():
            try:
                tab = TabId(str(raw_tab).strip().upper().replace(" ", "_"))
            except ValueError:
                notes.append(tr("map_note_unknown_bind_tab", tab=raw_tab))
                continue
            wid = normalize_wav_id(active_chart, str(raw_wid))
            try:
                new_model.set_bind(tab, wid)
            except ValueError:
                notes.append(tr("map_note_invalid_bind", tab=raw_tab, wav_id=raw_wid))
        if new_model.tab_wavs[TabId.LBD] or TabId.LBD in new_model.default_binds:
            new_model.separate_lbd = True
        parsed_auto_lp = parse_auto_lp_config(data.get("auto_lp"))
        cm_lower = parsed_auto_lp.chip_mode.lower()
        if cm_lower not in (AUTO_LP_CHIP_NONE, AUTO_LP_CHIP_AUTO, AUTO_LP_CHIP_EMPTY_ZZ):
            norm_lp_wid = normalize_wav_id(active_chart, parsed_auto_lp.chip_mode)
            if norm_lp_wid in new_model.tab_wavs[TabId.LP]:
                parsed_auto_lp.chip_mode = norm_lp_wid
            else:
                parsed_auto_lp.chip_mode = AUTO_LP_CHIP_NONE
        new_model.auto_lp = parsed_auto_lp
        new_model.swap_lr = bool(data.get("swap_lr", self.swap_lr))
        if data.get("toggle_on"):
            notes.append(tr("map_note_toggle_on_ignored"))
        self.tab_wavs = new_model.tab_wavs
        self.default_binds = new_model.default_binds
        self.explicit_unbinds = new_model.explicit_unbinds
        self.separate_lbd = new_model.separate_lbd
        self.auto_lp = new_model.auto_lp
        self.swap_lr = new_model.swap_lr
        return notes


def normalize_wav_id(chart: Optional[BmsChart], wav_id: str) -> str:
    cleaned = wav_id.strip()
    if chart is not None and chart.is_base62:
        return cleaned
    return cleaned.upper()


def save_mapping_json(model: AssignmentModel, path: Path | str) -> None:
    Path(path).write_text(
        json.dumps(model.to_json_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_mapping_json(
    model: AssignmentModel, path: Path | str, chart: Optional[BmsChart]
) -> List[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Mapping JSON must be an object")
    return model.load_json_dict(data, chart)


# ------------------------------------------------------------------- source list
def filter_wav_ids(
    chart: BmsChart, query: str = "", only_used: bool = False
) -> List[str]:
    """Return representative WAV IDs (folding identical physical files into 1 row), filtered by search/usage."""
    q = query.strip().lower()
    rep_to_group, _wid_to_rep = wav_physical_groups(chart)
    result: List[str] = []
    for rep_id, group_ids in rep_to_group.items():
        total_use = sum(chart.wavs[w].use_count for w in group_ids)
        if only_used and total_use <= 0:
            continue
        if q:
            haystack = " ".join(
                f"{w} {chart.wavs[w].resolved_name} {chart.wavs[w].declared_name}"
                for w in group_ids
            ).lower()
            if q not in haystack:
                continue
        result.append(rep_id)
    return result


def format_wav_tree_values(
    entry: WavEntry,
    assigned_tab: Optional[TabId] = None,
    *,
    chart: Optional[BmsChart] = None,
    group_ids: Optional[Sequence[str]] = None,
    total_use_count: Optional[int] = None,
) -> Tuple[str, str, str, str]:
    """Return 4 column strings `(ID, Filename, Count, Target)` for left ttk.Treeview."""
    if group_ids is None and chart is not None:
        group_ids = wav_group_for(chart, entry.wav_id)
    ids_label = ", ".join(group_ids) if group_ids else entry.wav_id
    if total_use_count is not None:
        use_count = total_use_count
    elif chart is not None and group_ids:
        use_count = sum(chart.wavs[w].use_count for w in group_ids if w in chart.wavs)
    else:
        use_count = entry.use_count

    fname = entry.resolved_name
    if not entry.exists:
        fname = f"{fname} {tr('tag_missing')}"
    count_str = f"×{use_count}"
    target_str = tab_label(assigned_tab) if assigned_tab is not None else ""
    return (ids_label, fname, count_str, target_str)


def format_wav_row(
    entry: WavEntry,
    assigned_tab: Optional[TabId] = None,
    *,
    chart: Optional[BmsChart] = None,
    group_ids: Optional[Sequence[str]] = None,
    total_use_count: Optional[int] = None,
) -> str:
    """``[WAV_ID(s)] resolved_name (×use_count)`` + missing/assignment tags (no redundant [已修正])."""
    if group_ids is None and chart is not None:
        group_ids = wav_group_for(chart, entry.wav_id)
    ids_label = ", ".join(group_ids) if group_ids else entry.wav_id
    if total_use_count is not None:
        use_count = total_use_count
    elif chart is not None and group_ids:
        use_count = sum(chart.wavs[w].use_count for w in group_ids if w in chart.wavs)
    else:
        use_count = entry.use_count

    text = f"[{ids_label}] {entry.resolved_name} (×{use_count})"
    if not entry.exists:
        text += f" {tr('tag_missing')}"
    if assigned_tab is not None:
        text += f" → {tab_label(assigned_tab)}"
    return text


def format_tab_row(
    entry: Optional[WavEntry],
    wav_id: str,
    *,
    chart: Optional[BmsChart] = None,
    group_ids: Optional[Sequence[str]] = None,
) -> str:
    if group_ids is None and chart is not None:
        group_ids = wav_group_for(chart, wav_id)
    ids_label = ", ".join(group_ids) if group_ids else wav_id
    if entry is None:
        return f"[{ids_label}] {tr('tag_undeclared')}"
    text = f"[{ids_label}] {entry.resolved_name}"
    if not entry.exists:
        text += f" {tr('tag_missing')}"
    return text


def auto_lp_combobox_values(
    model: AssignmentModel, chart: Optional[BmsChart]
) -> List[Tuple[str, str]]:
    """``[(display_text, chip_mode_value)]`` for LP Tab's Auto-LP selector."""
    active_chart = chart if chart is not None else model.chart
    values: List[Tuple[str, str]] = [
        (tr("auto_lp_chip_none"), AUTO_LP_CHIP_NONE),
        (tr("auto_lp_chip_auto"), AUTO_LP_CHIP_AUTO),
        (tr("auto_lp_chip_empty_zz"), AUTO_LP_CHIP_EMPTY_ZZ),
    ]
    for wid in model.display_ids_for_tab(TabId.LP, active_chart):
        entry = active_chart.wavs.get(wid) if active_chart is not None else None
        values.append((format_tab_row(entry, wid, chart=active_chart), wid))
    return values


def suggest_load_mapping_location(
    chart: Optional[BmsChart],
    last_output_dir: Optional[Path],
    out_text: str,
) -> Tuple[Optional[Path], Optional[str]]:
    """Pick default directory (and __bms2dtx.json if present) for Load Mapping dialog."""
    candidates: List[Path] = []
    if last_output_dir is not None:
        candidates.append(last_output_dir)
    if out_text.strip():
        candidates.append(Path(out_text.strip()))
    if chart is not None:
        candidates.append(chart.source_dir)
    for d in candidates:
        if d.is_dir():
            proj = d / PROJECT_MAPPING_FILENAME
            return d, (PROJECT_MAPPING_FILENAME if proj.is_file() else None)
    return None, None


def bind_combobox_values(
    model: AssignmentModel, chart: Optional[BmsChart], tab: TabId
) -> List[Tuple[str, Optional[str]]]:
    """``[(display_text, token_or_None)]`` for a drum Tab's default-bind Combobox."""
    active_chart = chart if chart is not None else model.chart
    values: List[Tuple[str, Optional[str]]] = []
    if model.default_bind_mode == "auto":
        auto_wid = model.auto_bind_for_tab(tab, active_chart)
        if auto_wid is not None:
            auto_entry = active_chart.wavs.get(auto_wid) if active_chart is not None else None
            auto_text = f"{tr('auto_bind_prefix')}{format_tab_row(auto_entry, auto_wid, chart=active_chart)}"
            values.append((auto_text, AUTO_BIND_SENTINEL))
    values.append((get_no_bind_label(), None))
    for wid in model.display_ids_for_tab(tab, active_chart):
        entry = active_chart.wavs.get(wid) if active_chart is not None else None
        values.append((format_tab_row(entry, wid, chart=active_chart), wid))
    return values


# --------------------------------------------------------------------- summaries
def radix_text(chart: BmsChart) -> str:
    base = tr("radix_base62_ext") if chart.is_base62 else tr("radix_base36_ok")
    text = base
    if chart.is_base62:
        text += tr("radix_warn_base62")
    if chart.exceeds_dtx_wav_limit:
        text += tr("radix_warn_limit")
    return text


def radix_needs_notice(chart: BmsChart) -> bool:
    return chart.is_base62 or chart.exceeds_dtx_wav_limit


def build_health_summary(chart: BmsChart) -> List[Tuple[str, str]]:
    """Label/value rows for the import health-check panel."""
    total = len(chart.wavs)
    corrected = sum(1 for w in chart.wavs.values() if w.suffix_corrected)
    missing = sum(1 for w in chart.wavs.values() if not w.exists)
    pruned = chart.pruned_empty_tracks
    pruned_text = f"{len(pruned)}" + (
        f" ({', '.join(pruned[:12])}{'…' if len(pruned) > 12 else ''})" if pruned else ""
    )
    return [
        (tr("health_title"), chart.full_title or "-"),
        (tr("health_artist"), chart.artist or "-"),
        (tr("health_bpm"), f"{chart.initial_bpm:g}"),
        (tr("health_encoding"), chart.encoding_used),
        (tr("health_radix"), radix_text(chart)),
        (
            tr("health_wav_count"),
            tr("health_wav_value", total=total, corrected=corrected, missing=missing),
        ),
        (
            tr("health_bgm_cols"),
            tr("health_bgm_value", count=len(chart.bgm_tracks), pruned=pruned_text),
        ),
        (tr("health_warnings"), tr("health_warnings_value", count=len(chart.warnings))),
    ]


def format_warnings(warnings: Sequence[Any]) -> str:
    if not warnings:
        return tr("no_warnings_text")
    return "\n".join(f"[{w.code}] {w.message}" for w in warnings)


def build_result_summary(result: ConversionResult) -> str:
    lines = [
        tr("result_dtx_line", path=result.dtx_path),
        tr("result_encoding_line", encoding=result.dtx_encoding),
    ]
    if result.bgm_path is not None and result.mix_stats is not None:
        ms = result.mix_stats
        lines.append(
            tr(
                "result_bgm_line",
                path=result.bgm_path,
                duration=ms.duration_sec,
                events=ms.mixed_event_count,
                peak=ms.peak_after_norm,
                rms=ms.rms,
                missing=ms.missing_event_count,
            )
        )
    else:
        lines.append(tr("result_bgm_none"))
    if result.preview_path is not None:
        lines.append(tr("result_preview_line", path=result.preview_path))
    lines.append(tr("result_placed_notes", count=result.placed_note_count))
    if result.auto_lp_placed_count > 0:
        lines.append(tr("result_auto_lp_line", count=result.auto_lp_placed_count))
    if result.project_json_path is not None:
        lines.append(tr("result_project_json_line", path=result.project_json_path))
    lines.append(
        tr(
            "result_copied_transcoded",
            copied=len(result.copied_files),
            transcoded=len(result.transcoded_files),
        )
    )
    if result.lane_bind_lines:
        lines.append(tr("result_lane_binds", count=len(result.lane_bind_lines)))
    if result.toggle_off_forced:
        lines.append(tr("result_toggle_off_forced"))
    lines.append("")
    lines.append(tr("result_warnings_header", count=len(result.warnings)))
    lines.append(format_warnings(result.warnings))
    return "\n".join(lines)


# ------------------------------------------------------------------ export helpers
def suggest_output_dir(bms_path: Path | str) -> Path:
    """``<bms_dir>_dtx`` beside the BMS folder (never inside it)."""
    src_dir = Path(bms_path).resolve().parent
    return src_dir.with_name(src_dir.name + "_dtx")


def check_output_dir(out_dir: Path | str, chart: BmsChart) -> Optional[str]:
    """Return an error message if ``out_dir`` would violate Rule 1, else ``None``."""
    out = Path(out_dir).resolve()
    src_dir = chart.source_dir.resolve()
    if out == src_dir:
        return tr("err_out_same_as_src")
    audio_dirs = {
        e.resolved_path.resolve().parent
        for e in chart.wavs.values()
        if e.exists and e.resolved_path is not None
    }
    if chart.preview is not None and chart.preview.exists and chart.preview.resolved_path is not None:
        audio_dirs.add(chart.preview.resolved_path.resolve().parent)
    if out in audio_dirs:
        return tr("err_out_in_audio_subdir")
    return None


def parse_dlevel(text: str) -> Optional[int]:
    """Empty → ``None``; otherwise a non-negative integer (raises ``ValueError``)."""
    cleaned = text.strip()
    if not cleaned:
        return None
    value = int(cleaned)
    if value < 0:
        raise ValueError("DLEVEL must be >= 0")
    return value


def default_dlevel_text(chart: BmsChart) -> str:
    return str(chart.playlevel) if chart.playlevel is not None else "50"
