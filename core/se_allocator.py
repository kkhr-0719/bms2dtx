"""Toggle = ON SE channel allocator: Slicer (SE1-SE5), non-interrupting Collapse pool, ±1/192 fallback."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .constants import (
    BASS_SE_CHANNELS,
    COLLAPSE_BACKUP_CHANNELS,
    COLLAPSE_PRIMARY_MID,
    COLLAPSE_PRIMARY_PRE,
    COLLAPSE_PRIMARY_TAIL,
    GUITAR_SE_CHANNELS,
    MAX_SLICER_COLUMNS,
    SLICER_SE_CHANNELS,
)
from .models import ConversionWarning, NoteEvent, PlacedDtxNote

_NUDGE_STEP = Fraction(1, 192)


class SeCollapseOverflowError(RuntimeError):
    """Raised when Collapse overflows all available SE channels (SE1-SE32)."""

    def __init__(self, overflow_events: List[NoteEvent], message: str) -> None:
        super().__init__(message)
        self.overflow_events = overflow_events


@dataclass
class SeAllocationResult:
    """Result of Toggle=ON Slicer + Collapse allocation."""

    placed_notes: List[PlacedDtxNote]
    slicer_channel_map: Dict[int, str]  # BGM column (1-based) -> SE channel ("61".."65")
    primary_pool_used: Tuple[str, ...]
    backup_pool_used: Tuple[str, ...]
    nudged_count: int = 0
    fallback_to_toggle_off: bool = False
    warnings: List[ConversionWarning] = field(default_factory=list)


def get_collapse_pools(
    *, guitar_enabled: bool, bass_enabled: bool
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Return (primary_pool, backup_pool) strictly following AGENTS.md Section 3.

    Primary non-interrupting pool:
      SE7-SE10 (67-70)
      + SE11-SE12 (71-72) if Guitar tab is empty
      + SE13-SE14 (73-74) if Bass tab is empty
      + SE15-SE23 (75-83)
      + SE30-SE32 (90-92)
    Backup pool (only used when ALL primary channels are occupied at that slot):
      SE24-SE29 (84-89)
    """
    primary: List[str] = list(COLLAPSE_PRIMARY_PRE)
    if not guitar_enabled:
        primary.extend(GUITAR_SE_CHANNELS)
    if not bass_enabled:
        primary.extend(BASS_SE_CHANNELS)
    primary.extend(COLLAPSE_PRIMARY_MID)
    primary.extend(COLLAPSE_PRIMARY_TAIL)
    return tuple(primary), COLLAPSE_BACKUP_CHANNELS


def _shift_pos(measure: int, pos: Fraction, delta: Fraction) -> Optional[Tuple[int, Fraction]]:
    new_pos = pos + delta
    new_measure = measure
    while new_pos >= 1:
        new_measure += 1
        new_pos -= 1
    while new_pos < 0:
        if new_measure == 0:
            return None
        new_measure -= 1
        new_pos += 1
    # Never nudge a legal 3-digit measure (000-999) into illegal 4-digit measure 1000+
    if measure <= 999 and new_measure > 999:
        return None
    return new_measure, new_pos


