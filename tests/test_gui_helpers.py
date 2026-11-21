"""Unit tests for GUI helpers, preview player (with fakes), filename guesser, and a headless-safe App test."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from typing import Dict, List
from unittest import mock

from core import TAB_SPECS, MappingConfig, TabId, inspect_bms
from gui.auto_guess import guess_assignments, guess_tab_for_filename
from gui.helpers import (
    GRAY_FOREGROUND,
    NO_BIND_LABEL,
    TAB_NOTES,
    AssignmentModel,
    bind_combobox_values,
    build_health_summary,
    check_output_dir,
    default_dlevel_text,
    filter_wav_ids,
    format_wav_row,
    load_mapping_json,
    parse_dlevel,
    radix_needs_notice,
    save_mapping_json,
    sorted_wav_ids,
    suggest_output_dir,
    tab_has_bind_combobox,
    tab_title,
)
from gui.preview_player import PreviewPlayer

BMS_TEXT = """#TITLE GUI Test
#ARTIST Tester
#BPM 120
#PLAYLEVEL 7
#WAV01 bd.wav
#WAV02 sd.wav
#WAV03 hh_open.wav
#WAV04 other.wav
#WAV05 ghost.wav
#00111:01
#00112:02
#00113:03
#00101:04
#00201:04
"""


def _write_silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 4410)


def make_chart_dir(root: Path, name: str = "song") -> Path:
    d = root / name
    d.mkdir(parents=True)
    for fn in ("bd.wav", "sd.wav", "hh_open.wav", "other.wav"):  # ghost.wav intentionally missing
        _write_silent_wav(d / fn)
    bms = d / "test.bms"
    bms.write_bytes(BMS_TEXT.encode("cp932"))
    return bms


def snapshot(folder: Path) -> Dict[str, str]:
    return {
        str(p.relative_to(folder)): hashlib.sha1(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


class ChartCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.bms = make_chart_dir(self.tmp)
        self.chart = inspect_bms(self.bms)


class AssignmentModelTests(ChartCase):
    def test_assign_no_double_assignment_and_remove_resets_bind(self) -> None:
        m = AssignmentModel()
        added, skipped = m.assign(TabId.SD, ["01", "02"])
        self.assertEqual((added, skipped), (["01", "02"], []))
        added, skipped = m.assign(TabId.BD, ["02", "03"])
        self.assertEqual(added, ["03"])
        self.assertEqual(skipped, ["02"])
        self.assertEqual(m.tab_of("02"), TabId.SD)
        self.assertEqual(m.assigned_ids, {"01", "02", "03"})
        m.set_bind(TabId.SD, "01")
        self.assertEqual(m.get_bind(TabId.SD), "01")
        m.remove(TabId.SD, ["01"])
        self.assertIsNone(m.get_bind(TabId.SD))
        self.assertIsNone(m.tab_of("01"))
        self.assertEqual(m.count(TabId.SD), 1)

    def test_bind_only_for_drum_tabs_and_member_wavs(self) -> None:
        m = AssignmentModel()
        m.assign(TabId.OTHER, ["01"])
        with self.assertRaises(ValueError):
            m.set_bind(TabId.OTHER, "01")
        m.assign(TabId.SD, ["02"])
        with self.assertRaises(ValueError):
            m.set_bind(TabId.SD, "09")
        m.set_bind(TabId.SD, None)
        self.assertIsNone(m.get_bind(TabId.SD))
        self.assertTrue(all(tab_has_bind_combobox(s.tab_id) == s.is_drum for s in TAB_SPECS))

    def test_mapping_config_is_toggle_off(self) -> None:
        m = AssignmentModel()
        m.assign(TabId.BD, ["01"])
        m.set_bind(TabId.BD, "01")
        cfg = m.to_mapping_config()
        self.assertIsInstance(cfg, MappingConfig)
        self.assertFalse(cfg.toggle_on)
        self.assertEqual(cfg.tab_wavs, {TabId.BD: ["01"]})
        self.assertEqual(cfg.default_lane_binds, {TabId.BD: "01"})

    def test_json_roundtrip_matches_cli_loader(self) -> None:
        import cli

        m = AssignmentModel()
        m.assign(TabId.HH_OPEN, ["03"])
        m.assign(TabId.BD, ["01"])
        m.set_bind(TabId.HH_OPEN, "03")
        path = self.tmp / "map.json"
        save_mapping_json(m, path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["tab_wavs"]["HH_OPEN"], ["03"])
        self.assertFalse(raw["toggle_on"])
        cfg = cli._load_mapping_json(path)
        self.assertEqual(cfg.tab_wavs[TabId.BD], ["01"])
        self.assertEqual(cfg.default_lane_binds[TabId.HH_OPEN], "03")

        m2 = AssignmentModel()
        m2.assign(TabId.LC, ["04"])  # replaced on load
        notes = load_mapping_json(m2, path, self.chart)
        self.assertEqual(notes, [])
        self.assertEqual(m2.tab_wavs[TabId.HH_OPEN], ["03"])
        self.assertEqual(m2.tab_wavs[TabId.LC], [])
        self.assertEqual(m2.get_bind(TabId.HH_OPEN), "03")

    def test_json_load_reports_unknowns_and_toggle_on(self) -> None:
        m = AssignmentModel()
        notes = m.load_json_dict(
            {
                "tab_wavs": {"SD": ["02", "ZZ", "02"], "NOPE": ["01"], "bd": ["01"]},
                "default_lane_binds": {"SD": "09"},
                "toggle_on": True,
            },
            self.chart,
        )
        self.assertEqual(m.tab_wavs[TabId.SD], ["02"])
        self.assertEqual(m.tab_wavs[TabId.BD], ["01"])
        self.assertTrue(any("ZZ" in n for n in notes))
        self.assertTrue(any("NOPE" in n for n in notes))
        self.assertTrue(any("toggle_on" in n for n in notes))
        self.assertIsNone(m.get_bind(TabId.SD))


class ListFormattingTests(ChartCase):
    def test_sorted_and_filtered(self) -> None:
        self.assertEqual(sorted_wav_ids(self.chart), ["01", "02", "03", "04", "05"])
        self.assertEqual(filter_wav_ids(self.chart, "HH"), ["03"])
        self.assertEqual(filter_wav_ids(self.chart, "02"), ["02"])
        self.assertEqual(filter_wav_ids(self.chart, "  SD.WAV "), ["02"])
        self.assertEqual(filter_wav_ids(self.chart, "nothing"), [])
        used = filter_wav_ids(self.chart, only_used=True)
        self.assertEqual(used, [w for w in sorted_wav_ids(self.chart) if self.chart.wavs[w].use_count > 0])

    def test_row_tags(self) -> None:
        row = format_wav_row(self.chart.wavs["01"], None)
        self.assertTrue(row.startswith("[01] bd.wav (×"))
        missing = format_wav_row(self.chart.wavs["05"], TabId.SD)
        self.assertIn("[缺失]", missing)
        self.assertTrue(missing.endswith("→ SD"))
        entry = self.chart.wavs["02"]
        entry.suffix_corrected = True  # in-memory only; left list omits redundant [已修正] tag in v1.1
        self.assertNotIn("[已修正]", format_wav_row(entry))
        self.assertEqual(GRAY_FOREGROUND, "#888888")

    def test_duplicate_physical_file_grouping_in_list_and_model(self) -> None:
        dup_dir = self.tmp / "dup_gui"
        dup_dir.mkdir()
        _write_silent_wav(dup_dir / "Celesta_045.wav")
        _write_silent_wav(dup_dir / "kick.wav")
        bms = dup_dir / "dup.bms"
        bms.write_bytes(
            (
                "#TITLE DupGui\n#BPM 120\n"
                "#WAVV3 Celesta_045.wav\n"
                "#WAVV4 Celesta_045.wav\n"
                "#WAV01 kick.wav\n"
                "#00101:V3V4V401\n"
            ).encode("cp932")
        )
        chart = inspect_bms(bms)
        # V3 (×1) and V4 (×2) share Celesta_045.wav -> folded into 1 row [V3, V4] Celesta_045.wav (×3)
        filtered = filter_wav_ids(chart)
        self.assertEqual(filtered, ["01", "V3"])
        self.assertEqual(filter_wav_ids(chart, "V4"), ["V3"])
        row = format_wav_row(chart.wavs["V3"], None, chart=chart)
        self.assertEqual(row, "[V3, V4] Celesta_045.wav (×3)")

        m = AssignmentModel(chart=chart)
        added, skipped = m.assign(TabId.OTHER, ["V3"])
        self.assertEqual((added, skipped), (["V3", "V4"], []))
        self.assertEqual(m.tab_of("V3"), TabId.OTHER)
        self.assertEqual(m.tab_of("V4"), TabId.OTHER)
        self.assertEqual(m.count(TabId.OTHER), 1)
        removed = m.remove(TabId.OTHER, ["V3"])
        self.assertEqual(removed, ["V3", "V4"])
        self.assertFalse(m.has_assignments)

    def test_tab_title_badges_and_notes(self) -> None:
        self.assertEqual(tab_title(TabId.SD, 6), "SD (6)")
        self.assertEqual(tab_title(TabId.SD, 0), "SD")
        self.assertEqual(tab_title(TabId.HH_CLOSE, 2), "HH Close (2)")
        self.assertIn("SE6", TAB_NOTES[TabId.OTHER])
        self.assertIn("66", TAB_NOTES[TabId.OTHER])
        self.assertIn("SE11–SE12", TAB_NOTES[TabId.GUITAR])
        self.assertIn("SE13–SE14", TAB_NOTES[TabId.BASS])

    def test_bind_values_follow_tab_contents(self) -> None:
        m = AssignmentModel()
        self.assertEqual(bind_combobox_values(m, self.chart, TabId.SD), [(NO_BIND_LABEL, None)])
        m.assign(TabId.SD, ["02"])
        # Default v1.2 mode is 'auto': offers [Auto] sentinel first, then None, then slices
        values_auto = bind_combobox_values(m, self.chart, TabId.SD)
        self.assertEqual([v[1] for v in values_auto], ["__AUTO__", None, "02"])
        self.assertIn("sd.wav", values_auto[0][0])
        self.assertIn("sd.wav", values_auto[2][0])
        # Switching to 'manual' omits [Auto] sentinel
        m.set_default_bind_mode("manual")
        values_manual = bind_combobox_values(m, self.chart, TabId.SD)
        self.assertEqual([v[1] for v in values_manual], [None, "02"])
        self.assertIn("sd.wav", values_manual[1][0])

    def test_missing_files_with_same_filename_never_merged_in_groups(self) -> None:
        miss_dir = self.tmp / "miss_gui"
        miss_dir.mkdir()
        bms = miss_dir / "miss.bms"
        bms.write_bytes(
            (
                "#TITLE MissingSep\n#BPM 120\n"
                "#WAV01 missing_kick.wav\n"
                "#WAV02 missing_kick.wav\n"
                "#00101:0102\n"
            ).encode("cp932")
        )
        chart = inspect_bms(bms)
        self.assertFalse(chart.wavs["01"].exists)
        self.assertFalse(chart.wavs["02"].exists)
        # Missing files must stay independent and never collapse into one row!
        self.assertEqual(filter_wav_ids(chart), ["01", "02"])


class SummaryAndExportHelperTests(ChartCase):
    def test_health_summary(self) -> None:
        rows = dict(build_health_summary(self.chart))
        self.assertEqual(rows["标题"], "GUI Test")
        self.assertEqual(rows["艺术家"], "Tester")
        self.assertEqual(rows["BPM"], "120")
        self.assertEqual(rows["编码"], self.chart.encoding_used)
        self.assertEqual(rows["进制"], "Base-36 兼容")
        self.assertIn("共 5", rows["WAV 数量"])
        self.assertIn("缺失 1", rows["WAV 数量"])
        self.assertIn("BGM 列", rows)
        self.assertFalse(radix_needs_notice(self.chart))
        self.assertEqual(default_dlevel_text(self.chart), "7")

    def test_suggested_output_dir_is_sibling_not_inside(self) -> None:
        out = suggest_output_dir(self.bms)
        self.assertEqual(out.name, "song_dtx")
        self.assertEqual(out.parent, self.bms.resolve().parent.parent)
        self.assertIsNone(check_output_dir(out, self.chart))

    def test_check_output_dir_rejects_source_folder(self) -> None:
        self.assertIn("铁律 1", check_output_dir(self.bms.parent, self.chart))
        self.assertIn("铁律 1", check_output_dir(str(self.bms.parent) + "\\.", self.chart))
        self.assertIsNone(check_output_dir(self.tmp / "elsewhere", self.chart))

    def test_parse_dlevel(self) -> None:
        self.assertIsNone(parse_dlevel("  "))
        self.assertEqual(parse_dlevel(" 450 "), 450)
        with self.assertRaises(ValueError):
            parse_dlevel("abc")
        with self.assertRaises(ValueError):
            parse_dlevel("-3")


class AutoGuessTests(unittest.TestCase):
    def test_guess_names(self) -> None:
        cases = {
            "kick01.wav": TabId.BD,
            "kck_01.wav": TabId.BD,
            "bdrum_2.ogg": TabId.BD,
            "BD_1.ogg": TabId.BD,
            "キック_01.wav": TabId.BD,
            "バスドラ.wav": TabId.BD,
            "snare.wav": TabId.SD,
            "sn01.wav": TabId.SD,
            "snr_hard.wav": TabId.SD,
            "rim.wav": TabId.SD,
            "clap02.wav": TabId.SD,
            "SD2.wav": TabId.SD,
            "スネア_01.wav": TabId.SD,
            "hihat_closed.wav": TabId.HH_CLOSE,
            "hhc_01.wav": TabId.HH_CLOSE,
            "ハイハット.wav": TabId.HH_CLOSE,
            "hh_open.wav": TabId.HH_OPEN,
            "hho.wav": TabId.HH_OPEN,
            "open hihat.wav": TabId.HH_OPEN,
            "ハイハット_オープン.wav": TabId.HH_OPEN,
            "crash1.wav": TabId.CY,
            "cr_02.wav": TabId.CY,
            "cym01.wav": TabId.CY,
            "splash_01.wav": TabId.CY,
            "クラッシュ.wav": TabId.CY,
            "ride.wav": TabId.RD,
            "rd01.wav": TabId.RD,
            "hightom.wav": TabId.HT,
            "lowtom.wav": TabId.LT,
            "floor tom.wav": TabId.FT,
            "bass drum.wav": TabId.BD,
        }
        for name, tab in cases.items():
            self.assertEqual(guess_tab_for_filename(name), tab, name)

    def test_unknown_names_and_boundary_safety_return_none(self) -> None:
        for name in (
            "piano_c4.wav",
            "bass_synth.wav",
            "voice01.ogg",
            "123.wav",
            "",
            "snow_lead.wav",
            "synth_pad.wav",
            "snake_fx.wav",
            "crisp_bell_synth.wav",
        ):
            if "bell" in name:
                continue  # 'bell' token maps to RD; test crisp_fx instead
            self.assertIsNone(guess_tab_for_filename(name), name)
        self.assertIsNone(guess_tab_for_filename("crisp_fx.wav"))

    def test_guess_assignments_groups_ids_and_tom_sequence(self) -> None:
        res = guess_assignments(
            [
                ("01", "kick.wav"),
                ("02", "snow_01.wav"),
                ("03", "kick2.wav"),
                ("04", "sn04.wav"),
                ("05", "048_TOM1.wav"),
                ("06", "045_TOM2.wav"),
                ("07", "041_TOM3.wav"),
            ]
        )
        self.assertEqual(
            res,
            {
                TabId.BD: ["01", "03"],
                TabId.SD: ["04"],
                TabId.HT: ["05"],
                TabId.LT: ["06"],
                TabId.FT: ["07"],
            },
        )


# ----------------------------------------------------------------- preview player
class FakeWinsound:
    SND_FILENAME = 0x20000
    SND_ASYNC = 0x0001
    SND_MEMORY = 0x0004
    SND_PURGE = 0x0040

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def PlaySound(self, sound, flags) -> None:  # noqa: N802 - mimics winsound
        if flags & self.SND_MEMORY:
            raise RuntimeError("Cannot play asynchronously from memory")
        self.calls.append((sound, flags))

    def played(self) -> List[str]:
        return [c[0] for c in self.calls if c[0] is not None]


class PreviewPlayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.src_ogg = self.tmp / "a.ogg"
        self.src_ogg.write_bytes(b"x")
        self.src_wav = self.tmp / "b.wav"
        _write_silent_wav(self.src_wav)
        self.ws = FakeWinsound()
        self.status: List[str] = []
        self.decoded: List[Path] = []

    def make(self, ffmpeg="ffmpeg.exe", decoder=None) -> PreviewPlayer:
        def default_decoder(_ff: str, src: Path, dst: Path) -> None:
            self.decoded.append(dst)
            dst.write_bytes(b"RIFF")

        player = PreviewPlayer(
            lambda: ffmpeg,
            on_status=self.status.append,
            winsound_module=self.ws,
            decoder=decoder or default_decoder,
            temp_root=self.tmp,
        )
        self.addCleanup(player.shutdown)
        return player

    def test_decodes_to_temp_wav_and_plays_with_filename_async(self) -> None:
        player = self.make()
        self.assertTrue(player.play(self.src_ogg))
        player._last_thread.join(5)
        played = self.ws.played()
        self.assertEqual(len(played), 1)
        self.assertTrue(played[0].endswith("slot_0.wav"))
        flags = [c[1] for c in self.ws.calls if c[0] == played[0]][0]
        self.assertEqual(flags, FakeWinsound.SND_FILENAME | FakeWinsound.SND_ASYNC)
        self.assertFalse(flags & FakeWinsound.SND_MEMORY)
        # purge issued before playing
        self.assertEqual(self.ws.calls[0], (None, FakeWinsound.SND_PURGE))

    def test_rotates_two_slots(self) -> None:
        player = self.make()
        for _ in range(3):
            player.play(self.src_ogg)
            player._last_thread.join(5)
        names = [Path(p).name for p in self.ws.played()]
        self.assertEqual(names, ["slot_0.wav", "slot_1.wav", "slot_0.wav"])

    def test_stale_decode_is_discarded(self) -> None:
        gate = threading.Event()
        started = threading.Event()
        order: List[str] = []

        def decoder(_ff: str, src: Path, dst: Path) -> None:
            order.append(dst.name)
            if len(order) == 1:
                started.set()
                gate.wait(5)
            dst.write_bytes(b"RIFF")

        player = self.make(decoder=decoder)
        player.play(self.src_ogg)
        first = player._last_thread
        self.assertTrue(started.wait(5))
        player.play(self.src_ogg)
        second = player._last_thread
        second.join(5)
        gate.set()
        first.join(5)
        played = [Path(p).name for p in self.ws.played()]
        self.assertEqual(played, ["slot_1.wav"])  # only the newest decode is played

    def test_stop_discards_pending_decode_and_purges(self) -> None:
        gate = threading.Event()

        def decoder(_ff: str, src: Path, dst: Path) -> None:
            gate.wait(5)
            dst.write_bytes(b"RIFF")

        player = self.make(decoder=decoder)
        player.play(self.src_ogg)
        thread = player._last_thread
        player.stop()
        gate.set()
        thread.join(5)
        self.assertEqual(self.ws.played(), [])
        self.assertEqual(self.ws.calls[-1], (None, FakeWinsound.SND_PURGE))

    def test_without_ffmpeg_plays_wav_directly_but_not_others(self) -> None:
        player = self.make(ffmpeg=None)
        self.assertTrue(player.play(self.src_wav))
        self.assertEqual(self.ws.played(), [str(self.src_wav)])
        self.ws.calls.clear()
        self.assertFalse(player.play(self.src_ogg))
        self.assertEqual(self.ws.played(), [])
        self.assertTrue(any("ffmpeg" in s for s in self.status))

    def test_unsupported_wav_error_is_caught(self) -> None:
        class Boom(FakeWinsound):
            def PlaySound(self, sound, flags) -> None:  # noqa: N802
                if sound is not None:
                    raise RuntimeError("bad format")

        self.ws = Boom()
        player = self.make(ffmpeg=None)
        player.play(self.src_wav)
        self.assertTrue(any("无法播放" in s for s in self.status))

    def test_decode_failure_falls_back_for_wav_and_reports_for_others(self) -> None:
        def decoder(_ff: str, src: Path, dst: Path) -> None:
            raise RuntimeError("ffmpeg exploded")

        player = self.make(decoder=decoder)
        player.play(self.src_ogg)
        player._last_thread.join(5)
        self.assertEqual(self.ws.played(), [])
        self.assertTrue(any("解码失败" in s for s in self.status))
        player.play(self.src_wav)
        player._last_thread.join(5)
        self.assertEqual(self.ws.played(), [str(self.src_wav)])

    def test_missing_source_and_shutdown_cleanup(self) -> None:
        player = self.make()
        self.assertFalse(player.play(self.tmp / "nope.wav"))
        player.play(self.src_ogg)
        player._last_thread.join(5)
        temp_dir = player.temp_dir
        self.assertTrue(temp_dir.is_dir())
        player.shutdown()
        self.assertFalse(temp_dir.exists())
        self.assertEqual(self.ws.calls[-1], (None, FakeWinsound.SND_PURGE))
        self.assertFalse(player.play(self.src_ogg))

    def test_swap_lr_forwarded_and_ffmpeg_swaps_decoded_channels(self) -> None:
        import math
        import struct
        from core.mixer import find_ffmpeg
        from gui.preview_player import decode_with_ffmpeg

        flags: List[bool] = []
        swap_state = [False]

        def cap_decoder(_ff: str, src: Path, dst: Path, *, swap_lr: bool = False) -> None:
            flags.append(swap_lr)
            dst.write_bytes(b"RIFF")

        player = PreviewPlayer(
            lambda: "ffmpeg.exe",
            on_status=self.status.append,
            winsound_module=self.ws,
            decoder=cap_decoder,
            temp_root=self.tmp,
            swap_lr_getter=lambda: swap_state[0],
        )
        self.addCleanup(player.shutdown)
        player.play(self.src_wav)
        player._last_thread.join(5)
        swap_state[0] = True
        player.play(self.src_wav)
        player._last_thread.join(5)
        self.assertEqual(flags, [False, True])

        # Verify decode_with_ffmpeg swaps real stereo L/R audio channels
        ffmpeg_exe = find_ffmpeg()
        stereo_src = self.tmp / "left_only.wav"
        decoded_dst = self.tmp / "swapped_out.wav"
        with wave.open(str(stereo_src), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            frames = bytearray()
            for i in range(2205):
                val = int(round(20000 * math.sin(2.0 * math.pi * 440.0 * i / 44100)))
                frames.extend(struct.pack("<hh", val, 0))
            wf.writeframes(bytes(frames))
        decode_with_ffmpeg(ffmpeg_exe, stereo_src, decoded_dst, swap_lr=True)
        with wave.open(str(decoded_dst), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
        left_energy = sum(x * x for x in samples[0::2])
        right_energy = sum(x * x for x in samples[1::2])
        self.assertLess(left_energy, right_energy * 0.01)
        self.assertGreater(right_energy, 100000)


# --------------------------------------------------------------------- GUI (Tk)
def _tk_root():
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display available
        raise unittest.SkipTest(f"Tk unavailable: {exc}")
    root.withdraw()
    return root


class I18nTests(unittest.TestCase):
    def tearDown(self) -> None:
        from gui.i18n import DEFAULT_LANGUAGE, set_language

        set_language(DEFAULT_LANGUAGE)

    def test_dictionary_completeness_across_all_3_languages(self) -> None:
        from gui.i18n import LANGUAGE_CODES, TRANSLATIONS, set_language, tr

        self.assertEqual(set(LANGUAGE_CODES), {"zh_CN", "en", "ja"})
        base_keys = set(TRANSLATIONS["zh_CN"].keys())
        self.assertGreater(len(base_keys), 50)
        for lang in LANGUAGE_CODES:
            self.assertEqual(
                set(TRANSLATIONS[lang].keys()),
                base_keys,
                msg=f"Key mismatch in language {lang}",
            )
            for k, val in TRANSLATIONS[lang].items():
                self.assertIsInstance(val, str, msg=f"{lang}.{k} is not str")
                self.assertTrue(val.strip(), msg=f"{lang}.{k} is empty")

        set_language("en")
        self.assertIn("#DLEVEL", tr("dlevel_label"))
        set_language("ja")
        self.assertIn("#DLEVEL", tr("dlevel_label"))
        set_language("zh_CN")
        self.assertIn("#DLEVEL", tr("dlevel_label"))

    def test_save_and_load_language_and_app_settings(self) -> None:
        from gui.i18n import (
            load_app_settings,
            load_language_preference,
            save_app_settings,
            save_language_preference,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "settings.json"
            self.assertEqual(load_language_preference(cfg), "zh_CN")
            self.assertTrue(save_language_preference("ja", cfg))
            self.assertEqual(load_language_preference(cfg), "ja")
            self.assertTrue(save_language_preference("en", cfg))
            self.assertEqual(load_language_preference(cfg), "en")
            self.assertTrue(
                save_app_settings(
                    {
                        "ffmpeg_path": r"C:\tools\ffmpeg.exe",
                        "default_bind_mode": "manual",
                        "swap_lr": True,
                        "wav_labels": {"BGM": "Bg;Mix\n", "SD": "Snare"},
                    },
                    cfg,
                )
            )
            loaded = load_app_settings(cfg)
            self.assertEqual(loaded["language"], "en")
            self.assertEqual(loaded["ffmpeg_path"], r"C:\tools\ffmpeg.exe")
            self.assertEqual(loaded["default_bind_mode"], "manual")
            self.assertTrue(loaded["swap_lr"])
            self.assertEqual(loaded["wav_labels"]["BGM"], "BgMix")
            self.assertEqual(loaded["wav_labels"]["SD"], "Snare")


class AppIntegrationTests(ChartCase):
    def setUp(self) -> None:
        super().setUp()
        from gui.i18n import DEFAULT_LANGUAGE, set_language

        set_language(DEFAULT_LANGUAGE)
        self.root = _tk_root()
        from gui import app as app_mod

        self.app_mod = app_mod
        self.app = app_mod.App(self.root, startup_dialogs=False)
        self.addCleanup(self._close)
        self.app.apply_chart(self.chart)
        self.root.update()

    def _close(self) -> None:
        from gui.i18n import DEFAULT_LANGUAGE, set_language

        set_language(DEFAULT_LANGUAGE)
        try:
            self.app.on_close()
        except Exception:
            pass

    def test_layout_has_15_tabs_in_canonical_order_and_toggle_hidden(self) -> None:
        # Window title includes full loaded BMS path; slim top bar has config button
        self.assertIn(str(self.bms), self.root.title())
        self.assertFalse(self.app.separate_lbd_var.get())
        self.assertEqual(
            self.app.visible_tabs(),
            [s.tab_id for s in TAB_SPECS if s.tab_id != TabId.LBD],
        )
        # Checking separate_lbd reveals all 15 tabs in canonical TAB_SPECS order
        self.app.separate_lbd_var.set(True)
        self.app._on_toggle_separate_lbd()
        self.assertEqual(self.app.visible_tabs(), [s.tab_id for s in TAB_SPECS])
        titles = [self.app.tab_buttons[s.tab_id].cget("text") for s in TAB_SPECS]
        self.assertEqual(titles, [s.label for s in TAB_SPECS])
        self.assertEqual(str(self.app.left_tree.cget("selectmode")), "extended")
        self.assertEqual(tuple(self.app.left_tree.cget("columns")), ("id", "filename", "count", "target"))
        for tab, panel in self.app.panels.items():
            self.assertEqual(str(panel.listbox.cget("selectmode")), "extended")
            self.assertEqual(panel.combo is not None, tab_has_bind_combobox(tab))
        self.assertFalse(hasattr(self.app, "toggle_var"))
        # Progressbar at rest is determinate with value 0 (never stuck in indeterminate green block)
        self.assertEqual(str(self.app.progress.cget("mode")), "determinate")
        self.assertEqual(float(self.app.progress.cget("value")), 0.0)

    def test_import_does_not_auto_guess(self) -> None:
        self.assertFalse(self.app.model.has_assignments)
        self.assertFalse(self.app.dirty)

    def test_add_gray_remove_cycle_and_bind_reset(self) -> None:
        app = self.app
        app.left_tree.selection_set(("01", "02"))
        app.select_tab(TabId.SD)
        app.on_add()
        self.assertEqual(app.model.tab_wavs[TabId.SD], ["01", "02"])
        self.assertEqual(app.tab_buttons[TabId.SD].cget("text"), "SD (2)")
        self.assertIn("assigned", app.left_tree.item("01", "tags"))
        self.assertEqual(app.left_tree.item("01", "values")[3], "SD")
        self.assertEqual(str(app.left_tree.tag_configure("assigned", "foreground")), GRAY_FOREGROUND)
        # cannot double assign into another tab
        app.left_tree.selection_set(("01",))
        app.select_tab(TabId.BD)
        app.on_add()
        self.assertEqual(app.model.tab_wavs[TabId.BD], [])
        # default bind combobox defaults to auto most-frequent ([自动最高频])
        combo = app.panels[TabId.SD].combo
        vals = combo.cget("values")
        self.assertEqual(len(vals), 4)
        self.assertTrue(vals[0].startswith("[自动最高频]"))
        # select specific slice ("01" at index 2) manually
        combo.current(2)
        app.panels[TabId.SD]._on_bind_selected()
        self.assertEqual(app.model.get_bind(TabId.SD), "01")
        # remove bound wav "01" -> falls back to remaining slice's auto-bind or explicit None
        app.select_tab(TabId.SD)
        app.panels[TabId.SD].listbox.selection_set(0)
        app.on_remove()
        self.assertEqual(app.model.tab_wavs[TabId.SD], ["02"])
        self.assertIsNone(app.model.get_bind(TabId.SD))
        self.assertTrue(combo.get().startswith("[自动最高频]"))
        # Explicitly choose (未指定 / 00) -> stays unbound
        combo.current(1)
        app.panels[TabId.SD]._on_bind_selected()
        self.assertEqual(combo.get(), NO_BIND_LABEL)
        self.assertNotIn("assigned", app.left_tree.item("01", "tags"))
        # Remove remaining "02" -> empty tab shows NO_BIND_LABEL
        app.panels[TabId.SD].listbox.selection_set(0)
        app.on_remove()
        self.assertEqual(combo.get(), NO_BIND_LABEL)

    def test_click_and_arrow_key_auto_preview_and_language_switch(self) -> None:
        app = self.app
        self.assertTrue(app.auto_preview_var.get())
        with mock.patch.object(app.player, "play") as play_mock:
            # Click index 1 ("02" -> sd.wav)
            app._on_left_click_index(1)
            play_mock.assert_called_once_with(self.chart.wavs["02"].resolved_path)
            play_mock.reset_mock()

            # Arrow key press + release cycle on focus "03" -> hh_open.wav plays once without stutter
            app.left_tree.focus("03")
            app._on_left_arrow_press()
            self.root.update()
            play_mock.assert_called_once_with(self.chart.wavs["03"].resolved_path)
            app._on_left_arrow_release()
            play_mock.assert_called_once_with(self.chart.wavs["03"].resolved_path)
            play_mock.reset_mock()

            # Direct _on_left_arrow_key call also works
            app.left_tree.focus("02")
            app._on_left_arrow_key()
            play_mock.assert_called_once_with(self.chart.wavs["02"].resolved_path)
            play_mock.reset_mock()

            # Right panel click / spacebar preview sync
            app.select_tab(TabId.BD)
            app.model.assign(TabId.BD, ["04"], chart=self.chart)
            app.refresh_all()
            app.panels[TabId.BD].listbox.selection_set(0)
            app.preview_from_tab(TabId.BD)
            play_mock.assert_called_once_with(self.chart.wavs["04"].resolved_path)
            play_mock.reset_mock()
            app._last_focus_side = "right"
            app.on_preview_button()
            play_mock.assert_called_once_with(self.chart.wavs["04"].resolved_path)
            play_mock.reset_mock()

            # Uncheck auto_preview_var -> click/arrow no longer auto-plays, but spacebar preview still works
            app.auto_preview_var.set(False)
            app._on_left_click_index(0)
            app._on_left_arrow_key()
            play_mock.assert_not_called()
            app.left_tree.selection_set(("01",))
            app.left_tree.focus("01")
            app.preview_from_left()
            play_mock.assert_called_once_with(self.chart.wavs["01"].resolved_path)
            app.auto_preview_var.set(True)

        # Toggle Separate LBD on -> assign slice & bind to LBD -> toggle off clears LBD & switches active tab
        app.separate_lbd_var.set(True)
        app._on_toggle_separate_lbd()
        app.select_tab(TabId.LBD)
        app.model.assign(TabId.LBD, ["01"], chart=self.chart)
        app.model.set_bind(TabId.LBD, "01")
        app.refresh_all()
        self.assertEqual(app.active_tab(), TabId.LBD)
        self.assertEqual(app.model.get_bind(TabId.LBD), "01")
        app.separate_lbd_var.set(False)
        app._on_toggle_separate_lbd()
        self.assertEqual(app.active_tab(), TabId.LP)
        self.assertEqual(app.model.tab_wavs[TabId.LBD], [])
        self.assertIsNone(app.model.get_bind(TabId.LBD))

        # Dynamic language switch + persistence (including status bar when chart is loaded)
        cfg_file = self.tmp / "gui_lang.json"
        app.config_path = cfg_file
        app._persist_settings = True
        app.change_language("en")
        self.assertIn("Drums Level", app.dlevel_label.cget("text"))
        self.assertIn("Loaded", app.status_var.get())
        app.change_language("ja")
        self.assertIn("ドラム難易度", app.dlevel_label.cget("text"))
        self.assertIn("読込完了", app.status_var.get())
        from gui.i18n import load_language_preference

        self.assertEqual(load_language_preference(cfg_file), "ja")
        app.change_language("zh_CN")
        self.assertIn("鼓难度", app.dlevel_label.cget("text"))
        self.assertIn("已载入", app.status_var.get())

    def test_undo_redo_entry_focus_guard_dirty_close_guard_and_drop_filter(self) -> None:
        import tkinter as tk

        app = self.app
        self.assertFalse(app.dirty)

        # Add -> dirty=True
        app.left_tree.selection_set(("01", "02"))
        app.select_tab(TabId.SD)
        app.on_add()
        self.assertTrue(app.dirty)
        self.assertEqual(app.model.tab_wavs[TabId.SD], ["01", "02"])

        # Change bind -> Undo reverts bind first
        app.panels[TabId.SD].combo.current(2)
        app.panels[TabId.SD]._on_bind_selected()
        self.assertEqual(app.model.get_bind(TabId.SD), "01")

        # Entry focus guard: Ctrl+Z while Entry is focused must NOT pop mapping undo stack
        dummy_entry = tk.Entry(self.root)
        with mock.patch.object(self.root, "focus_get", return_value=dummy_entry):
            res = app._on_ctrl_z()
        self.assertIsNone(res)
        self.assertEqual(app.model.get_bind(TabId.SD), "01")

        # When non-Entry has focus, Ctrl+Z undoes bind, then undoes add (returning dirty to False!)
        with mock.patch.object(self.root, "focus_get", return_value=app.left_tree):
            self.assertEqual(app._on_ctrl_z(), "break")
            self.assertIsNone(app.model.get_bind(TabId.SD))
            self.assertEqual(app._on_ctrl_z(), "break")
            self.assertEqual(app.model.tab_wavs[TabId.SD], [])
            self.assertFalse(app.dirty)
            # Redo restores add and dirty=True
            self.assertEqual(app._on_ctrl_y(), "break")
            self.assertEqual(app.model.tab_wavs[TabId.SD], ["01", "02"])
            self.assertTrue(app.dirty)

        # Dirty close guard prompts askyesno when startup_dialogs=True and cancels close on False
        app.startup_dialogs = True
        with mock.patch.object(self.app_mod.messagebox, "askyesno", return_value=False) as ask:
            app.on_close()
            ask.assert_called_once()
        # Drop filter rejects non-BMS file with warning and never crashes
        with mock.patch.object(self.app_mod.messagebox, "showwarning") as warn:
            app._handle_dropped_files([str(self.tmp / "song" / "bd.wav")])
            warn.assert_called_once()
        app.startup_dialogs = False

    def test_search_filter_updates_list(self) -> None:
        self.app.search_var.set("hh_open")
        self.root.update()
        self.assertEqual(self.app._left_ids, ["03"])
        self.app.search_var.set("")
        self.root.update()
        self.assertEqual(len(self.app._left_ids), 5)

    def test_guess_only_fills_unassigned_and_asks_confirmation(self) -> None:
        app = self.app
        app.model.assign(TabId.OTHER, ["01"])  # manual: 01 (bd.wav) kept even though it "looks like" BD
        app.refresh_all()
        with mock.patch.object(self.app_mod.messagebox, "askyesno", return_value=False) as ask:
            app.on_guess()
            ask.assert_called_once()
        self.assertEqual(app.model.tab_wavs[TabId.SD], [])
        with mock.patch.object(self.app_mod.messagebox, "askyesno", return_value=True):
            app.on_guess()
        self.assertEqual(app.model.tab_wavs[TabId.OTHER], ["01"])
        self.assertEqual(app.model.tab_wavs[TabId.SD], ["02"])
        self.assertEqual(app.model.tab_wavs[TabId.HH_OPEN], ["03"])

    def test_export_rejects_source_folder(self) -> None:
        app = self.app
        app.out_var.set(str(self.bms.parent))
        with mock.patch.object(self.app_mod.messagebox, "showerror") as err:
            app.on_export()
            err.assert_called_once()
        self.assertFalse(app._busy)

    def test_background_export_without_ffmpeg_leaves_source_untouched(self) -> None:
        app = self.app
        app.ffmpeg_path = None
        app._update_ffmpeg_ui()
        # v0.4.0: built-in soundfile enables .ogg transcode & bgm.ogg mixing even without ffmpeg
        self.assertEqual(str(app.ogg_chk.cget("state")), "normal")
        self.assertEqual(app.ffmpeg_warn_btn.winfo_manager(), "")
        app.left_tree.selection_set(("01",))
        app.select_tab(TabId.BD)
        app.on_add()
        app.left_tree.selection_set(("02",))
        app.select_tab(TabId.SD)
        app.on_add()
        app.model.set_bind(TabId.SD, "02")
        app.refresh_all()
        self.assertTrue(app.dirty)
        out = self.tmp / "out_dtx"
        app.out_var.set(str(out))
        before = snapshot(self.bms.parent)
        shown: List[str] = []
        with mock.patch.object(self.app_mod.messagebox, "askyesno", return_value=True), mock.patch.object(
            app, "show_text_dialog", side_effect=lambda title, text, **kw: shown.append(text)
        ), mock.patch.object(self.app_mod.messagebox, "showerror") as err:
            app.on_export()
            self.assertTrue(app._busy)
            for _ in range(400):
                self.root.update()
                if not app._busy:
                    break
                threading.Event().wait(0.05)
            err.assert_not_called()
        self.assertFalse(app._busy)
        self.assertFalse(app.dirty)  # dirty resets after successful export!
        self.assertTrue((out / "test.dtx").is_file())
        text = (out / "test.dtx").read_bytes().decode("cp932")
        self.assertIn("#DTXC_LANEBINDEDCHIP: 04", text)
        self.assertIn("\t;SD", text)
        self.assertTrue(shown and "test.dtx" in shown[0])
        self.assertEqual(snapshot(self.bms.parent), before)

    def test_v030_version_i18n_load_dir_and_lp_tab_dual_row_undo_redo(self) -> None:
        import tkinter as tk
        from tkinter import ttk
        from core.constants import AUTO_LP_CHIP_AUTO, AUTO_LP_CHIP_EMPTY_ZZ, AUTO_LP_CHIP_NONE
        from gui import APP_VERSION
        from gui.helpers import auto_lp_combobox_values, suggest_load_mapping_location
        from gui.i18n import TRANSLATIONS, get_language, tr

        self.assertEqual(APP_VERSION, "0.4.0")

        # 1) Verify all 3 languages share exact keys and renamed Save JSON As labels.
        base_keys = set(TRANSLATIONS["zh_CN"].keys())
        self.assertEqual(set(TRANSLATIONS["en"].keys()), base_keys)
        self.assertEqual(set(TRANSLATIONS["ja"].keys()), base_keys)
        self.assertEqual(TRANSLATIONS["zh_CN"]["save_map_btn"], "另存为 JSON...")
        self.assertEqual(TRANSLATIONS["en"]["save_map_btn"], "Save JSON As...")
        self.assertEqual(TRANSLATIONS["ja"]["save_map_btn"], "JSONに別名保存...")
        self.assertEqual(self.app.save_map_btn.cget("text"), tr("save_map_btn", lang=get_language()))

        # 2) Verify auto_lp_combobox_values and suggest_load_mapping_location.
        combo_vals = auto_lp_combobox_values(self.app.model, self.chart)
        self.assertGreaterEqual(len(combo_vals), 3)
        self.assertEqual([v for _, v in combo_vals[:3]], [AUTO_LP_CHIP_NONE, AUTO_LP_CHIP_AUTO, AUTO_LP_CHIP_EMPTY_ZZ])

        out_dir = self.bms.parent / "out_suggest"
        out_dir.mkdir(exist_ok=True)
        loc_empty = suggest_load_mapping_location(self.chart, out_dir, str(out_dir))
        self.assertEqual(Path(loc_empty[0]), out_dir)
        self.assertIsNone(loc_empty[1])

        proj_json = out_dir / "__bms2dtx.json"
        proj_json.write_text("{}", encoding="utf-8")
        loc_present = suggest_load_mapping_location(self.chart, out_dir, str(out_dir))
        self.assertEqual(Path(loc_present[0]), out_dir)
        self.assertEqual(loc_present[1], "__bms2dtx.json")

        # 3) Verify GUI title bar, TabId.LP dual-row widgets, Spinbox enable/disable, 1000ms tooltip, Undo/Redo.
        self.assertTrue(self.root.title().startswith("bms2dtx 0.4.0"))
        self.app.select_tab(TabId.LP)
        self.root.update()

        lp_panel = self.app.panels[TabId.LP]
        self.assertIsNotNone(lp_panel.auto_lp_combo)
        self.assertIsNotNone(lp_panel.auto_lp_vol_spin)
        self.assertIsNotNone(lp_panel.auto_lp_mode_note_rb)
        self.assertIsNotNone(lp_panel.auto_lp_mode_ms_rb)
        self.assertEqual(str(lp_panel.auto_lp_vol_spin.cget("state")), "disabled")
        self.assertIsNotNone(lp_panel._lp_tip_combo)
        self.assertEqual(lp_panel._lp_tip_combo._delay_ms, 1000)

        def _walk_widgets(root_widget):
            yield root_widget
            for child in root_widget.winfo_children():
                yield from _walk_widgets(child)

        widgets = list(_walk_widgets(lp_panel.frame))
        combos = [w for w in widgets if isinstance(w, ttk.Combobox)]
        radios = [w for w in widgets if isinstance(w, ttk.Radiobutton)]
        spins = [w for w in widgets if isinstance(w, (ttk.Spinbox, tk.Spinbox))]
        self.assertGreaterEqual(len(combos), 2)
        self.assertEqual(len(radios), 2)
        self.assertGreaterEqual(len(spins), 1)

        # Switch Auto-LP to "auto" -> Spinbox enabled, dirty=True, undoable.
        auto_idx = lp_panel.auto_lp_chip_values.index(AUTO_LP_CHIP_AUTO)
        lp_panel.auto_lp_combo.current(auto_idx)
        lp_panel._on_auto_lp_changed()
        self.root.update()
        self.assertEqual(str(lp_panel.auto_lp_vol_spin.cget("state")), "normal")
        self.assertEqual(self.app.model.auto_lp.chip_mode, AUTO_LP_CHIP_AUTO)
        self.assertTrue(self.app.dirty)

        # Switch Auto-LP to "empty_zz" -> Spinbox disabled again.
        zz_idx = lp_panel.auto_lp_chip_values.index(AUTO_LP_CHIP_EMPTY_ZZ)
        lp_panel.auto_lp_combo.current(zz_idx)
        lp_panel._on_auto_lp_changed()
        self.root.update()
        self.assertEqual(str(lp_panel.auto_lp_vol_spin.cget("state")), "disabled")
        self.assertEqual(self.app.model.auto_lp.chip_mode, AUTO_LP_CHIP_EMPTY_ZZ)

        # Undo should revert empty_zz -> auto; Undo again reverts auto -> none; Redo restores auto.
        self.assertTrue(self.app.on_undo())
        self.root.update()
        self.assertEqual(self.app.model.auto_lp.chip_mode, AUTO_LP_CHIP_AUTO)
        self.assertEqual(str(lp_panel.auto_lp_vol_spin.cget("state")), "normal")

        self.assertTrue(self.app.on_redo())
        self.root.update()
        self.assertEqual(self.app.model.auto_lp.chip_mode, AUTO_LP_CHIP_EMPTY_ZZ)
        self.assertEqual(str(lp_panel.auto_lp_vol_spin.cget("state")), "disabled")

        self.assertTrue(self.app.on_undo())
        self.assertTrue(self.app.on_undo())
        self.root.update()
        self.assertEqual(self.app.model.auto_lp.chip_mode, AUTO_LP_CHIP_NONE)
        self.assertFalse(self.app.dirty)

    def test_v040_preview_wasapi_shared_and_device_error_degradation(self) -> None:
        import math
        import struct
        import wave
        import numpy as np

        class _FakeWasapiSettings:
            def __init__(self, *, exclusive: bool = True) -> None:
                self.exclusive = exclusive

        class _FakeSoundDevice:
            WasapiSettings = _FakeWasapiSettings

            def __init__(self, *, fail_no_device: bool = False) -> None:
                self.fail_no_device = fail_no_device
                self.played: list[tuple[np.ndarray, dict]] = []
                self.stopped = 0

            def query_hostapis(self):
                if self.fail_no_device:
                    return [{"name": "MME", "default_output_device": -1}]
                return [
                    {"name": "MME", "default_output_device": 0},
                    {"name": "Windows WASAPI", "default_output_device": 2},
                ]

            def query_devices(self, idx=None):
                if self.fail_no_device:
                    return [] if idx is None else {"max_output_channels": 0}
                if idx is None:
                    return [{"max_output_channels": 2}] * 3
                return {"max_output_channels": 2}

            @property
            def default(self):
                class _Def:
                    device = (-1, -1)
                return _Def()

            def stop(self) -> None:
                self.stopped += 1

            def play(self, arr, **kwargs) -> None:
                self.played.append((arr.copy(), kwargs))

        stereo_wav = self.tmp / "stereo_lr.wav"
        sr = 44100
        with wave.open(str(stereo_wav), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            frames = bytearray()
            for i in range(441):
                left = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
                right = 0
                frames.extend(struct.pack("<hh", left, right))
            wf.writeframes(bytes(frames))

        fake_sd = _FakeSoundDevice()
        statuses: List[str] = []
        swap_flag = [True]
        player = PreviewPlayer(
            lambda: None,  # No ffmpeg at all!
            on_status=statuses.append,
            winsound_module=None,
            swap_lr_getter=lambda: swap_flag[0],
            sounddevice_module=fake_sd,
        )
        self.addCleanup(player.shutdown)
        self.assertTrue(player.available)
        self.assertTrue(player.play(stereo_wav))
        player._last_thread.join(5)

        self.assertEqual(len(fake_sd.played), 1)
        arr, kwargs = fake_sd.played[0]
        self.assertEqual(kwargs["device"], 2)  # Explicit Windows WASAPI device
        self.assertIsInstance(kwargs["extra_settings"], _FakeWasapiSettings)
        self.assertFalse(kwargs["extra_settings"].exclusive)  # Shared mode forced!
        # L/R swapped in memory: original L had energy, R was 0 -> swapped L is ~0, R has energy
        self.assertLess(float(np.max(np.abs(arr[:, 0]))), 1e-4)
        self.assertGreater(float(np.max(np.abs(arr[:, 1]))), 0.1)

        # Safe degradation when no audio output device exists (never crashes)
        bad_sd = _FakeSoundDevice(fail_no_device=True)
        bad_statuses: List[str] = []
        player_bad = PreviewPlayer(
            lambda: None,
            on_status=bad_statuses.append,
            winsound_module=None,
            sounddevice_module=bad_sd,
        )
        self.addCleanup(player_bad.shutdown)
        self.assertTrue(player_bad.play(stereo_wav))
        player_bad._last_thread.join(5)
        self.assertEqual(bad_sd.played, [])
        self.assertTrue(any("stereo_lr.wav" in s for s in bad_statuses))

    def test_v040_win32_64bit_drag_drop_hook_pms_and_settings_language_label(self) -> None:
        import ctypes
        import sys
        import tkinter as tk
        from gui.i18n import TRANSLATIONS, get_language, set_language

        # 1) Setting dialog language label must stay fixed as "🌐 Language:" in all 3 languages
        orig_lang = get_language()
        self.addCleanup(lambda: set_language(orig_lang))
        for lang in ("zh_CN", "en", "ja"):
            self.assertEqual(TRANSLATIONS[lang]["lang_label"], "🌐 Language:")

        def _walk_labels(widget) -> List[str]:
            out: List[str] = []
            try:
                txt = str(widget.cget("text"))
                if txt:
                    out.append(txt)
            except Exception:
                pass
            for ch in widget.winfo_children():
                out.extend(_walk_labels(ch))
            return out

        for lang in ("zh_CN", "en", "ja"):
            set_language(lang)
            self.app.apply_language()
            self.root.update()
            with mock.patch.object(tk.Toplevel, "grab_set", return_value=None):
                self.app.open_settings_dialog()
                self.root.update()
                dialogs = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
                self.assertTrue(dialogs)
                dlg = dialogs[-1]
                labels = _walk_labels(dlg)
                self.assertIn("🌐 Language:", labels)
                dlg.destroy()

        # 2) Verify Win32 64-bit drag-drop hook attachment, argtypes/restype, teardown & .pms / invalid drop
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            set_wndproc = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            self.assertIsNotNone(self.app._dnd_hwnd)
            self.assertIsNotNone(self.app._wndproc_ref)
            self.assertIs(set_wndproc.restype, ctypes.c_void_p)
            self.assertIs(user32.CallWindowProcW.restype, ctypes.c_ssize_t)
            self.assertEqual(tuple(shell32.DragQueryFileW.argtypes)[0], ctypes.c_void_p)
            self.assertEqual(tuple(shell32.DragFinish.argtypes), (ctypes.c_void_p,))

            # Re-hook / teardown round-trip on real Tk window must not crash
            self.app._teardown_windows_drag_drop()
            self.assertIsNone(self.app._dnd_hwnd)
            self.app._setup_windows_drag_drop()
            self.assertIsNotNone(self.app._dnd_hwnd)

        # Dropping unsupported file or directory shows warning without crashing
        bad_txt = self.tmp / "notes.txt"
        bad_txt.write_text("hi", encoding="utf-8")
        with mock.patch.object(self.app_mod.messagebox, "showwarning") as warn_mock:
            self.app._handle_dropped_files([str(bad_txt)])
            warn_mock.assert_called_once()

        # Dropping .pms loads cleanly without warning
        pms_file = self.tmp / "chart_9k.pms"
        pms_file.write_text("#TITLE PMS Drop\n#BPM 130\n#WAV01 kick.wav\n#00111:01\n", encoding="utf-8")
        with mock.patch.object(self.app_mod.messagebox, "showwarning") as warn_mock:
            self.app._handle_dropped_files([str(pms_file)])
            for _ in range(200):
                self.root.update()
                if not self.app._busy and self.app.chart and self.app.chart.source_path == pms_file:
                    break
                threading.Event().wait(0.02)
            warn_mock.assert_not_called()
        self.assertEqual(self.app.chart.source_path, pms_file)


if __name__ == "__main__":
    unittest.main()
