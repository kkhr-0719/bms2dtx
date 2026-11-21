"""Optional real-chart batch regression tests driven by BMS2DTX_REAL_CHARTS_DIR."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.constants import LEGAL_DTX_NOTE_CHANNELS, RESERVED_AUTO_LP_WAV_IDS, TabId
from core.converter import batch_convert, convert_bms, inspect_bms
from core.models import ConversionOptions, MappingConfig

_ENV_VAR = "BMS2DTX_REAL_CHARTS_DIR"
_REAL_CHARTS_RAW = os.environ.get(_ENV_VAR, "").strip()
_REAL_CHARTS_DIR = Path(_REAL_CHARTS_RAW) if _REAL_CHARTS_RAW else None


@unittest.skipUnless(
    _REAL_CHARTS_DIR is not None and _REAL_CHARTS_DIR.is_dir(),
    f"Set {_ENV_VAR} to a local chart directory to run optional real-chart regression tests",
)
class TestRealChartsBatch(unittest.TestCase):
    def test_inspect_and_batch_convert_real_charts_readonly(self) -> None:
        assert _REAL_CHARTS_DIR is not None
        chart_files = sorted(
            p
            for p in _REAL_CHARTS_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in (".bms", ".bme", ".bml")
        )
        if not chart_files:
            self.skipTest(f"No BMS/BME files found in {_REAL_CHARTS_DIR}")

        before_meta = {
            p.name: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in _REAL_CHARTS_DIR.iterdir()
        }

        for chart_path in chart_files:
            chart = inspect_bms(chart_path)
            self.assertGreater(chart.initial_bpm, 0.0)
            self.assertIn(chart.base, (36, 62))

        first_chart = inspect_bms(chart_files[0])
        sample_ids = list(first_chart.referenced_wav_ids[:4])
        mapping = MappingConfig(
            tab_wavs={TabId.BD: sample_ids[:2], TabId.SD: sample_ids[2:4]} if sample_ids else {},
            default_bind_mode="auto",
            toggle_on=False,
        )

        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp) / "batch_out"
            results = batch_convert(
                chart_files[:3],
                out_root,
                mapping=mapping,
                utf8=False,
                render_bgm=False,
            )
            for _src, res in results.items():
                self.assertFalse(res.effective_toggle_on)
                self.assertTrue(res.dtx_path.is_file())
                self.assertTrue(res.project_json_path and res.project_json_path.is_file())
                for dtx_wid in res.wav_id_map.values():
                    self.assertNotIn(dtx_wid, RESERVED_AUTO_LP_WAV_IDS)

            # Render full bgm.ogg on the first chart
            bgm_out = Path(tmp) / "bgm_full"
            res_full = convert_bms(
                chart_files[0],
                ConversionOptions(output_dir=bgm_out, utf8=False, render_bgm=True),
                mapping=mapping,
            )
            self.assertTrue(res_full.dtx_path.is_file())
            if res_full.bgm_path is not None:
                self.assertTrue(res_full.bgm_path.is_file())

        after_meta = {
            p.name: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in _REAL_CHARTS_DIR.iterdir()
        }
        self.assertEqual(before_meta, after_meta)
        # Keep reference to legal channels so import stays verified
        self.assertIn("1B", LEGAL_DTX_NOTE_CHANNELS)


if __name__ == "__main__":
    unittest.main()
