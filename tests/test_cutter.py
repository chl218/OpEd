import shutil
import subprocess
from pathlib import Path

import pytest

import numpy as np

from fixtures import RATE, dominant_freq, frame_color, make_color_video
from oped.cutter import SmartCutError, concat_list, cut_audio, cut_video, snap_to_frames
from oped.media import probe, run

COLORS = [("red", 10), ("green", 10), ("blue", 10)]   # tones 440 / 880 / 1320


def _has_encoder(name: str) -> bool:
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    out = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


@pytest.mark.parametrize("vcodec", [
    "libx264",
    pytest.param("libx265", marks=pytest.mark.skipif(
        not _has_encoder("libx265"), reason="ffmpeg lacks libx265")),
])
def test_smart_cut_is_frame_exact_and_in_sync(tools, tmp_path, vcodec):
    src = make_color_video(tools, tmp_path / "it's [x] 01.mkv", COLORS, vcodec=vcodec, audio_codecs=("aac", "flac"))
    info = probe(tools, src)
    keep = [(0, 5.0), (15.0, 30)]                     # cut 5..15, mid-GOP on both sides
    v = cut_video(tools, info, keep, tmp_path / "v.mkv", "smart", tmp_path)
    a = cut_audio(tools, info, keep, tmp_path / "a.mka", tmp_path)
    assert abs(probe(tools, v).duration - 20) < 0.1
    assert frame_color(tools, v, 4.96)[0] > 200        # last red frame
    assert frame_color(tools, v, 5.01)[1] > 100        # first green frame
    assert frame_color(tools, v, 10.05)[2] > 200       # blue
    assert dominant_freq(tools, a, 4.7) == pytest.approx(440, rel=0.03)
    assert dominant_freq(tools, a, 5.3) == pytest.approx(880, rel=0.03)
    assert dominant_freq(tools, a, 5.3, stream=1) == pytest.approx(880, rel=0.03)


def test_short_interval_inside_one_gop(tools, tmp_path):
    src = make_color_video(tools, tmp_path / "s.mkv", COLORS)
    v = cut_video(tools, probe(tools, src), [(10.3, 11.1)], tmp_path / "v.mkv", "smart", tmp_path)
    assert abs(probe(tools, v).duration - 0.8) < 0.1


def test_unsupported_codec_raises_smartcuterror(tools, tmp_path):
    src = make_color_video(tools, tmp_path / "m.mkv", COLORS, vcodec="mpeg4")
    with pytest.raises(SmartCutError):
        cut_video(tools, probe(tools, src), [(0, 5)], tmp_path / "v.mkv", "smart", tmp_path)


@pytest.mark.parametrize("mode", ["reencode", "keyframe"])
def test_fallback_modes(tools, tmp_path, mode):
    src = make_color_video(tools, tmp_path / "f.mkv", COLORS, vcodec="mpeg4")
    v = cut_video(tools, probe(tools, src), [(0, 5), (15, 30)], tmp_path / "v.mkv", mode, tmp_path)
    tol = 0.1 if mode == "reencode" else 2.5
    assert abs(probe(tools, v).duration - 20) < tol


def test_concat_list_escapes_quotes():
    assert "file 'it'\\''s.mkv'" in concat_list([(Path("it's.mkv"), 1.0, 2.0)])


def test_no_stray_temp_files(tools, tmp_path, monkeypatch):
    src_dir = tmp_path / "src"
    out_dir = tmp_path / "out 日本 [1]"
    work = tmp_path / "work"
    for d in (src_dir, out_dir, work):
        d.mkdir()
    src = make_color_video(tools, src_dir / "ep 'q' [1] 第1話.mkv", COLORS)
    info = probe(tools, src)
    cut_video(tools, info, [(0, 5.0), (15.0, 30)], out_dir / "v.mkv", "smart", work)
    cut_audio(tools, info, [(0, 5.0), (15.0, 30)], out_dir / "a.mka", work)
    assert sorted(p.name for p in out_dir.iterdir()) == ["a.mka", "v.mkv"]
    assert list(work.iterdir()) == []

    import oped.cutter as cutter

    def broken_validate(*args, **kwargs):
        raise SmartCutError("injected")

    monkeypatch.setattr(cutter, "_validate", broken_validate)
    with pytest.raises(SmartCutError):
        cut_video(tools, info, [(0, 5.0)], out_dir / "bad.mkv", "smart", work)
    assert sorted(p.name for p in out_dir.iterdir()) == ["a.mka", "v.mkv"]
    assert list(work.iterdir()) == []


