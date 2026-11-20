"""Encoding helpers strictly following AGENTS.md Rule 2."""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from .models import ConversionWarning


def decode_bms_bytes(raw: bytes) -> Tuple[str, str, List[ConversionWarning]]:
    """Decode BMS/BME bytes following AGENTS.md Rule 2.

    Order:
    - If UTF-8 BOM (EF BB BF) is present, try utf-8-sig first so CP932 does not
      misinterpret the BOM bytes as mojibake.
    - Otherwise try cp932 (strict), then utf-8-sig (strict), then cp932 with
      errors='replace' (with warning).
    """
    warnings: List[ConversionWarning] = []

    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig", warnings
        except UnicodeDecodeError:
            pass

    try:
        return raw.decode("cp932"), "cp932", warnings
    except UnicodeDecodeError:
        pass

    try:
        return raw.decode("utf-8-sig"), "utf-8-sig", warnings
    except UnicodeDecodeError:
        pass

    warnings.append(
        ConversionWarning(
            code="ENCODING_REPLACE_FALLBACK",
            message=(
                "Failed strict cp932 and utf-8-sig decoding; fell back to "
                "cp932 with replacement characters."
            ),
        )
    )
    return raw.decode("cp932", errors="replace"), "cp932-replace", warnings


def read_bms_text(path: Path) -> Tuple[str, str, List[ConversionWarning]]:
    """Read and decode a BMS/BME file from disk without modifying it."""
    raw = path.read_bytes()
    return decode_bms_bytes(raw)


def encode_dtx_text(
    text: str, *, utf8: bool = False
) -> Tuple[bytes, str, List[ConversionWarning]]:
    """Encode DTX text as cp932 (default) or utf-8 (--utf8).

    Never crashes on unencodable characters when exporting cp932; replaces only
    unencodable characters and emits a clear warning advising --utf8.
    """
    warnings: List[ConversionWarning] = []
    if utf8:
        return text.encode("utf-8"), "utf-8", warnings

    try:
        return text.encode("cp932"), "cp932", warnings
    except UnicodeEncodeError as exc:
        bad_chars = sorted(
            {
                ch
                for ch in text
                if _is_unencodable_cp932(ch)
            }
        )
        preview = "".join(bad_chars[:10])
        warnings.append(
            ConversionWarning(
                code="CP932_ENCODE_LOSS",
                message=(
                    f"Some characters ({preview!r}) cannot be encoded in cp932 "
                    f"({exc.reason}); replaced with '?'. Use --utf8 to preserve them."
                ),
            )
        )
        return text.encode("cp932", errors="replace"), "cp932", warnings


def _is_unencodable_cp932(ch: str) -> bool:
    try:
        ch.encode("cp932")
        return False
    except UnicodeEncodeError:
        return True
