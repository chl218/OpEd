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


def _name_match(file_name: str, stem: str) -> tuple[int, int] | None:
    """Match rank of a sidecar name against a video stem, or None.

    Exact stem (``E1.ass`` for ``E1``) ranks above a ``stem + "."`` prefix
    (``E1.en.ass``); among prefixes the longer stem ranks higher, so ``E1.ass`` never
    attaches to ``E10`` and ``01.5.ass`` goes to ``01.5``, not ``01``.
    """
    if Path(file_name).stem == stem:
        return (1, len(stem))
    if file_name.startswith(stem + "."):
        return (0, len(stem))
    return None


def _best_video(f: Path, videos: list[Path], sub_roots: list[Path]) -> Path | None:
    """The one video a sidecar belongs to: a ``Subs/<stem>/`` folder wins, then the
    best name match (see ``_name_match``)."""
    best, best_rank = None, None
    for v in videos:
        if any(f.parent == r / v.stem for r in sub_roots):
            rank = (2, len(v.stem))
        else:
            m = _name_match(f.name, v.stem)
            if m is None:
                continue
            rank = m
        if best_rank is None or rank > best_rank:
            best, best_rank = v, rank
    return best


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

    by_video: dict[Path, EpisodeFiles] = {v: EpisodeFiles(video=v) for v in videos}
    for f in sorted(entries + sub_files, key=lambda p: natural_key(str(p))):
        ext = f.suffix.lower()
        if ext not in SUB_EXTS and ext != ".mka":
            continue
        owner = _best_video(f, videos, sub_roots)
        if owner is None:
            continue
        if ext in SUB_EXTS:
            by_video[owner].sidecars.append(f)
        elif _name_match(f.name, owner.stem) is not None:
            season.warnings.append(f"External audio not supported, ignored: {f.name}")
    season.episodes = [by_video[v] for v in videos]
    return season
