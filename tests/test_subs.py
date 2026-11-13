import pysubs2
import pytest

from fixtures import extract_sub_events, make_color_video, write_ass
from oped.media import Chapter, probe, run
from oped.subs import retime_embedded_subs, retime_sidecar, write_ffmetadata
from oped.timeline import TimeMap

LINES = [(1, 3, "before"), (9, 12, "edge"), (12, 14, "inside"), (14, 22, "spans"), (25, 27, "after")]
TM = lambda: TimeMap([(10, 20)], 30)


def test_retime_sidecar_ass(tmp_path):
    src = tmp_path / "E1.ass"; write_ass(src, LINES)
    retime_sidecar(src, tmp_path / "out" / "E1.ass", TM())
    ev = [(e.start / 1000, e.end / 1000, e.text) for e in pysubs2.load(str(tmp_path / "out" / "E1.ass"))]
    assert ev == [(1, 3, "before"), (9, 10, "edge"), (10, 12, "spans"), (15, 17, "after")]


def test_retime_sidecar_srt_keeps_styles_section(tmp_path):
    src = tmp_path / "E1.ass"; write_ass(src, LINES)
    retime_sidecar(src, tmp_path / "o" / "E1.ass", TM())
    assert "Style: Default,Arial,28" in (tmp_path / "o" / "E1.ass").read_text(encoding="utf-8")
    srt = tmp_path / "E1.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nhi\n\n2\n00:00:12,000 --> 00:00:14,000\ngone\n", encoding="utf-8")
    retime_sidecar(srt, tmp_path / "o" / "E1.srt", TM())
    assert [e.text for e in pysubs2.load(str(tmp_path / "o" / "E1.srt"))] == ["hi"]


def test_retime_embedded_text_and_image(tools, tmp_path):
    src = make_color_video(tools, tmp_path / "e.mkv", [("red", 30)], ass_lines=LINES, image_sub=True)
    out = retime_embedded_subs(probe(tools, src), TM(), tmp_path / "subs.mkv")
    text = extract_sub_events(tools, out, stream=0)
    assert text == [(1, 3), (9, 10), (10, 12), (15, 17)]
    image = extract_sub_events(tools, out, stream=1)
    assert (15, 17) in [(round(a), round(b)) for a, b in image]
    # Deviation from the plan text: `all(not (10 < a < 20))` contradicts (15, 17) in image
    # (15 is inside 10..20). The intent is "no cue from the cut region survives", so the
    # forbidden range is the collapsed span (10, 15) in output time.
    assert all(not (10 < a < 15) for a, _ in image)
    assert [(round(a), round(b)) for a, b in image] == [(1, 3), (15, 17)]


def test_retime_embedded_mp4_mov_text(tools, tmp_path):
    src = make_color_video(tools, tmp_path / "e.mp4", [("red", 30)], ass_lines=LINES)
    out = retime_embedded_subs(probe(tools, src), TM(), tmp_path / "subs.mp4")
    text = [(round(a, 2), round(b, 2)) for a, b in extract_sub_events(tools, out, stream=0)]
    # mov_text also stores blank gap cues; real cues are retimed and "inside" is gone
    assert {(1, 3), (9, 10), (10, 12), (15, 17)} <= set(text)
    assert (12, 14) not in text and len(text) == 7


def test_image_packets_without_duration_snap_not_drop(tools, tmp_path):
    import fixtures
    lines = [(1, 3, "a"), (12, 14, "in"), (14, 16, "in2"), (25, 27, "z")]
    idx = fixtures._write_vobsub(tmp_path / "img", lines)
    src = fixtures._vobsub_mkv(idx, lines, tmp_path / "nodur.mkv", durations=False)
    out = retime_embedded_subs(probe(tools, src), TM(), tmp_path / "o.mkv")
    starts = [round(a, 2) for a, _ in extract_sub_events(tools, out, stream=0)]
    assert starts == [1, 10, 10, 15]    # nothing dropped; cut-interior packets collapse onto the cut point


def test_no_subtitle_streams_returns_none(tools, tmp_path):
    src = make_color_video(tools, tmp_path / "n.mkv", [("red", 3)])
    assert retime_embedded_subs(probe(tools, src), TM(), tmp_path / "x.mkv") is None


def test_chapters(tmp_path):
    ch = (Chapter(0, 10, "Cold open"), Chapter(12, 20, "OP"), Chapter(20, 30, "Part A"))
    meta = write_ffmetadata(ch, TM(), tmp_path / "ch.txt").read_text()
    assert "title=Cold open" in meta and "title=Part A" in meta and "title=OP" not in meta
    assert "START=10000" in meta and "END=20000" in meta


def test_chapters_escape_and_empty(tmp_path):
    BS = chr(92)
    ch = (Chapter(0, 5, "a=b;c#d" + BS + "e" + chr(10) + "f"),)
    meta = write_ffmetadata(ch, TM(), tmp_path / "c.txt").read_text()
    assert "title=a" + BS + "=b" + BS + ";c" + BS + "#d" + BS + BS + "e" + BS + chr(10) + "f" in meta
    assert write_ffmetadata((Chapter(12, 15, "x"),), TM(), tmp_path / "d.txt") is None
    assert write_ffmetadata((), TM(), tmp_path / "e.txt") is None