def allocate_se_channels(
    unmapped_events: Sequence[NoteEvent],
    wav_id_map: Dict[str, str],
    *,
    slicer_bgm_columns: Sequence[int] = (),
    guitar_enabled: bool = False,
    bass_enabled: bool = False,
    overflow_strategy: str = "fail",
    extra_collapse_events: Sequence[NoteEvent] = (),
) -> SeAllocationResult:
    """Allocate remaining unmapped events to Slicer (SE1-SE5) and Collapse pool.

    Never allocates out-of-bounds SE33+ channels or channel 01.
    """
    if overflow_strategy not in ("fail", "nudge_192", "fallback_toggle_off"):
        raise ValueError(f"Unknown overflow_strategy: {overflow_strategy!r}")

    # Validate Slicer selection: at most 5 distinct BGM columns -> SE1..SE5 (61..65)
    dedup_cols: List[int] = []
    seen_cols: Set[int] = set()
    for col in slicer_bgm_columns:
        if col < 1:
            raise ValueError(f"Invalid BGM column index for Slicer: {col}")
        if col not in seen_cols:
            seen_cols.add(col)
            dedup_cols.append(col)

    if len(dedup_cols) > MAX_SLICER_COLUMNS:
        raise ValueError(
            f"Slicer supports at most {MAX_SLICER_COLUMNS} BGM columns (SE1-SE5), "
            f"got {len(dedup_cols)}."
        )

    slicer_channel_map: Dict[int, str] = {
        col: SLICER_SE_CHANNELS[idx] for idx, col in enumerate(dedup_cols)
    }

    primary_pool, backup_pool = get_collapse_pools(
        guitar_enabled=guitar_enabled,
        bass_enabled=bass_enabled,
    )

    warnings: List[ConversionWarning] = []
    placed_notes: List[PlacedDtxNote] = []
    collapse_queue: List[NoteEvent] = []

    # 1. Place Slicer columns 1-to-1 onto SE1..SE5 (no Collapse!)
    slicer_occupied: Dict[str, Set[Tuple[int, Fraction]]] = {
        ch: set() for ch in slicer_channel_map.values()
    }
    for ev in unmapped_events:
        if ev.channel == "01" and ev.bgm_column in slicer_channel_map:
            ch = slicer_channel_map[ev.bgm_column]
            key = (ev.measure, ev.pos)
            if key in slicer_occupied[ch]:
                continue
            slicer_occupied[ch].add(key)
            placed_notes.append(
                PlacedDtxNote(
                    measure=ev.measure,
                    pos=ev.pos,
                    channel=ch,
                    dtx_wav_id=wav_id_map[ev.wav_id],
                    original_wav_id=ev.wav_id,
                    time_sec=ev.time_sec,
                )
            )
        else:
            collapse_queue.append(ev)

    if extra_collapse_events:
        # Tab-displaced collisions bypass Slicer SE1-SE5 and route directly to non-interrupting Collapse pool
        collapse_queue.extend(extra_collapse_events)

    # 2. Collapse remaining events using minimum-interference bin packing
    collapse_queue.sort(key=lambda e: (e.time_sec, e.measure, e.pos, e.order_index))

    all_collapse_channels = primary_pool + backup_pool
    occupied_slots: Dict[str, Set[Tuple[int, Fraction]]] = {
        ch: set() for ch in all_collapse_channels
    }
    last_time: Dict[str, Optional[float]] = {ch: None for ch in all_collapse_channels}
    used_backup = False

    def _pick_channel(
        pool: Tuple[str, ...], slot: Tuple[int, Fraction]
    ) -> Optional[str]:
        candidates = [ch for ch in pool if slot not in occupied_slots[ch]]
        if not candidates:
            return None
        # Prefer untouched channels first; otherwise pick channel with oldest last_time (largest gap)
        return min(
            candidates,
            key=lambda ch: (
                0 if last_time[ch] is None else 1,
                last_time[ch] if last_time[ch] is not None else -1.0,
                pool.index(ch),
            ),
        )

    def _try_place(
        slot: Tuple[int, Fraction], ev_time: float
    ) -> Optional[Tuple[str, bool]]:
        ch = _pick_channel(primary_pool, slot)
        if ch is not None:
            return ch, False
        ch = _pick_channel(backup_pool, slot)
        if ch is not None:
            return ch, True
        return None

    overflow_events: List[NoteEvent] = []
    nudged_count = 0

    for ev in collapse_queue:
        base_slot = (ev.measure, ev.pos)
        chosen = _try_place(base_slot, ev.time_sec)
        if chosen is not None:
            ch, is_backup = chosen
            if is_backup:
                used_backup = True
            occupied_slots[ch].add(base_slot)
            last_time[ch] = ev.time_sec
            placed_notes.append(
                PlacedDtxNote(
                    measure=ev.measure,
                    pos=ev.pos,
                    channel=ch,
                    dtx_wav_id=wav_id_map[ev.wav_id],
                    original_wav_id=ev.wav_id,
                    time_sec=ev.time_sec,
                    nudged_192=0,
                )
            )
            continue

        # Overflow occurred at base_slot!
        if overflow_strategy == "fallback_toggle_off":
            warnings.append(
                ConversionWarning(
                    code="SE_COLLAPSE_FALLBACK_TOGGLE_OFF",
                    message=(
                        f"Collapse overflowed all {len(all_collapse_channels)} SE channels at "
                        f"measure {ev.measure:03d} pos {ev.pos}; falling back to Toggle=OFF (bgm.ogg)."
                    ),
                )
            )
            return SeAllocationResult(
                placed_notes=[],
                slicer_channel_map=slicer_channel_map,
                primary_pool_used=primary_pool,
                backup_pool_used=backup_pool,
                nudged_count=0,
                fallback_to_toggle_off=True,
                warnings=warnings,
            )

        if overflow_strategy == "nudge_192":
            placed_nudged = False
            for nudge_dir, delta in ((1, _NUDGE_STEP), (-1, -_NUDGE_STEP)):
                shifted = _shift_pos(ev.measure, ev.pos, delta)
                if shifted is None:
                    continue
                chosen_nudge = _try_place(shifted, ev.time_sec)
                if chosen_nudge is not None:
                    ch, is_backup = chosen_nudge
                    if is_backup:
                        used_backup = True
                    occupied_slots[ch].add(shifted)
                    last_time[ch] = ev.time_sec
                    nudged_count += 1
                    placed_notes.append(
                        PlacedDtxNote(
                            measure=shifted[0],
                            pos=shifted[1],
                            channel=ch,
                            dtx_wav_id=wav_id_map[ev.wav_id],
                            original_wav_id=ev.wav_id,
                            time_sec=ev.time_sec,
                            nudged_192=nudge_dir,
                        )
                    )
                    placed_nudged = True
                    break
            if placed_nudged:
                continue

        overflow_events.append(ev)

    if used_backup:
        warnings.append(
            ConversionWarning(
                code="SE_BACKUP_POOL_USED",
                message=(
                    "Primary non-interrupting SE pool was exhausted at peak polyphony; "
                    "utilized backup pool SE24-SE29 (84-89)."
                ),
            )
        )

    if nudged_count > 0:
        warnings.append(
            ConversionWarning(
                code="SE_OVERFLOW_NUDGED_192",
                message=(
                    f"Nudged {nudged_count} overflowing note(s) by ±1/192 measure to fit "
                    f"within SE1-SE32. Note: this is a last-resort fallback and may be "
                    f"affected by DTXCreator 192-grid quantization."
                ),
            )
        )

    if overflow_events:
        first = overflow_events[0]
        raise SeCollapseOverflowError(
            overflow_events=overflow_events,
            message=(
                f"{len(overflow_events)} note(s) could not fit into SE1-SE32 "
                f"(first overflow at measure {first.measure:03d} pos {first.pos}). "
                f"Choose Toggle=OFF (bgm.ogg), ±1/192 nudge fallback, or adjust mapping."
            ),
        )

    return SeAllocationResult(
        placed_notes=placed_notes,
        slicer_channel_map=slicer_channel_map,
        primary_pool_used=primary_pool,
        backup_pool_used=backup_pool,
        nudged_count=nudged_count,
        fallback_to_toggle_off=False,
        warnings=warnings,
    )
