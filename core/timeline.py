"""Exact piecewise-linear BMS/DTX timeline calculator (BPM, exBPM, measure length 02)."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Sequence

from .models import BmsChart, BpmChange


@dataclass(frozen=True)
class TimelineSegment:
    """Constant-BPM segment within a single measure."""

    measure: int
    start_pos: Fraction
    end_pos: Fraction
    start_time: float
    end_time: float
    bpm: float
    measure_length: float


class Timeline:
    """Computes exact event timestamps across BPM changes and variable measure lengths.

    Measure 000 beat 0 corresponds to t = 0.0 seconds.
    For a measure of length multiplier L (default 1.0 = 4 beats) at constant BPM B:
        duration(df) = (240.0 * L * df) / B
    """

    def __init__(
        self,
        initial_bpm: float,
        measure_lengths: Dict[int, float],
        bpm_changes: Sequence[BpmChange],
        max_measure: int,
    ) -> None:
        self.initial_bpm = initial_bpm if initial_bpm > 0 else 130.0
        self.measure_lengths = dict(measure_lengths)
        self.max_measure = max(0, max_measure)

        # Group BPM changes by measure; if 03 and 08 occur at the exact same pos, 08 wins.
        by_measure: Dict[int, Dict[Fraction, BpmChange]] = {}
        ordered_changes = sorted(
            bpm_changes,
            key=lambda bc: (bc.measure, bc.pos, 0 if bc.source_channel == "03" else 1),
        )
        for bc in ordered_changes:
            if bc.bpm <= 0 or bc.measure < 0:
                continue
            by_measure.setdefault(bc.measure, {})[bc.pos] = bc

        self._segments_by_measure: Dict[int, List[TimelineSegment]] = {}
        self._measure_start_times: Dict[int, float] = {}
        self._measure_end_times: Dict[int, float] = {}

        cur_time = 0.0
        cur_bpm = self.initial_bpm
        end_measure = max(
            self.max_measure,
            max(self.measure_lengths.keys(), default=0),
            max(by_measure.keys(), default=0),
        )

        for m in range(0, end_measure + 1):
            length = self.measure_lengths.get(m, 1.0)
            if length <= 0:
                length = 1.0
            self._measure_start_times[m] = cur_time
            changes = by_measure.get(m, {})

            # Apply BPM change at exact start of measure (pos == 0) immediately
            if Fraction(0) in changes:
                cur_bpm = changes[Fraction(0)].bpm

            split_points = sorted(
                pos for pos in changes.keys() if Fraction(0) < pos < Fraction(1)
            )
            seg_list: List[TimelineSegment] = []
            prev_pos = Fraction(0)

            for pos in split_points:
                dt = (240.0 * length * float(pos - prev_pos)) / cur_bpm
                next_time = cur_time + dt
                seg_list.append(
                    TimelineSegment(
                        measure=m,
                        start_pos=prev_pos,
                        end_pos=pos,
                        start_time=cur_time,
                        end_time=next_time,
                        bpm=cur_bpm,
                        measure_length=length,
                    )
                )
                cur_time = next_time
                prev_pos = pos
                cur_bpm = changes[pos].bpm

            # Final segment to end of measure (pos == 1)
            dt = (240.0 * length * float(Fraction(1) - prev_pos)) / cur_bpm
            next_time = cur_time + dt
            seg_list.append(
                TimelineSegment(
                    measure=m,
                    start_pos=prev_pos,
                    end_pos=Fraction(1),
                    start_time=cur_time,
                    end_time=next_time,
                    bpm=cur_bpm,
                    measure_length=length,
                )
            )
            cur_time = next_time
            self._segments_by_measure[m] = seg_list
            self._measure_end_times[m] = cur_time

        self._end_measure = end_measure
        self._end_time = cur_time
        self._final_bpm = cur_bpm

    def time_at(self, measure: int, pos: Fraction) -> float:
        """Return exact timestamp in seconds for (measure, pos)."""
        if measure < 0:
            return 0.0
        # Normalize pos outside [0, 1) cleanly if ever passed
        while pos >= 1:
            measure += 1
            pos -= 1
        while pos < 0 and measure > 0:
            measure -= 1
            pos += 1
        if pos < 0:
            pos = Fraction(0)

        if measure <= self._end_measure:
            segments = self._segments_by_measure[measure]
            for seg in segments:
                if seg.start_pos <= pos < seg.end_pos:
                    return seg.start_time + (
                        240.0 * seg.measure_length * float(pos - seg.start_pos)
                    ) / seg.bpm
            return segments[-1].end_time

        # Extrapolate past end_measure with final BPM and default 4/4 measure length
        extra_measures = (measure - self._end_measure - 1) + float(pos)
        return self._end_time + (240.0 * extra_measures) / self._final_bpm

    def measure_start_time(self, measure: int) -> float:
        return self.time_at(measure, Fraction(0))

    def measure_end_time(self, measure: int) -> float:
        if measure in self._measure_end_times:
            return self._measure_end_times[measure]
        return self.time_at(measure + 1, Fraction(0))

    @property
    def total_duration_sec(self) -> float:
        return self._measure_end_times.get(self.max_measure, self._end_time)


def build_timeline_for_chart(chart: BmsChart) -> Timeline:
    """Build Timeline and populate time_sec on all chart events and BPM changes."""
    timeline = Timeline(
        initial_bpm=chart.initial_bpm,
        measure_lengths=chart.measure_lengths,
        bpm_changes=chart.bpm_changes,
        max_measure=chart.max_measure,
    )
    for ev in chart.events:
        ev.time_sec = timeline.time_at(ev.measure, ev.pos)
    for ev in chart.ln_end_events:
        ev.time_sec = timeline.time_at(ev.measure, ev.pos)
    chart.bpm_changes = [
        BpmChange(
            measure=bc.measure,
            pos=bc.pos,
            bpm=bc.bpm,
            source_channel=bc.source_channel,
            time_sec=timeline.time_at(bc.measure, bc.pos),
        )
        for bc in chart.bpm_changes
    ]
    chart.song_duration_sec = timeline.total_duration_sec
    return timeline
