"""Tests for BMS parsing, B1-Bn :00 alignment, empty track pruning, #BASE 62, #STOP, #RANDOM, LN."""

from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from core.constants import BASE62_DIGITS, TabId
from core.mapping import WavLimitExceededError, build_mapping_plan
from core.models import MappingConfig
from core.parser import parse_bms


def _write_bms(root: Path, name: str, content: str, encoding: str = "cp932") -> Path:
    p = root / name
    p.write_bytes(content.encode(encoding))
    return p


class TestBgmAlignmentAndPruning(unittest.TestCase):
    def test_preserve_00_placeholders_and_prune_all_empty_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for fn in ("a.ogg", "b.ogg", "c.ogg"):
                (root / fn).write_bytes(b"dummy")

            bms = _write_bms(
                root,
                "align.bms",
                "\n".join(
                    [
                        "#TITLE AlignTest",
                        "#BPM 120",
                        "#WAV01 a.wav",
                        "#WAV02 b.wav",
                        "#WAV03 c.wav",
                        # Measure 001: B1=00, B2=00, B3=01, B4=00
                        "#00101:00",
                        "#00101:00",
                        "#00101:0100",
                        "#00101:00",
                        # Measure 002: B1=02, B2=00, B3=00, B4=03
                        "#00201:02",
                        "#00201:00",
                        "#00201:00",
                        "#00201:0003",
                    ]
                ),
            )
            chart = parse_bms(bms)

            self.assertEqual(chart.max_bgm_columns_raw, 4)
            # B2 is empty across the whole song -> pruned! B1, B3, B4 keep true labels!
            self.assertEqual(chart.pruned_empty_tracks, ["B2"])
            self.assertEqual([t.track_label for t in chart.bgm_tracks], ["B1", "B3", "B4"])

            ev_by_col = {ev.bgm_column: (ev.measure, ev.pos, ev.wav_id) for ev in chart.events}
            self.assertEqual(ev_by_col[1], (2, Fraction(0), "02"))
            self.assertEqual(ev_by_col[3], (1, Fraction(0), "01"))
            self.assertEqual(ev_by_col[4], (2, Fraction(1, 2), "03"))

            # Suffix auto-correction occurred in memory
            self.assertEqual(chart.wavs["01"].resolved_name, "a.ogg")
            self.assertTrue(chart.wavs["01"].suffix_corrected)


class TestLongNotesAndIgnoredChannels(unittest.TestCase):
    def test_lntype1_start_end_and_lnobj_and_invisible_mines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for fn in ("s1.wav", "s2.wav", "ln.wav"):
                (root / fn).write_bytes(b"dummy")

            bms = _write_bms(
                root,
                "ln.bms",
                "\n".join(
                    [
                        "#TITLE LNTest",
                        "#BPM 120",
                        "#LNTYPE 1",
                        "#LNOBJ ZZ",
                        "#WAV01 s1.wav",
                        "#WAV02 s2.wav",
                        "#WAV03 ln.wav",
                        # Normal note + #LNOBJ ZZ end marker on channel 11
                        "#00111:01ZZ",
                        # LNTYPE 1 start in measure 1, end in measure 2 on channel 51
                        "#00151:0003",
                        "#00251:0300",
                        # Invisible channel 31, mine channel D1, BGA channel 04 -> ignored
                        "#00131:02",
                        "#001D1:02",
                        "#00104:01",
                    ]
                ),
            )
            chart = parse_bms(bms)

            # Only 2 playable notes: 01 at m1 pos 0, and 03 at m1 pos 1/2
            self.assertEqual(len(chart.events), 2)
            self.assertEqual(
                [(e.measure, e.pos, e.channel, e.wav_id) for e in chart.events],
                [
                    (1, Fraction(0), "11", "01"),
                    (1, Fraction(1, 2), "51", "03"),
                ],
            )
            # 2 LN end markers recorded separately (not playable)
            self.assertEqual(len(chart.ln_end_events), 2)
            self.assertTrue(all(e.is_ln_end for e in chart.ln_end_events))


