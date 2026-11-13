from pathlib import Path

import pysubs2
import pytest

import oped.export as export
from fixtures import color_switch, make_color_video, raise_, tone_switch, write_ass
from oped.cutter import SmartCutError
from oped.discovery import discover
from oped.export import EpisodeResult, existing_outputs, export_episode, export_season, output_folder
from oped.media import FfmpegError, probe, run
from oped.project import Cut, load_project

FONT = Path(__file__).parent / "data" / "oped-test.ttf"
FRAME = 1 / 24


def build(tools, tmp_path):
    season_dir = tmp_path / "Show S1"
    src = make_color_video(tools, season_dir / "E1.mkv", [("red", 10), ("green", 10), ("blue", 10)],
                           audio_codecs=("aac", "flac"), ass_lines=[(1, 3, "a"), (25, 27, "b")],
                           image_sub=True, chapters=[(0, "A"), (12, "OP"), (20, "B")], font=FONT)
    write_ass(season_dir / "E1.en.ass", [(25, 27, "side")])
    make_color_video(tools, season_dir / "E2.mkv", [("red", 5)])
    (season_dir / "Fonts").mkdir(); (season_dir / "Fonts" / "x.ttf").write_bytes(b"0")
    return season_dir, discover(season_dir)


def test_export_episode_keeps_everything(tools, tmp_path):
    season_dir, season = build(tools, tmp_path)
    out_dir = output_folder(season_dir)
    r = export_episode(tools, season.episodes[0], season_dir, [(10, 20)], out_dir)
    assert r.status == "trimmed" and r.mode == "smart"
    info = probe(tools, out_dir / "E1.mkv")
    assert abs(info.duration - 20) < 0.1
    assert [s.kind for s in info.streams].count("audio") == 2
    assert [s.kind for s in info.streams].count("subtitle") == 2
    assert any(s.kind == "attachment" for s in info.streams)
    assert info.audio()[0].tags.get("language") == probe(tools, season_dir / "E1.mkv").audio()[0].tags.get("language")
    assert [c.title for c in info.chapters] == ["A", "B"]
    side = pysubs2.load(str(out_dir / "E1.en.ass"))
    assert side[0].start == 15000
    assert not list(out_dir.glob("*.part"))


def test_cut_at_file_edges(tools, tmp_path):
    season_dir, season = build(tools, tmp_path)
    r = export_episode(tools, season.episodes[0], season_dir, [(0, 5), (25, 30)], output_folder(season_dir))
    assert r.status == "trimmed"
    assert abs(probe(tools, output_folder(season_dir) / "E1.mkv").duration - 20) < 0.1
    # Both embedded subtitle cues fell in the cuts; the (empty) streams are still kept.
    assert [s.kind for s in probe(tools, output_folder(season_dir) / "E1.mkv").streams].count("subtitle") == 2


def test_season_copies_uncut_and_fonts_and_reports_existing(tools, tmp_path):
    season_dir, season = build(tools, tmp_path)
    p = load_project(season)
    p.episodes["E1.mkv"].cuts.append(Cut("c", 10, 20, "approved"))
    p.episodes["E2.mkv"].cuts.append(Cut("d", 1, 2, "rejected"))
    res = export_season(tools, p, season, ["E1.mkv", "E2.mkv"])
    assert [r.status for r in res] == ["trimmed", "copied"]
    assert (output_folder(season_dir) / "Fonts" / "x.ttf").exists()
    assert output_folder(season_dir) / "E2.mkv" in existing_outputs(season, ["E2.mkv"])


def test_smart_failure_reports_needs_fallback(tools, tmp_path, monkeypatch):
    season_dir, season = build(tools, tmp_path)
    monkeypatch.setattr(export, "cut_video", raise_(SmartCutError("hevc main12")))
    r = export_episode(tools, season.episodes[0], season_dir, [(10, 20)], output_folder(season_dir))
    assert r.status == "needs_fallback" and "hevc" in r.message
    assert not (output_folder(season_dir) / "E1.mkv").exists()


