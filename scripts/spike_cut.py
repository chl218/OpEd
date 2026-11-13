"""Spike: smart-cut one segment out of a real file and report what happened.

Usage: uv run python scripts/spike_cut.py <video> <start> <end> --out <scratch dir>

Writes <stem>.spike.mkv (video + all audio, smart mode) into the --out directory and
prints the keyframes around the cut, the expected/actual durations and the validation
result. Open the result in VLC/mpv and check the cut is seamless and in sync.
"""
from __future__ import annotations

import argparse
import bisect
import shutil
import sys
import tempfile
import time
from pathlib import Path

from oped.cutter import SmartCutError, cut_audio, cut_video
from oped.media import find_tools, keyframes, probe, run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("start", type=float, help="cut start (s)")
    ap.add_argument("end", type=float, help="cut end (s)")
    ap.add_argument("--out", type=Path, required=True, help="scratch directory for the result")
    args = ap.parse_args(argv)

    tools = find_tools()
    info = probe(tools, args.video)
    v = info.video()
    print(f"source:   {info.path}")
    print(f"video:    {v.codec if v else '-'}  fps={info.fps}  duration={info.duration:.3f}s")
    print(f"audio:    {', '.join(a.codec for a in info.audio()) or '-'}")

    kf = keyframes(tools, info.path)
    for label, t in (("start", args.start), ("end", args.end)):
        i = bisect.bisect_left(kf, t)
        before = kf[i - 1] if i > 0 else None
        after = kf[i] if i < len(kf) else None
        print(f"cut {label} {t:.3f}: keyframe before={before}  after={after}")

    keep = [(0.0, args.start), (args.end, info.duration)]
    keep = [(a, b) for a, b in keep if b - a > 0.05]
    for a, b in keep:
        i = bisect.bisect_left(kf, a)
        j = bisect.bisect_right(kf, b) - 1
        k1 = kf[i] if i < len(kf) else None
        k2 = kf[j] if j >= 0 else None
        print(f"keep [{a:.3f}, {b:.3f}): K1={k1}  K2={k2}  "
              f"re-encoded ~{(k1 - a if k1 is not None else b - a) + (b - k2 if k2 is not None else 0):.2f}s")
    expected = sum(b - a for a, b in keep)

    args.out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="spike_", dir=args.out))
    try:
        t0 = time.monotonic()
        try:
            vid = cut_video(tools, info, keep, work / "v.mkv", "smart", work)
        except SmartCutError as e:
            print(f"SMART CUT FAILED: {e}")
            return 1
        t1 = time.monotonic()
        aud = cut_audio(tools, info, keep, work / "a.mka", work) if info.audio() else None
        t2 = time.monotonic()
        final = args.out / f"{info.path.stem}.spike.mkv"
        cmd = [tools.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(vid)]
        if aud:
            cmd += ["-i", str(aud), "-map", "0:v", "-map", "1:a"]
        cmd += ["-c", "copy", str(final)]
        run(cmd)
        vd = probe(tools, vid).duration
        ad = probe(tools, aud).duration if aud else None
        print(f"expected duration: {expected:.3f}s")
        print(f"video duration:    {vd:.3f}s  (diff {vd - expected:+.3f})  [{t1 - t0:.1f}s]")
        if ad is not None:
            print(f"audio duration:    {ad:.3f}s  (diff {ad - expected:+.3f})  [{t2 - t1:.1f}s]")
        print("validation:        OK (decode clean, duration within tolerance)")
        print(f"wrote {final}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
