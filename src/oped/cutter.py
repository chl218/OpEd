"""Video and audio cutting: frame-exact smart cut, full re-encode and keyframe-snapped copy.

All cut functions take KEEP intervals (source seconds) and write a single output file.
Every intermediate file lives in a private temp dir under ``workdir`` that is removed
on success and on error; the final file is written there first and moved to ``out``.
"""
from __future__ import annotations

import bisect
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .media import FfmpegError, MediaInfo, Tools, probe, run
from .timeline import Interval

Mode = Literal["smart", "reencode", "keyframe"]

SMART_ENCODERS: dict[str, tuple[str, list[str]]] = {
    "h264": ("libx264", ["-crf", "14", "-preset", "slow"]),
    "hevc": ("libx265", ["-crf", "16", "-preset", "slow", "-x265-params", "repeat-headers=1:log-level=error"]),
}
_FALLBACK_ENCODER: tuple[str, list[str]] = ("libx264", ["-crf", "16"])

DURATION_TOLERANCE = 0.1
_EPS = 0.0005  # half a millisecond: frame-selection guard against float/ms rounding
_NUDGE = 1e-5  # keeps concat seek targets at/after the intended keyframe despite rounding


class SmartCutError(Exception):
    pass


# ---------------------------------------------------------------- concat lists

def _quote(path: Path) -> str:
    return "'" + Path(path).as_posix().replace("'", "'\\''") + "'"


def _concat_text(entries: list[tuple[Path, float | None, float | None, float | None]]) -> str:
    lines = ["ffconcat version 1.0"]
    for path, inpoint, outpoint, duration in entries:
        lines.append(f"file {_quote(path)}")
        if inpoint is not None:
            lines.append(f"inpoint {inpoint:.6f}")
        if outpoint is not None:
            lines.append(f"outpoint {outpoint:.6f}")
        if duration is not None:
            lines.append(f"duration {duration:.6f}")
    return "\n".join(lines) + "\n"


def concat_list(entries: list[tuple[Path, float | None, float | None]]) -> str:
    """Concat-demuxer script: one ``file '...'`` per entry plus optional inpoint/outpoint."""
    return _concat_text([(p, i, o, None) for p, i, o in entries])


def _write_list(work: Path, name: str, entries) -> Path:
    lst = work / name
    lst.write_text(_concat_text(entries), encoding="utf-8")
    return lst


# ---------------------------------------------------------------- probing helpers

@dataclass(frozen=True)
class _Packet:
    pts: float
    dts: float
    key: bool


def _video_packets(tools: Tools, path: Path) -> list[_Packet]:
    """First video stream's packets in decode order."""
    out = run([
        tools.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,dts_time,flags", "-of", "csv=p=0", str(path),
    ]).stdout.decode("utf-8", "replace")
    pkts = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        try:
            pts = float(parts[0])
        except ValueError:
            continue
        try:
            dts = float(parts[1])
        except ValueError:
            dts = pts
        pkts.append(_Packet(pts, dts, "K" in parts[2]))
    return pkts


def _stream_field(tools: Tools, path: Path, field: str, select: str = "v:0") -> str:
    out = run([
        tools.ffprobe, "-v", "error", "-select_streams", select,
        "-show_entries", f"stream={field}", "-of", "default=nw=1:nk=1", str(path),
    ]).stdout.decode("utf-8", "replace").split()
    return out[0] if out else ""


def _start_time(tools: Tools, path: Path) -> float:
    try:
        return float(_stream_field(tools, path, "start_time"))
    except ValueError:
        return 0.0


def _first_video(info: MediaInfo):
    v = info.video()
    if v is None:
        raise SmartCutError(f"{info.path.name}: no video stream")
    return v


# ---------------------------------------------------------------- piece builders

def _encode_piece(tools: Tools, src: Path, s: float, e: float, encoder: tuple[str, list[str]],
                  pix_fmt: str, out: Path) -> None:
    """Re-encode source frames with pts in [s, e) into ``out`` (timestamps start near 0)."""
    codec, args = encoder
    cmd = [
        tools.ffmpeg, "-v", "error", "-nostdin", "-y",
        "-copyts", "-ss", f"{max(0.0, s - _EPS):.6f}", "-i", str(src),
        "-map", "0:v:0",
        "-vf", f"trim=start={s - _EPS:.6f}:end={e - _EPS:.6f},setpts=PTS-STARTPTS",
        "-c:v", codec, *args,
    ]
    if pix_fmt:
        cmd += ["-pix_fmt", pix_fmt]
    cmd += ["-fps_mode", "passthrough", "-an", "-sn", "-dn", "-map_metadata", "-1",
            "-map_chapters", "-1", str(out)]
    run(cmd)


