"""15-Tab mapping, Base-36 WAV renumbering, and #DTXC_LANEBINDEDCHIP generation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .constants import (
    AUTO_LP_CHIP_AUTO,
    AUTO_LP_CHIP_EMPTY_ZZ,
    AUTO_LP_CHIP_NONE,
    AUTO_LP_RESONANT_WAV_ID,
    AUTO_LP_SILENT_WAV_ID,
    AUTO_LP_THRESHOLD_MS,
    AUTO_LP_VALID_DENOMINATORS,
    DRUM_TABS,
    DTX_MAX_MAPPED_SLOTS_TOGGLE_OFF,
    DTX_MAX_WAV_SLOTS,
    DTXC_BIND_LANES,
    MAX_SLICER_COLUMNS,
    TAB_ORDER,
    TAB_SPEC_MAP,
    TOM_OVERFLOW_SE_CHANNEL,
    TOM_TABS,
    TabId,
    int_to_base36,
)
from .models import (
    BmsChart,
    ConversionWarning,
    HiHatConflictRemoval,
    MappingConfig,
    MergedSliceOccurrence,
    MergedSliceSpec,
    NoteEvent,
    PlacedDtxNote,
    TomOverflowRecord,
)


class WavLimitExceededError(ValueError):
    """Raised when retained slices exceed DTX Base-36 capacity."""


@dataclass
class MappingPlan:
    """Validated mapping plan ready for mixing / SE allocation / DTX writing."""

    effective_toggle_on: bool
    toggle_off_forced: bool
    normalized_tab_wavs: Dict[TabId, List[str]]
    wav_to_tab: Dict[str, TabId]
    wav_id_map: Dict[str, str]  # original BMS wav_id -> renumbered DTX base-36 id
    ordered_retained_orig_ids: List[str]  # original BMS wav_ids in renumbered order
    tab_placed_notes: List[PlacedDtxNote]
    unmapped_events: List[NoteEvent]
    lane_bind_lines: List[str]
    guitar_enabled: bool
    bass_enabled: bool
    tab_collision_events: List[NoteEvent] = field(default_factory=list)
    warnings: List[ConversionWarning] = field(default_factory=list)
    dtx_wav_labels: Dict[str, str] = field(default_factory=dict)
    wav_volumes: Dict[str, int] = field(default_factory=dict)
    auto_lp_orig_wav_id: Optional[str] = None
    auto_lp_use_silent_zz: bool = False
    auto_lp_use_zy: bool = False
    auto_lp_placed_count: int = 0
    hihat_conflict_removals: List[HiHatConflictRemoval] = field(default_factory=list)
    tom_overflow_records: List[TomOverflowRecord] = field(default_factory=list)
    merged_slices: List[MergedSliceSpec] = field(default_factory=list)


def resolve_auto_bind_wav_id(
    chart: Optional[BmsChart], wav_ids: Sequence[str]
) -> Optional[str]:
    """Pick the highest-frequency WAV ID among wav_ids (ties broken by list order)."""
    cleaned = [w for w in wav_ids if w]
    if not cleaned:
        return None
    if chart is None:
        return cleaned[0]

    phys_groups: Dict[str, List[str]] = defaultdict(list)
    wid_to_phys: Dict[str, str] = {}
    for wid, entry in chart.wavs.items():
        if entry.exists and entry.resolved_path is not None:
            phys = os.path.normcase(os.path.abspath(str(entry.resolved_path)))
            phys_groups[phys].append(wid)
            wid_to_phys[wid] = phys

    group_order: List[str] = []
    group_rep: Dict[str, str] = {}
    group_score: Dict[str, int] = {}

    for wid in cleaned:
        phys = wid_to_phys.get(wid)
        gkey = f"P:{phys}" if phys is not None else f"M:{wid}"
        if gkey in group_rep:
            continue
        group_order.append(gkey)
        group_rep[gkey] = wid
        score = 0
        members = phys_groups[phys] if phys is not None else [wid]
        seen_members: Set[str] = set()
        for mwid in members:
            if mwid in seen_members:
                continue
            seen_members.add(mwid)
            entry = chart.wavs.get(mwid)
            if entry is not None:
                score += entry.use_count
            else:
                score += sum(1 for ev in chart.events if ev.wav_id == mwid)
        group_score[gkey] = score

    if not group_order:
        return None
    best_key = max(group_order, key=lambda gk: group_score[gk])
    return group_rep[best_key]


def _norm_wav_id(chart: BmsChart, wid: str) -> str:
    cleaned = wid.strip()
    return cleaned if chart.is_base62 else cleaned.upper()


def _resolve_tab_key(raw_key: TabId | str) -> TabId:
    if isinstance(raw_key, TabId):
        return raw_key
    key_up = str(raw_key).strip().upper().replace(" ", "_")
    return TabId(key_up)


def _resolve_swapped_wav_set(
    chart: BmsChart, swapped_wav_ids: Sequence[str]
) -> Set[str]:
    """Normalize per-slice L/R overrides and propagate across shared physical files."""
    if not swapped_wav_ids:
        return set()
    norm_ids: Set[str] = set()
    for raw_wid in swapped_wav_ids:
        wid = _norm_wav_id(chart, str(raw_wid))
        if wid:
            norm_ids.add(wid)
    if not norm_ids:
        return set()
    swapped_paths: Set[str] = set()
    for wid in list(norm_ids):
        entry = chart.wavs.get(wid)
        if entry is not None and entry.exists and entry.resolved_path is not None:
            swapped_paths.add(os.path.normcase(os.path.abspath(str(entry.resolved_path))))
    if swapped_paths:
        for wid, entry in chart.wavs.items():
            if entry.exists and entry.resolved_path is not None:
                phys = os.path.normcase(os.path.abspath(str(entry.resolved_path)))
                if phys in swapped_paths:
                    norm_ids.add(wid)
    return norm_ids


def build_mapping_plan(
    chart: BmsChart,
    config: MappingConfig,
    *,
    auto_merge_duplicates: Optional[bool] = None,
    global_swap_lr: bool = False,
) -> MappingPlan:
    """Validate 15-Tab mapping, enforce Rule 5 Toggle-OFF lock, renumber WAVs, and place Tab notes."""
    warnings: List[ConversionWarning] = []

    phys_groups: Dict[str, List[str]] = defaultdict(list)
    wid_to_phys: Dict[str, str] = {}
    for wid, entry in chart.wavs.items():
        if entry.exists and entry.resolved_path is not None:
            phys = os.path.normcase(os.path.abspath(str(entry.resolved_path)))
            phys_groups[phys].append(wid)
            wid_to_phys[wid] = phys

    # 1. Normalize and validate Tab 1..15 assignments (no duplicate WAV across tabs)
    normalized_tab_wavs: Dict[TabId, List[str]] = {tab: [] for tab in TAB_ORDER}
    wav_to_tab: Dict[str, TabId] = {}

    for raw_tab, raw_ids in config.tab_wavs.items():
        tab = _resolve_tab_key(raw_tab)
        seen_in_tab: Set[str] = set()
        for raw_id in raw_ids:
            wid = _norm_wav_id(chart, raw_id)
            if not wid or wid == "00":
                continue
            if wid in seen_in_tab:
                continue
            if wid in wav_to_tab:
                raise ValueError(
                    f"WAV {wid!r} is assigned to both {wav_to_tab[wid].value} and {tab.value}."
                )
            seen_in_tab.add(wid)
            wav_to_tab[wid] = tab
            normalized_tab_wavs[tab].append(wid)

    # When LBD is hidden (separate_lbd=False), fold any residual LBD assignments deterministically into BD
    if not config.separate_lbd and normalized_tab_wavs[TabId.LBD]:
        existing_bd = set(normalized_tab_wavs[TabId.BD])
        for wid in normalized_tab_wavs[TabId.LBD]:
            wav_to_tab[wid] = TabId.BD
            if wid not in existing_bd:
                normalized_tab_wavs[TabId.BD].append(wid)
                existing_bd.add(wid)
        normalized_tab_wavs[TabId.LBD] = []

    def _merge_unassigned_siblings() -> None:
        for tab in TAB_ORDER:
            expanded: List[str] = []
            seen_in_tab = set(normalized_tab_wavs[tab])
            for wid in list(normalized_tab_wavs[tab]):
                expanded.append(wid)
                phys = wid_to_phys.get(wid)
                if phys is not None:
                    for sibling in phys_groups.get(phys, ()):
                        if sibling not in wav_to_tab and sibling not in seen_in_tab:
                            seen_in_tab.add(sibling)
                            wav_to_tab[sibling] = tab
                            expanded.append(sibling)
            normalized_tab_wavs[tab] = expanded

    def _collect_mapped_and_partition() -> Tuple[List[str], Dict[TabId, List[NoteEvent]], List[NoteEvent], List[str]]:
        ordered: List[str] = []
        for tab in TAB_ORDER:
            ordered.extend(normalized_tab_wavs[tab])
        by_tab: Dict[TabId, List[NoteEvent]] = {tab: [] for tab in TAB_ORDER}
        unmapped: List[NoteEvent] = []
        for ev in chart.events:
            tab = wav_to_tab.get(ev.wav_id)
            if tab is not None:
                by_tab[tab].append(ev)
            else:
                unmapped.append(ev)
        decl_unmapped: List[str] = []
        seen_unm: Set[str] = set()
        for ev in unmapped:
            if ev.wav_id in chart.wavs and ev.wav_id not in seen_unm:
                seen_unm.add(ev.wav_id)
                decl_unmapped.append(ev.wav_id)
        return ordered, by_tab, unmapped, decl_unmapped

    # Auto-include unassigned sibling #WAVxx IDs pointing to the exact same physical file on disk
    merged_siblings = False
    if auto_merge_duplicates is True or (auto_merge_duplicates is None and not config.toggle_on):
        _merge_unassigned_siblings()
        merged_siblings = True

    mapped_ordered, mapped_events_by_tab, unmapped_events, declared_unmapped_ids = (
        _collect_mapped_and_partition()
    )

    # 2. Enforce AGENTS.md Rule 5: Toggle-OFF lock when slices exceed DTX 36-base limit (1295)
    total_toggle_on_slices = len(mapped_ordered) + len(declared_unmapped_ids)
    toggle_off_forced = False
    effective_toggle_on = config.toggle_on

    if chart.toggle_off_locked or total_toggle_on_slices > DTX_MAX_WAV_SLOTS:
        toggle_off_forced = True
        if config.toggle_on:
            warnings.append(
                ConversionWarning(
                    code="TOGGLE_OFF_FORCED",
                    message=(
                        f"Requested Toggle=ON, but slice count exceeds DTX Base-36 limit "
                        f"({DTX_MAX_WAV_SLOTS}). Forced Toggle=OFF (offline bgm.ogg mix)."
                    ),
                )
            )
        effective_toggle_on = False

    # If Toggle=ON was forced to Toggle=OFF by Rule 5 and siblings were not yet merged, merge them now
    if not effective_toggle_on and not merged_siblings and auto_merge_duplicates is not False:
        _merge_unassigned_siblings()
        merged_siblings = True
        mapped_ordered, mapped_events_by_tab, unmapped_events, declared_unmapped_ids = (
            _collect_mapped_and_partition()
        )

    mapped_undefined = [wid for wid in mapped_ordered if wid not in chart.wavs]
    if mapped_undefined:
        sample = ", ".join(mapped_undefined[:8])
        more = f" (+{len(mapped_undefined) - 8} more)" if len(mapped_undefined) > 8 else ""
        warnings.append(
            ConversionWarning(
                code="MAPPED_UNDEFINED_WAV",
                message=(
                    f"{len(mapped_undefined)} WAV ID(s) mapped into Tabs 1-15 have no "
                    f"#WAVxx declaration in the BMS chart ({sample}{more})."
                ),
            )
        )

    if effective_toggle_on:
        filtered_unmapped: List[NoteEvent] = []
        skipped_undefined = 0
        for ev in unmapped_events:
            if ev.wav_id not in chart.wavs:
                skipped_undefined += 1
                continue
            filtered_unmapped.append(ev)
        unmapped_events = filtered_unmapped
        if skipped_undefined > 0:
            warnings.append(
                ConversionWarning(
                    code="UNDEFINED_WAV_SKIPPED_TOGGLE_ON",
                    message=(
                        f"Skipped {skipped_undefined} unmapped event(s) referencing "
                        f"undeclared #WAVxx IDs in Toggle=ON mode."
                    ),
                )
            )

    unmapped_referenced_ids = declared_unmapped_ids

    # 3. Compute exact WAV renumbering order (AGENTS.md Section 4.4)
    # Toggle OFF: 01 = bgm.ogg, then LC -> HH Close -> ... -> Bass (02..ZZ)
    # Toggle ON : LC -> ... -> Bass -> SE1..SE5 (Slicer cols) -> remaining slices (01..ZZ)
    candidate_retained_ids: List[str] = list(mapped_ordered)
    seen_retained: Set[str] = set(candidate_retained_ids)

    if effective_toggle_on:
        # Validate Slicer columns: at most 5 distinct positive 1-based BGM columns
        slicer_col_order: List[int] = []
        seen_slicer: Set[int] = set()
        for col in config.slicer_bgm_columns:
            if col < 1:
                raise ValueError(f"Invalid BGM column index for Slicer: {col}")
            if col not in seen_slicer:
                seen_slicer.add(col)
                slicer_col_order.append(col)
        if len(slicer_col_order) > MAX_SLICER_COLUMNS:
            raise ValueError(
                f"Slicer supports at most {MAX_SLICER_COLUMNS} BGM columns (SE1-SE5), "
                f"got {len(slicer_col_order)}."
            )
        # First add WAVs appearing in Slicer BGM columns (SE1..SE5 in user-selected order)
        for col in slicer_col_order:
            for ev in unmapped_events:
                if ev.channel == "01" and ev.bgm_column == col:
                    if ev.wav_id not in seen_retained:
                        seen_retained.add(ev.wav_id)
                        candidate_retained_ids.append(ev.wav_id)
        # Then add all remaining unmapped slices
        for wid in unmapped_referenced_ids:
            if wid not in seen_retained:
                seen_retained.add(wid)
                candidate_retained_ids.append(wid)

    # Deduplicate retained WAV slots by physical file path so identical physical files share 1 DTX slot
    ordered_retained_orig_ids: List[str] = []
    orig_to_slot_idx: Dict[str, int] = {}
    phys_to_slot_idx: Dict[str, int] = {}
    for orig_wid in candidate_retained_ids:
        phys = wid_to_phys.get(orig_wid)
        if phys is not None and phys in phys_to_slot_idx:
            orig_to_slot_idx[orig_wid] = phys_to_slot_idx[phys]
            continue
        slot_idx = len(ordered_retained_orig_ids)
        ordered_retained_orig_ids.append(orig_wid)
        orig_to_slot_idx[orig_wid] = slot_idx
        if phys is not None:
            phys_to_slot_idx[phys] = slot_idx
            for sibling_wid in phys_groups.get(phys, ()):
                orig_to_slot_idx.setdefault(sibling_wid, slot_idx)

    # Validate capacity against deduplicated DTX WAV slots
    if not effective_toggle_on:
        if len(ordered_retained_orig_ids) > DTX_MAX_MAPPED_SLOTS_TOGGLE_OFF:
            raise WavLimitExceededError(
                f"Mapped slice count ({len(ordered_retained_orig_ids)}) exceeds Toggle=OFF maximum "
                f"of {DTX_MAX_MAPPED_SLOTS_TOGGLE_OFF} (02-ZZ; 01 is reserved for bgm.ogg)."
            )
    else:
        if len(ordered_retained_orig_ids) > DTX_MAX_WAV_SLOTS:
            raise WavLimitExceededError(
                f"Retained slice count ({len(ordered_retained_orig_ids)}) exceeds DTX Base-36 maximum "
                f"of {DTX_MAX_WAV_SLOTS} (01-ZZ)."
            )

    start_slot = 2 if not effective_toggle_on else 1
    wav_id_map: Dict[str, str] = {
        wid: int_to_base36(start_slot + idx) for wid, idx in orig_to_slot_idx.items()
    }

    # 4. Generate #DTXC_LANEBINDEDCHIP lines (only for drum tabs 1..12)
    normalized_binds: Dict[TabId, str] = {}
    bound_orig_by_tab: Dict[TabId, str] = {}
    explicit_unbind_tabs: Set[TabId] = set()
    auto_bind_tabs: Set[TabId] = set()
    raw_bind_tabs: Set[TabId] = {_resolve_tab_key(rt) for rt in config.default_lane_binds}
    for raw_tab, raw_wid in config.default_lane_binds.items():
        tab = _resolve_tab_key(raw_tab)
        if not config.separate_lbd and tab == TabId.LBD:
            cleaned_lbd = str(raw_wid).strip()
            if (
                not cleaned_lbd
                or cleaned_lbd == "00"
                or cleaned_lbd.lower() in ("none", "auto")
                or TabId.BD in raw_bind_tabs
            ):
                continue
            tab = TabId.BD
        if tab not in DRUM_TABS:
            raise ValueError(
                f"Tab {tab.value} does not support #DTXC_LANEBINDEDCHIP default binding."
            )
        cleaned_raw = str(raw_wid).strip()
        if not cleaned_raw or cleaned_raw == "00" or cleaned_raw.lower() == "none":
            explicit_unbind_tabs.add(tab)
            continue
        if cleaned_raw.lower() == "auto":
            auto_bind_tabs.add(tab)
            continue
        wid = _norm_wav_id(chart, cleaned_raw)
        if wid not in wav_id_map:
            raise ValueError(
                f"Default bound WAV {wid!r} for tab {tab.value} is not mapped into any retained WAV."
            )
        normalized_binds[tab] = wav_id_map[wid]
        bound_orig_by_tab[tab] = wid

    global_auto_bind = str(config.default_bind_mode).strip().lower() == "auto"
    for tab in DRUM_TABS:
        if tab in normalized_binds or tab in explicit_unbind_tabs:
            continue
        if not (global_auto_bind or tab in auto_bind_tabs):
            continue
        tab_wids = normalized_tab_wavs[tab]
        if not tab_wids:
            continue
        best_orig = resolve_auto_bind_wav_id(chart, tab_wids)
        if best_orig is not None and best_orig in wav_id_map:
            normalized_binds[tab] = wav_id_map[best_orig]
            bound_orig_by_tab[tab] = best_orig

    lane_bind_lines: List[str] = []
    for lane_code, main_tab, sub_tab in DTXC_BIND_LANES:
        main_chip = normalized_binds.get(main_tab, "00")
        sub_chip = normalized_binds.get(sub_tab, "00") if sub_tab is not None else "00"
        if (
            lane_code == "03"
            and sub_chip == "00"
            and not config.separate_lbd
            and TabId.BD in normalized_binds
        ):
            sub_chip = normalized_binds[TabId.BD]
        if main_chip != "00" or sub_chip != "00":
            lane_bind_lines.append(f"#DTXC_LANEBINDEDCHIP: {lane_code} {main_chip} {sub_chip}")

    guitar_enabled = len(normalized_tab_wavs[TabId.GUITAR]) > 0
    bass_enabled = len(normalized_tab_wavs[TabId.BASS]) > 0

    dtx_wav_labels: Dict[str, str] = {}
    if not effective_toggle_on:
        dtx_wav_labels["01"] = "BGM"
    for orig_wid in ordered_retained_orig_ids:
        dtx_wid = wav_id_map[orig_wid]
        tab = wav_to_tab.get(orig_wid)
        if tab is not None:
            dtx_wav_labels[dtx_wid] = tab.value

    slot_to_canonical_orig: Dict[str, str] = {
        wav_id_map[wid]: wid for wid in ordered_retained_orig_ids
    }
    swapped_set = _resolve_swapped_wav_set(chart, config.swapped_wav_ids)

    # 5a. Cross-lane Hi-Hat physical conflict guard: HH Open (18) owns the tick and removes
    # simultaneous HH Close (11) or source LP (1B) notes cleanly (NEVER reflow to bgm.ogg / SE).
    hihat_conflict_removals: List[HiHatConflictRemoval] = []
    hh_open_ticks: Set[Tuple[int, Fraction]] = {
        (ev.measure, ev.pos) for ev in mapped_events_by_tab[TabId.HH_OPEN]
    }
    if hh_open_ticks:
        for conflict_tab in (TabId.HH_CLOSE, TabId.LP):
            events_list = mapped_events_by_tab[conflict_tab]
            if not events_list:
                continue
            ch_code = TAB_SPEC_MAP[conflict_tab].dtx_channels[0]
            kept_events: List[NoteEvent] = []
            seen_removal_keys: Set[Tuple[int, Fraction, TabId, str]] = set()
            for ev in events_list:
                tick = (ev.measure, ev.pos)
                if tick in hh_open_ticks:
                    rkey = (ev.measure, ev.pos, conflict_tab, ev.wav_id)
                    if rkey not in seen_removal_keys:
                        seen_removal_keys.add(rkey)
                        entry = chart.wavs.get(ev.wav_id)
                        fname = entry.resolved_name if entry is not None else "(undeclared)"
                        hihat_conflict_removals.append(
                            HiHatConflictRemoval(
                                measure=ev.measure,
                                pos=ev.pos,
                                tab=conflict_tab,
                                channel=ch_code,
                                wav_id=ev.wav_id,
                                filename=fname,
                            )
                        )
                    continue
                kept_events.append(ev)
            mapped_events_by_tab[conflict_tab] = kept_events

        hihat_conflict_removals.sort(
            key=lambda r: (r.measure, r.pos, TAB_ORDER.index(r.tab), r.wav_id)
        )
        for rec in hihat_conflict_removals:
            warnings.append(
                ConversionWarning(
                    code="HIHAT_OPEN_CONFLICT_REMOVED",
                    message=(
                        f"M{rec.measure:03d} pos {rec.pos}: removed {rec.tab.value}({rec.channel}) "
                        f"#WAV{rec.wav_id} ({rec.filename}) due to simultaneous HH Open (18)."
                    ),
                )
            )

    # 5b. Place Tab 1..15 notes onto their DTX channels
    tab_placed_notes: List[PlacedDtxNote] = []
    tab_collision_events: List[NoteEvent] = []
    collision_details: List[str] = []
    tom_overflow_records: List[TomOverflowRecord] = []
    merged_slices: List[MergedSliceSpec] = []
    merged_combo_cache: Dict[Tuple[Tuple[str, bool], ...], MergedSliceSpec] = {}
    tom_overflow_by_tick: Dict[
        Tuple[int, Fraction], List[Tuple[int, Tuple[int, int, int], TabId, NoteEvent]]
    ] = defaultdict(list)

    next_free_slot = start_slot + len(ordered_retained_orig_ids)
    slot_exhausted_count = 0

    for tab_idx, tab in enumerate(TAB_ORDER):
        spec = TAB_SPEC_MAP[tab]
        tab_events = mapped_events_by_tab[tab]
        if not tab_events:
            continue

        channels = spec.dtx_channels
        cap = len(channels)
        default_dtx_wid = normalized_binds.get(tab)

        def _prio(ev: NoteEvent, *, _default_dtx: Optional[str] = default_dtx_wid) -> Tuple[int, int, int]:
            is_bgm = 1 if ev.channel == "01" else 0
            is_not_default = (
                0 if (_default_dtx is not None and wav_id_map.get(ev.wav_id) == _default_dtx) else 1
            )
            return (is_bgm, is_not_default, ev.order_index)

        grouped: Dict[Tuple[int, Fraction], List[NoteEvent]] = defaultdict(list)
        for ev in tab_events:
            grouped[(ev.measure, ev.pos)].append(ev)

        occupied_by_ch: Dict[str, Dict[Tuple[int, Fraction], str]] = {
            ch: {} for ch in channels
        }
        last_time_by_ch: Dict[str, Optional[float]] = {ch: None for ch in channels}

        for slot_key in sorted(grouped.keys()):
            slot_events = grouped[slot_key]
            # Deduplicate identical (DTX WAV slot, effective_swap) at the same tick
            best_by_key: Dict[Tuple[str, bool], NoteEvent] = {}
            for ev in slot_events:
                dtx_wid = wav_id_map.get(ev.wav_id, ev.wav_id)
                eff_swap = bool(global_swap_lr) ^ (ev.wav_id in swapped_set)
                dedup_key = (dtx_wid, eff_swap)
                prev = best_by_key.get(dedup_key)
                if prev is None or _prio(ev) < _prio(prev):
                    best_by_key[dedup_key] = ev

            ranked = sorted(best_by_key.values(), key=_prio)

            # Toggle=OFF: Tom lanes route 1 overflow to SE7 (67); remaining drum lanes merge distinct slices
            if not effective_toggle_on and spec.is_drum:
                if len(ranked) == 1:
                    ev = ranked[0]
                    tab_placed_notes.append(
                        PlacedDtxNote(
                            measure=ev.measure,
                            pos=ev.pos,
                            channel=channels[0],
                            dtx_wav_id=wav_id_map[ev.wav_id],
                            original_wav_id=ev.wav_id,
                            time_sec=ev.time_sec,
                        )
                    )
                    continue

                if tab in TOM_TABS:
                    winner = ranked[0]
                    tab_placed_notes.append(
                        PlacedDtxNote(
                            measure=winner.measure,
                            pos=winner.pos,
                            channel=channels[0],
                            dtx_wav_id=wav_id_map[winner.wav_id],
                            original_wav_id=winner.wav_id,
                            time_sec=winner.time_sec,
                        )
                    )
                    for extra_ev in ranked[1:]:
                        tom_overflow_by_tick[slot_key].append(
                            (tab_idx, _prio(extra_ev), tab, extra_ev)
                        )
                    continue

                # Non-Tom drum lane: strictly deduplicated offline mix-merge of distinct components
                existing_items: List[Tuple[str, bool, str, NoteEvent]] = []
                missing_items: List[NoteEvent] = []
                for ev in ranked:
                    dtx_wid = wav_id_map[ev.wav_id]
                    canon_orig = slot_to_canonical_orig.get(dtx_wid, ev.wav_id)
                    eff_swap = bool(global_swap_lr) ^ (ev.wav_id in swapped_set)
                    entry = chart.wavs.get(canon_orig) or chart.wavs.get(ev.wav_id)
                    if entry is not None and entry.exists and entry.resolved_path is not None:
                        existing_items.append((canon_orig, eff_swap, entry.resolved_name, ev))
                    else:
                        missing_items.append(ev)

                if len(existing_items) == 1:
                    canon_orig, _eff_swap, _fname, winner = existing_items[0]
                    tab_placed_notes.append(
                        PlacedDtxNote(
                            measure=winner.measure,
                            pos=winner.pos,
                            channel=channels[0],
                            dtx_wav_id=wav_id_map[winner.wav_id],
                            original_wav_id=canon_orig,
                            time_sec=winner.time_sec,
                        )
                    )
                    if missing_items:
                        missing_sample = ", ".join(f"#WAV{m.wav_id}" for m in missing_items[:4])
                        warnings.append(
                            ConversionWarning(
                                code="MERGED_SLICE_MISSING_COMPONENT_SKIPPED",
                                message=(
                                    f"M{winner.measure:03d} pos {winner.pos}: skipped missing "
                                    f"simultaneous slice(s) ({missing_sample}) on {tab.value}({channels[0]})."
                                ),
                            )
                        )
                    continue

                if len(existing_items) >= 2:
                    existing_items.sort(key=lambda item: (item[0], item[1]))
                    combo_key: Tuple[Tuple[str, bool], ...] = tuple(
                        (item[0], item[1]) for item in existing_items
                    )
                    spec_obj = merged_combo_cache.get(combo_key)
                    if spec_obj is None:
                        if next_free_slot <= DTX_MAX_WAV_SLOTS:
                            merged_dtx_wid = int_to_base36(next_free_slot)
                            next_free_slot += 1
                            components = tuple(
                                (item[0], item[1], item[2]) for item in existing_items
                            )
                            spec_obj = MergedSliceSpec(
                                combo_key=combo_key,
                                dtx_wav_id=merged_dtx_wid,
                                first_tab=tab,
                                components=components,
                            )
                            merged_combo_cache[combo_key] = spec_obj
                            merged_slices.append(spec_obj)
                            dtx_wav_labels[merged_dtx_wid] = f"{tab.value}_MIX"
                    if spec_obj is not None:
                        spec_obj.occurrences.append(
                            MergedSliceOccurrence(
                                measure=slot_key[0],
                                pos=slot_key[1],
                                tab=tab,
                                channel=channels[0],
                            )
                        )
                        ref_ev = existing_items[0][3]
                        tab_placed_notes.append(
                            PlacedDtxNote(
                                measure=slot_key[0],
                                pos=slot_key[1],
                                channel=channels[0],
                                dtx_wav_id=spec_obj.dtx_wav_id,
                                original_wav_id=spec_obj.dtx_wav_id,
                                time_sec=ref_ev.time_sec,
                            )
                        )
                        if missing_items:
                            missing_sample = ", ".join(f"#WAV{m.wav_id}" for m in missing_items[:4])
                            warnings.append(
                                ConversionWarning(
                                    code="MERGED_SLICE_MISSING_COMPONENT_SKIPPED",
                                    message=(
                                        f"M{slot_key[0]:03d} pos {slot_key[1]}: skipped missing "
                                        f"simultaneous slice(s) ({missing_sample}) while merging on {tab.value}({channels[0]})."
                                    ),
                                )
                            )
                        continue
                    else:
                        # Capacity 01..ZX exhausted -> fallback winner + reflow extras to bgm.ogg
                        slot_exhausted_count += 1
                        winner = ranked[0]
                        tab_placed_notes.append(
                            PlacedDtxNote(
                                measure=winner.measure,
                                pos=winner.pos,
                                channel=channels[0],
                                dtx_wav_id=wav_id_map[winner.wav_id],
                                original_wav_id=winner.wav_id,
                                time_sec=winner.time_sec,
                            )
                        )
                        for extra_ev in ranked[1:]:
                            tab_collision_events.append(extra_ev)
                            collision_details.append(
                                f"M{extra_ev.measure:03d} {tab.value}({channels[0]}) #WAV{extra_ev.wav_id}"
                            )
                        continue

                # All colliding slices are missing audio files -> keep winner on lane, reflow rest
                winner = ranked[0]
                tab_placed_notes.append(
                    PlacedDtxNote(
                        measure=winner.measure,
                        pos=winner.pos,
                        channel=channels[0],
                        dtx_wav_id=wav_id_map[winner.wav_id],
                        original_wav_id=winner.wav_id,
                        time_sec=winner.time_sec,
                    )
                )
                for extra_ev in ranked[1:]:
                    tab_collision_events.append(extra_ev)
                    collision_details.append(
                        f"M{extra_ev.measure:03d} {tab.value}({channels[0]}) #WAV{extra_ev.wav_id}"
                    )
                continue

            # Standard placement (Other/Guitar/Bass SE polyphony, or Toggle=ON tabs)
            winners = ranked[:cap]
            displaced = ranked[cap:]

            for ev in winners:
                dtx_wid = wav_id_map[ev.wav_id]
                if cap == 1:
                    chosen = channels[0]
                else:
                    candidates = [ch for ch in channels if slot_key not in occupied_by_ch[ch]]
                    chosen = min(
                        candidates,
                        key=lambda ch: (
                            0 if last_time_by_ch[ch] is None else 1,
                            last_time_by_ch[ch] if last_time_by_ch[ch] is not None else -1.0,
                            channels.index(ch),
                        ),
                    )
                occupied_by_ch[chosen][slot_key] = dtx_wid
                last_time_by_ch[chosen] = ev.time_sec
                tab_placed_notes.append(
                    PlacedDtxNote(
                        measure=ev.measure,
                        pos=ev.pos,
                        channel=chosen,
                        dtx_wav_id=dtx_wid,
                        original_wav_id=ev.wav_id,
                        time_sec=ev.time_sec,
                    )
                )

            for ev in displaced:
                tab_collision_events.append(ev)
                collision_details.append(
                    f"M{ev.measure:03d} {tab.value}({'/'.join(channels)}) #WAV{ev.wav_id}"
                )

    # 5c. Resolve Tom overflow queue for Toggle=OFF: strictly 1 note/tick to SE7 (67); extras -> bgm.ogg
    if tom_overflow_by_tick:
        for slot_key in sorted(tom_overflow_by_tick.keys()):
            items = sorted(tom_overflow_by_tick[slot_key], key=lambda item: (item[0], item[1]))
            first_tab_idx, _first_prio, first_tab, first_ev = items[0]
            _ = first_tab_idx
            first_ch = TAB_SPEC_MAP[first_tab].dtx_channels[0]
            first_entry = chart.wavs.get(first_ev.wav_id)
            first_fname = first_entry.resolved_name if first_entry is not None else "(undeclared)"
            tab_placed_notes.append(
                PlacedDtxNote(
                    measure=first_ev.measure,
                    pos=first_ev.pos,
                    channel=TOM_OVERFLOW_SE_CHANNEL,
                    dtx_wav_id=wav_id_map[first_ev.wav_id],
                    original_wav_id=first_ev.wav_id,
                    time_sec=first_ev.time_sec,
                )
            )
            tom_overflow_records.append(
                TomOverflowRecord(
                    measure=first_ev.measure,
                    pos=first_ev.pos,
                    source_tab=first_tab,
                    source_channel=first_ch,
                    wav_id=first_ev.wav_id,
                    filename=first_fname,
                    target_channel=TOM_OVERFLOW_SE_CHANNEL,
                )
            )
            for _ex_idx, _ex_prio, ex_tab, ex_ev in items[1:]:
                ex_ch = TAB_SPEC_MAP[ex_tab].dtx_channels[0]
                ex_entry = chart.wavs.get(ex_ev.wav_id)
                ex_fname = ex_entry.resolved_name if ex_entry is not None else "(undeclared)"
                unmapped_events.append(ex_ev)
                tom_overflow_records.append(
                    TomOverflowRecord(
                        measure=ex_ev.measure,
                        pos=ex_ev.pos,
                        source_tab=ex_tab,
                        source_channel=ex_ch,
                        wav_id=ex_ev.wav_id,
                        filename=ex_fname,
                        target_channel="BGM",
                    )
                )
                warnings.append(
                    ConversionWarning(
                        code="TOM_OVERFLOW_BGM_REFLOW",
                        message=(
                            f"M{ex_ev.measure:03d} pos {ex_ev.pos}: {ex_tab.value}({ex_ch}) "
                            f"#WAV{ex_ev.wav_id} ({ex_fname}) exceeded SE7 (67) single-note cap "
                            f"and reflowed into bgm.ogg."
                        ),
                    )
                )
        unmapped_events.sort(key=lambda e: (e.measure, e.pos, e.order_index))

    if slot_exhausted_count > 0:
        warnings.append(
            ConversionWarning(
                code="MERGED_SLICE_SLOT_EXHAUSTED",
                message=(
                    f"DTX Base-36 capacity ({DTX_MAX_WAV_SLOTS}, 01..ZX) reached while allocating "
                    f"merged drum slices; {slot_exhausted_count} collision slot(s) fell back to bgm.ogg."
                ),
            )
        )

    tab_placed_notes.sort(key=lambda n: (n.measure, n.pos, n.channel))

    if tab_collision_events:
        if not effective_toggle_on:
            unmapped_events.extend(tab_collision_events)
            unmapped_events.sort(key=lambda e: (e.measure, e.pos, e.order_index))
            action_text = "reflowed into bgm.ogg"
        else:
            action_text = "rerouted to non-interrupting SE pool"
        sample = ", ".join(collision_details[:10])
        more = (
            f" (+{len(collision_details) - 10} more)"
            if len(collision_details) > 10
            else ""
        )
        warnings.append(
            ConversionWarning(
                code="TAB_LANE_COLLISION",
                message=(
                    f"{len(tab_collision_events)} simultaneous note(s) on mapped Tab lane(s) "
                    f"{action_text}: {sample}{more}."
                ),
            )
        )

    # 6. Auto-LP (OHH 18 -> CHH 11 automatic LP 1B pedal insertion) using permanently reserved ZY/ZZ or direct slot reuse
    wav_volumes: Dict[str, int] = {}
    auto_lp_orig_wav_id: Optional[str] = None
    auto_lp_use_silent_zz = False
    auto_lp_use_zy = False
    auto_lp_placed_count = 0

    auto_lp_cfg = config.auto_lp
    raw_chip_mode = str(auto_lp_cfg.chip_mode or AUTO_LP_CHIP_NONE).strip()
    mode_lower = raw_chip_mode.lower()
    target_lp_dtx_wid: Optional[str] = None

    resolved_lp_orig: Optional[str] = None
    if mode_lower == AUTO_LP_CHIP_EMPTY_ZZ:
        target_lp_dtx_wid = AUTO_LP_SILENT_WAV_ID
        auto_lp_use_silent_zz = True
        wav_volumes[AUTO_LP_SILENT_WAV_ID] = 0
        dtx_wav_labels[AUTO_LP_SILENT_WAV_ID] = "LP_GHOST"
    elif mode_lower == AUTO_LP_CHIP_AUTO:
        lp_orig = bound_orig_by_tab.get(TabId.LP)
        if lp_orig is None and normalized_tab_wavs[TabId.LP]:
            lp_orig = resolve_auto_bind_wav_id(chart, normalized_tab_wavs[TabId.LP])
        if lp_orig is not None and lp_orig in wav_id_map:
            resolved_lp_orig = slot_to_canonical_orig.get(wav_id_map[lp_orig], lp_orig)
    elif mode_lower not in ("", AUTO_LP_CHIP_NONE):
        cand_wid = _norm_wav_id(chart, raw_chip_mode)
        if cand_wid in wav_id_map and wav_to_tab.get(cand_wid) == TabId.LP:
            resolved_lp_orig = slot_to_canonical_orig.get(wav_id_map[cand_wid], cand_wid)
        else:
            warnings.append(
                ConversionWarning(
                    code="AUTO_LP_UNMAPPED_CHIP",
                    message=(
                        f"Auto-LP chip {raw_chip_mode!r} is not mapped to the LP Tab; "
                        f"skipped automatic LP insertion."
                    ),
                )
            )

    if resolved_lp_orig is not None:
        auto_lp_orig_wav_id = resolved_lp_orig
        if bool(auto_lp_cfg.use_zy):
            auto_lp_use_zy = True
            target_lp_dtx_wid = AUTO_LP_RESONANT_WAV_ID
            wav_volumes[AUTO_LP_RESONANT_WAV_ID] = max(0, min(100, int(auto_lp_cfg.volume)))
            dtx_wav_labels[AUTO_LP_RESONANT_WAV_ID] = TabId.LP.value
        else:
            auto_lp_use_zy = False
            target_lp_dtx_wid = wav_id_map[resolved_lp_orig]

    if target_lp_dtx_wid is not None:
        ohh_by_tick: Dict[Tuple[int, Fraction], PlacedDtxNote] = {}
        chh_by_tick: Dict[Tuple[int, Fraction], PlacedDtxNote] = {}
        lp_ticks: Set[Tuple[int, Fraction]] = set()
        for note in tab_placed_notes:
            tick = (note.measure, note.pos)
            if note.channel == "18":
                ohh_by_tick[tick] = note
            elif note.channel == "11":
                chh_by_tick[tick] = note
            elif note.channel == "1B":
                lp_ticks.add(tick)

        if ohh_by_tick and chh_by_tick:
            thresh_mode = (
                str(auto_lp_cfg.threshold_mode or "note_value").strip().lower()
            )
            num = max(1, int(auto_lp_cfg.note_num))
            raw_den = int(auto_lp_cfg.note_den)
            den = raw_den if raw_den in AUTO_LP_VALID_DENOMINATORS else 4
            max_whole_diff = Fraction(num, den)
            max_ms_diff = max(0.0, float(auto_lp_cfg.threshold_ms))

            max_used_m = max(
                max((k[0] for k in ohh_by_tick.keys()), default=0),
                max((k[0] for k in chh_by_tick.keys()), default=0),
            )
            measure_len_frac: Dict[int, Fraction] = {}
            measure_cum_whole: List[Fraction] = [Fraction(0)]
            for m_idx in range(max_used_m + 1):
                raw_len = chart.measure_lengths.get(m_idx, 1.0)
                if raw_len <= 0:
                    raw_len = 1.0
                ml_frac = Fraction(raw_len).limit_denominator(1920)
                measure_len_frac[m_idx] = ml_frac
                measure_cum_whole.append(measure_cum_whole[-1] + ml_frac)

            def _whole_pos(m_idx: int, p: Fraction) -> Fraction:
                return measure_cum_whole[m_idx] + p * measure_len_frac[m_idx]

            all_ticks = sorted(set(ohh_by_tick.keys()) | set(chh_by_tick.keys()) | lp_ticks)
            pending_ohh: Optional[PlacedDtxNote] = None
            lp_seen_since_ohh = False
            inserted_lp_notes: List[PlacedDtxNote] = []

            for tick in all_ticks:
                has_lp_at_tick = tick in lp_ticks
                if tick in chh_by_tick and pending_ohh is not None:
                    ohh = pending_ohh
                    chh = chh_by_tick[tick]
                    # Rule 1: Only first CHH after OHH consumes the pending open state
                    pending_ohh = None
                    # Rule 2: Skip if any original LP appeared since OHH or exists on this CHH/OHH tick
                    if not lp_seen_since_ohh and not has_lp_at_tick and tick not in ohh_by_tick:
                        if thresh_mode == AUTO_LP_THRESHOLD_MS:
                            dt_ms = (chh.time_sec - ohh.time_sec) * 1000.0
                            within_threshold = 0.0 < dt_ms <= (max_ms_diff + 1e-6)
                        else:
                            dt_whole = _whole_pos(chh.measure, chh.pos) - _whole_pos(
                                ohh.measure, ohh.pos
                            )
                            within_threshold = Fraction(0) < dt_whole <= max_whole_diff
                        if within_threshold:
                            inserted_lp_notes.append(
                                PlacedDtxNote(
                                    measure=chh.measure,
                                    pos=chh.pos,
                                    channel="1B",
                                    dtx_wav_id=target_lp_dtx_wid,
                                    original_wav_id=auto_lp_orig_wav_id or AUTO_LP_SILENT_WAV_ID,
                                    time_sec=chh.time_sec,
                                )
                            )
                            lp_ticks.add(tick)
                            has_lp_at_tick = True

                if has_lp_at_tick and pending_ohh is not None:
                    lp_seen_since_ohh = True

                # Rule 3: Consecutive OHH resets anchor to the latest OHH
                if tick in ohh_by_tick:
                    pending_ohh = ohh_by_tick[tick]
                    lp_seen_since_ohh = False

            if inserted_lp_notes:
                tab_placed_notes.extend(inserted_lp_notes)
                tab_placed_notes.sort(key=lambda n: (n.measure, n.pos, n.channel))
                auto_lp_placed_count = len(inserted_lp_notes)

    return MappingPlan(
        effective_toggle_on=effective_toggle_on,
        toggle_off_forced=toggle_off_forced,
        normalized_tab_wavs=normalized_tab_wavs,
        wav_to_tab=wav_to_tab,
        wav_id_map=wav_id_map,
        ordered_retained_orig_ids=ordered_retained_orig_ids,
        tab_placed_notes=tab_placed_notes,
        unmapped_events=unmapped_events,
        lane_bind_lines=lane_bind_lines,
        guitar_enabled=guitar_enabled,
        bass_enabled=bass_enabled,
        tab_collision_events=tab_collision_events,
        warnings=warnings,
        dtx_wav_labels=dtx_wav_labels,
        wav_volumes=wav_volumes,
        auto_lp_orig_wav_id=auto_lp_orig_wav_id,
        auto_lp_use_silent_zz=auto_lp_use_silent_zz,
        auto_lp_use_zy=auto_lp_use_zy,
        auto_lp_placed_count=auto_lp_placed_count,
        hihat_conflict_removals=hihat_conflict_removals,
        tom_overflow_records=tom_overflow_records,
        merged_slices=merged_slices,
    )