# ---------------------------------------------------------------- beyond the plan


def _tagged_source(tools, tmp_path, name="E1.mkv"):
    """Source whose streams carry language/title tags and non-trivial default/forced flags."""
    plain = make_color_video(tools, tmp_path / "plain.mkv", [("red", 10), ("green", 10), ("blue", 10)],
                             audio_codecs=("aac", "flac"), ass_lines=[(1, 3, "a"), (25, 27, "b")],
                             image_sub=True, font=FONT)
    season_dir = tmp_path / "Tagged S1"
    season_dir.mkdir()
    src = season_dir / name
    run([tools.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(plain), "-map", "0", "-c", "copy",
         "-metadata:s:v:0", "language=jpn",
         "-metadata:s:a:0", "language=jpn", "-metadata:s:a:0", "title=Japanese 2.0",
         "-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "title=English FLAC",
         "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Full Subs",
         "-metadata:s:s:1", "language=eng", "-metadata:s:s:1", "title=Signs",
         "-disposition:a:0", "default", "-disposition:a:1", "0",
         "-disposition:s:0", "0", "-disposition:s:1", "default+forced",
         str(src)])
    return season_dir, discover(season_dir)


def _meta(info):
    return [(s.kind, s.tags.get("language"), s.tags.get("title"),
             s.disposition.get("default"), s.disposition.get("forced"))
            for s in info.streams if s.kind != "attachment"]


def test_stream_tags_and_dispositions_survive(tools, tmp_path):
    season_dir, season = _tagged_source(tools, tmp_path)
    out_dir = output_folder(season_dir)
    r = export_episode(tools, season.episodes[0], season_dir, [(10, 20)], out_dir)
    assert r.status == "trimmed", r.message
    src, out = probe(tools, season_dir / "E1.mkv"), probe(tools, out_dir / "E1.mkv")
    assert _meta(src)[1:] == [("audio", "jpn", "Japanese 2.0", 1, 0), ("audio", "eng", "English FLAC", 0, 0),
                              ("subtitle", "eng", "Full Subs", 0, 0), ("subtitle", "eng", "Signs", 1, 1)]
    assert _meta(out) == _meta(src)
    att = [s for s in out.streams if s.kind == "attachment"]
    assert len(att) == 1 and att[0].tags.get("filename") == FONT.name


def test_off_grid_cuts_keep_sidecar_and_chapters_on_the_video(tools, tmp_path):
    """Cut times off the 24 fps grid plus a dropped 29 ms sliver: a plain TimeMap of the
    requested cuts would put the 20 s events ~70 ms late; they must follow the cut video."""
    season_dir = tmp_path / "Grid S1"
    make_color_video(tools, season_dir / "E1.mkv", [("red", 10), ("green", 10), ("blue", 10)],
                     chapters=[(0, "A"), (20, "B")])
    write_ass(season_dir / "E1.ass", [(20, 22, "blue")])
    season = discover(season_dir)
    out_dir = output_folder(season_dir)
    r = export_episode(tools, season.episodes[0], season_dir, [(5.0, 8.001), (8.03, 15.001)], out_dir)
    assert r.status == "trimmed", r.message
    out = out_dir / "E1.mkv"
    video_t = color_switch(tools, out, 9.5, 10.5, channel=2)        # source 20.0 s
    cue = pysubs2.load(str(out_dir / "E1.ass"))[0].start / 1000
    chapter = probe(tools, out).chapters[1]
    assert chapter.title == "B"
    assert abs(cue - video_t) < FRAME, (cue, video_t)
    assert abs(chapter.start - video_t) < FRAME, (chapter.start, video_t)


