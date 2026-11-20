"""Read-only parent folder audio suffix validation and in-memory auto-correction."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .constants import SUPPORTED_AUDIO_EXTENSIONS
from .models import WavEntry

_EXT_PRIORITY: Dict[str, int] = {
    ext: idx for idx, ext in enumerate(SUPPORTED_AUDIO_EXTENSIONS)
}


class ParentFolderAudioIndex:
    """Case-insensitive read-only file index for a BMS parent folder.

    Supports both flat files and relative subdirectories (e.g. 'wav\\kick.wav').
    Never mutates any file on disk.
    """

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self._dir_cache: Dict[Path, Tuple[Dict[str, Path], Dict[str, List[Path]]]] = {}

    def _scan_dir(self, directory: Path) -> Tuple[Dict[str, Path], Dict[str, List[Path]]]:
        resolved_dir = directory
        if resolved_dir in self._dir_cache:
            return self._dir_cache[resolved_dir]

        exact_ci: Dict[str, Path] = {}
        by_stem_ci: Dict[str, List[Path]] = {}

        if resolved_dir.is_dir():
            for child in resolved_dir.iterdir():
                if not child.is_file():
                    continue
                lower_name = child.name.lower()
                exact_ci.setdefault(lower_name, child)
                if child.suffix.lower() in _EXT_PRIORITY:
                    by_stem_ci.setdefault(child.stem.lower(), []).append(child)

        for candidates in by_stem_ci.values():
            candidates.sort(
                key=lambda p: (_EXT_PRIORITY.get(p.suffix.lower(), 999), p.name.lower())
            )

        self._dir_cache[resolved_dir] = (exact_ci, by_stem_ci)
        return exact_ci, by_stem_ci

    def resolve_wav(self, wav_id: str, declared_name: str) -> WavEntry:
        """Resolve a #WAVxx filename against the parent folder in memory."""
        cleaned = declared_name.strip()
        if not cleaned:
            return WavEntry(
                wav_id=wav_id,
                declared_name=cleaned,
                resolved_name=cleaned,
                resolved_path=None,
                exists=False,
            )
        normalized_rel = cleaned.replace("\\", "/")
        rel_path = Path(normalized_rel)

        # Reject absolute, drive-relative, rooted, or parent-traversal paths for safety
        if (
            rel_path.is_absolute()
            or bool(rel_path.drive)
            or bool(rel_path.root)
            or ".." in rel_path.parts
        ):
            return WavEntry(
                wav_id=wav_id,
                declared_name=cleaned,
                resolved_name=cleaned,
                resolved_path=None,
                exists=False,
            )

        # Resolve target directory case-insensitively if nested
        target_dir = self._resolve_subdir_ci(rel_path.parent)
        if target_dir is None:
            return WavEntry(
                wav_id=wav_id,
                declared_name=cleaned,
                resolved_name=cleaned,
                resolved_path=None,
                exists=False,
            )

        exact_ci, by_stem_ci = self._scan_dir(target_dir)
        lower_name = rel_path.name.lower()

        # 1. Exact filename match (case-insensitive, preserves exact disk casing)
        if lower_name in exact_ci:
            matched = exact_ci[lower_name]
            rel_matched = matched.relative_to(self.root_dir).as_posix()
            return WavEntry(
                wav_id=wav_id,
                declared_name=cleaned,
                resolved_name=rel_matched,
                resolved_path=matched,
                exists=True,
                suffix_corrected=False,
                case_corrected=(matched.name != rel_path.name),
            )

        # 2. Same stem (or double-extension e.g. kick.wav.ogg) with another audio extension
        stem_lower = rel_path.stem.lower()
        candidates = by_stem_ci.get(stem_lower) or by_stem_ci.get(lower_name)
        if candidates:
            matched = candidates[0]
            rel_matched = matched.relative_to(self.root_dir).as_posix()
            return WavEntry(
                wav_id=wav_id,
                declared_name=cleaned,
                resolved_name=rel_matched,
                resolved_path=matched,
                exists=True,
                suffix_corrected=(matched.suffix.lower() != rel_path.suffix.lower()),
                case_corrected=(matched.stem != rel_path.stem),
            )

        return WavEntry(
            wav_id=wav_id,
            declared_name=cleaned,
            resolved_name=cleaned,
            resolved_path=None,
            exists=False,
        )

    def _resolve_subdir_ci(self, rel_parent: Path) -> Optional[Path]:
        if str(rel_parent) in ("", "."):
            return self.root_dir
        cur = self.root_dir
        for part in rel_parent.parts:
            if not cur.is_dir():
                return None
            lower = part.lower()
            found: Optional[Path] = None
            for child in cur.iterdir():
                if child.is_dir() and child.name.lower() == lower:
                    found = child
                    break
            if found is None:
                return None
            cur = found
        return cur
