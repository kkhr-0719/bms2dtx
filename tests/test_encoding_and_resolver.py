"""Tests for CP932/UTF-8 encoding rules and read-only parent folder suffix resolution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.audio_resolver import ParentFolderAudioIndex
from core.encoding import decode_bms_bytes, encode_dtx_text


class TestEncodingRules(unittest.TestCase):
    def test_cp932_japanese_preferred(self) -> None:
        text = "#TITLE 桜の季節\r\n#ARTIST 作曲者テスト\r\n"
        raw = text.encode("cp932")
        decoded, enc, warnings = decode_bms_bytes(raw)
        self.assertEqual(enc, "cp932")
        self.assertEqual(decoded, text)
        self.assertEqual(warnings, [])

    def test_utf8_with_bom_does_not_mojibake_as_cp932(self) -> None:
        # Pure ASCII + UTF-8 BOM: naive cp932 decode would corrupt EF BB BF!
        text = "#TITLE SampleTitle\n#ARTIST SampleArtist\n"
        raw = text.encode("utf-8-sig")
        decoded, enc, warnings = decode_bms_bytes(raw)
        self.assertEqual(enc, "utf-8-sig")
        self.assertEqual(decoded, text)
        self.assertFalse(decoded.startswith("\ufeff"))
        self.assertEqual(warnings, [])

    def test_utf8_without_bom_fallback_when_cp932_fails(self) -> None:
        # Emoji and Korean characters fail strict cp932, so fallback must pick utf-8-sig
        text = "#TITLE 한국어 🎵 テスト\n"
        raw = text.encode("utf-8")
        decoded, enc, warnings = decode_bms_bytes(raw)
        self.assertEqual(enc, "utf-8-sig")
        self.assertEqual(decoded, text)
        self.assertEqual(warnings, [])

    def test_invalid_bytes_fallback_to_cp932_replace_with_warning(self) -> None:
        # 0x81 0x00 is invalid in both cp932 (bad trail byte) and utf-8
        raw = b"#TITLE \x81\x00test\n"
        decoded, enc, warnings = decode_bms_bytes(raw)
        self.assertEqual(enc, "cp932-replace")
        self.assertIn("test", decoded)
        self.assertTrue(any(w.code == "ENCODING_REPLACE_FALLBACK" for w in warnings))

    def test_encode_dtx_cp932_default_and_utf8_option(self) -> None:
        jp_text = "#TITLE: 桜吹雪\r\n#ARTIST: テスト\r\n"
        raw_cp932, enc1, w1 = encode_dtx_text(jp_text, utf8=False)
        self.assertEqual(enc1, "cp932")
        self.assertEqual(raw_cp932.decode("cp932"), jp_text)
        self.assertEqual(w1, [])

        raw_utf8, enc2, w2 = encode_dtx_text(jp_text, utf8=True)
        self.assertEqual(enc2, "utf-8")
        self.assertEqual(raw_utf8.decode("utf-8"), jp_text)
        self.assertEqual(w2, [])

    def test_encode_dtx_cp932_unencodable_warns_without_crashing(self) -> None:
        text = "#TITLE: Hello 🥁 한국어\r\n"
        raw, enc, warnings = encode_dtx_text(text, utf8=False)
        self.assertEqual(enc, "cp932")
        decoded = raw.decode("cp932")
        self.assertIn("Hello", decoded)
        self.assertIn("?", decoded)
        self.assertTrue(any(w.code == "CP932_ENCODE_LOSS" for w in warnings))

        raw_u8, enc_u8, warnings_u8 = encode_dtx_text(text, utf8=True)
        self.assertEqual(enc_u8, "utf-8")
        self.assertEqual(raw_u8.decode("utf-8"), text)
        self.assertEqual(warnings_u8, [])


class TestParentFolderAudioResolver(unittest.TestCase):
    def test_suffix_auto_correction_in_memory_and_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "kick.ogg").write_bytes(b"OggS-dummy")
            (root / "SNARE.FLAC").write_bytes(b"fLaC-dummy")
            (root / "ride.mp3").write_bytes(b"ID3-dummy")
            (root / "tom.xa").write_bytes(b"XA-dummy")
            sub = root / "SubDir"
            sub.mkdir()
            (sub / "hat_open.ogg").write_bytes(b"OggS-hat")

            idx = ParentFolderAudioIndex(root)

            e1 = idx.resolve_wav("01", "kick.wav")
            self.assertTrue(e1.exists)
            self.assertTrue(e1.suffix_corrected)
            self.assertEqual(e1.declared_name, "kick.wav")
            self.assertEqual(e1.resolved_name, "kick.ogg")
            self.assertEqual(e1.resolved_path, root / "kick.ogg")

            e2 = idx.resolve_wav("02", "snare.wav")
            self.assertTrue(e2.exists)
            self.assertTrue(e2.suffix_corrected)
            self.assertEqual(e2.resolved_name, "SNARE.FLAC")

            e3 = idx.resolve_wav("03", "ride.wav")
            self.assertTrue(e3.exists)
            self.assertEqual(e3.resolved_name, "ride.mp3")

            e4 = idx.resolve_wav("04", "tom.wav")
            self.assertTrue(e4.exists)
            self.assertEqual(e4.resolved_name, "tom.xa")

            e5 = idx.resolve_wav("05", r"subdir\hat_open.wav")
            self.assertTrue(e5.exists)
            self.assertTrue(e5.suffix_corrected)
            self.assertEqual(e5.resolved_name, "SubDir/hat_open.ogg")

            e_missing = idx.resolve_wav("06", "nonexistent.wav")
            self.assertFalse(e_missing.exists)
            self.assertIsNone(e_missing.resolved_path)

            e_escape = idx.resolve_wav("07", "../outside.wav")
            self.assertFalse(e_escape.exists)
            self.assertIsNone(e_escape.resolved_path)

            # Verify parent directory files were never renamed or added
            self.assertEqual(
                sorted(p.name for p in root.iterdir()),
                ["SNARE.FLAC", "SubDir", "kick.ogg", "ride.mp3", "tom.xa"],
            )

    def test_rejects_drive_relative_rooted_empty_and_supports_double_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "kick.wav.ogg").write_bytes(b"OggS-double")
            idx = ParentFolderAudioIndex(root)

            for bad in ("", "C:kick.wav.ogg", "/kick.wav.ogg", r"\kick.wav.ogg", "../kick.wav.ogg"):
                e = idx.resolve_wav("01", bad)
                self.assertFalse(e.exists, msg=f"Should reject {bad!r}")
                self.assertIsNone(e.resolved_path)

            # Double-extension fallback: #WAV02 kick.wav -> kick.wav.ogg
            e_dbl = idx.resolve_wav("02", "kick.wav")
            self.assertTrue(e_dbl.exists)
            self.assertTrue(e_dbl.suffix_corrected)
            self.assertEqual(e_dbl.resolved_name, "kick.wav.ogg")


if __name__ == "__main__":
    unittest.main()
