from oped.discovery import discover


def touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return p


def test_natural_sort_and_video_filter(tmp_path):
    for n in ["Ep 10.mkv", "Ep 2.mkv", "Ep 1.mp4", "notes.txt", "oped-project.json"]:
        touch(tmp_path / n)
    s = discover(tmp_path)
    assert [e.video.name for e in s.episodes] == ["Ep 1.mp4", "Ep 2.mkv", "Ep 10.mkv"]


def test_sidecars_match_by_stem_not_prefix(tmp_path):
    touch(tmp_path / "E1.mkv")
    touch(tmp_path / "E10.mkv")
    touch(tmp_path / "E1.ass")
    touch(tmp_path / "E1.en.srt")
    touch(tmp_path / "E10.eng.forced.ass")
    s = discover(tmp_path)
    by = {e.video.name: sorted(p.name for p in e.sidecars) for e in s.episodes}
    assert by == {"E1.mkv": ["E1.ass", "E1.en.srt"], "E10.mkv": ["E10.eng.forced.ass"]}


def test_subs_folder_layouts_fonts_and_mka_warning(tmp_path):
    name = "[SubsPlease] Frieren - 01 (1080p) [A1B2C3D4]"
    touch(tmp_path / f"{name}.mkv")
    touch(tmp_path / "Subs" / f"{name}.ass")
    touch(tmp_path / "Subs" / name / "2_English.srt")
    touch(tmp_path / f"{name}.mka")
    touch(tmp_path / "Fonts" / "a.ttf")
    s = discover(tmp_path)
    assert sorted(p.name for p in s.episodes[0].sidecars) == ["2_English.srt", f"{name}.ass"]
    assert s.font_dirs == [tmp_path / "Fonts"]
    assert any("External audio" in w for w in s.warnings)
