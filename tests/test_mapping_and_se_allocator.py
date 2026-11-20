"""Tests for 15-Tab mapping, #DTXC_LANEBINDEDCHIP, WAV renumbering, Slicer, Collapse, ±1/192."""

from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from core.constants import (
    ALL_SE_CHANNELS,
    COLLAPSE_BACKUP_CHANNELS,
    LEGAL_DTX_NOTE_CHANNELS,
    SLICER_SE_CHANNELS,
    TAB_ORDER,
    TabId,
    int_to_base36,
)
from core.converter import convert_bms
from core.mapping import build_mapping_plan
from core.models import ConversionOptions, MappingConfig, NoteEvent
from core.parser import parse_bms
from core.se_allocator import (
    SeCollapseOverflowError,
    allocate_se_channels,
    get_collapse_pools,
)


class TestMappingAndRenumbering(unittest.TestCase):
    def _make_15tab_chart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        # Assign intentionally scrambled original BMS WAV IDs (ZZ, ZY, ...) to verify renumbering
        orig_ids = [int_to_base36(100 + i) for i in range(18)]
        lines = ["#TITLE Full15Tab", "#BPM 120"]
        for i, wid in enumerate(orig_ids):
            fname = f"s{i:02d}.wav"
            (root / fname).write_bytes(b"dummy")
            lines.append(f"#WAV{wid} {fname}")
        # Put 1 note per WAV across B1..B3
        lines.append(f"#00101:{''.join(orig_ids[:8])}")
        lines.append(f"#00101:{''.join(orig_ids[8:16])}")
        lines.append(f"#00101:{''.join(orig_ids[16:18])}")
        bms_path = root / "full15.bms"
        bms_path.write_text("\n".join(lines), encoding="utf-8")
        return parse_bms(bms_path), orig_ids

    def test_15tab_channels_wav_renumbering_and_lane_binded_chip(self) -> None:
        chart, orig_ids = self._make_15tab_chart()
        # Map in REVERSE order inside dict to verify canonical TAB_ORDER renumbering!
        tab_wavs = {
            TabId.BASS: [orig_ids[14]],
            TabId.GUITAR: [orig_ids[13]],
            TabId.OTHER: [orig_ids[12]],
            TabId.RD: [orig_ids[11]],
            TabId.CY: [orig_ids[10]],
            TabId.FT: [orig_ids[9]],
            TabId.LT: [orig_ids[8]],
            TabId.BD: [orig_ids[7]],
            TabId.HT: [orig_ids[6]],
            TabId.SD: [orig_ids[5]],
            TabId.LBD: [orig_ids[4]],
            TabId.LP: [orig_ids[3]],
            TabId.HH_OPEN: [orig_ids[2]],
            TabId.HH_CLOSE: [orig_ids[1]],
            TabId.LC: [orig_ids[0]],
        }
        # Bind selected drum tabs (including dual-slot lanes 02, 03, 09)
        binds = {
            TabId.LC: orig_ids[0],
            TabId.HH_CLOSE: orig_ids[1],
            TabId.HH_OPEN: orig_ids[2],
            TabId.LBD: orig_ids[4],  # LP unset -> 03 00 <LBD>
            TabId.SD: orig_ids[5],
            TabId.BD: orig_ids[7],
            TabId.CY: orig_ids[10],  # RD unset -> 09 <CY> 00
        }
        cfg = MappingConfig(tab_wavs=tab_wavs, default_lane_binds=binds, toggle_on=False)
        plan = build_mapping_plan(chart, cfg)

        # Toggle=OFF reserves 01 for bgm.ogg, so 15 mapped slices get 02..0G in exact TAB_ORDER!
        expected_dtx_ids = [int_to_base36(2 + i) for i in range(15)]
        for i, tab in enumerate(TAB_ORDER):
            orig = orig_ids[i]
            self.assertEqual(plan.wav_id_map[orig], expected_dtx_ids[i])

        # Verify exact DTX channels placed
        ch_by_orig = {n.original_wav_id: n.channel for n in plan.tab_placed_notes}
        self.assertEqual(ch_by_orig[orig_ids[0]], "1A")  # LC
        self.assertEqual(ch_by_orig[orig_ids[1]], "11")  # HH Close
        self.assertEqual(ch_by_orig[orig_ids[2]], "18")  # HH Open
        self.assertEqual(ch_by_orig[orig_ids[3]], "1B")  # LP
        self.assertEqual(ch_by_orig[orig_ids[4]], "1C")  # LBD
        self.assertEqual(ch_by_orig[orig_ids[5]], "12")  # SD
        self.assertEqual(ch_by_orig[orig_ids[6]], "14")  # HT
        self.assertEqual(ch_by_orig[orig_ids[7]], "13")  # BD
        self.assertEqual(ch_by_orig[orig_ids[8]], "15")  # LT
        self.assertEqual(ch_by_orig[orig_ids[9]], "17")  # FT
        self.assertEqual(ch_by_orig[orig_ids[10]], "16")  # CY
        self.assertEqual(ch_by_orig[orig_ids[11]], "19")  # RD
        self.assertEqual(ch_by_orig[orig_ids[12]], "66")  # Other -> SE6
        self.assertIn(ch_by_orig[orig_ids[13]], ("71", "72"))  # Guitar -> SE11-12
        self.assertIn(ch_by_orig[orig_ids[14]], ("73", "74"))  # Bass -> SE13-14

        # Verify #DTXC_LANEBINDEDCHIP uses RENUMBERED DTX WAV IDs
        self.assertEqual(
            plan.lane_bind_lines,
            [
                f"#DTXC_LANEBINDEDCHIP: 01 {plan.wav_id_map[orig_ids[0]]} 00",
                f"#DTXC_LANEBINDEDCHIP: 02 {plan.wav_id_map[orig_ids[1]]} {plan.wav_id_map[orig_ids[2]]}",
                f"#DTXC_LANEBINDEDCHIP: 03 00 {plan.wav_id_map[orig_ids[4]]}",
                f"#DTXC_LANEBINDEDCHIP: 04 {plan.wav_id_map[orig_ids[5]]} 00",
                f"#DTXC_LANEBINDEDCHIP: 06 {plan.wav_id_map[orig_ids[7]]} 00",
                f"#DTXC_LANEBINDEDCHIP: 09 {plan.wav_id_map[orig_ids[10]]} 00",
            ],
        )

    def test_toggle_on_renumbering_order_tabs_then_slicer_then_rest(self) -> None:
        chart, orig_ids = self._make_15tab_chart()
        # orig_ids[16], orig_ids[17] are in B3; orig_ids[0] in LC; orig_ids[15] in B2
        cfg = MappingConfig(
            tab_wavs={TabId.LC: [orig_ids[0]], TabId.BD: [orig_ids[7]]},
            toggle_on=True,
            slicer_bgm_columns=[3],  # B3 has orig_ids[16], orig_ids[17]
        )
        plan = build_mapping_plan(chart, cfg)
        self.assertTrue(plan.effective_toggle_on)
        # Toggle=ON starts at 01: LC -> BD -> SE1 (B3: orig_ids[16], orig_ids[17]) -> remaining
        self.assertEqual(plan.wav_id_map[orig_ids[0]], "01")
        self.assertEqual(plan.wav_id_map[orig_ids[7]], "02")
        self.assertEqual(plan.wav_id_map[orig_ids[16]], "03")
        self.assertEqual(plan.wav_id_map[orig_ids[17]], "04")

    def test_duplicate_wav_across_tabs_rejected(self) -> None:
        chart, orig_ids = self._make_15tab_chart()
        cfg = MappingConfig(
            tab_wavs={TabId.SD: [orig_ids[0]], TabId.BD: [orig_ids[0]]},
            toggle_on=False,
        )
        with self.assertRaises(ValueError):
            build_mapping_plan(chart, cfg)

    def test_guitar_and_bass_dual_channel_polyphony(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for fn in ("g1.wav", "g2.wav", "b1.wav", "b2.wav"):
                (root / fn).write_bytes(b"dummy")
            bms = root / "gb.bms"
            bms.write_text(
                "\n".join(
                    [
                        "#TITLE GB",
                        "#BPM 120",
                        "#WAV01 g1.wav",
                        "#WAV02 g2.wav",
                        "#WAV03 b1.wav",
                        "#WAV04 b2.wav",
                        # Both guitar notes and both bass notes hit simultaneously at m1 pos 0!
                        "#00101:01",
                        "#00101:02",
                        "#00101:03",
                        "#00101:04",
                    ]
                ),
                encoding="utf-8",
            )
            chart = parse_bms(bms)
            plan = build_mapping_plan(
                chart,
                MappingConfig(
                    tab_wavs={TabId.GUITAR: ["01", "02"], TabId.BASS: ["03", "04"]},
                    toggle_on=False,
                ),
            )
            guitar_chs = {n.channel for n in plan.tab_placed_notes if n.original_wav_id in ("01", "02")}
            bass_chs = {n.channel for n in plan.tab_placed_notes if n.original_wav_id in ("03", "04")}
            self.assertEqual(guitar_chs, {"71", "72"})
            self.assertEqual(bass_chs, {"73", "74"})

    def test_lbd_hidden_syncs_bd_default_bind_to_lane_03_and_separate_lbd_overrides(self) -> None:
        chart, orig_ids = self._make_15tab_chart()
        # 1. Default separate_lbd=False: BD bound -> automatically populates lane 03's LBD sub-slot!
        cfg_hidden = MappingConfig(
            tab_wavs={
                TabId.LP: [orig_ids[3]],
                TabId.BD: [orig_ids[7]],
            },
            default_lane_binds={
                TabId.BD: orig_ids[7],
            },
            toggle_on=False,
            separate_lbd=False,
        )
        plan_hidden = build_mapping_plan(chart, cfg_hidden)
        bd_dtx = plan_hidden.wav_id_map[orig_ids[7]]
        self.assertEqual(
            plan_hidden.lane_bind_lines,
            [
                f"#DTXC_LANEBINDEDCHIP: 03 00 {bd_dtx}",
                f"#DTXC_LANEBINDEDCHIP: 06 {bd_dtx} 00",
            ],
        )

        # When LP is also bound and separate_lbd=False, lane 03 gets <LP_dtx> <BD_dtx>
        cfg_lp_bd = MappingConfig(
            tab_wavs={
                TabId.LP: [orig_ids[3]],
                TabId.BD: [orig_ids[7]],
            },
            default_lane_binds={
                TabId.LP: orig_ids[3],
                TabId.BD: orig_ids[7],
            },
            toggle_on=False,
            separate_lbd=False,
        )
        plan_lp_bd = build_mapping_plan(chart, cfg_lp_bd)
        lp_dtx = plan_lp_bd.wav_id_map[orig_ids[3]]
        bd_dtx2 = plan_lp_bd.wav_id_map[orig_ids[7]]
        self.assertEqual(
            plan_lp_bd.lane_bind_lines,
            [
                f"#DTXC_LANEBINDEDCHIP: 03 {lp_dtx} {bd_dtx2}",
                f"#DTXC_LANEBINDEDCHIP: 06 {bd_dtx2} 00",
            ],
        )

        # 2. separate_lbd=True: BD bind is NOT synced to lane 03 when LBD is unbound
        cfg_sep_unbound = MappingConfig(
            tab_wavs={TabId.BD: [orig_ids[7]]},
            default_lane_binds={TabId.BD: orig_ids[7]},
            toggle_on=False,
            separate_lbd=True,
        )
        plan_sep_unbound = build_mapping_plan(chart, cfg_sep_unbound)
        self.assertEqual(
            plan_sep_unbound.lane_bind_lines,
            [f"#DTXC_LANEBINDEDCHIP: 06 {plan_sep_unbound.wav_id_map[orig_ids[7]]} 00"],
        )

        # When separate_lbd=True and LBD has its own slice + bind, outputs to 1C and binds lane 03 sub-slot
        cfg_sep_bound = MappingConfig(
            tab_wavs={
                TabId.LBD: [orig_ids[4]],
                TabId.BD: [orig_ids[7]],
            },
            default_lane_binds={
                TabId.LBD: orig_ids[4],
                TabId.BD: orig_ids[7],
            },
            toggle_on=False,
            separate_lbd=True,
        )
        plan_sep_bound = build_mapping_plan(chart, cfg_sep_bound)
        lbd_dtx = plan_sep_bound.wav_id_map[orig_ids[4]]
        bd_dtx3 = plan_sep_bound.wav_id_map[orig_ids[7]]
        self.assertEqual(
            plan_sep_bound.lane_bind_lines,
            [
                f"#DTXC_LANEBINDEDCHIP: 03 00 {lbd_dtx}",
                f"#DTXC_LANEBINDEDCHIP: 06 {bd_dtx3} 00",
            ],
        )
        ch_by_orig = {n.original_wav_id: n.channel for n in plan_sep_bound.tab_placed_notes}
        self.assertEqual(ch_by_orig[orig_ids[4]], "1C")

    def test_forced_toggle_off_merges_physical_siblings_and_deduplicates_capacity(self) -> None:
        from core.constants import BASE62_DIGITS

        def _b62(n: int) -> str:
            return BASE62_DIGITS[n // 62] + BASE62_DIGITS[n % 62]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "Shared_Celesta.wav").write_bytes(b"dummy")
            for i in range(1, 1295):
                (root / f"f{i}.wav").write_bytes(b"dummy")

            # Build a #BASE 62 chart with >1295 #WAVxx definitions so chart.toggle_off_locked is True,
            # where 3 different #WAVxx IDs (with mixed casing and slashes) point to the same physical file,
            # and the user maps 1294 unique physical files (1296 raw #WAV IDs) while requesting toggle_on=True.
            lines = ["#TITLE ForcedDedup", "#BASE 62", "#BPM 120"]
            lines.append("#WAVa1 sub/Shared_Celesta.wav")
            lines.append(r"#WAVa2 SUB\shared_celesta.WAV")
            lines.append("#WAVa3 sub/SHARED_CELESTA.wav")
            mapped_ids = ["a1"]
            for i in range(1, 1294):
                wid = _b62(100 + i)
                lines.append(f"#WAV{wid} f{i}.wav")
                mapped_ids.append(wid)
            # Add extra unmapped #WAV definitions so total > 1295 -> toggle_off_locked = True
            for j in range(1, 10):
                wid = _b62(1500 + j)
                lines.append(f"#WAV{wid} f1.wav")
            lines.append("#00101:a1a2a3")
            bms = root / "forced_dedup.bms"
            bms.write_text("\n".join(lines), encoding="utf-8")
            chart = parse_bms(bms)
            self.assertTrue(chart.toggle_off_locked)

            # Request toggle_on=True with only "a1" specified alongside 1293 other unique files:
            # Rule 5 forces effective_toggle_on=False, merges "a2" and "a3" into OTHER (1296 raw IDs,
            # but 1294 deduplicated physical files <= 1294 Toggle=OFF limit!).
            plan = build_mapping_plan(
                chart,
                MappingConfig(
                    tab_wavs={TabId.OTHER: mapped_ids},
                    toggle_on=True,
                ),
            )
            self.assertFalse(plan.effective_toggle_on)
            self.assertTrue(plan.toggle_off_forced)
            self.assertEqual(len(plan.ordered_retained_orig_ids), 1294)
            self.assertEqual(plan.wav_id_map["a1"], "02")
            self.assertEqual(plan.wav_id_map["a2"], "02")
            self.assertEqual(plan.wav_id_map["a3"], "02")
            self.assertEqual(plan.unmapped_events, [])
            self.assertEqual(len(plan.tab_placed_notes), 3)


class TestSeAllocatorSlicerCollapseAndOverflow(unittest.TestCase):
    def test_collapse_pool_releases_guitar_and_bass_when_empty(self) -> None:
        prim_both_empty, backup = get_collapse_pools(guitar_enabled=False, bass_enabled=False)
        self.assertEqual(len(prim_both_empty), 20)
        self.assertIn("71", prim_both_empty)
        self.assertIn("72", prim_both_empty)
        self.assertIn("73", prim_both_empty)
        self.assertIn("74", prim_both_empty)
        self.assertEqual(backup, COLLAPSE_BACKUP_CHANNELS)

        prim_both_used, _ = get_collapse_pools(guitar_enabled=True, bass_enabled=True)
        self.assertEqual(len(prim_both_used), 16)
        for ch in ("71", "72", "73", "74"):
            self.assertNotIn(ch, prim_both_used)

    def test_slicer_max_5_columns_1_to_1_no_collapse(self) -> None:
        events = [
            NoteEvent(1, Fraction(0), "01", f"0{c}", bgm_column=c, track_label=f"B{c}", time_sec=2.0, order_index=c)
            for c in range(1, 6)
        ]
        wav_map = {f"0{c}": f"0{c}" for c in range(1, 6)}

        res = allocate_se_channels(events, wav_map, slicer_bgm_columns=[1, 2, 3, 4, 5])
        self.assertEqual(
            [res.slicer_channel_map[c] for c in range(1, 6)],
            list(SLICER_SE_CHANNELS),
        )
        self.assertEqual([n.channel for n in res.placed_notes], list(SLICER_SE_CHANNELS))

        # Selecting 6 columns MUST fail!
        with self.assertRaises(ValueError):
            allocate_se_channels(events, wav_map, slicer_bgm_columns=[1, 2, 3, 4, 5, 6])

    def test_collapse_min_interference_longest_gap_and_backup_pool_last(self) -> None:
        # 20 simultaneous notes at t=1.0 -> must use ONLY primary pool (0 backup channels!)
        events = []
        wav_map = {}
        for i in range(20):
            wid = int_to_base36(i + 1)
            wav_map[wid] = wid
            events.append(
                NoteEvent(1, Fraction(0), "01", wid, bgm_column=i + 1, time_sec=1.0, order_index=i)
            )

        res20 = allocate_se_channels(events, wav_map, guitar_enabled=False, bass_enabled=False)
        chs20 = {n.channel for n in res20.placed_notes}
        self.assertEqual(len(chs20), 20)
        self.assertTrue(chs20.isdisjoint(set(COLLAPSE_BACKUP_CHANNELS)))

        # Add a 21st simultaneous note at t=1.0 -> NOW backup pool SE24 ("84") is engaged!
        wid21 = int_to_base36(21)
        wav_map[wid21] = wid21
        events.append(NoteEvent(1, Fraction(0), "01", wid21, bgm_column=21, time_sec=1.0, order_index=20))
        res21 = allocate_se_channels(events, wav_map, guitar_enabled=False, bass_enabled=False)
        chs21 = {n.channel for n in res21.placed_notes}
        self.assertIn("84", chs21)
        self.assertTrue(any(w.code == "SE_BACKUP_POOL_USED" for w in res21.warnings))

        # Verify longest-gap selection across time on sequential notes:
        # Note at t=1.0 -> ch A (67), note at t=2.0 -> untouched ch B (68), ...,
        # After all 20 primary channels used once at t=1..20, note at t=21.0 MUST pick ch 67 (oldest t=1.0)!
        seq_events = [
            NoteEvent(1 + i, Fraction(0), "01", "01", bgm_column=1, time_sec=float(i + 1), order_index=i)
            for i in range(21)
        ]
        res_seq = allocate_se_channels(seq_events, {"01": "01"}, guitar_enabled=False, bass_enabled=False)
        # First 20 notes spread across all 20 untouched primary channels; 21st reuses the oldest (67)
        self.assertEqual(len({n.channel for n in res_seq.placed_notes[:20]}), 20)
        self.assertEqual(res_seq.placed_notes[20].channel, res_seq.placed_notes[0].channel)

    def test_overflow_fail_nudge_192_and_fallback_toggle_off(self) -> None:
        # 28 simultaneous unmapped notes at measure 1 pos 0 (26 total Collapse channels when Guitar+Bass empty!)
        events = []
        wav_map = {}
        for i in range(28):
            wid = int_to_base36(i + 1)
            wav_map[wid] = wid
            events.append(
                NoteEvent(1, Fraction(0), "01", wid, bgm_column=i + 1, time_sec=2.0, order_index=i)
            )

        # 1. Strategy "fail" raises SeCollapseOverflowError with 2 overflowing events
        with self.assertRaises(SeCollapseOverflowError) as ctx:
            allocate_se_channels(events, wav_map, overflow_strategy="fail")
        self.assertEqual(len(ctx.exception.overflow_events), 2)

        # 2. Strategy "nudge_192" places all 28 legally using +1/192 nudge, NO SE33+!
        res_nudge = allocate_se_channels(events, wav_map, overflow_strategy="nudge_192")
        self.assertEqual(len(res_nudge.placed_notes), 28)
        self.assertEqual(res_nudge.nudged_count, 2)
        self.assertTrue(any(w.code == "SE_OVERFLOW_NUDGED_192" for w in res_nudge.warnings))
        for n in res_nudge.placed_notes:
            self.assertIn(n.channel, ALL_SE_CHANNELS)
            self.assertIn(n.channel, LEGAL_DTX_NOTE_CHANNELS)
        nudged_notes = [n for n in res_nudge.placed_notes if n.nudged_192 != 0]
        self.assertEqual(len(nudged_notes), 2)
        for n in nudged_notes:
            self.assertEqual(n.pos, Fraction(1, 192))

        # 3. Strategy "fallback_toggle_off" signals clean Toggle=OFF fallback
        res_fb = allocate_se_channels(events, wav_map, overflow_strategy="fallback_toggle_off")
        self.assertTrue(res_fb.fallback_to_toggle_off)
        self.assertTrue(any(w.code == "SE_COLLAPSE_FALLBACK_TOGGLE_OFF" for w in res_fb.warnings))

    def test_nudge_192_at_measure_999_uses_negative_shift_instead_of_measure_1000(self) -> None:
        events = [
            NoteEvent(999, Fraction(191, 192), "01", int_to_base36(i + 1), bgm_column=i + 1, time_sec=100.0, order_index=i)
            for i in range(27)
        ]
        wav_map = {int_to_base36(i + 1): int_to_base36(i + 1) for i in range(27)}
        res = allocate_se_channels(events, wav_map, overflow_strategy="nudge_192")
        self.assertEqual(len(res.placed_notes), 27)
        self.assertEqual(res.nudged_count, 1)
        self.assertLessEqual(max(n.measure for n in res.placed_notes), 999)
        nudged = [n for n in res.placed_notes if n.nudged_192 != 0]
        self.assertEqual(len(nudged), 1)
        self.assertEqual((nudged[0].measure, nudged[0].pos, nudged[0].nudged_192), (999, Fraction(190, 192), -1))

    def test_mapping_warns_undefined_wav_and_validates_slicer_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.wav").write_bytes(b"dummy")
            bms = root / "m.bms"
            bms.write_text("#TITLE M\n#BPM 120\n#WAV01 a.wav\n#00101:01\n", encoding="utf-8")
            chart = parse_bms(bms)

            plan = build_mapping_plan(chart, MappingConfig(tab_wavs={TabId.BD: ["ZZ"]}, toggle_on=False))
            self.assertTrue(any(w.code == "MAPPED_UNDEFINED_WAV" for w in plan.warnings))

            with self.assertRaises(ValueError):
                build_mapping_plan(chart, MappingConfig(toggle_on=True, slicer_bgm_columns=[1, 2, 3, 4, 5, 6]))

    def test_tab_collision_priority_and_reflow_toggle_off_and_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for idx in range(1, 10):
                (root / f"w{idx}.wav").write_bytes(b"dummy")
            bms = root / "col.bms"
            bms.write_text(
                "\n".join(
                    [
                        "#TITLE CollisionPriority",
                        "#BPM 120",
                        *[f"#WAV0{idx} w{idx}.wav" for idx in range(1, 10)],
                        # SD conflict at M001 pos 0:
                        #   B1 has non-default 01 (earlier order_index), B2 has default-bound 02 -> default 02 wins over 01!
                        "#00101:01",
                        "#00101:02",
                        # BD conflict at M002 pos 0:
                        #   B1 has default-bound 03, playable ch 11 has non-default 04 -> playable 04 wins!
                        "#00201:03",
                        "#00211:04",
                        # Other (SE6) conflict at M003 pos 0: 05 and 06 on B1/B2 -> 05 wins, 06 displaced
                        "#00301:05",
                        "#00301:06",
                        # Guitar (SE11-12) 3 simultaneous notes at M004 pos 0: 07, 08, 09 -> 07, 08 win, 09 displaced
                        "#00401:07",
                        "#00401:08",
                        "#00401:09",
                    ]
                ),
                encoding="utf-8",
            )
            chart = parse_bms(bms)
            cfg_off = MappingConfig(
                tab_wavs={
                    TabId.SD: ["01", "02"],
                    TabId.BD: ["03", "04"],
                    TabId.OTHER: ["05", "06"],
                    TabId.GUITAR: ["07", "08", "09"],
                },
                default_lane_binds={TabId.SD: "02", TabId.BD: "03"},
                toggle_on=False,
            )
            plan_off = build_mapping_plan(chart, cfg_off)

            placed_by_measure = {n.measure: n for n in plan_off.tab_placed_notes if n.measure in (1, 2, 3)}
            self.assertEqual(placed_by_measure[1].original_wav_id, "02")  # default bound beat BGM order
            self.assertEqual(placed_by_measure[2].original_wav_id, "04")  # playable beat BGM default
            self.assertEqual(placed_by_measure[3].original_wav_id, "05")  # order_index tiebreak

            guitar_winners = [n.original_wav_id for n in plan_off.tab_placed_notes if n.measure == 4]
            self.assertEqual(guitar_winners, ["07", "08"])

            # Toggle=OFF: displaced 01, 03, 06, 09 reflowed into unmapped_events for bgm.ogg
            displaced_ids = [ev.wav_id for ev in plan_off.tab_collision_events]
            self.assertEqual(displaced_ids, ["01", "03", "06", "09"])
            unmapped_ids = {ev.wav_id for ev in plan_off.unmapped_events}
            self.assertTrue({"01", "03", "06", "09"}.issubset(unmapped_ids))

            col_warn = [w for w in plan_off.warnings if w.code == "TAB_LANE_COLLISION"][0]
            self.assertIn("M001 SD(12) #WAV01", col_warn.message)
            self.assertIn("M002 BD(13) #WAV03", col_warn.message)
            self.assertIn("M003 OTHER(66) #WAV06", col_warn.message)
            self.assertIn("M004 GUITAR(71/72) #WAV09", col_warn.message)
            self.assertIn("bgm.ogg", col_warn.message)

            # Toggle=ON: displaced collisions bypass Slicer SE1..SE5 (even if col 1 is sliced!)
            # and route into the non-interrupting Collapse pool!
            cfg_on = MappingConfig(
                tab_wavs=cfg_off.tab_wavs,
                default_lane_binds=cfg_off.default_lane_binds,
                toggle_on=True,
                slicer_bgm_columns=[1],
            )
            plan_on = build_mapping_plan(chart, cfg_on)
            se_res = allocate_se_channels(
                plan_on.unmapped_events,
                plan_on.wav_id_map,
                slicer_bgm_columns=cfg_on.slicer_bgm_columns,
                guitar_enabled=plan_on.guitar_enabled,
                bass_enabled=plan_on.bass_enabled,
                extra_collapse_events=plan_on.tab_collision_events,
            )
            rerouted = {
                (n.measure, n.original_wav_id): n.channel
                for n in se_res.placed_notes
            }
            for key in ((1, "01"), (2, "03"), (3, "06"), (4, "09")):
                self.assertIn(key, rerouted)
                self.assertNotIn(rerouted[key], SLICER_SE_CHANNELS)
                self.assertIn(rerouted[key], ALL_SE_CHANNELS)
                self.assertNotIn(rerouted[key], ("71", "72"))  # Guitar active -> SE11-12 excluded


if __name__ == "__main__":
    unittest.main()