def _copy_piece(tools: Tools, src: Path, codec: str, k1: float, out_dts: float,
                work: Path, out: Path) -> None:
    """Stream-copy decode-order packets from keyframe ``k1`` up to (not incl.) dts ``out_dts``.

    Leading pictures that follow ``k1`` in decode order but display before it (open GOP)
    are dropped; parameter sets are repeated in-band so the piece survives being joined
    with differently-configured encoded pieces.
    """
    # Seek target nudged up so 6-decimal rounding can never land on the previous
    # keyframe; outpoint nudged down so rounding can never let the K2 packet through.
    lst = _write_list(work, f"{out.stem}.txt", [(src, k1 + _NUDGE, out_dts - _EPS, None)])
    bsfs = []
    if codec in ("h264", "hevc"):
        bsfs.append(f"{codec}_mp4toannexb")
    # The first packet is K1; ffmpeg may already have shifted timestamps, so compare
    # against it rather than against zero.
    bsfs.append("noise=drop=lt(pts\\,startpts)")
    run([
        tools.ffmpeg, "-v", "error", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", str(lst),
        "-map", "0:v:0", "-c", "copy", "-bsf:v", ",".join(bsfs),
        "-map_metadata", "-1", "-map_chapters", "-1", str(out),
    ])


def _join(tools: Tools, entries, work: Path, out: Path, maps: list[str]) -> None:
    lst = _write_list(work, "join.txt", entries)
    run([
        tools.ffmpeg, "-v", "error", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", str(lst),
        *maps, "-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1", str(out),
    ])


# ---------------------------------------------------------------- smart cut plan

@dataclass(frozen=True)
class _Piece:
    kind: Literal["encode", "copy"]
    start: float          # first displayed source pts
    end: float            # display end (exclusive) in source time
    k1: float = 0.0       # copy: keyframe to start from
    out_dts: float = 0.0  # copy: decode-order end (dts of the next keyframe)


def _frame_end(pts_sorted: list[float], last: float, fps: float | None) -> float:
    i = bisect.bisect_right(pts_sorted, last + _EPS)
    if i < len(pts_sorted):
        return pts_sorted[i]
    if len(pts_sorted) >= 2:
        return last + (pts_sorted[-1] - pts_sorted[-2])
    return last + (1.0 / fps if fps else 0.04)


def _plan_interval(pkts: list[_Packet], pts_sorted: list[float], a: float, b: float,
                   fps: float | None) -> list[_Piece]:
    lo = bisect.bisect_left(pts_sorted, a - _EPS)
    hi = bisect.bisect_left(pts_sorted, b - _EPS)
    if hi <= lo:
        return []
    f0, fl = pts_sorted[lo], pts_sorted[hi - 1]
    end = _frame_end(pts_sorted, fl, fps)

    keys = [(i, p) for i, p in enumerate(pkts) if p.key]
    k1 = next(((i, p) for i, p in keys if p.pts >= f0 - _EPS), None)
    k2 = next(((i, p) for i, p in reversed(keys) if p.pts <= b + _EPS and p.pts < end - _EPS), None)
    if k1 is None or k2 is None or k1[1].pts >= k2[1].pts - _EPS:
        return [_Piece("encode", f0, end)]

    i1, p1 = k1
    i2, p2 = k2
    # Display end of the copied run: the earliest pts at/after K2 in decode order
    # (K2 itself, or its leading pictures in an open GOP).
    m_end = min(p.pts for p in pkts[i2:])
    kept = [p.pts for p in pkts[i1:i2] if p.pts >= p1.pts - _EPS]
    if not kept or max(kept) >= m_end - _EPS or min(kept) < p1.pts - _EPS:
        raise SmartCutError(f"cannot stream-copy between keyframes {p1.pts:.3f} and {p2.pts:.3f}")
    pieces = []
    if f0 < p1.pts - _EPS:
        pieces.append(_Piece("encode", f0, p1.pts))
    pieces.append(_Piece("copy", p1.pts, m_end, k1=p1.pts, out_dts=p2.dts))
    if m_end < end - _EPS:
        pieces.append(_Piece("encode", m_end, end))
    return pieces


def _smart(tools: Tools, info: MediaInfo, keep: list[Interval], work: Path, out: Path) -> None:
    v = _first_video(info)
    encoder = SMART_ENCODERS[v.codec]
    pix_fmt = _stream_field(tools, info.path, "pix_fmt")
    pkts = _video_packets(tools, info.path)
    pts_sorted = sorted(p.pts for p in pkts)

    entries = []
    for n, (a, b) in enumerate(keep):
        for m, piece in enumerate(_plan_interval(pkts, pts_sorted, float(a), float(b), info.fps)):
            path = work / f"piece_{n:03d}_{m}.mkv"
            if piece.kind == "encode":
                _encode_piece(tools, info.path, piece.start, piece.end, encoder, pix_fmt, path)
            else:
                _copy_piece(tools, info.path, v.codec, piece.k1, piece.out_dts, work, path)
            st = _start_time(tools, path)
            entries.append((path, st, st + (piece.end - piece.start), None))
    if not entries:
        raise SmartCutError("nothing to keep")
    _join(tools, entries, work, out, ["-map", "0:v:0"])


