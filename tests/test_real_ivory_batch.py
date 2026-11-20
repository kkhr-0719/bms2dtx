"""Real-world batch regression tests on all 8 BMS charts in 'Ivory [Snowyplane]'."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.constants import LEGAL_DTX_NOTE_CHANNELS, TabId
from core.converter import batch_convert, convert_bms, inspect_bms
from core.models import ConversionOptions, MappingConfig

IVORY_DIR = Path(__file__).resolve().parent.parent / "Ivory [Snowyplane]"
_DATA_LINE_RE = re.compile(r"^#(\d{3})([0-9A-Za-z]{2}):\s*(.*)$")


def _ivory_drum_mapping() -> MappingConfig:
    """Realistic 15-Tab drum+bass mapping for Ivory [Snowyplane]."""
    return MappingConfig(
        tab_wavs={
            TabId.BD: ["RZ", "S0"],  # kick_000, kick_001
            TabId.SD: ["Q8", "Q9", "QA", "QB", "QC", "QD"],  # drums_000..005
            TabId.HH_OPEN: ["S1", "S2", "S3"],  # drums_opend_hat_001..002
            TabId.CY: ["RU", "RV", "RW", "RX", "RY"],  # cymbals_crash_000..004
            TabId.RD: ["RI", "RJ", "RK", "RL", "RM"],  # cymbals_001..005
            TabId.OTHER: ["TT"],  # fadeout
            TabId.BASS: ["S8", "S9", "SA", "SB"],  # bass slices
        },
        default_lane_binds={
            TabId.BD: "RZ",
            TabId.SD: "QA",
            TabId.HH_OPEN: "S1",
            TabId.CY: "RU",
            TabId.RD: "RI",
        },
        toggle_on=False,
    )


def _ffprobe_audio(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    return data["streams"][0]


@unittest.skipUnless(IVORY_DIR.is_dir(), "Ivory [Snowyplane] fixture folder not found")
class TestRealIvoryBatch(unittest.TestCase):
    def test_inspect_all_8_ivory_charts_consistency_and_readonly(self) -> None:
        bms_files = sorted(IVORY_DIR.glob("*.bms"))
        self.assertEqual(len(bms_files), 8)

        file_count_before = len(list(IVORY_DIR.iterdir()))

        for bms_path in bms_files:
            chart = inspect_bms(bms_path)
            self.assertEqual(chart.encoding_used, "cp932")
            self.assertEqual(chart.base, 36)
            self.assertFalse(chart.is_base62)
            self.assertEqual(len(chart.wavs), 1073)
            # Every declared .wav resolves in-memory to .ogg on disk with 0 missing!
            self.assertEqual(sum(1 for w in chart.wavs.values() if w.exists), 1073)
            self.assertEqual(sum(1 for w in chart.wavs.values() if w.suffix_corrected), 1073)
            # All 8 difficulties share the exact same 11,264 playable audio events!
            self.assertEqual(len(chart.events), 11264, msg=f"Event mismatch on {bms_path.name}")
            # B23 is empty on all 8 charts and must be pruned while preserving true B24..B32 indices
            self.assertIn("B23", chart.pruned_empty_tracks)
            bgm_labels = [t.track_label for t in chart.bgm_tracks]
            self.assertNotIn("B23", bgm_labels)
            self.assertIn("B1", bgm_labels)
            self.assertIn("B24", bgm_labels)
            self.assertIn("B32", bgm_labels)

        self.assertEqual(len(list(IVORY_DIR.iterdir())), file_count_before)

    def test_batch_convert_all_8_ivory_charts_with_real_bgm_and_dtx_validation(self) -> None:
        bms_files = sorted(IVORY_DIR.glob("*.bms"))
        mapping = _ivory_drum_mapping()

        # Snapshot Ivory folder filenames + sizes + mtimes to assert zero mutation
        before_meta = {
            p.name: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in IVORY_DIR.iterdir()
        }

        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp) / "ivory_batch_out"
            results = batch_convert(
                bms_files,
                out_root,
                mapping=mapping,
                utf8=False,
                render_bgm=False,
            )

            self.assertEqual(len(results), 8)

            for bms_path, res in results.items():
                self.assertFalse(res.effective_toggle_on)
                self.assertEqual(res.dtx_encoding, "cp932")
                self.assertTrue(res.dtx_path.is_file())
                self.assertIsNone(res.bgm_path)
                self.assertIsNone(res.mix_stats)

                # Verify .dtx decodes cleanly in cp932 with zero mojibake / replacement chars
                dtx_text = res.dtx_path.read_bytes().decode("cp932")
                self.assertNotIn("\ufffd", dtx_text)
                self.assertIn("#WAV01: bgm.ogg", dtx_text)
                self.assertIn("#BGMWAV: 01", dtx_text)
                self.assertIn("#00001: 01", dtx_text)

                # Verify ALL retained slice files (excluding unrendered bgm.ogg) exist in output directory
                for _wid, fname in res.dtx_wav_table:
                    if fname == "bgm.ogg":
                        continue
                    self.assertTrue(
                        (res.output_dir / fname).is_file(),
                        msg=f"Missing retained audio {fname} for {bms_path.name}",
                    )

                # Verify #DTXC_LANEBINDEDCHIP present (5 lanes: 02=HH, 03=LP/LBD synced from BD, 04=SD, 06=BD, 09=CY/RD)
                self.assertEqual(len(res.lane_bind_lines), 5)
                for line in res.lane_bind_lines:
                    self.assertIn(line, dtx_text)

                # Verify duplicate #WAV S2 and S3 (both drums_opend_hat_002.ogg) share 1 DTX WAV slot
                self.assertEqual(res.wav_id_map["S2"], res.wav_id_map["S3"])
                hat2_slots = [wid for wid, fn in res.dtx_wav_table if fn == "drums_opend_hat_002.ogg"]
                self.assertEqual(len(hat2_slots), 1)

                # Verify Rule 6: NO illegal channels (SE33+) and AT MOST ONE channel 01 line
                ch01_count = 0
                for line in dtx_text.splitlines():
                    m = _DATA_LINE_RE.match(line.strip())
                    if not m:
                        continue
                    ch = m.group(2).upper()
                    if ch in ("02", "08"):
                        continue
                    self.assertIn(ch, LEGAL_DTX_NOTE_CHANNELS, msg=f"Illegal DTX channel {ch} in {bms_path.name}")
                    if ch == "01":
                        ch01_count += 1
                self.assertEqual(ch01_count, 1)

            # Render full bgm.ogg mix on the densest chart to verify real Vorbis audio & stats
            densest_bms = max(
                bms_files,
                key=lambda p: (len(inspect_bms(p).events), p.stat().st_size),
            )
            bgm_out = Path(tmp) / "ivory_bgm_full"
            res_full = convert_bms(
                densest_bms,
                ConversionOptions(output_dir=bgm_out, utf8=False, render_bgm=True),
                mapping=mapping,
            )
            self.assertIsNotNone(res_full.bgm_path)
            assert res_full.bgm_path is not None and res_full.mix_stats is not None
            self.assertTrue(res_full.bgm_path.is_file())
            self.assertGreater(res_full.mix_stats.mixed_event_count, 9000)
            self.assertGreater(res_full.mix_stats.rms, 0.03, msg=f"BGM too quiet/silent on {densest_bms.name}")
            self.assertGreater(res_full.mix_stats.peak_after_norm, 0.2)
            self.assertLessEqual(res_full.mix_stats.peak_after_norm, 0.991)
            self.assertGreater(res_full.mix_stats.duration_sec, 125.0)

            stream = _ffprobe_audio(res_full.bgm_path)
            self.assertEqual(stream["codec_name"], "vorbis")
            self.assertEqual(int(stream["sample_rate"]), 44100)
            self.assertEqual(int(stream["channels"]), 2)
            self.assertGreater(float(stream["duration"]), 125.0)

            for _wid, fname in res_full.dtx_wav_table:
                self.assertTrue(
                    (res_full.output_dir / fname).is_file(),
                    msg=f"Missing retained audio {fname} for {densest_bms.name}",
                )

            # Also verify UTF-8 export on one real chart
            utf8_out = Path(tmp) / "ivory_utf8"
            res_u8 = convert_bms(
                IVORY_DIR / "_ivory_spn.bms",
                ConversionOptions(output_dir=utf8_out, utf8=True, render_bgm=False),
                mapping=mapping,
            )
            self.assertEqual(res_u8.dtx_encoding, "utf-8")
            self.assertIn("#TITLE: Ivory [NORMAL]", res_u8.dtx_path.read_text(encoding="utf-8"))

        # Confirm Ivory [Snowyplane] is 100% unchanged after batch conversion!
        after_meta = {
            p.name: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in IVORY_DIR.iterdir()
        }
        self.assertEqual(before_meta, after_meta)


if __name__ == "__main__":
    unittest.main()
