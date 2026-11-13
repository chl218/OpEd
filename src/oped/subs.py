"""Subtitle and chapter retiming after cuts."""
from __future__ import annotations

from pathlib import Path

import av
import pysubs2

from oped.media import Chapter, MediaInfo
from oped.timeline import TimeMap

TEXT_SIDECARS = {".ass", ".ssa", ".srt", ".vtt"}


def _retime_packets(src: Path, out: Path, tm: TimeMap, indices: list[int] | None,
                    text_streams: set[int] | None = None) -> bool:
    """Demux subtitle streams of ``src`` (PyAV stream indices), retime, mux into ``out``.

    ``text_streams`` holds the indices to treat as text; all others are image streams.
    Returns False (and writes nothing) when no stream or no packet survives.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(src)) as inp:
        subs = [s for s in inp.streams if s.type == "subtitle" and (indices is None or s.index in indices)]
        if not subs:
            return False
        if text_streams is None:
            text_streams = {s.index for s in subs if s.codec_context.name in ("ass", "ssa", "subrip", "srt", "mov_text", "webvtt", "text")}
        by_index = {s.index: s for s in subs}
        items = []  # (out_pts_sec, order, stream_index, packet, out_dur_sec)
        for n, pkt in enumerate(inp.demux(*subs)):
            if pkt.pts is None:
                continue
            tb = pkt.time_base
            a = float(pkt.pts * tb)
            d = float(pkt.duration * tb) if pkt.duration else 0.0
            idx = pkt.stream.index
            if idx in text_streams:
                span = tm.map_span(a, a + d)
            elif d > 0:
                span = tm.map_span_whole(a, a + d)
            else:
                t = tm.snap(a)
                span = (t, t)
            if span is None:
                continue
            items.append((span[0], n, idx, pkt, span[1] - span[0], tb))
        if not items:
            # Every cue was cut. A matroska file holding only packet-less subtitle
            # streams comes out unreadable, so write nothing; callers keep the empty
            # streams some other way (export maps them from the source, packets dropped).
            return False
        items.sort(key=lambda x: (x[0], x[1]))
        with av.open(str(out), "w") as outc:
            omap = {i: outc.add_stream_from_template(s) for i, s in by_index.items()}
            for start, _, idx, pkt, dur, tb in items:
                pkt.stream = omap[idx]
                pkt.time_base = tb
                pkt.pts = pkt.dts = round(start / tb)
                pkt.duration = round(dur / tb) if dur > 0 else 0
                outc.mux(pkt)
    return True


def retime_embedded_subs(info: MediaInfo, tm: TimeMap, out: Path) -> Path | None:
    """Retime all subtitle streams into ``out``; None if there are none or every cue was cut."""
    if not info.subtitles():
        return None
    out = Path(out)
    text_ffidx = {s.index for s in info.subtitles() if s.is_text_sub}
    with av.open(str(info.path)) as inp:
        # ffprobe stream indices equal PyAV stream indices
        subs = [s.index for s in inp.streams if s.type == "subtitle"]
    ok = _retime_packets(info.path, out, tm, subs, text_ffidx)
    return out if ok else None


def retime_sidecar(src: Path, dst: Path, tm: TimeMap) -> None:
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".sup":
        _retime_packets(src, dst, tm, None, set())
        return
    subs = pysubs2.load(str(src))
    kept = []
    for ev in subs:
        span = tm.map_span(ev.start / 1000, ev.end / 1000)
        if span is None:
            continue
        ev.start, ev.end = round(span[0] * 1000), round(span[1] * 1000)
        kept.append(ev)
    subs.events = kept
    subs.save(str(dst))


def _esc(s: str) -> str:
    for ch in ("\\", "=", ";", "#", "\n"):
        s = s.replace(ch, "\\" + ch)
    return s


def write_ffmetadata(chapters: tuple[Chapter, ...], tm: TimeMap, path: Path) -> Path | None:
    starts = []
    for c in chapters:
        t = tm.map(c.start)
        if t is not None:
            starts.append((t, c.title))
    # A chapter whose mapped start equals the next one's (it began exactly where a cut
    # began) or the output end would be zero-length: keep only the later chapter.
    ends = [round(t * 1000) for t, _ in starts[1:]] + [round(tm.out_duration * 1000)]
    starts = [s for s, nxt in zip(starts, ends) if round(s[0] * 1000) < nxt]
    if not starts:
        return None
    lines = [";FFMETADATA1"]
    for i, (t, title) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else tm.out_duration
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(t * 1000)}",
                  f"END={round(end * 1000)}", f"title={_esc(title)}"]
    path = Path(path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