def _color_switch(tools, path, lo, hi, channel):
    """Time (to ~0.25 ms) at which ``channel`` first dominates the frame on screen."""
    for _ in range(12):
        mid = (lo + hi) / 2
        if frame_color(tools, path, mid)[channel] > 200:
            hi = mid
        else:
            lo = mid
    return hi


def _tone_switch(tools, path, lo, hi, f_old, f_new, stream=0):
    """Time at which tone ``f_new`` overtakes ``f_old`` (4 ms windows, samples placed by pts)."""
    out = run([
        tools.ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", f"0:a:{stream}",
        "-af", f"aresample=async=1:min_hard_comp=0.001:first_pts=0,atrim=start={lo}:end={hi}",
        "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-",
    ]).stdout
    x = np.frombuffer(out, dtype="<i2").astype(np.float64)
    n = int(0.004 * RATE)
    tt = np.arange(n) / RATE
    for i in range(0, len(x) - n, n // 4):
        seg = x[i:i + n]
        if abs(seg @ np.exp(-2j * np.pi * f_new * tt)) > abs(seg @ np.exp(-2j * np.pi * f_old * tt)):
            return lo + (i + n / 2) / RATE
    raise AssertionError("no tone switch found")


def test_snap_to_frames_uses_the_video_frame_grid(tools, tmp_path):
    src = make_color_video(tools, tmp_path / "g.mkv", COLORS)          # 24 fps, ms timestamps
    snapped = snap_to_frames(tools, probe(tools, src), [(0, 5.01), (15.03, 30), (29.99, 29.995)])
    assert snapped == [pytest.approx((0, 5.042), abs=0.001), pytest.approx((15.042, 30.0), abs=0.001)]


def test_off_grid_cuts_keep_audio_in_sync_with_video(tools, tmp_path):
    src = make_color_video(tools, tmp_path / "o.mkv", COLORS, audio_codecs=("aac", "flac"))
    info = probe(tools, src)
    keep = [(0, 5.01), (15.03, 30)]                    # neither boundary is on the 24 fps grid
    v = cut_video(tools, info, keep, tmp_path / "v.mkv", "smart", tmp_path)
    a = cut_audio(tools, info, keep, tmp_path / "a.mka", tmp_path)
    # Source green->blue at 20.0 s; it must land at the same output time in video and audio.
    video_t = _color_switch(tools, v, 9.5, 10.5, channel=2)
    for stream in (0, 1):
        audio_t = _tone_switch(tools, a, 9.5, 10.5, 880, 1320, stream)
        assert abs(audio_t - video_t) < 0.008, (stream, audio_t, video_t)


@pytest.mark.parametrize("vcodec", [
    "libx264",
    pytest.param("libx265", marks=pytest.mark.skipif(
        not _has_encoder("libx265"), reason="ffmpeg lacks libx265")),
])
@pytest.mark.parametrize("ext", ["mkv", "mp4"])
def test_smart_cut_source_without_bframes(tools, tmp_path, vcodec, ext):
    src = make_color_video(tools, tmp_path / f"nb.{ext}", COLORS, fps="24000/1001", vcodec=vcodec,
                           vopts=("-bf", "0"))
    v = cut_video(tools, probe(tools, src), [(0, 5), (15, 30)], tmp_path / "v.mkv", "smart", tmp_path)
    assert abs(probe(tools, v).duration - 20) < 0.1
    assert frame_color(tools, v, 4.9)[0] > 200
    assert frame_color(tools, v, 5.06)[1] > 100
