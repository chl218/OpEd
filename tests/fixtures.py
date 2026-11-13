"""Synthetic media generators for tests (ffmpeg lavfi based)."""
from __future__ import annotations

import shutil
import struct
import tempfile
from pathlib import Path

import numpy as np

from oped.media import Tools, run

W, H = 320, 180
RATE = 48000


def _fmt_ass_time(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _write_ass(path: Path, lines: list[tuple[float, float, str]]) -> None:
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n\n"
        "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,"
        "Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        "Style: Default,Arial,28,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,"
        "100,100,0,0,1,2,0,2,10,10,10,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    body = "".join(
        f"Dialogue: 0,{_fmt_ass_time(s)},{_fmt_ass_time(e)},Default,,0,0,0,,{t}\n"
        for s, e, t in lines
    )
    path.write_text(head + body, encoding="utf-8")


def _esc_meta(s: str) -> str:
    for ch in ("\\", "=", ";", "#", "\n"):
        s = s.replace(ch, "\\" + ch)
    return s


def _write_ffmetadata(path: Path, chapters: list[tuple[float, str]], total: float) -> None:
    out = [";FFMETADATA1"]
    for i, (start, title) in enumerate(chapters):
        end = chapters[i + 1][0] if i + 1 < len(chapters) else total
        out += [
            "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={round(start * 1000)}", f"END={round(end * 1000)}",
            f"title={_esc_meta(title)}",
        ]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _pts(marker: int, ms: float) -> bytes:
    v = int(ms * 90)
    return bytes([
        (marker << 4) | (((v >> 30) & 7) << 1) | 1,
        (v >> 22) & 0xFF,
        (((v >> 15) & 0x7F) << 1) | 1,
        (v >> 7) & 0xFF,
        ((v & 0x7F) << 1) | 1,
    ])


def _spu_packet(duration: float) -> bytes:
    """Tiny 8x4 solid DVD subpicture (colour index 1); ffmpeg cannot render text->dvdsub."""
    pix_top = bytes([0x21, 0x21])
    pix_bot = bytes([0x21, 0x21])
    ctrl1 = (
        b"\x00\x00" + b"\x00\x00"  # date, next offset (patched below)
        + b"\x01"
        + b"\x03\x01\x00"  # palette
        + b"\x04\x0f\xff"  # alpha
        + b"\x05" + bytes([0x00, 0x00, 0x07, 0x00, 0x00, 0x03])  # x 0-7, y 0-3
        + b"\x06" + struct.pack(">HH", 4, 6)  # field offsets
        + b"\xff"
    )
    ctrl1_off = 4 + len(pix_top) + len(pix_bot)
    ctrl2_off = ctrl1_off + len(ctrl1)
    ctrl1 = ctrl1[:2] + struct.pack(">H", ctrl2_off) + ctrl1[4:]
    date = max(1, int(duration * 90000 / 1024))
    ctrl2 = struct.pack(">HH", date, ctrl2_off) + b"\x02\xff"
    total = ctrl2_off + len(ctrl2)
    return struct.pack(">HH", total, ctrl1_off) + pix_top + pix_bot + ctrl1 + ctrl2


def _write_vobsub(base: Path, lines: list[tuple[float, float, str]]) -> Path:
    """Write base.idx/base.sub (MPEG-PS, private stream 1) and return the .idx path."""
    pack = bytes.fromhex("000001ba44000400040101" "89c3f8")
    sub = bytearray()
    entries = []
    for start, end, _ in lines:
        pos = len(sub)
        spu = _spu_packet(max(end - start, 0.1))
        pes_body = b"\x81\x80\x05" + _pts(2, start * 1000) + b"\x20" + spu
        sub += pack + b"\x00\x00\x01\xbd" + struct.pack(">H", len(pes_body)) + pes_body
        entries.append((start, pos))
    idx = [
        "# VobSub index file, v7 (do not modify this line!)",
        "size: 320x180",
        "palette: 000000, ffffff, 000000, 000000, 000000, 000000, 000000, 000000, "
        "000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000",
        "langidx: 0", "", "id: en, index: 0",
    ]
    for start, pos in entries:
        ms = round(start * 1000)
        idx.append(
            f"timestamp: {ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}:{ms % 1000:03d}, "
            f"filepos: {pos:09x}"
        )
    base.with_suffix(".sub").write_bytes(bytes(sub))
    idx_path = base.with_suffix(".idx")
    idx_path.write_text("\n".join(idx) + "\n", encoding="ascii")
    return idx_path


