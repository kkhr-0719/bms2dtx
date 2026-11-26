"""Tests for exact BPM/measure-length timeline math and DTX serialization invariants."""

from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from core.dtx_writer import generate_dtx_text
from core.models import BpmChange, PlacedDtxNote
from core.parser import parse_bms
from core.timeline import Timeline


class TestTimelineMath(unittest.TestCase):
    def test_bpm_changes_and_measure_lengths_exact(self) -> None:
        # Measure 0: length 1.0, BPM 120 -> duration = 240/120 = 2.0s
        # Measure 1: length 0.75 (3/4), starts at BPM 120, changes at pos 1/3 to BPM 240
        #   first 1/3 of 0.75 = 1 beat @ 120 BPM = 0.5s (t = 2.0 -> 2.5)
        #   last 2/3 of 0.75 = 2 beats @ 240 BPM = 0.5s (t = 2.5 -> 3.0)
        # Measure 2: resets to length 1.0 (4/4) in BMS, BPM 240 -> duration = 1.0s (t = 3.0 -> 4.0)
        tl = Timeline(
            initial_bpm=120.0,
            measure_lengths={1: 0.75},
            bpm_changes=[BpmChange(measure=1, pos=Fraction(1, 3), bpm=240.0, source_channel="08")],
            max_measure=2,
        )
        self.assertAlmostEqual(tl.time_at(0, Fraction(0)), 0.0, places=9)
        self.assertAlmostEqual(tl.time_at(0, Fraction(1, 2)), 1.0, places=9)
        self.assertAlmostEqual(tl.time_at(1, Fraction(0)), 2.0, places=9)
        self.assertAlmostEqual(tl.time_at(1, Fraction(1, 3)), 2.5, places=9)
        self.assertAlmostEqual(tl.time_at(2, Fraction(0)), 3.0, places=9)
        self.assertAlmostEqual(tl.time_at(2, Fraction(1, 2)), 3.5, places=9)
        self.assertAlmostEqual(tl.total_duration_sec, 4.0, places=9)

    def test_bpm_change_at_measure_start_pos_zero(self) -> None:
        # Measure 0 @ 120 BPM -> 2.0s; Measure 1 changes to 240 BPM right at pos 0 -> 1.0s
        tl = Timeline(
            initial_bpm=120.0,
            measure_lengths={},
            bpm_changes=[BpmChange(measure=1, pos=Fraction(0), bpm=240.0, source_channel="03")],
            max_measure=1,
        )
        self.assertAlmostEqual(tl.time_at(1, Fraction(0)), 2.0, places=9)
        self.assertAlmostEqual(tl.time_at(1, Fraction(1, 2)), 2.5, places=9)
        self.assertAlmostEqual(tl.total_duration_sec, 3.0, places=9)


