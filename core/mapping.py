"""15-Tab mapping, Base-36 WAV renumbering, and #DTXC_LANEBINDEDCHIP generation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .constants import (
    DRUM_TABS,
    DTX_MAX_MAPPED_SLOTS_TOGGLE_OFF,
    DTX_MAX_WAV_SLOTS,
    DTXC_BIND_LANES,
    MAX_SLICER_COLUMNS,
    TAB_ORDER,
    TAB_SPEC_MAP,
    TabId,
    int_to_base36,
)
from .models import BmsChart, ConversionWarning, MappingConfig, NoteEvent, PlacedDtxNote


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


def build_mapping_plan(
    chart: BmsChart,
    config: MappingConfig,
    *,
    auto_merge_duplicates: Optional[bool] = None,
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

    # 5. Place Tab 1..15 notes onto their DTX channels (never silently drop simultaneous collisions)
    tab_placed_notes: List[PlacedDtxNote] = []
    tab_collision_events: List[NoteEvent] = []
    collision_details: List[str] = []

    for tab in TAB_ORDER:
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
            # Deduplicate identical DTX WAV slots (including merged duplicate physical files) at the same tick
            best_by_wav: Dict[str, NoteEvent] = {}
            for ev in slot_events:
                dedup_key = wav_id_map.get(ev.wav_id, ev.wav_id)
                prev = best_by_wav.get(dedup_key)
                if prev is None or _prio(ev) < _prio(prev):
                    best_by_wav[dedup_key] = ev

            ranked = sorted(best_by_wav.values(), key=_prio)
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
    )
