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


def _by(season):
    return {e.video.name: sorted(str(p.relative_to(season.folder)).replace("\\", "/") for p in e.sidecars)
            for e in season.episodes}


def test_sidecar_goes_only_to_the_longest_matching_stem(tmp_path):
    """`01.5.ass` matches `01` by prefix and `01.5` exactly: only 01.5 may get it."""
    for n in ("[G] Show - 01.mkv", "[G] Show - 01.5.mkv", "[G] Show - 01.ass",
              "[G] Show - 01.5.ass", "[G] Show - 01.5.en.srt", "[G] Show - 01.en.srt"):
        touch(tmp_path / n)
    assert _by(discover(tmp_path)) == {
        "[G] Show - 01.mkv": ["[G] Show - 01.ass", "[G] Show - 01.en.srt"],
        "[G] Show - 01.5.mkv": ["[G] Show - 01.5.ass", "[G] Show - 01.5.en.srt"],
    }


def test_longest_stem_wins_under_subs_folder(tmp_path):
    for n in ("[G] Show - 01.mkv", "[G] Show - 01.5.mkv", "E1.mkv", "E10.mkv"):
        touch(tmp_path / n)
    for n in ("[G] Show - 01.ass", "[G] Show - 01.5.ass", "[G] Show - 01.5.en.ass",
              "E1.ass", "E10.ass", "E10.en.ass"):
        touch(tmp_path / "Subs" / n)
    touch(tmp_path / "Subs" / "[G] Show - 01" / "track.srt")
    touch(tmp_path / "Subs" / "[G] Show - 01.5" / "track.srt")
    assert _by(discover(tmp_path)) == {
        "E1.mkv": ["Subs/E1.ass"],
        "E10.mkv": ["Subs/E10.ass", "Subs/E10.en.ass"],
        "[G] Show - 01.mkv": ["Subs/[G] Show - 01.ass", "Subs/[G] Show - 01/track.srt"],
        "[G] Show - 01.5.mkv": ["Subs/[G] Show - 01.5.ass", "Subs/[G] Show - 01.5.en.ass",
                                "Subs/[G] Show - 01.5/track.srt"],
    }


def test_non_ascii_and_apostrophe_names(tmp_path):
    a = "[Grp] Frieren's Journey 葬送のフリーレン - 01"
    b = a + ".5"
    for n in (f"{a}.mkv", f"{b}.mkv", f"{a}.ja.ass", f"{b}.ja.ass"):
        touch(tmp_path / n)
    touch(tmp_path / "Subs" / a / "日本語.ass")
    assert _by(discover(tmp_path)) == {
        f"{a}.mkv": [f"Subs/{a}/日本語.ass", f"{a}.ja.ass"],
        f"{b}.mkv": [f"{b}.ja.ass"],
    }
