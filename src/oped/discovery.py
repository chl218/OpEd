"""Episode discovery and sidecar (subtitle) matching for a season folder."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".webm", ".mov"}
SUB_EXTS = {".ass", ".ssa", ".srt", ".vtt", ".sup"}
FONT_DIRS = ("Fonts", "fonts", "Attachments")
SUB_DIRS = ("Subs", "subs")


def natural_key(name: str) -> tuple:
    parts = re.split(r"(\d+)", name.lower())
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts if p != "")


@dataclass
class EpisodeFiles:
    video: Path
    sidecars: list[Path] = field(default_factory=list)


@dataclass
class Season:
    folder: Path
    episodes: list[EpisodeFiles] = field(default_factory=list)
    font_dirs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _name_matches(file_name: str, stem: str) -> bool:
    fstem = Path(file_name).stem
    return fstem == stem or file_name.startswith(stem + ".")


def discover(folder: Path) -> Season:
    folder = Path(folder)
    season = Season(folder=folder)
    entries = [p for p in folder.iterdir() if p.is_file()]
    videos = sorted(
        (p for p in entries if p.suffix.lower() in VIDEO_EXTS),
        key=lambda p: natural_key(p.name),
    )

    sub_roots: list[Path] = []
    seen: set[str] = set()
    for d in SUB_DIRS:
        p = folder / d
        if p.is_dir() and d.lower() not in seen:
            seen.add(d.lower())
            sub_roots.append(p)
    sub_files = [f for r in sub_roots for f in sorted(r.rglob("*")) if f.is_file()]

    seen = set()
    for d in FONT_DIRS:
        p = folder / d
        if p.is_dir() and d.lower() not in seen:
            seen.add(d.lower())
            season.font_dirs.append(p)

    for v in videos:
        ep = EpisodeFiles(video=v)
        candidates = entries + sub_files
        for f in sorted(candidates, key=lambda p: natural_key(str(p))):
            ext = f.suffix.lower()
            if f == v:
                continue
            in_stem_dir = any(
                r in f.parents and f.parent == r / v.stem for r in sub_roots
            )
            if ext in SUB_EXTS:
                if _name_matches(f.name, v.stem) or in_stem_dir:
                    ep.sidecars.append(f)
            elif ext == ".mka" and _name_matches(f.name, v.stem):
                season.warnings.append(f"External audio not supported, ignored: {f.name}")
        season.episodes.append(ep)
    return season
