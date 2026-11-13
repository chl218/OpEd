import shutil

import pytest

from fixtures import dominant_freq, frame_color, make_color_video
from oped.media import ToolsMissing, find_tools, keyframes, probe


def test_find_tools_missing_gives_install_hint(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ToolsMissing) as e:
        find_tools()
    assert "ffmpeg" in str(e.value) and ("winget" in str(e.value) or "brew" in str(e.value) or "apt" in str(e.value))


def test_probe_reports_streams_duration_chapters(tools, tmp_path):
    p = make_color_video(tools, tmp_path / "[Grp] Show - 01 (1080p) 'x' 日本.mkv",
                         [("red", 10), ("green", 10)], audio_codecs=("aac", "flac"),
                         ass_lines=[(1, 2, "hi")], chapters=[(0, "A"), (10, "B")])
    info = probe(tools, p)
    assert info.container == "mkv"
    assert abs(info.duration - 20) < 0.1
    assert [s.kind for s in info.streams][:4] == ["video", "audio", "audio", "subtitle"]
    assert info.subtitles()[0].is_text_sub
    assert [round(c.start) for c in info.chapters] == [0, 10]


def test_keyframes_follow_gop(tools, tmp_path):
    p = make_color_video(tools, tmp_path / "a.mkv", [("red", 10)], gop=48)
    kf = keyframes(tools, p)
    assert kf[0] == pytest.approx(0, abs=0.05)
    assert kf[1] == pytest.approx(2.0, abs=0.05)


def test_fixture_helpers(tools, tmp_path):
    p = make_color_video(tools, tmp_path / "a.mkv", [("red", 3), ("blue", 3)])
    assert frame_color(tools, p, 1.0)[0] > 200
    assert dominant_freq(tools, p, 4.0) == pytest.approx(880, rel=0.03)
