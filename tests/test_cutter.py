import shutil
import subprocess
from pathlib import Path

import pytest

from fixtures import dominant_freq, frame_color, make_color_video
from oped.cutter import SmartCutError, concat_list, cut_audio, cut_video
from oped.media import probe

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


def test_no_stray_temp_files(tools, tmp_path):
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
    with pytest.raises(SmartCutError):
        cut_video(tools, info, [(0, 5.0)], out_dir / "bad.mkv", "smart", work / "missing" / "x")
