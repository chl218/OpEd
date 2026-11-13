"""Episode and season export: cut, retime, final mux into ``<Season> (trimmed)/``.

Every output is written under a temporary name (``<out>.part`` or a file in a private
work dir inside the output folder) and moved onto its final name with ``os.replace``
only once complete, so a half-written file never carries the final name.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import av
import pysubs2

from .cutter import Mode, SmartCutError, cut_audio, cut_video, keyframe_intervals, snap_to_frames
from .discovery import EpisodeFiles, Season
from .media import FfmpegError, MediaInfo, StreamInfo, Tools, probe, run
from .project import Project
from .subs import retime_embedded_subs, retime_sidecar, write_ffmetadata
from .timeline import Interval, TimeMap, _complement, keep_intervals, merge_cuts

Status = Literal["trimmed", "copied", "failed", "needs_fallback"]

# ffmpeg muxer per output extension (the ``.part`` suffix hides it from ffmpeg).
_FORMATS = {".mkv": "matroska", ".mka": "matroska", ".webm": "webm",
            ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov"}
_FASTSTART = {"mp4", "mov"}
_STREAM_TAGS = ("language", "title")
_DISPOSITIONS = ("default", "dub", "original", "comment", "lyrics", "karaoke", "forced",
                 "hearing_impaired", "visual_impaired", "captions", "descriptions")


@dataclass
class EpisodeResult:
    file: str
    status: Status
    mode: Mode | None = None
    message: str = ""


def output_folder(season_folder: Path) -> Path:
    season_folder = Path(season_folder)
    return season_folder.with_name(season_folder.name + " (trimmed)")


def _out_path(path: Path, season_folder: Path, out_dir: Path) -> Path:
    return Path(out_dir) / Path(path).relative_to(season_folder)


def existing_outputs(season: Season, files: list[str]) -> list[Path]:
    """Output paths (videos + sidecars) for ``files`` that already exist."""
    out_dir = output_folder(season.folder)
    wanted = set(files)
    found = []
    for ep in season.episodes:
        if ep.video.name not in wanted:
            continue
        for p in [ep.video, *ep.sidecars]:
            o = _out_path(p, season.folder, out_dir)
            if o.exists():
                found.append(o)
    return found


# ---------------------------------------------------------------- helpers

def _part(path: Path) -> Path:
    return path.with_name(path.name + ".part")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _copy_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = _part(dst)
    try:
        shutil.copy2(src, part)
        os.replace(part, dst)
    finally:
        _unlink(part)


def _stream_args(n: int, s: StreamInfo) -> list[str]:
    args = []
    for key in _STREAM_TAGS:
        if s.tags.get(key):
            args += [f"-metadata:s:{n}", f"{key}={s.tags[key]}"]
    flags = [d for d in _DISPOSITIONS if s.disposition.get(d)]
    args += [f"-disposition:{n}", "+".join(flags) if flags else "0"]
    return args


def _final_keep(tools: Tools, info: MediaInfo, cuts: list[Interval], mode: Mode) -> list[Interval]:
    """The source intervals the cut video actually keeps (frame grid, or keyframes)."""
    keep = keep_intervals(merge_cuts(cuts, info.duration), info.duration)
    if mode == "keyframe" and keep:
        return keyframe_intervals(tools, info, keep)
    return snap_to_frames(tools, info, keep)


def _time_map(info: MediaInfo, keep: list[Interval]) -> TimeMap:
    """TimeMap whose cuts are exactly the gaps between the kept intervals.

    This includes slivers dropped by ``keep_intervals``, the frame-grid / keyframe
    snapping, and any head or tail, so subtitles and chapters follow the cut video.
    """
    duration = max(info.duration, keep[-1][1])
    return TimeMap(_complement(keep, 0.0, duration), duration)


def _sidecar_error(path: Path, e: Exception) -> str:
    if isinstance(e, UnicodeDecodeError):
        return (f"{path.name}: subtitle file is not UTF-8 text ({e.reason} at byte {e.start}); "
                "re-save it as UTF-8 and export again")
    return f"{path.name}: cannot read subtitle file: {e}"


# ---------------------------------------------------------------- episode

def export_episode(tools: Tools, ep: EpisodeFiles, season_folder: Path, cuts: list[Interval],
                   out_dir: Path, mode: Mode = "smart",
                   progress: Callable[[float], None] | None = None) -> EpisodeResult:
    season_folder, out_dir = Path(season_folder), Path(out_dir)
    name = ep.video.name
    report = progress or (lambda _x: None)
    report(0.0)
    final = _out_path(ep.video, season_folder, out_dir)
    try:
        if not cuts:
            _copy_atomic(ep.video, final)
            for side in ep.sidecars:
                _copy_atomic(side, _out_path(side, season_folder, out_dir))
            report(1.0)
            return EpisodeResult(name, "copied")
        info = probe(tools, ep.video)
        keep = _final_keep(tools, info, cuts, mode)
        if not keep:
            return EpisodeResult(name, "failed", mode, "the approved cuts leave nothing to keep")
        final.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=".oped-work-", dir=final.parent))
    except FfmpegError as e:
        return EpisodeResult(name, "failed", mode if cuts else None, e.stderr.strip()[-500:] or str(e))
    except (OSError, ValueError, SmartCutError) as e:
        return EpisodeResult(name, "failed", mode if cuts else None, str(e))

    part = _part(final)
    try:
        tm = _time_map(info, keep)
        # Sidecars first: they are cheap and the most likely to be unreadable.
        sides: list[tuple[Path, Path]] = []
        for n, side in enumerate(ep.sidecars):
            tmp = work / "side" / str(n) / side.name
            try:
                retime_sidecar(side, tmp, tm)
            except (UnicodeDecodeError, pysubs2.exceptions.Pysubs2Error, av.error.FFmpegError,
                    ValueError) as e:
                return EpisodeResult(name, "failed", mode, _sidecar_error(side, e))
            sides.append((tmp, _out_path(side, season_folder, out_dir)))
        report(0.05)

        video = cut_video(tools, info, keep, work / "v.mkv", mode, work)
        report(0.6)
        audio = cut_audio(tools, info, keep, work / "a.mka", work) if info.audio() else None
        report(0.75)
        sub_ext = ".mp4" if info.container == "mp4" else ".mkv"
        subs = retime_embedded_subs(info, tm, work / f"subs{sub_ext}")
        meta = write_ffmetadata(info.chapters, tm, work / "chapters.ffmeta") if info.chapters else None
        report(0.85)

        _mux(tools, info, video, audio, subs, meta, part, final.suffix.lower())
        for tmp, dst in sides:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dst)
        os.replace(part, final)
        report(1.0)
        return EpisodeResult(name, "trimmed", mode)
    except SmartCutError as e:
        return EpisodeResult(name, "needs_fallback", mode, str(e))
    except FfmpegError as e:
        return EpisodeResult(name, "failed", mode, e.stderr.strip()[-500:] or str(e))
    except (OSError, ValueError, av.error.FFmpegError) as e:
        return EpisodeResult(name, "failed", mode, str(e))
    finally:
        _unlink(part)
        shutil.rmtree(work, ignore_errors=True)


def _mux(tools: Tools, info: MediaInfo, video: Path, audio: Path | None, subs: Path | None,
         meta: Path | None, part: Path, ext: str) -> None:
    cmd: list[str] = [tools.ffmpeg, "-v", "error", "-nostdin", "-y"]
    inputs = [video] + ([audio] if audio else []) + ([subs] if subs else []) + [info.path]
    for p in inputs:
        cmd += ["-i", str(p)]
    src = len(inputs) - 1
    meta_idx = -1
    if meta:
        cmd += ["-i", str(meta)]
        meta_idx = len(inputs)
    cmd += ["-map", "0:v"]
    k = 1
    if audio:
        cmd += ["-map", f"{k}:a"]
        k += 1
    if subs:
        cmd += ["-map", f"{k}:s"]
    elif info.subtitles():
        # Every cue was cut: keep the (now empty) streams, codec headers and all.
        cmd += ["-map", f"{src}:s", "-bsf:s", "noise=drop=1"]
    cmd += ["-map", f"{src}:t?", "-map_chapters", str(meta_idx), "-map_metadata", str(src), "-c", "copy"]

    # Output stream order: first video, all audio, all subtitles (source order), then
    # attachments, which keep their own filename/mimetype tags.
    v = info.video()
    ordered = ([v] if v else []) + (info.audio() if audio else []) + info.subtitles()
    for n, s in enumerate(ordered):
        cmd += _stream_args(n, s)

    fmt = _FORMATS.get(ext, "matroska")
    if fmt == "matroska":
        cmd += ["-default_mode", "passthrough"]
    if fmt in _FASTSTART:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-f", fmt, str(part)]
    run(cmd)


# ---------------------------------------------------------------- season

def export_season(tools: Tools, project: Project, season: Season, files: list[str],
                  modes: dict[str, Mode] | None = None,
                  progress: Callable[[str, float], None] | None = None) -> list[EpisodeResult]:
    """Export ``files`` into the output folder. The overwrite check is the caller's job."""
    modes = modes or {}
    out_dir = output_folder(season.folder)
    out_dir.mkdir(parents=True, exist_ok=True)
    for d in season.font_dirs:
        shutil.copytree(d, _out_path(d, season.folder, out_dir), dirs_exist_ok=True)
    by_name = {ep.video.name: ep for ep in season.episodes}
    results = []
    for f in files:
        ep = by_name.get(f)
        if ep is None:
            results.append(EpisodeResult(f, "failed", None, "episode not found in the season folder"))
            continue
        cb = (lambda x, f=f: progress(f, x)) if progress else None
        results.append(export_episode(tools, ep, season.folder, project.approved_intervals(f), out_dir,
                                      modes.get(f, "smart"), cb))
    return results
