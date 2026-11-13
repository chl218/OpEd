"""ffmpeg/ffprobe discovery, subprocess helpers and media probing."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Literal

TEXT_SUB_CODECS = {"ass", "ssa", "subrip", "webvtt", "mov_text", "text"}

_KINDS = {"video", "audio", "subtitle", "attachment", "data"}


def _install_hint() -> str:
    if sys.platform == "win32":
        return "winget install ffmpeg"
    if sys.platform == "darwin":
        return "brew install ffmpeg"
    return "sudo apt install ffmpeg"


class ToolsMissing(Exception):
    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            f"Could not find {' and '.join(missing)} on PATH. "
            f"Install ffmpeg (includes ffprobe): {_install_hint()}"
        )


class FfmpegError(Exception):
    def __init__(self, cmd: list[str], stderr: str):
        self.cmd = list(cmd)
        self.stderr = stderr[-2000:]
        super().__init__(f"command failed: {' '.join(map(str, cmd[:3]))} ...\n{self.stderr}")


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str


def find_tools(ffmpeg: str | None = None, ffprobe: str | None = None) -> Tools:
    ff = ffmpeg or shutil.which("ffmpeg")
    fp = ffprobe or shutil.which("ffprobe")
    missing = [n for n, v in (("ffmpeg", ff), ("ffprobe", fp)) if not v]
    if missing:
        raise ToolsMissing(missing)
    return Tools(ffmpeg=str(ff), ffprobe=str(fp))


def run(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[bytes]:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        [str(c) for c in cmd],
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        raise FfmpegError(cmd, err)
    return proc


@dataclass(frozen=True)
class StreamInfo:
    index: int
    kind: Literal["video", "audio", "subtitle", "attachment", "data"]
    codec: str
    tags: dict[str, str]
    disposition: dict[str, int]

    @property
    def is_text_sub(self) -> bool:
        return self.codec in TEXT_SUB_CODECS


@dataclass(frozen=True)
class Chapter:
    start: float
    end: float
    title: str


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    container: Literal["mkv", "mp4", "other"]
    streams: tuple[StreamInfo, ...]
    chapters: tuple[Chapter, ...]
    fps: float | None

    def video(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.kind == "video"), None)

    def audio(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.kind == "audio"]

    def subtitles(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.kind == "subtitle"]

    def primary_audio(self) -> StreamInfo | None:
        auds = self.audio()
        for s in auds:
            if s.disposition.get("default"):
                return s
        return auds[0] if auds else None


def _fps(s: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        v = s.get(key)
        if v and v != "0/0":
            try:
                f = float(Fraction(v))
            except (ValueError, ZeroDivisionError):
                continue
            if f > 0:
                return f
    return None


def probe(tools: Tools, path: Path) -> MediaInfo:
    path = Path(path)
    out = run([
        tools.ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", str(path),
    ]).stdout
    data = json.loads(out.decode("utf-8", "replace"))
    fmt = data.get("format", {})
    names = fmt.get("format_name", "")
    if "matroska" in names:
        container = "mkv"
    elif "mp4" in names or "mov" in names:
        container = "mp4"
    else:
        container = "other"

    streams = []
    fps = None
    for s in data.get("streams", []):
        kind = s.get("codec_type", "data")
        if kind not in _KINDS:
            kind = "data"
        if kind == "video" and fps is None and not s.get("disposition", {}).get("attached_pic"):
            fps = _fps(s)
        streams.append(StreamInfo(
            index=int(s["index"]),
            kind=kind,
            codec=s.get("codec_name", ""),
            tags={k: str(v) for k, v in s.get("tags", {}).items()},
            disposition={k: int(v) for k, v in s.get("disposition", {}).items()},
        ))
    chapters = tuple(
        Chapter(float(c["start_time"]), float(c["end_time"]), c.get("tags", {}).get("title", ""))
        for c in data.get("chapters", [])
    )
    return MediaInfo(
        path=path,
        duration=float(fmt.get("duration", 0) or 0),
        container=container,
        streams=tuple(streams),
        chapters=chapters,
        fps=fps,
    )


def keyframes(tools: Tools, path: Path) -> list[float]:
    out = run([
        tools.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags", "-of", "csv", str(path),
    ]).stdout.decode("utf-8", "replace")
    result = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 3 and parts[0] == "packet" and "K" in parts[2]:
            try:
                result.append(float(parts[1]))
            except ValueError:
                pass
    return sorted(result)