class TestDtxWriterInvariants(unittest.TestCase):
    def _make_chart(self, bms_text: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "test.bms"
        p.write_text(bms_text, encoding="utf-8")
        return parse_bms(p)

    def test_sticky_measure_length_02_resets_properly(self) -> None:
        chart = self._make_chart(
            "\n".join(
                [
                    "#TITLE Sticky02",
                    "#BPM 120",
                    "#WAV01 a.wav",
                    "#00102:0.75",
                    "#00202:0.75",
                    # Measure 3 has NO 02 line in BMS -> returns to 1.0!
                    "#00402:0.5",
                    "#00501:01",
                ]
            )
        )
        dtx_text, warnings = generate_dtx_text(
            chart,
            dtx_wav_table=[("01", "bgm.ogg")],
            placed_notes=[],
            lane_bind_lines=[],
            effective_toggle_on=False,
        )
        self.assertEqual(warnings, [])
        # Must emit 00102: 0.75, skip redundant 00202, emit 00302: 1, emit 00402: 0.5, emit 00502: 1
        self.assertIn("#00102: 0.75", dtx_text)
        self.assertNotIn("#00202:", dtx_text)
        self.assertIn("#00302: 1", dtx_text)
        self.assertIn("#00402: 0.5", dtx_text)
        self.assertIn("#00502: 1", dtx_text)
        # Toggle=OFF emits #BGMWAV: 01 and single #00001: 01
        self.assertIn("#BGMWAV: 01", dtx_text)
        self.assertEqual(dtx_text.count("01: 01"), 1)  # #00001: 01 once

    def test_rejects_se33_out_of_bounds_and_channel_01_stacking(self) -> None:
        chart = self._make_chart("#TITLE Safe\n#BPM 120\n#WAV01 a.wav\n#00101:01\n")

        # SE33 ("93") MUST raise ValueError (Rule 6)
        with self.assertRaises(ValueError):
            generate_dtx_text(
                chart,
                dtx_wav_table=[("01", "bgm.ogg"), ("02", "a.wav")],
                placed_notes=[
                    PlacedDtxNote(1, Fraction(0), "93", "02", "01")
                ],
                lane_bind_lines=[],
                effective_toggle_on=False,
            )

        # Attempting to place an extra note on channel 01 MUST raise ValueError (Rule 6)
        with self.assertRaises(ValueError):
            generate_dtx_text(
                chart,
                dtx_wav_table=[("01", "bgm.ogg"), ("02", "a.wav")],
                placed_notes=[
                    PlacedDtxNote(1, Fraction(0), "01", "02", "01")
                ],
                lane_bind_lines=[],
                effective_toggle_on=False,
            )

    def test_base62_exbpm_and_hex_bpm_normalized_to_base36_channel_08(self) -> None:
        chart = self._make_chart(
            "\n".join(
                [
                    "#TITLE BpmNorm",
                    "#BASE 62",
                    "#BPM 120",
                    "#BPMa1 185.25",
                    "#WAV01 a.wav",
                    "#00103:96",       # 0x96 = 150 BPM
                    "#00208:00a1",     # 185.25 BPM at pos 1/2
                ]
            )
        )
        dtx_text, _ = generate_dtx_text(
            chart,
            dtx_wav_table=[("01", "bgm.ogg")],
            placed_notes=[],
            lane_bind_lines=[],
            effective_toggle_on=False,
        )
        self.assertIn("#BPM01: 150", dtx_text)
        self.assertIn("#BPM02: 185.25", dtx_text)
        self.assertIn("#00108: 01", dtx_text)
        self.assertIn("#00208: 0002", dtx_text)
        self.assertNotIn("a1", dtx_text)

    def test_pathological_lcm_quantizes_safely_with_warning(self) -> None:
        chart = self._make_chart("#TITLE Lcm\n#BPM 120\n#WAV01 a.wav\n#00101:01\n")
        # 1/7, 1/11, 1/13, 1/17 -> LCM = 17017 > 1536
        notes = [
            PlacedDtxNote(1, Fraction(1, 7), "12", "02", "01"),
            PlacedDtxNote(1, Fraction(1, 11), "12", "02", "01"),
            PlacedDtxNote(1, Fraction(1, 13), "12", "02", "01"),
            PlacedDtxNote(1, Fraction(1, 17), "12", "02", "01"),
        ]
        dtx_text, warnings = generate_dtx_text(
            chart,
            dtx_wav_table=[("01", "bgm.ogg"), ("02", "a.wav")],
            placed_notes=notes,
            lane_bind_lines=[],
            effective_toggle_on=False,
        )
        self.assertTrue(any(w.code == "MEASURE_GRID_QUANTIZED" for w in warnings))
        self.assertIn("#00112: ", dtx_text)

    def test_sticky_measure_length_02_resets_after_last_measure(self) -> None:
        chart = self._make_chart(
            "#TITLE Last02\n#BPM 120\n#WAV01 a.wav\n#00502:0.5\n#00501:01\n"
        )
        dtx_text, warnings = generate_dtx_text(
            chart,
            dtx_wav_table=[("01", "bgm.ogg")],
            placed_notes=[],
            lane_bind_lines=[],
            effective_toggle_on=False,
            comment="Line1\nLine2",
        )
        self.assertEqual(warnings, [])
        self.assertIn("#00502: 0.5", dtx_text)
        self.assertIn("#00602: 1", dtx_text)
        self.assertIn("#COMMENT: Line1 Line2", dtx_text)

    def test_sticky_measure_length_02_multi_bar_alternating_reset(self) -> None:
        chart = self._make_chart(
            "\n".join(
                [
                    "#TITLE Multi02",
                    "#BPM 120",
                    "#WAV01 a.wav",
                    "#00102:0.75",
                    # Measure 2 returns to 1.0 implicitly
                    "#00402:0.5",
                    "#00502:0.75",
                    # Measure 6 returns to 1.0 implicitly
                    "#00802:0.5",
                    "#00801:01",
                ]
            )
        )
        dtx_text, warnings = generate_dtx_text(
            chart,
            dtx_wav_table=[("01", "bgm.ogg")],
            placed_notes=[],
            lane_bind_lines=[],
            effective_toggle_on=False,
        )
        self.assertEqual(warnings, [])
        self.assertIn("#00102: 0.75", dtx_text)
        self.assertIn("#00202: 1", dtx_text)
        self.assertNotIn("#00302:", dtx_text)
        self.assertIn("#00402: 0.5", dtx_text)
        self.assertIn("#00502: 0.75", dtx_text)
        self.assertIn("#00602: 1", dtx_text)
        self.assertNotIn("#00702:", dtx_text)
        self.assertIn("#00802: 0.5", dtx_text)
        self.assertIn("#00902: 1", dtx_text)

    def test_comment_default_signature_with_and_without_genre_and_preview_header(self) -> None:
        chart_with_genre = self._make_chart(
            "#TITLE WithGenre\n#GENRE Drum & Bass\n#BPM 174\n#WAV01 a.wav\n#00101:01\n"
        )
        dtx_with, _ = generate_dtx_text(
            chart_with_genre,
            dtx_wav_table=[("01", "bgm.ogg")],
            placed_notes=[],
            lane_bind_lines=[],
            effective_toggle_on=False,
            preview_filename="sub/pre;1.ogg",
        )
        self.assertIn("#GENRE: Drum & Bass", dtx_with)
        self.assertIn("#COMMENT: via bms2dtx", dtx_with)
        self.assertIn(r"#PREVIEW: sub\pre_1.ogg", dtx_with)
        self.assertNotIn("Converted from", dtx_with)
        self.assertTrue(chart_with_genre.source_path.name.endswith(".bms"))

        chart_no_genre = self._make_chart(
            "#TITLE NoGenre\n#BPM 120\n#WAV01 a.wav\n#00101:01\n"
        )
        dtx_without, _ = generate_dtx_text(
            chart_no_genre,
            dtx_wav_table=[("01", "bgm.ogg")],
            placed_notes=[],
            lane_bind_lines=[],
            effective_toggle_on=False,
        )
        self.assertNotIn("#GENRE:", dtx_without)
        self.assertIn("#COMMENT: via bms2dtx", dtx_without)
        self.assertNotIn("#PREVIEW:", dtx_without)
        self.assertNotIn("Converted from", dtx_without)

    def test_wav_inline_labels_sanitization_and_bga_serialization(self) -> None:
        chart = self._make_chart(
            "#TITLE LabelsBga\n#GENRE Jazz;Fusion\n#BPM 130\n#WAV01 a.wav\n#00101:01\n"
        )
        dtx_text, warnings = generate_dtx_text(
            chart,
            dtx_wav_table=[("01", "bgm.ogg"), ("02", "snare.wav"), ("03", "empty.wav")],
            placed_notes=[PlacedDtxNote(1, Fraction(0), "12", "02", "01")],
            lane_bind_lines=[],
            effective_toggle_on=False,
            dtx_wav_labels={"01": "BGM", "02": "SD", "03": "CUSTOM"},
            wav_labels={"BGM": "Mix;Bg\nMain", "SD": "Snare\tHit", "CUSTOM": ""},
            bga_headers=[("BMP", "01", "bg;1.png"), ("AVI", "02", "sub/movie.mp4")],
            bga_placed_events=[
                (1, Fraction(0), "04", "01"),
                (2, Fraction(1, 2), "54", "02"),
            ],
        )
        self.assertEqual(warnings, [])
        self.assertIn("#GENRE: Jazz；Fusion", dtx_text)
        self.assertIn("#COMMENT: via bms2dtx", dtx_text)
        self.assertIn("#WAV01: bgm.ogg\t;MixBg Main", dtx_text)
        self.assertIn("#WAV02: snare.wav\t;Snare Hit", dtx_text)
        self.assertIn("#WAV03: empty.wav\r\n", dtx_text)
        self.assertIn("#BMP01: bg_1.png", dtx_text)
        self.assertIn(r"#AVI02: sub\movie.mp4", dtx_text)
        self.assertIn("#00104: 01", dtx_text)
        self.assertIn("#00254: 0002", dtx_text)

    def test_dlevel_omitted_by_default_and_mix_wav_label_inherits_base(self) -> None:
        chart = self._make_chart(
            "#TITLE NoDlevel\n#PLAYLEVEL 9\n#BPM 140\n#WAV01 a.wav\n#00101:01\n"
        )
        dtx_default, _ = generate_dtx_text(
            chart,
            dtx_wav_table=[("01", "bgm.ogg"), ("02", "sd_mix.wav")],
            placed_notes=[PlacedDtxNote(1, Fraction(0), "12", "02", "02")],
            lane_bind_lines=[],
            effective_toggle_on=False,
            dtx_wav_labels={"02": "SD_MIX"},
            wav_labels={"SD": "Snare"},
        )
        self.assertNotIn("#DLEVEL", dtx_default)
        self.assertIn("#WAV02: sd_mix.wav\t;Snare_MIX", dtx_default)

        dtx_explicit, _ = generate_dtx_text(
            chart,
            dtx_wav_table=[("01", "bgm.ogg")],
            placed_notes=[],
            lane_bind_lines=[],
            effective_toggle_on=False,
            dlevel=450,
        )
        self.assertIn("#DLEVEL: 450", dtx_explicit)


if __name__ == "__main__":
    unittest.main()
