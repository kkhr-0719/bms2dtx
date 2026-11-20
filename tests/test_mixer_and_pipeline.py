"""End-to-end audio mixing, timing accuracy, peak protection, transcoding, and Rule 1 tests."""

from __future__ import annotations

import hashlib
import math
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from core.constants import TabId
from core.converter import batch_convert, convert_bms
from core.mixer import _decode_audio_to_f32_bytes, find_ffmpeg, render_bgm_ogg
from core.models import ConversionOptions, MappingConfig
from core.parser import parse_bms


def _write_sine_wav(
    path: Path, freq_hz: float, duration_sec: float, amplitude: float = 0.6, sample_rate: int = 44100
) -> None:
    n_frames = int(round(duration_sec * sample_rate))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            val = int(max(-32767, min(32767, round(32767.0 * amplitude * math.sin(2.0 * math.pi * freq_hz * i / sample_rate)))))
            frames.extend(struct.pack("<hh", val, val))
        wf.writeframes(bytes(frames))


def _dir_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    snap: dict[str, tuple[int, str]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            data = p.read_bytes()
            snap[p.relative_to(root).as_posix()] = (len(data), hashlib.sha256(data).hexdigest())
    return snap


def _window_rms(raw_f32: bytes, sample_rate: int, t0: float, t1: float) -> float:
    f0 = max(0, int(round(t0 * sample_rate)))
    f1 = min(len(raw_f32) // 8, int(round(t1 * sample_rate)))
    if f1 <= f0:
        return 0.0
    sub = raw_f32[f0 * 8 : f1 * 8]
    vals = struct.unpack(f"<{len(sub) // 4}f", sub)
    return math.sqrt(sum(v * v for v in vals) / len(vals))


class TestMixerAndPipeline(unittest.TestCase):
    def test_readonly_guard_rejects_same_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp)
            _write_sine_wav(src_dir / "kick.wav", 120.0, 0.1)
            bms = src_dir / "song.bms"
            bms.write_text("#TITLE Guard\n#BPM 120\n#WAV01 kick.wav\n#00101:01\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                convert_bms(bms, ConversionOptions(output_dir=src_dir))

    def test_end_to_end_toggle_off_timing_exclusion_ln_and_transcode(self) -> None:
        ffmpeg_exe = find_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            src_dir = tmp_root / "src_bms"
            out_dir = tmp_root / "out_dtx"
            src_dir.mkdir()

            # 1. Generate real audio slices:
            #    - kick.wav (kept as .wav on disk to test --transcode-ogg!)
            #    - synth.ogg (referenced as synth.wav in BMS to test suffix auto-correction!)
            #    - ln.wav (used on LN channel 51 to verify LN end marker is NOT played twice!)
            _write_sine_wav(src_dir / "kick.wav", 100.0, 0.12, amplitude=0.7)
            synth_wav_tmp = tmp_root / "synth_tmp.wav"
            _write_sine_wav(synth_wav_tmp, 660.0, 0.12, amplitude=0.7)
            subprocess.run(
                [ffmpeg_exe, "-v", "error", "-y", "-i", str(synth_wav_tmp), "-c:a", "libvorbis", "-q:a", "6", str(src_dir / "synth.ogg")],
                check=True,
            )
            _write_sine_wav(src_dir / "ln.wav", 440.0, 0.10, amplitude=0.7)

            # Timeline:
            #   Measure 0: BPM 120, length 1.0 -> [0.0s, 2.0s)
            #     kick (01, mapped to BD!) at m0 pos 0 (t=0.0s) -> MUST be excluded from bgm.ogg!
            #   Measure 1: length 0.75 (3/4!), BPM 120 -> duration = 1.5s -> [2.0s, 3.5s)
            #     BPM changes to 240 (0xF0) at m1 pos 1/3 (t = 2.0 + 0.5 = 2.5s)
            #     So remaining 2/3 of 3/4 bar = 2 beats @ 240 BPM = 0.5s -> Measure 2 starts at t = 3.0s!
            #     ln (03, unmapped LN on ch 51): START at m1 pos 0 (t=2.0s), END at m1 pos 2/3 (t=2.75s)!
            #   Measure 2: resets to length 1.0 @ 240 BPM -> [3.0s, 4.0s)
            #     synth (02, unmapped) at m2 pos 1/2 -> t = 3.5s!
            bms_path = src_dir / "timing.bms"
            bms_path.write_bytes(
                "\r\n".join(
                    [
                        "#TITLE タイミングテスト",
                        "#ARTIST テスト作者",
                        "#BPM 120",
                        "#LNTYPE 1",
                        "#WAV01 kick.wav",
                        "#WAV02 synth.wav",
                        "#WAV03 ln.wav",
                        "#00001:0100",
                        "#00102:0.75",
                        "#00103:00F000",
                        "#00151:030003",
                        "#00201:0002",
                    ]
                ).encode("cp932")
            )

            before_snap = _dir_snapshot(src_dir)

            mapping = MappingConfig(
                tab_wavs={TabId.BD: ["01"]},
                default_lane_binds={TabId.BD: "01"},
                toggle_on=False,
            )
            opts = ConversionOptions(
                output_dir=out_dir,
                utf8=False,
                transcode_wav_to_ogg=True,
            )
            res = convert_bms(bms_path, opts, mapping=mapping)

            # 1. Source folder MUST be 100% untouched (Rule 1)
            self.assertEqual(_dir_snapshot(src_dir), before_snap)

            # 2. Retained kick.wav was transcoded to kick.ogg in output_dir & updated in .dtx (Rule 3)
            self.assertTrue((out_dir / "kick.ogg").is_file())
            self.assertFalse((out_dir / "kick.wav").exists())
            self.assertEqual(len(res.transcoded_files), 1)
            self.assertEqual(res.dtx_wav_table, [("01", "bgm.ogg"), ("02", "kick.ogg")])

            # 3. Verify .dtx content, CP932 encoding, sticky 02 reset, and #DTXC_LANEBINDEDCHIP
            dtx_text = res.dtx_path.read_bytes().decode("cp932")
            self.assertIn("#TITLE: タイミングテスト", dtx_text)
            self.assertIn("#WAV01: bgm.ogg", dtx_text)
            self.assertIn("#BGMWAV: 01", dtx_text)
            self.assertIn("#WAV02: kick.ogg", dtx_text)
            self.assertIn("#00102: 0.75", dtx_text)
            self.assertIn("#00202: 1", dtx_text)
            self.assertIn("#00001: 01", dtx_text)
            self.assertIn("#00013: 02", dtx_text)  # kick mapped to BD (13), renumbered to 02!
            self.assertIn("#DTXC_LANEBINDEDCHIP: 06 02 00", dtx_text)

            # 4. Decode rendered bgm.ogg and verify EXACT timing, exclusion, and no phantom LN end!
            self.assertIsNotNone(res.bgm_path)
            assert res.bgm_path is not None and res.mix_stats is not None
            self.assertTrue(res.bgm_path.is_file())
            self.assertGreater(res.mix_stats.rms, 0.01)
            self.assertLessEqual(res.mix_stats.peak_after_norm, 0.991)

            raw_f32, err = _decode_audio_to_f32_bytes(res.bgm_path, ffmpeg_exe, 44100)
            self.assertIsNone(err)
            assert raw_f32 is not None

            # [0.02s, 0.12s]: kick was mapped to BD -> MUST be silent in bgm.ogg!
            self.assertLess(_window_rms(raw_f32, 44100, 0.02, 0.12), 0.005)
            # [2.01s, 2.09s]: LN START (03) at t=2.0s -> MUST be loud!
            self.assertGreater(_window_rms(raw_f32, 44100, 2.01, 2.09), 0.15)
            # [2.76s, 2.86s]: LN END marker at t=2.75s -> MUST be silent (no phantom double-hit)!
            self.assertLess(_window_rms(raw_f32, 44100, 2.76, 2.86), 0.005)
            # [3.51s, 3.60s]: synth (02, suffix-corrected from .wav -> .ogg) at t=3.5s -> MUST be loud!
            self.assertGreater(_window_rms(raw_f32, 44100, 3.51, 3.60), 0.15)

    def test_peak_protection_and_pure_python_fallback_match_numpy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 4 overlapping loud distinct WAVs at amplitude 0.7 -> raw peak ~2.8 > 0.99!
            for idx in range(1, 5):
                _write_sine_wav(root / f"loud{idx}.wav", 330.0, 0.15, amplitude=0.7)
            bms = root / "clip.bms"
            bms.write_text(
                "\n".join(
                    [
                        "#TITLE ClipTest",
                        "#BPM 120",
                        "#WAV01 loud1.wav",
                        "#WAV02 loud2.wav",
                        "#WAV03 loud3.wav",
                        "#WAV04 loud4.wav",
                        "#00001:01",
                        "#00001:02",
                        "#00001:03",
                        "#00001:04",
                    ]
                ),
                encoding="utf-8",
            )
            chart = parse_bms(bms)

            out_np = root / "out_np" / "bgm.ogg"
            out_py = root / "out_py" / "bgm.ogg"
            stats_np, w_np = render_bgm_ogg(chart, chart.events, out_np, use_numpy=True)
            stats_py, w_py = render_bgm_ogg(chart, chart.events, out_py, use_numpy=False)

            self.assertEqual(w_np, [])
            self.assertEqual(w_py, [])
            self.assertGreater(stats_np.peak_before_norm, 2.0)
            self.assertAlmostEqual(stats_np.peak_after_norm, 0.99, places=3)
            self.assertAlmostEqual(stats_py.peak_after_norm, 0.99, places=3)
            self.assertAlmostEqual(stats_np.rms, stats_py.rms, places=4)
            self.assertAlmostEqual(stats_np.gain_applied, stats_py.gain_applied, places=4)

    def test_edge_cases_empty_chart_transcode_collision_bgm_name_and_overflow_fallback(self) -> None:
        ffmpeg_exe = find_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)

            # 1. Empty chart (no notes at all) -> does not crash, emits warning & valid .dtx
            empty_src = tmp_root / "empty_src"
            empty_src.mkdir()
            empty_bms = empty_src / "empty.bms"
            empty_bms.write_text("#TITLE Empty\n#BPM 120\n", encoding="utf-8")
            res_empty = convert_bms(empty_bms, ConversionOptions(output_dir=tmp_root / "empty_out"))
            self.assertTrue(res_empty.dtx_path.is_file())
            self.assertTrue(any(w.code == "BGM_MIX_SILENT_NO_EVENTS" for w in res_empty.warnings))

            # 2. Retained slice name collisions:
            #    - foo.ogg AND foo.wav both retained with transcode_wav_to_ogg=True
            #    - bgm.ogg as a retained slice name in Toggle=OFF (must not clobber generated bgm.ogg!)
            #    - duplicate WAV IDs pointing to same foo.wav (deduplicated transcode)
            col_src = tmp_root / "col_src"
            col_src.mkdir()
            _write_sine_wav(col_src / "foo.wav", 220.0, 0.08)
            tmp_w = tmp_root / "tmp_foo.wav"
            _write_sine_wav(tmp_w, 550.0, 0.08)
            subprocess.run(
                [ffmpeg_exe, "-v", "error", "-y", "-i", str(tmp_w), "-c:a", "libvorbis", "-q:a", "6", str(col_src / "foo.ogg")],
                check=True,
            )
            subprocess.run(
                [ffmpeg_exe, "-v", "error", "-y", "-i", str(tmp_w), "-c:a", "libvorbis", "-q:a", "6", str(col_src / "bgm.ogg")],
                check=True,
            )
            col_bms = col_src / "col.bms"
            col_bms.write_text(
                "\n".join(
                    [
                        "#TITLE Collision",
                        "#BPM 120",
                        "#WAV01 foo.ogg",
                        "#WAV02 foo.wav",
                        "#WAV03 foo.wav",
                        "#WAV04 bgm.ogg",
                        "#00101:01020304",
                    ]
                ),
                encoding="utf-8",
            )
            res_col = convert_bms(
                col_bms,
                ConversionOptions(output_dir=tmp_root / "col_out", transcode_wav_to_ogg=True),
                mapping=MappingConfig(
                    tab_wavs={
                        TabId.SD: ["01", "02", "03"],
                        TabId.BD: ["04"],
                    },
                    toggle_on=False,
                ),
            )
            wav_dict = dict(res_col.dtx_wav_table)
            self.assertEqual(wav_dict["01"], "bgm.ogg")
            self.assertEqual(wav_dict["02"], "foo.ogg")
            # orig 02 and 03 both point to the SAME physical file foo.wav -> merged into 1 DTX slot "03" (foo_1.ogg)!
            self.assertEqual(res_col.wav_id_map["02"], "03")
            self.assertEqual(res_col.wav_id_map["03"], "03")
            self.assertNotEqual(wav_dict["03"], "foo.ogg")
            self.assertTrue(wav_dict["03"].endswith(".ogg"))
            # orig 04 (named bgm.ogg) gets slot "04" and was disambiguated to bgm_1.ogg so it didn't clobber bgm.ogg!
            self.assertEqual(res_col.wav_id_map["04"], "04")
            self.assertEqual(wav_dict["04"], "bgm_1.ogg")
            self.assertEqual(len(res_col.dtx_wav_table), 4)
            self.assertEqual(len(res_col.transcoded_files), 1)
            for _wid, fn in res_col.dtx_wav_table:
                self.assertTrue((res_col.output_dir / fn).is_file())

            # 3. End-to-end Toggle=ON overflow with overflow_strategy="fallback_toggle_off"
            ov_src = tmp_root / "ov_src"
            ov_src.mkdir()
            _write_sine_wav(ov_src / "hit.wav", 440.0, 0.08, amplitude=0.3)
            ov_lines = ["#TITLE OverflowFB", "#BPM 120"]
            for i in range(1, 29):
                ov_lines.append(f"#WAV{i:02d} hit.wav")
            for i in range(1, 29):
                ov_lines.append(f"#00101:{i:02d}")
            ov_bms = ov_src / "ov.bms"
            ov_bms.write_text("\n".join(ov_lines), encoding="utf-8")
            res_ov = convert_bms(
                ov_bms,
                ConversionOptions(output_dir=tmp_root / "ov_out"),
                mapping=MappingConfig(
                    tab_wavs={TabId.BD: ["01"]},
                    toggle_on=True,
                    overflow_strategy="fallback_toggle_off",
                ),
            )
            self.assertFalse(res_ov.effective_toggle_on)
            self.assertTrue(res_ov.toggle_off_forced)
            self.assertIsNotNone(res_ov.bgm_path)
            assert res_ov.bgm_path is not None and res_ov.mix_stats is not None
            self.assertTrue(res_ov.bgm_path.is_file())
            self.assertGreater(res_ov.mix_stats.rms, 0.01)

            # 4. Base-62 <= 1295 slices in Toggle=ON renumbers lowercase IDs to legal Base-36
            b62_src = tmp_root / "b62_small"
            b62_src.mkdir()
            _write_sine_wav(b62_src / "a.wav", 440.0, 0.05)
            _write_sine_wav(b62_src / "b.wav", 550.0, 0.05)
            b62_bms = b62_src / "small62.bms"
            b62_bms.write_text(
                "#TITLE Small62\n#BASE 62\n#BPM 120\n#WAVa1 a.wav\n#WAVzz b.wav\n#00101:a1zz\n",
                encoding="utf-8",
            )
            res_b62 = convert_bms(
                b62_bms,
                ConversionOptions(output_dir=tmp_root / "b62_out"),
                mapping=MappingConfig(tab_wavs={TabId.BD: ["a1"]}, toggle_on=True),
            )
            self.assertTrue(res_b62.effective_toggle_on)
            self.assertEqual(res_b62.wav_id_map, {"a1": "01", "zz": "02"})
            dtx_txt = res_b62.dtx_path.read_text(encoding="cp932")
            self.assertIn("#WAV01: a.wav", dtx_txt)
            self.assertIn("#WAV02: b.wav", dtx_txt)
            self.assertNotIn("a1", dtx_txt)
            self.assertNotIn("zz", dtx_txt)

    def test_cli_inspect_and_convert_with_map_json(self) -> None:
        import json
        import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "src"
            out_dir = root / "out"
            src_dir.mkdir()
            _write_sine_wav(src_dir / "bd.wav", 100.0, 0.08)
            _write_sine_wav(src_dir / "bg.wav", 440.0, 0.08)
            bms = src_dir / "demo.bms"
            bms.write_text(
                "#TITLE CliDemo\n#BPM 120\n#WAV01 bd.wav\n#WAV02 bg.wav\n#00101:0102\n",
                encoding="utf-8",
            )
            map_json = root / "map.json"
            map_json.write_text(
                json.dumps(
                    {
                        "tab_wavs": {"BD": ["01"]},
                        "default_lane_binds": {"BD": "01"},
                        "toggle_on": False,
                    }
                ),
                encoding="utf-8",
            )
            rc1 = cli.main(["--inspect", str(bms)])
            self.assertEqual(rc1, 0)
            rc2 = cli.main([str(bms), "--out", str(out_dir), "--map-json", str(map_json), "--utf8"])
            self.assertEqual(rc2, 0)
            self.assertTrue((out_dir / "demo.dtx").is_file())
            self.assertTrue((out_dir / "bgm.ogg").is_file())
            self.assertTrue((out_dir / "bd.wav").is_file())

    def test_corrupt_audio_jp_cp932_batch_collisions_readonly_and_cli_gbk(self) -> None:
        import json
        import os
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 1. Corrupt 0-byte .wav & garbage .ogg -> graceful warnings, no partial .ogg stub
            csrc = root / "corrupt_src"
            csrc.mkdir()
            (csrc / "zero.wav").write_bytes(b"")
            (csrc / "garbage.ogg").write_bytes(b"NOT_AN_OGG_FILE")
            cbms = csrc / "corrupt.bms"
            cbms.write_text(
                "#TITLE Corrupt\n#BPM 120\n#WAV01 zero.wav\n#WAV02 garbage.ogg\n#00101:0102\n",
                encoding="utf-8",
            )
            res_c = convert_bms(
                cbms,
                ConversionOptions(output_dir=root / "corrupt_out", transcode_wav_to_ogg=True),
                mapping=MappingConfig(tab_wavs={TabId.BD: ["01"]}, toggle_on=False),
            )
            codes = {w.code for w in res_c.warnings}
            self.assertIn("WAV_TRANSCODE_FAILED", codes)
            self.assertIn("AUDIO_DECODE_FAILED", codes)
            self.assertFalse((root / "corrupt_out" / "zero.ogg").exists())

            # 2. Japanese CP932 #WAVxx filename + --transcode-ogg
            jsrc = root / "jp_src"
            jsrc.mkdir()
            _write_sine_wav(jsrc / "ドラム_キック.wav", 120.0, 0.08)
            _write_sine_wav(jsrc / "ベース.wav", 240.0, 0.08)
            jbms = jsrc / "日本語.bms"
            jbms.write_bytes(
                "#TITLE テスト曲\r\n#BPM 120\r\n#WAV01 ドラム_キック.wav\r\n#WAV02 ベース.wav\r\n#00101:0102\r\n".encode("cp932")
            )
            res_j = convert_bms(
                jbms,
                ConversionOptions(output_dir=root / "jp_out", transcode_wav_to_ogg=True, utf8=False),
                mapping=MappingConfig(tab_wavs={TabId.BD: ["01"]}, toggle_on=False),
            )
            self.assertTrue((root / "jp_out" / "ドラム_キック.ogg").is_file())
            dtx_j = res_j.dtx_path.read_bytes().decode("cp932")
            self.assertIn("#WAV02: ドラム_キック.ogg", dtx_j)

            # 3. batch_convert same-stem collision (.bms + .bme) & Rule 1 guard against writing inside source_dir
            bsrc = root / "batch_src"
            bsrc.mkdir()
            (bsrc / "song.bms").write_text("#TITLE SongBMS\n#BPM 120\n#WAV01 a.wav\n#00101:01\n", encoding="utf-8")
            (bsrc / "song.bme").write_text("#TITLE SongBME\n#BPM 140\n#WAV01 a.wav\n#00101:01\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                batch_convert([bsrc / "song.bms"], bsrc, render_bgm=False)
            with self.assertRaises(ValueError):
                batch_convert([bsrc / "song.bms"], bsrc / "nested_out", render_bgm=False)

            bout = root / "batch_out"
            res_b = batch_convert([bsrc / "song.bms", bsrc / "song.bme"], bout, render_bgm=False)
            p_bms = res_b[bsrc / "song.bms"].dtx_path
            p_bme = res_b[bsrc / "song.bme"].dtx_path
            self.assertNotEqual(p_bms, p_bme)
            self.assertIn("#TITLE: SongBMS", p_bms.read_text(encoding="cp932"))
            self.assertIn("#TITLE: SongBME", p_bme.read_text(encoding="cp932"))

            # 4. Toggle=ON -> fallback_toggle_off drops stale UNDEFINED_WAV_SKIPPED_TOGGLE_ON warning
            fsrc = root / "fb_src"
            fsrc.mkdir()
            _write_sine_wav(fsrc / "hit.wav", 440.0, 0.05, amplitude=0.3)
            flines = ["#TITLE FbClean", "#BPM 120"]
            for i in range(1, 29):
                flines.append(f"#WAV{i:02d} hit.wav")
            for i in range(1, 29):
                flines.append(f"#00101:{i:02d}")
            flines.append("#00201:ZZ")  # undeclared WAV reference
            fbms = fsrc / "fb.bms"
            fbms.write_text("\n".join(flines), encoding="utf-8")
            res_fb = convert_bms(
                fbms,
                ConversionOptions(output_dir=root / "fb_out", render_bgm=False),
                mapping=MappingConfig(tab_wavs={TabId.BD: ["01"]}, toggle_on=True, overflow_strategy="fallback_toggle_off"),
            )
            fb_codes = [w.code for w in res_fb.warnings]
            self.assertIn("SE_COLLAPSE_FALLBACK_TOGGLE_OFF", fb_codes)
            self.assertNotIn("UNDEFINED_WAV_SKIPPED_TOGGLE_ON", fb_codes)

            # 5. CLI --inspect under GBK console encoding must not crash on emoji/Korean
            esrc = root / "emoji_src"
            esrc.mkdir()
            ebms = esrc / "emoji.bms"
            ebms.write_text("#TITLE 🎵 Korean 한국어 🥁\n#BPM 120\n", encoding="utf-8")
            env = os.environ.copy()
            env.pop("PYTHONUTF8", None)
            env["PYTHONIOENCODING"] = "gbk"
            cli_script = Path(__file__).resolve().parent.parent / "cli.py"
            proc = subprocess.run(
                [sys.executable, str(cli_script), "--inspect", str(ebms)],
                capture_output=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr.decode("utf-8", errors="replace"))
            parsed_json = json.loads(proc.stdout.decode("gbk"))
            self.assertEqual(parsed_json["title"], "🎵 Korean 한국어 🥁")

    def test_navigator_audio_dedup_downward_norm_semicolon_backslash_ffmpeg_and_reflow(self) -> None:
        from unittest import mock

        ffmpeg_exe = find_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 1. Same-timestamp same-WAV deduplication + downward-only peak protection (never boost quiet audio)
            dedup_src = root / "dedup_src"
            dedup_src.mkdir()
            _write_sine_wav(dedup_src / "same.wav", 440.0, 0.12, amplitude=0.5)
            _write_sine_wav(dedup_src / "quiet.wav", 220.0, 0.12, amplitude=0.15)
            dedup_bms = dedup_src / "dedup.bms"
            dedup_bms.write_text(
                "\n".join(
                    [
                        "#TITLE Dedup",
                        "#BPM 120",
                        "#WAV01 same.wav",
                        "#00001:01",
                        "#00001:01",
                        "#00001:01",
                        "#00001:01",
                    ]
                ),
                encoding="utf-8",
            )
            chart_dedup = parse_bms(dedup_bms)
            stats_dedup, _ = render_bgm_ogg(chart_dedup, chart_dedup.events, root / "dedup_out" / "bgm.ogg")
            self.assertEqual(stats_dedup.mixed_event_count, 1)
            self.assertLess(stats_dedup.peak_before_norm, 0.8)
            self.assertEqual(stats_dedup.gain_applied, 1.0)

            quiet_bms = dedup_src / "quiet.bms"
            quiet_bms.write_text("#TITLE Quiet\n#BPM 120\n#WAV01 quiet.wav\n#00001:01\n", encoding="utf-8")
            chart_quiet = parse_bms(quiet_bms)
            stats_quiet, _ = render_bgm_ogg(chart_quiet, chart_quiet.events, root / "quiet_out" / "bgm.ogg")
            self.assertEqual(stats_quiet.gain_applied, 1.0)
            self.assertLess(stats_quiet.peak_before_norm, 0.2)
            self.assertAlmostEqual(stats_quiet.peak_after_norm, stats_quiet.peak_before_norm, places=5)

            # 2. Semicolon ';' sanitization in metadata & retained filenames + Windows '\' paths
            semi_src = root / "semi_src"
            (semi_src / "sub").mkdir(parents=True)
            _write_sine_wav(semi_src / "sub" / "kick;1.wav", 120.0, 0.08)
            _write_sine_wav(semi_src / "sub" / "kick_1.wav", 180.0, 0.08)
            semi_bms = semi_src / "semi.bms"
            semi_bms.write_text(
                "\n".join(
                    [
                        "#TITLE Foo;Bar",
                        "#ARTIST A;B",
                        "#GENRE Jazz;House",
                        "#BPM 120",
                        "#WAV01 sub/kick;1.wav",
                        "#WAV02 sub/kick_1.wav",
                        "#00101:0102",
                    ]
                ),
                encoding="utf-8",
            )
            semi_snap = _dir_snapshot(semi_src)
            semi_out = root / "semi_out"
            res_semi = convert_bms(
                semi_bms,
                ConversionOptions(output_dir=semi_out, render_bgm=False),
                mapping=MappingConfig(tab_wavs={TabId.BD: ["01", "02"]}, toggle_on=False),
            )
            self.assertEqual(_dir_snapshot(semi_src), semi_snap)
            dtx_semi = res_semi.dtx_path.read_text(encoding="cp932")
            self.assertIn("#TITLE: Foo；Bar", dtx_semi)
            self.assertIn("#ARTIST: A；B", dtx_semi)
            self.assertIn("Jazz；House", dtx_semi)
            self.assertIn("#WAV02: sub\\kick_1.wav", dtx_semi)
            self.assertIn("#WAV03: sub\\kick_1_1.wav", dtx_semi)
            self.assertTrue((semi_out / "sub" / "kick_1.wav").is_file())
            self.assertTrue((semi_out / "sub" / "kick_1_1.wav").is_file())

            # 3. find_ffmpeg 3-tier lookup: local app dir / bin/ takes precedence over PATH
            app_dir = root / "fake_app"
            (app_dir / "bin").mkdir(parents=True)
            local_bin_ffmpeg = app_dir / "bin" / "ffmpeg.exe"
            local_bin_ffmpeg.write_bytes(b"stub")
            fake_exe = app_dir / "bms2dtx.exe"
            fake_exe.write_bytes(b"stub")
            with mock.patch("core.mixer.sys.executable", str(fake_exe)), mock.patch(
                "core.mixer.shutil.which", return_value=r"C:\path\ffmpeg.exe"
            ):
                self.assertEqual(Path(find_ffmpeg()).resolve(), local_bin_ffmpeg.resolve())
                local_root_ffmpeg = app_dir / "ffmpeg.exe"
                local_root_ffmpeg.write_bytes(b"stub")
                self.assertEqual(Path(find_ffmpeg()).resolve(), local_root_ffmpeg.resolve())

            # 4. Toggle=OFF Tab collision reflows displaced note into bgm.ogg with clear warning
            col_src = root / "reflow_src"
            col_src.mkdir()
            _write_sine_wav(col_src / "snare_main.wav", 180.0, 0.12, amplitude=0.6)
            _write_sine_wav(col_src / "snare_reflow.wav", 880.0, 0.12, amplitude=0.6)
            col_bms = col_src / "reflow.bms"
            col_bms.write_text(
                "\n".join(
                    [
                        "#TITLE ReflowTest",
                        "#BPM 120",
                        "#WAV01 snare_main.wav",
                        "#WAV02 snare_reflow.wav",
                        # At measure 000 pos 1/4 (t=0.5s): 01 on playable lane 11, 02 on BGM 01
                        "#00011:00010000",
                        "#00001:00020000",
                    ]
                ),
                encoding="utf-8",
            )
            res_reflow = convert_bms(
                col_bms,
                ConversionOptions(output_dir=root / "reflow_out", render_bgm=True),
                mapping=MappingConfig(tab_wavs={TabId.SD: ["01", "02"]}, toggle_on=False),
            )
            # Only winner (01 -> DTX 02) on SD channel 12
            dtx_reflow = res_reflow.dtx_path.read_text(encoding="cp932")
            self.assertIn("#00012: 00020000", dtx_reflow)
            self.assertNotIn("03", dtx_reflow.split("#00012:")[1].splitlines()[0])
            # Displaced 02 reflowed into bgm.ogg and is audible at t=0.5s!
            assert res_reflow.bgm_path is not None and res_reflow.mix_stats is not None
            self.assertEqual(res_reflow.mix_stats.mixed_event_count, 1)
            raw_reflow, err = _decode_audio_to_f32_bytes(res_reflow.bgm_path, ffmpeg_exe, 44100)
            self.assertIsNone(err)
            assert raw_reflow is not None
            self.assertGreater(_window_rms(raw_reflow, 44100, 0.51, 0.60), 0.15)
            col_warnings = [w for w in res_reflow.warnings if w.code == "TAB_LANE_COLLISION"]
            self.assertEqual(len(col_warnings), 1)
            self.assertIn("M000", col_warnings[0].message)
            self.assertIn("SD(12)", col_warnings[0].message)
            self.assertIn("#WAV02", col_warnings[0].message)

    def test_preview_audio_resolution_copy_collision_and_missing_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 1. Nested #PREVIEW with suffix correction (.wav -> .ogg) and ';' sanitization
            src1 = root / "prev_src1"
            (src1 / "SubDir").mkdir(parents=True)
            _write_sine_wav(src1 / "kick.wav", 120.0, 0.08)
            (src1 / "SubDir" / "Pre;Song.ogg").write_bytes(b"OggS-preview")
            bms1 = src1 / "prev1.bms"
            bms1.write_text(
                "\n".join(
                    [
                        "#TITLE PreviewTest",
                        "#GENRE Electronic",
                        "#BPM 128",
                        r"#PREVIEW subdir\pre;song.wav",
                        "#WAV01 kick.wav",
                        "#00101:01",
                    ]
                ),
                encoding="utf-8",
            )
            snap1 = _dir_snapshot(src1)
            out1 = root / "prev_out1"
            res1 = convert_bms(
                bms1,
                ConversionOptions(output_dir=out1, render_bgm=False),
                mapping=MappingConfig(tab_wavs={TabId.BD: ["01"]}, toggle_on=False),
            )
            self.assertEqual(_dir_snapshot(src1), snap1)
            self.assertIsNotNone(res1.preview_path)
            self.assertTrue((out1 / "SubDir" / "Pre_Song.ogg").is_file())
            dtx1 = res1.dtx_path.read_text(encoding="cp932")
            self.assertIn(r"#PREVIEW: SubDir\Pre_Song.ogg", dtx1)
            self.assertIn("#COMMENT: Electronic (w/ bms2dtx)", dtx1)

            # 2. #PREVIEW name collision with retained slice -> disambiguated with _1
            src2 = root / "prev_src2"
            src2.mkdir()
            _write_sine_wav(src2 / "kick.wav", 120.0, 0.08)
            bms2 = src2 / "prev2.bms"
            bms2.write_text(
                "#TITLE PrevCollision\n#BPM 120\n#PREVIEW kick.wav\n#WAV01 kick.wav\n#00101:01\n",
                encoding="utf-8",
            )
            out2 = root / "prev_out2"
            res2 = convert_bms(
                bms2,
                ConversionOptions(output_dir=out2, render_bgm=False),
                mapping=MappingConfig(tab_wavs={TabId.BD: ["01"]}, toggle_on=False),
            )
            self.assertTrue((out2 / "kick.wav").is_file())
            self.assertTrue((out2 / "kick_1.wav").is_file())
            dtx2 = res2.dtx_path.read_text(encoding="cp932")
            self.assertIn("#WAV02: kick.wav", dtx2)
            self.assertIn("#PREVIEW: kick_1.wav", dtx2)
            self.assertIn("#COMMENT: prepped w/ bms2dtx", dtx2)

            # 3. Missing #PREVIEW file -> records MISSING_PREVIEW_FILE warning, never crashes
            src3 = root / "prev_src3"
            src3.mkdir()
            _write_sine_wav(src3 / "kick.wav", 120.0, 0.08)
            bms3 = src3 / "prev3.bms"
            bms3.write_text(
                "#TITLE PrevMissing\n#BPM 120\n#PREVIEW missing_preview.mp3\n#WAV01 kick.wav\n#00101:01\n",
                encoding="utf-8",
            )
            out3 = root / "prev_out3"
            res3 = convert_bms(
                bms3,
                ConversionOptions(output_dir=out3, render_bgm=False),
                mapping=MappingConfig(tab_wavs={TabId.BD: ["01"]}, toggle_on=False),
            )
            self.assertIsNone(res3.preview_path)
            self.assertTrue(any(w.code == "MISSING_PREVIEW_FILE" for w in res3.warnings))
            dtx3 = res3.dtx_path.read_text(encoding="cp932")
            self.assertNotIn("#PREVIEW:", dtx3)

    def test_duplicate_wav_same_physical_file_deduplication_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "dup_src"
            src.mkdir()
            _write_sine_wav(src / "Celesta_045.wav", 520.0, 0.08)
            _write_sine_wav(src / "snare_hit.wav", 200.0, 0.08)
            bms = src / "dup.bms"
            bms.write_text(
                "\n".join(
                    [
                        "#TITLE DupPhysical",
                        "#BPM 120",
                        "#WAVV3 Celesta_045.wav",
                        "#WAVV4 Celesta_045.wav",
                        "#WAV25 snare_hit.wav",
                        "#WAV26 snare_hit.wav",
                        "#00101:V3V4",
                        "#00112:2526",
                    ]
                ),
                encoding="utf-8",
            )
            out = root / "dup_out"
            # Even if only "25" and "V3" are passed, sibling IDs "26" and "V4" are merged & mapped to the same DTX slot!
            res = convert_bms(
                bms,
                ConversionOptions(output_dir=out, render_bgm=False),
                mapping=MappingConfig(
                    tab_wavs={
                        TabId.SD: ["25", "26"],
                        TabId.OTHER: ["V3"],
                    },
                    default_lane_binds={TabId.SD: "26"},
                    toggle_on=False,
                ),
            )
            self.assertEqual(res.wav_id_map["25"], "02")
            self.assertEqual(res.wav_id_map["26"], "02")
            self.assertEqual(res.wav_id_map["V3"], "03")
            self.assertEqual(res.wav_id_map["V4"], "03")
            self.assertEqual(
                res.dtx_wav_table,
                [("01", "bgm.ogg"), ("02", "snare_hit.wav"), ("03", "Celesta_045.wav")],
            )
            self.assertEqual(len(res.copied_files), 2)
            dtx = res.dtx_path.read_text(encoding="cp932")
            self.assertIn("#00112: 0202", dtx)
            self.assertIn("#00166: 0303", dtx)
            self.assertIn("#DTXC_LANEBINDEDCHIP: 04 02 00", dtx)


if __name__ == "__main__":
    unittest.main()