def test_keyframe_mode_keeps_audio_subs_and_chapters_with_the_video(tools, tmp_path):
    season_dir = tmp_path / "Key S1"
    make_color_video(tools, season_dir / "E1.mkv", [("red", 10), ("green", 10), ("blue", 10)],
                     chapters=[(0, "A"), (20, "B")])                  # keyframes every 2 s
    write_ass(season_dir / "E1.ass", [(20, 22, "blue")])
    season = discover(season_dir)
    out_dir = output_folder(season_dir)
    r = export_episode(tools, season.episodes[0], season_dir, [(5, 15.5)], out_dir, mode="keyframe")
    assert r.status == "trimmed" and r.mode == "keyframe", r.message
    out = out_dir / "E1.mkv"
    # Keyframe mode keeps 0..4 and 16..30, so source 20 s lands at 8 s (not 9.5 s).
    video_t = color_switch(tools, out, 7.5, 10.0, channel=2)
    audio_t = tone_switch(tools, out, 7.5, 10.0, 880, 1320)
    cue = pysubs2.load(str(out_dir / "E1.ass"))[0].start / 1000
    assert abs(video_t - 8.0) < 0.05
    assert abs(audio_t - video_t) < 0.03
    assert abs(cue - video_t) < FRAME and abs(probe(tools, out).chapters[1].start - video_t) < FRAME


def test_reexport_failure_never_leaves_part_or_clobbers_existing(tools, tmp_path, monkeypatch):
    season_dir, season = build(tools, tmp_path)
    out_dir = output_folder(season_dir)
    out_dir.mkdir()
    (out_dir / "E1.mkv").write_bytes(b"old video")
    (out_dir / "E1.en.ass").write_bytes(b"old subs")
    assert set(existing_outputs(season, ["E1.mkv", "E2.mkv"])) == {out_dir / "E1.mkv", out_dir / "E1.en.ass"}

    real_run = export.run

    def failing_mux(cmd, **kw):
        if any(str(c).endswith(".part") for c in cmd):
            Path(str(cmd[-1])).write_bytes(b"half")                # a half-written .part
            raise FfmpegError(cmd, "Error muxing: No space left on device")
        return real_run(cmd, **kw)

    monkeypatch.setattr(export, "run", failing_mux)
    r = export_episode(tools, season.episodes[0], season_dir, [(10, 20)], out_dir)
    assert r.status == "failed" and "No space left" in r.message
    assert (out_dir / "E1.mkv").read_bytes() == b"old video"
    assert (out_dir / "E1.en.ass").read_bytes() == b"old subs"
    assert sorted(p.name for p in out_dir.iterdir()) == ["E1.en.ass", "E1.mkv"]

    monkeypatch.setattr(export, "run", real_run)
    r = export_episode(tools, season.episodes[0], season_dir, [(10, 20)], out_dir)
    assert r.status == "trimmed"
    assert abs(probe(tools, out_dir / "E1.mkv").duration - 20) < 0.1
    assert pysubs2.load(str(out_dir / "E1.en.ass"))[0].start == 15000
    assert sorted(p.name for p in out_dir.iterdir()) == ["E1.en.ass", "E1.mkv"]


def test_undecodable_sidecar_fails_only_that_episode(tools, tmp_path):
    season_dir = tmp_path / "Enc S1"
    make_color_video(tools, season_dir / "E1.mkv", [("red", 4)])
    make_color_video(tools, season_dir / "E2.mkv", [("red", 4)])
    (season_dir / "E1.srt").write_bytes("1\n00:00:00,500 --> 00:00:01,000\n\xe9t\xe9\n".encode("cp1252"))
    season = discover(season_dir)
    p = load_project(season)
    for f in ("E1.mkv", "E2.mkv"):
        p.episodes[f].cuts.append(Cut(f, 1.5, 2.5, "approved"))
    res = export_season(tools, p, season, ["E1.mkv", "E2.mkv"])
    assert [r.status for r in res] == ["failed", "trimmed"]
    assert "E1.srt" in res[0].message and "UTF-8" in res[0].message
    out_dir = output_folder(season_dir)
    assert sorted(x.name for x in out_dir.iterdir()) == ["E2.mkv"]