def _validate(tools: Tools, path: Path, expected: float) -> None:
    proc = run([tools.ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"])
    err = proc.stderr.decode("utf-8", "replace").strip()
    if err:
        raise SmartCutError(f"decode check failed: {err[-500:]}")
    got = probe(tools, path).duration
    if abs(got - expected) > DURATION_TOLERANCE:
        raise SmartCutError(f"duration {got:.3f}s, expected {expected:.3f}s")


# ---------------------------------------------------------------- fallbacks

def _reencode(tools: Tools, info: MediaInfo, keep: list[Interval], out: Path) -> None:
    v = _first_video(info)
    codec, args = SMART_ENCODERS.get(v.codec, _FALLBACK_ENCODER)
    parts = []
    for n, (a, b) in enumerate(keep):
        parts.append(f"[0:v:0]trim=start={a - _EPS:.6f}:end={b - _EPS:.6f},setpts=PTS-STARTPTS[v{n}]")
    graph = ";".join(parts) + ";" + "".join(f"[v{n}]" for n in range(len(keep)))
    graph += f"concat=n={len(keep)}:v=1:a=0[v]"
    run([
        tools.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(info.path),
        "-filter_complex", graph, "-map", "[v]", "-c:v", codec, *args,
        "-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1", str(out),
    ])


def _keyframe(tools: Tools, info: MediaInfo, keep: list[Interval], work: Path, out: Path) -> None:
    """Snap each interval to the nearest keyframes and stream-copy whole GOPs."""
    _first_video(info)
    pkts = _video_packets(tools, info.path)
    keys = [p for p in pkts if p.key]
    if not keys:
        raise FfmpegError([tools.ffprobe], "no keyframes found")
    entries = []
    for a, b in keep:
        start = min(keys, key=lambda p: abs(p.pts - a))
        later = [p for p in keys if p.pts > start.pts]
        end = min(later, key=lambda p: abs(p.pts - b)) if later else None
        if end is None or abs(b - info.duration) < abs(b - end.pts):
            entries.append((info.path, start.pts + _NUDGE, None, max(info.duration - start.pts, 0.0)))
        else:
            entries.append((info.path, start.pts + _NUDGE, end.dts - _EPS, end.pts - start.pts))
    _join(tools, entries, work, out, ["-map", "0:v:0"])


# ---------------------------------------------------------------- public API

def _finish(tmp_out: Path, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_out), str(out))
    return out


def cut_video(tools: Tools, info: MediaInfo, keep: list[Interval], out: Path, mode: Mode,
              workdir: Path) -> Path:
    """Write the first video stream, restricted to ``keep``, to ``out`` (.mkv)."""
    if mode == "smart":
        v = _first_video(info)
        if v.codec not in SMART_ENCODERS:
            raise SmartCutError(f"smart cut does not support {v.codec or 'unknown'} video")
    elif mode not in ("reencode", "keyframe"):
        raise ValueError(f"unknown mode {mode!r}")
    expected = sum(b - a for a, b in keep)
    try:
        Path(workdir).mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="oped_cut_", dir=workdir))
    except OSError as e:
        if mode == "smart":
            raise SmartCutError(f"cannot create work dir: {e}") from e
        raise
    try:
        tmp_out = work / "video.mkv"
        if mode == "smart":
            try:
                _smart(tools, info, keep, work, tmp_out)
                _validate(tools, tmp_out, expected)
            except FfmpegError as e:
                raise SmartCutError(str(e)) from e
        elif mode == "reencode":
            _reencode(tools, info, keep, tmp_out)
        else:
            _keyframe(tools, info, keep, work, tmp_out)
        return _finish(tmp_out, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cut_audio(tools: Tools, info: MediaInfo, keep: list[Interval], out: Path, workdir: Path) -> Path:
    """Write all audio streams, stream-copied and restricted to ``keep``, to ``out`` (.mka).

    The concat demuxer seeks on the video stream, so ``inpoint`` on the source would
    start audio at the previous video keyframe. Each interval is therefore first
    extracted audio-only (output-side ``-ss``/``-to`` drops packets outside it), and the
    pieces are joined with the concat demuxer.
    """
    Path(workdir).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="oped_cut_", dir=workdir))
    try:
        entries = []
        for n, (a, b) in enumerate(keep):
            piece = work / f"audio_{n:03d}.mka"
            run([
                tools.ffmpeg, "-v", "error", "-nostdin", "-y",
                "-copyts", "-ss", f"{max(0.0, a - 1.0):.6f}", "-i", str(info.path),
                "-ss", f"{a:.6f}", "-to", f"{b:.6f}",
                "-map", "0:a", "-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1",
                str(piece),
            ])
            entries.append((piece, 0.0, float(b - a), None))
        tmp_out = work / "audio.mka"
        _join(tools, entries, work, tmp_out, ["-map", "0:a"])
        return _finish(tmp_out, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