def make_color_video(
    tools: Tools,
    path: Path,
    colors: list[tuple[str, float]],
    *,
    fps: int | str = 24,
    gop: int = 48,
    vcodec: str = "libx264",
    vopts: tuple[str, ...] = (),
    audio_codecs: tuple[str, ...] = ("aac",),
    ass_lines: list[tuple[float, float, str]] | None = None,
    image_sub: bool = False,
    chapters: list[tuple[float, str]] | None = None,
    font: Path | None = None,
) -> Path:
    """Segment i is a solid colour with a sine tone of 440*(i+1) Hz (all audio tracks identical)."""
    path = Path(path)
    ext = path.suffix.lower()
    total = sum(d for _, d in colors)
    tmp = Path(tempfile.mkdtemp(prefix="oped_fx_"))  # ASCII-safe helper files
    try:
        cmd: list[str] = [tools.ffmpeg, "-y", "-v", "error", "-nostdin"]
        for i, (color, dur) in enumerate(colors):
            cmd += ["-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}:r={fps}:d={dur}"]
            cmd += ["-f", "lavfi", "-i", f"sine=frequency={440 * (i + 1)}:sample_rate={RATE}:d={dur}"]
        n = len(colors)
        n_in = 2 * n
        inputs = "".join(f"[{2 * i}:v][{2 * i + 1}:a]" for i in range(n))
        na = len(audio_codecs)
        if na > 1:
            fc = (f"{inputs}concat=n={n}:v=1:a=1[v][ac];[ac]asplit={na}"
                  + "".join(f"[a{k}]" for k in range(na)))
        else:
            fc = f"{inputs}concat=n={n}:v=1:a=1[v][a0]"
        cmd += ["-filter_complex", fc]
        maps = ["-map", "[v]"] + [x for k in range(na) for x in ("-map", f"[a{k}]")]

        extra = 0
        sub_codecs: list[str] = []
        if ass_lines:
            ass = tmp / "subs.ass"
            _write_ass(ass, ass_lines)
            cmd += ["-i", str(ass)]
            maps += ["-map", f"{n_in + extra}:0"]
            extra += 1
            sub_codecs.append("mov_text" if ext in (".mp4", ".m4v", ".mov") else "ass")
        if image_sub:
            idx = _write_vobsub(tmp / "img", ass_lines or [(0.5, 1.5, "x")])
            cmd += ["-i", str(idx)]
            maps += ["-map", f"{n_in + extra}:0"]
            extra += 1
            sub_codecs.append("copy")
        meta_idx = None
        if chapters:
            meta = tmp / "chapters.ffmeta"
            _write_ffmetadata(meta, chapters, total)
            cmd += ["-i", str(meta)]
            meta_idx = n_in + extra
        if font:
            cmd += ["-attach", str(font), "-metadata:s:t", "mimetype=font/ttf"]

        cmd += maps
        cmd += ["-c:v", vcodec, *vopts, "-pix_fmt", "yuv420p", "-g", str(gop),
                "-keyint_min", str(gop), "-sc_threshold", "0"]
        for k, ac in enumerate(audio_codecs):
            cmd += [f"-c:a:{k}", ac]
        for k, sc in enumerate(sub_codecs):
            cmd += [f"-c:s:{k}", sc]
        if meta_idx is not None:
            cmd += ["-map_metadata", str(meta_idx), "-map_chapters", str(meta_idx)]
        cmd += ["-t", str(total), str(path)]
        run(cmd)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return path


def frame_color(tools: Tools, path: Path, t: float) -> tuple[int, int, int]:
    """Mean RGB of the frame on screen at time t (the last frame with pts <= t).

    A plain ``-ss t`` returns the first frame with pts >= t, i.e. the *next* frame
    whenever t falls inside a frame's display interval.
    """
    t0 = max(0.0, t - 1.0)
    out = run([
        tools.ffmpeg, "-v", "error", "-nostdin", "-ss", f"{t0:.6f}", "-i", str(path),
        # Timestamps are relative to t0 here; an output -t would instead count from the
        # first decoded frame and so overshoot by up to one frame.
        "-vf", f"select=lte(t\\,{t - t0 + 0.0005:.6f}),scale=1:1:flags=area", "-pix_fmt", "rgb24",
        "-fps_mode", "passthrough", "-f", "rawvideo", "-",
    ]).stdout
    r, g, b = out[-3:]
    return int(r), int(g), int(b)


def dominant_freq(tools: Tools, path: Path, t: float, stream: int = 0) -> float:
    out = run([
        tools.ffmpeg, "-v", "error", "-nostdin", "-ss", str(t), "-t", "0.2", "-i", str(path),
        "-map", f"0:a:{stream}", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-",
    ]).stdout
    x = np.frombuffer(out, dtype="<i2").astype(np.float64)
    x = x * np.hanning(len(x))
    spec = np.abs(np.fft.rfft(x))
    return float(np.argmax(spec) * RATE / len(x))