def test_cuts_covering_everything_fail_clearly(tools, tmp_path):
    season_dir, season = build(tools, tmp_path)
    r = export_episode(tools, season.episodes[1], season_dir, [(0, 3), (2.98, 5)], output_folder(season_dir))
    assert r.status == "failed" and "nothing" in r.message.lower()
    assert not list(output_folder(season_dir).glob("*"))


def test_fansub_names_subs_folder_and_e1_vs_e10(tools, tmp_path):
    season_dir = tmp_path / "Sousou no Frieren [BD] 葬送のフリーレン"
    fansub = "[SubsPlease] Sousou no Frieren's - 01 (1080p) [A1B2C3D4]"
    make_color_video(tools, season_dir / f"{fansub}.mkv", [("red", 3), ("green", 3)])
    write_ass(season_dir / f"{fansub}.en.ass", [(4, 5, "x")])
    make_color_video(tools, season_dir / "E1.mkv", [("red", 3)])
    make_color_video(tools, season_dir / "E10.mkv", [("red", 3)])
    write_ass(season_dir / "E1.ass", [(2, 2.5, "one")])
    (season_dir / "Subs").mkdir()
    write_ass(season_dir / "Subs" / "E10.ass", [(2, 2.5, "ten")])
    season = discover(season_dir)
    p = load_project(season)
    p.episodes[f"{fansub}.mkv"].cuts.append(Cut("a", 1, 2, "approved"))
    p.episodes["E10.mkv"].cuts.append(Cut("b", 0, 1, "approved"))
    seen = []
    res = export_season(tools, p, season, [f"{fansub}.mkv", "E1.mkv", "E10.mkv"],
                        progress=lambda f, x: seen.append((f, x)))
    assert [r.status for r in res] == ["trimmed", "copied", "trimmed"], [r.message for r in res]
    out = output_folder(season_dir)
    assert out.name == season_dir.name + " (trimmed)"
    assert abs(probe(tools, out / f"{fansub}.mkv").duration - 5) < 0.1
    assert pysubs2.load(str(out / f"{fansub}.en.ass"))[0].start == 3000
    assert [e.text for e in pysubs2.load(str(out / "E1.ass"))] == ["one"]
    assert pysubs2.load(str(out / "E1.ass"))[0].start == 2000                # copied unchanged
    assert pysubs2.load(str(out / "Subs" / "E10.ass"))[0].start == 1000      # retimed, same rel path
    assert not (out / "E10.ass").exists()
    assert (f"{fansub}.mkv", 1.0) in seen and ("E10.mkv", 1.0) in seen


def test_mp4_stays_mp4_with_faststart(tools, tmp_path):
    season_dir = tmp_path / "Mp4 S1"
    make_color_video(tools, season_dir / "E1.mp4", [("red", 5), ("green", 5)],
                     ass_lines=[(1, 2, "a"), (7, 8, "b")], chapters=[(0, "A"), (6, "B")])
    season = discover(season_dir)
    out_dir = output_folder(season_dir)
    calls = []
    r = export_episode(tools, season.episodes[0], season_dir, [(2, 4)], out_dir, progress=calls.append)
    assert r.status == "trimmed", r.message
    info = probe(tools, out_dir / "E1.mp4")
    assert info.container == "mp4" and abs(info.duration - 8) < 0.1
    assert [s.codec for s in info.subtitles()] == ["mov_text"]
    assert [c.title for c in info.chapters] == ["A", "B"] and abs(info.chapters[1].start - 4) < 0.05
    data = (out_dir / "E1.mp4").read_bytes()
    assert data.find(b"moov") < data.find(b"mdat")                          # +faststart
    assert calls and calls[-1] == 1.0 and calls == sorted(calls)
    assert isinstance(r, EpisodeResult)