class TestBase62DetectionAndToggleOffLock(unittest.TestCase):
    def test_explicit_base62_preserves_case_and_locks_toggle_off_over_1295(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = ["#TITLE Base62Heavy", "#BPM 140", "#BASE 62"]

            # Create 1300 distinct base-62 WAV IDs (> 1295 DTX base-36 limit!)
            ids = []
            for idx in range(1, 1301):
                hi, lo = divmod(idx, 62)
                wid = BASE62_DIGITS[hi] + BASE62_DIGITS[lo]
                ids.append(wid)
                lines.append(f"#WAV{wid} slice_{idx:04d}.wav")

            # Verify case distinction: both '0A' (10) and '0a' (36) exist
            self.assertIn("0A", ids)
            self.assertIn("0a", ids)

            # Place notes referencing both uppercase and lowercase base-62 IDs
            lines.append("#00101:0A0a")
            lines.append(f"#00211:{ids[-1]}00")

            bms = _write_bms(root, "b62.bms", "\n".join(lines))
            chart = parse_bms(bms)

            self.assertEqual(chart.base, 62)
            self.assertTrue(chart.is_base62)
            self.assertTrue(chart.exceeds_dtx_wav_limit)
            self.assertTrue(chart.toggle_off_locked)
            self.assertIn("0A", chart.wavs)
            self.assertIn("0a", chart.wavs)
            self.assertNotEqual(chart.wavs["0A"].declared_name, chart.wavs["0a"].declared_name)
            self.assertTrue(any(w.code == "BASE62_DETECTED" for w in chart.warnings))
            self.assertTrue(any(w.code == "WAV_LIMIT_EXCEEDED_LOCK_TOGGLE_OFF" for w in chart.warnings))

            # Even if caller asks for toggle_on=True, mapping MUST force Toggle=OFF!
            cfg = MappingConfig(
                tab_wavs={
                    TabId.BD: ["0A", "0a"],
                    TabId.SD: [ids[-1]],
                },
                toggle_on=True,
            )
            plan = build_mapping_plan(chart, cfg)
            self.assertFalse(plan.effective_toggle_on)
            self.assertTrue(plan.toggle_off_forced)
            self.assertTrue(any(w.code == "TOGGLE_OFF_FORCED" for w in plan.warnings))

            # Renumbered to legal uppercase base-36 02..04 (01 reserved for bgm.ogg)
            self.assertEqual(plan.wav_id_map["0A"], "03")  # SD comes before BD in TAB_ORDER!
            self.assertEqual(plan.wav_id_map["0a"], "04")
            self.assertEqual(plan.wav_id_map[ids[-1]], "02")  # SD is Tab 6, BD is Tab 8

            # Mapping > 1294 slices in Toggle=OFF raises WavLimitExceededError
            too_many_cfg = MappingConfig(
                tab_wavs={TabId.OTHER: ids[:1295]},
                toggle_on=False,
            )
            with self.assertRaises(WavLimitExceededError):
                build_mapping_plan(chart, too_many_cfg)

    def test_implicit_base62_case_collision_detected_without_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bms = _write_bms(
                root,
                "implicit62.bms",
                "\n".join(
                    [
                        "#TITLE Implicit62",
                        "#BPM 120",
                        "#WAV0A upper.wav",
                        "#WAV0a lower.wav",
                        "#00101:0A0a",
                    ]
                ),
            )
            chart = parse_bms(bms)
            self.assertTrue(chart.is_base62)
            self.assertEqual(chart.base, 62)
            self.assertEqual(chart.wavs["0A"].declared_name, "upper.wav")
            self.assertEqual(chart.wavs["0a"].declared_name, "lower.wav")

    def test_uniform_lowercase_base36_not_misclassified_as_base62(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bms = _write_bms(
                root,
                "lower36.bms",
                "\n".join(
                    [
                        "#TITLE Lower36",
                        "#BPM 120",
                        "#WAV0a kick.wav",
                        "#WAV0b snare.wav",
                        "#00101:0a0b",
                    ]
                ),
            )
            chart = parse_bms(bms)
            self.assertFalse(chart.is_base62)
            self.assertEqual(chart.base, 36)
            self.assertIn("0A", chart.wavs)
            self.assertIn("0B", chart.wavs)
            self.assertEqual([e.wav_id for e in chart.events], ["0A", "0B"])

    def test_mixed_case_base36_and_explicit_base36_not_misclassified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "kick.wav").write_bytes(b"dummy")
            # Legal case-insensitive Base-36: uppercase #WAV0A referenced as lowercase 0a
            bms1 = _write_bms(
                root,
                "mixed36.bms",
                "#TITLE Mixed36\n#BPM 120\n#WAV0A kick.wav\n#00101:0a\n",
            )
            c1 = parse_bms(bms1)
            self.assertEqual(c1.base, 36)
            self.assertFalse(c1.is_base62)
            self.assertIn("0A", c1.wavs)
            self.assertEqual([e.wav_id for e in c1.events], ["0A"])
            self.assertEqual(c1.wavs["0A"].use_count, 1)
            self.assertFalse(any(w.code == "BASE62_DETECTED" for w in c1.warnings))
            self.assertFalse(any(w.code == "UNDEFINED_WAV_REFERENCE" for w in c1.warnings))

            # Explicit #BASE 36 MUST stay Base-36 even with mixed header/data casing
            bms2 = _write_bms(
                root,
                "exp36.bms",
                "#TITLE Exp36\n#BASE 36\n#BPM 120\n#WAV0A kick.wav\n#WAV0b kick.wav\n#00101:0a0B\n",
            )
            c2 = parse_bms(bms2)
            self.assertEqual(c2.base, 36)
            self.assertFalse(c2.is_base62)
            self.assertEqual([e.wav_id for e in c2.events], ["0A", "0B"])
            self.assertFalse(any(w.code == "UNDEFINED_WAV_REFERENCE" for w in c2.warnings))

    def test_lntype2_gaps_cross_measure_and_exbpm_and_non_finite_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s.wav").write_bytes(b"dummy")
            bms = _write_bms(
                root,
                "ln2_exbpm.bms",
                "\n".join(
                    [
                        "#TITLE LN2ExBpm",
                        "#BPM inf",
                        "#PLAYLEVEL inf",
                        "#EXBPM01 240",
                        "#BPM02 inf",
                        "#LNTYPE 2",
                        "#WAV00 reserved.wav",
                        "#WAV03 s.wav",
                        "#WAV04 s.wav",
                        "#00102:inf",
                        "#00108:01",
                        "#00111:00",  # all-empty play channel -> pruned
                        "#00151:0303000303",  # 2 separate holds with 00 gap
                        "#00551:0303",  # 3rd separate hold after empty measures
                        "#00152:0004",  # cross-measure hold starts m1 pos 1/2
                        "#00252:0400",  # continues seamlessly into m2 pos 0
                        "#09004:01",  # trailing BGA at m90 must NOT bloat max_measure
                    ]
                ),
            )
            chart = parse_bms(bms)
            codes = {w.code for w in chart.warnings}
            self.assertIn("INVALID_INITIAL_BPM", codes)
            self.assertIn("INVALID_EXBPM", codes)
            self.assertIn("INVALID_MEASURE_LENGTH", codes)
            self.assertIn("RESERVED_ID_00_IGNORED", codes)
            self.assertIn("LNTYPE_2_APPROXIMATED", codes)
            self.assertNotIn("00", chart.wavs)
            self.assertEqual(chart.initial_bpm, 130.0)
            self.assertIsNone(chart.playlevel)
            self.assertEqual(chart.ex_bpms.get("01"), 240.0)
            self.assertEqual(len(chart.bpm_changes), 1)
            self.assertEqual(chart.bpm_changes[0].bpm, 240.0)
            # 3 starts on ch 51 + 1 start on ch 52 (cross-measure hold is 1 start)
            starts_51 = [(e.measure, e.pos) for e in chart.events if e.channel == "51"]
            starts_52 = [(e.measure, e.pos) for e in chart.events if e.channel == "52"]
            self.assertEqual(starts_51, [(1, Fraction(0)), (1, Fraction(3, 5)), (5, Fraction(0))])
            self.assertEqual(starts_52, [(1, Fraction(1, 2))])
            # Trailing BGA at measure 90 must NOT bloat max_measure past measure 5!
            self.assertEqual(chart.max_measure, 5)
            # Empty play channel 11 is pruned
            self.assertIn("11", chart.pruned_empty_tracks)


class TestStopAndRandomWarnings(unittest.TestCase):
    def test_stop_and_random_emit_clear_warnings_and_no_double_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bms = _write_bms(
                root,
                "warn.bms",
                "\n".join(
                    [
                        "#TITLE WarnChart",
                        "#BPM 120",
                        "#WAV01 a.wav",
                        "#WAV02 b.wav",
                        "#STOP01 48",
                        "#RANDOM 2",
                        "#IF 1",
                        "#00111:01",
                        "#ENDIF",
                        "#IF 2",
                        "#00111:02",
                        "#ENDIF",
                        "#00209:01",
                    ]
                ),
            )
            chart = parse_bms(bms)
            codes = {w.code for w in chart.warnings}
            self.assertIn("STOP_UNSUPPORTED", codes)
            self.assertIn("UNEXPANDED_RANDOM_BRANCH", codes)
            # Only branch 1 (#IF 1 -> 01) should be active, NOT branch 2 (02)!
            self.assertEqual(len(chart.events), 1)
            self.assertEqual(chart.events[0].wav_id, "01")

    def test_nested_random_in_inactive_if_or_switch_and_sequential_if_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bms = _write_bms(
                root,
                "complex_cond.bms",
                "\n".join(
                    [
                        "#TITLE ComplexCond",
                        "#BPM 120",
                        "#WAV01 a.wav",
                        "#WAV02 b.wav",
                        "#WAV03 c.wav",
                        "#WAV04 d.wav",
                        # 1. Nested #RANDOM inside inactive #IF 2 must NOT suppress lines after #ENDIF
                        "#RANDOM 2",
                        "#IF 2",
                        "  #RANDOM 2",
                        "  #IF 1",
                        "    #00111:01",
                        "  #ENDIF",
                        "#ENDIF",
                        "#00211:02",
                        # 2. Sequential #IF 1 .. #ENDIF followed by #IF 2 .. #ELSE .. #ENDIF
                        "#IF 1",
                        "  #00311:01",
                        "#ENDIF",
                        "#IF 2",
                        "  #00411:02",
                        "#ELSE",
                        "  #00411:03",
                        "#ENDIF",
                        # 3. #RANDOM inside inactive #SWITCH #CASE 2 + #SKIP
                        "#SWITCH 2",
                        "#CASE 2",
                        "  #RANDOM 2",
                        "  #IF 1",
                        "    #00511:01",
                        "  #ENDIF",
                        "#SKIP",
                        "#CASE 1",
                        "  #00611:04",
                        "#SKIP",
                        "#ENDSW",
                        "#00711:04",
                    ]
                ),
            )
            chart = parse_bms(bms)
            self.assertEqual(
                [(e.measure, e.wav_id) for e in chart.events],
                [(2, "02"), (3, "01"), (4, "03"), (6, "04"), (7, "04")],
            )

    def test_pms_9key_18_channels_and_bml_lntype1_lntype2_lnobj_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1) .pms covering all 11-19 & 21-29 (18 visible channels)
            lines = ["#TITLE PMS9Key", "#BPM 120", "#WAV01 a.wav", "#WAV02 b.wav"]
            channels_18 = [f"1{i}" for i in range(1, 10)] + [f"2{i}" for i in range(1, 10)]
            for idx, ch in enumerate(channels_18):
                wid = "01" if idx < 9 else "02"
                lines.append(f"#001{ch}:{wid}")
            pms = root / "nine_key.pms"
            pms.write_text("\n".join(lines), encoding="utf-8")
            chart_pms = parse_bms(pms)
            self.assertEqual({e.channel for e in chart_pms.events}, set(channels_18))
            self.assertEqual(chart_pms.wavs["01"].use_count, 9)
            self.assertEqual(chart_pms.wavs["02"].use_count, 9)

            # 2) .bml #LNTYPE 1 + #LNOBJ: LN start counted, LN end & LNOBJ end silent & excluded from use_count
            bml1 = root / "hold1.bml"
            bml1.write_text(
                "\n".join(
                    [
                        "#TITLE BML1",
                        "#BPM 120",
                        "#LNTYPE 1",
                        "#LNOBJ ZZ",
                        "#WAV01 start_ch51.wav",
                        "#WAV02 start_ch69.wav",
                        "#WAV03 body_obj.wav",
                        "#WAVZZ end_obj.wav",
                        "#00151:0100",
                        "#00251:0100",
                        "#00169:0200",
                        "#00269:0200",
                        "#00111:03ZZ",
                    ]
                ),
                encoding="utf-8",
            )
            c1 = parse_bms(bml1)
            self.assertEqual([(e.channel, e.wav_id, e.is_ln_end) for e in c1.events], [
                ("51", "01", False),
                ("69", "02", False),
                ("11", "03", False),
            ])
            self.assertEqual(len(c1.ln_end_events), 3)
            self.assertTrue(all(e.is_ln_end for e in c1.ln_end_events))
            self.assertEqual(c1.wavs["01"].use_count, 1)
            self.assertEqual(c1.wavs["02"].use_count, 1)
            self.assertEqual(c1.wavs["03"].use_count, 1)
            self.assertEqual(c1.wavs["ZZ"].use_count, 0)

            # 3) .bml #LNTYPE 2: contiguous identical WAV triggers once, tail tokens marked as ln_end
            bml2 = root / "hold2.bml"
            bml2.write_text(
                "\n".join(
                    [
                        "#TITLE BML2",
                        "#BPM 120",
                        "#LNTYPE 2",
                        "#WAV01 hold.wav",
                        "#00151:01010100",
                    ]
                ),
                encoding="utf-8",
            )
            c2 = parse_bms(bml2)
            self.assertEqual(len(c2.events), 1)
            self.assertEqual(c2.wavs["01"].use_count, 1)
            self.assertEqual(len(c2.ln_end_events), 2)


if __name__ == "__main__":
    unittest.main()
