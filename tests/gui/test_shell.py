import json

from PySide6.QtCore import Qt

from conftest import raise_
from oped.gui import main_window
from oped.gui.main_window import MainWindow
from oped.media import ToolsMissing
from oped.project import Cut, EpisodeState, Project


def test_missing_ffmpeg_shows_banner_not_crash(qtbot, monkeypatch):
    monkeypatch.setattr(main_window, "find_tools", raise_(ToolsMissing(["ffmpeg"])))
    w = MainWindow()
    qtbot.addWidget(w)
    assert "install ffmpeg" in w.banner.text()
    assert not w.action_scan.isEnabled()
    assert not w.action_export.isEnabled()


def test_open_folder_lists_and_autosaves(qtbot, tmp_path):
    for n in ("E2.mkv", "E10.mkv", "E1.mkv"):
        (tmp_path / n).write_bytes(b"")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_folder(tmp_path)
    assert [w.episode_list.item(i).data(Qt.UserRole) for i in range(3)] == [
        "E1.mkv",
        "E2.mkv",
        "E10.mkv",
    ]
    w.project.episodes["E1.mkv"].flags.append("No OP 1 found")
    w.mark_dirty()
    qtbot.waitUntil(lambda: (tmp_path / "oped-project.json").exists(), timeout=2000)
    w.episode_list.refresh(w.project)
    assert "No OP 1 found" in w.episode_list.item(0).text()


def test_empty_folder_and_corrupt_project_do_not_crash(qtbot, tmp_path):
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_folder(tmp_path)
    assert w.episode_list.count() == 0
    assert "No video files" in w.banner.text()
    (tmp_path / "E1.mkv").write_bytes(b"")
    (tmp_path / "oped-project.json").write_text("{not json", encoding="utf-8")
    w.open_folder(tmp_path)
    assert w.episode_list.count() == 1
    assert "project" in w.banner.text().lower()


def test_badges_and_sidecar_tooltip(qtbot, tmp_path):
    for n in ("E1.mkv", "E2.mkv", "E3.mkv"):
        (tmp_path / n).write_bytes(b"")
    (tmp_path / "E1.ass").write_text("", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_folder(tmp_path)
    from oped.project import Reference

    p = w.project
    p.references = [Reference("r1", "OP", "OP", "E1.mkv", 0, 90), Reference("r2", "ED", "ED", "E1.mkv", 1300, 1390)]
    p.episodes["E1.mkv"].cuts = [Cut("a", 0, 90, "approved", "r1", 0.9), Cut("b", 1300, 1390, "approved", "r2", 0.9)]
    p.episodes["E1.mkv"].scanned = True
    p.episodes["E2.mkv"].cuts = [Cut("c", 0, 90, "proposed", "r1", 0.7)]
    p.episodes["E3.mkv"].scanned = True
    w.episode_list.refresh(p)
    assert "✓ OP" in w.episode_list.item(0).text() and "✓ ED" in w.episode_list.item(0).text()
    assert "E1.ass" in w.episode_list.item(0).toolTip()
    assert "●" in w.episode_list.item(1).text()
    assert "copied unchanged" in w.episode_list.item(2).text()


def test_choose_ffmpeg_retries_and_enables(qtbot, monkeypatch, tmp_path):
    calls = []

    def fake(ffmpeg=None, ffprobe=None):
        calls.append((ffmpeg, ffprobe))
        if ffmpeg is None:
            raise ToolsMissing(["ffmpeg"])
        return "TOOLS"

    monkeypatch.setattr(main_window, "find_tools", fake)
    monkeypatch.setattr(main_window, "_tool_works", lambda p: True)
    w = MainWindow()
    qtbot.addWidget(w)
    assert not w.action_scan.isEnabled()
    w.set_tool_paths("a/ffmpeg", "a/ffprobe")
    assert w.tools == "TOOLS"
    assert w.action_scan.isEnabled() is False  # no folder yet
    (tmp_path / "E1.mkv").write_bytes(b"")
    w.open_folder(tmp_path)
    assert w.action_scan.isEnabled()
    w2 = MainWindow()
    qtbot.addWidget(w2)
    assert w2.tools == "TOOLS"  # stored paths reused


import pytest


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"episodes": 5}'])
def test_corrupt_project_backed_up_outside_season_folder(qtbot, tmp_path, monkeypatch, content):
    data = tmp_path / "data"
    monkeypatch.setattr(main_window.platformdirs, "user_data_dir", lambda *a, **k: str(data))
    season = tmp_path / "season"
    season.mkdir()
    (season / "E1.mkv").write_bytes(b"")
    (season / "oped-project.json").write_text(content, encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_folder(season)
    assert sorted(p.name for p in season.iterdir()) == ["E1.mkv", "oped-project.json"]
    backups = list((data / "backups").glob("*.json"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == content
    assert str(backups[0]) in w.banner.text()
    assert w.episode_list.count() == 1


def test_corrupt_project_backup_failure_is_reported(qtbot, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")  # a file where a directory is needed
    monkeypatch.setattr(main_window.platformdirs, "user_data_dir", lambda *a, **k: str(blocker))
    season = tmp_path / "season"
    season.mkdir()
    (season / "E1.mkv").write_bytes(b"")
    (season / "oped-project.json").write_text("{bad", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_folder(season)
    assert "failed" in w.banner.text() and "backed up to" not in w.banner.text()
    assert sorted(p.name for p in season.iterdir()) == ["E1.mkv", "oped-project.json"]


def test_pending_edit_flushed_on_folder_switch(qtbot, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "E1.mkv").write_bytes(b"")
    (b / "E1.mkv").write_bytes(b"")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_folder(a)
    w.project.episodes["E1.mkv"].flags.append("pending")
    w.mark_dirty()
    w.open_folder(b)
    saved = json.loads((a / "oped-project.json").read_text(encoding="utf-8"))
    assert saved["episodes"]["E1.mkv"]["flags"] == ["pending"]


def test_invalid_tool_paths_not_saved_and_fall_back_to_path(qtbot, monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings

    seen = []

    def fake(ffmpeg=None, ffprobe=None):
        seen.append((ffmpeg, ffprobe))
        return "PATH_TOOLS"

    monkeypatch.setattr(main_window, "find_tools", fake)
    bogus = tmp_path / "nothing"
    w = MainWindow()
    qtbot.addWidget(w)
    w.set_tool_paths(str(bogus), str(bogus))
    assert QSettings("oped", "oped").value("ffmpeg_path") is None
    assert w.tools == "PATH_TOOLS"
    s = QSettings("oped", "oped")
    s.setValue("ffmpeg_path", str(bogus))
    s.setValue("ffprobe_path", str(bogus))
    seen.clear()
    w2 = MainWindow()
    qtbot.addWidget(w2)
    assert seen == [(None, None)]


def _slow_tool(tmp_path):
    import sys

    if sys.platform == "win32":
        f = tmp_path / "slow.bat"
        f.write_text("@echo off\r\nping -n 4 127.0.0.1 >nul\r\n")
    else:
        f = tmp_path / "slow.sh"
        f.write_text("#!/bin/sh\nsleep 3\n")
        f.chmod(0o755)
    return f


def test_hanging_tool_is_invalid_not_saved_and_refusal_shown(qtbot, monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings

    monkeypatch.setattr(main_window, "TOOL_CHECK_TIMEOUT", 0.5)
    monkeypatch.setattr(main_window, "find_tools", lambda ffmpeg=None, ffprobe=None: "PATH_TOOLS")
    slow = _slow_tool(tmp_path)
    assert main_window._tool_works(str(slow)) is False
    w = MainWindow()
    qtbot.addWidget(w)
    w.set_tool_paths(str(slow), str(slow))
    assert QSettings("oped", "oped").value("ffmpeg_path") is None
    assert w.tools == "PATH_TOOLS"
    assert "not accepted" in w.banner.text()


def test_unreadable_project_file_is_not_treated_as_corrupt(qtbot, tmp_path, monkeypatch):
    """S3: a locked/offline project file must not be replaced by a fresh project that
    autosaves over it."""
    data = tmp_path / "data"
    monkeypatch.setattr(main_window.platformdirs, "user_data_dir", lambda *a, **k: str(data))
    season = tmp_path / "season"
    season.mkdir()
    (season / "E1.mkv").write_bytes(b"")
    good = '{"version": 1, "references": [], "episodes": {}}'
    (season / "oped-project.json").write_text(good, encoding="utf-8")
    monkeypatch.setattr(main_window, "load_project", raise_(PermissionError(13, "file is locked")))
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_folder(season)
    assert w.project is None
    assert "file is locked" in w.banner.text() and "not saved" in w.banner.text()
    assert not w.action_scan.isEnabled() and not w.action_export.isEnabled()
    w.mark_dirty()
    w._autosave()
    assert (season / "oped-project.json").read_text(encoding="utf-8") == good
    assert not data.exists()  # no backup, no fallback save


def test_banner_says_where_the_project_is_saved_when_folder_is_readonly(qtbot, tmp_path, monkeypatch):
    elsewhere = tmp_path / "data" / "projects" / "abc.json"
    monkeypatch.setattr(main_window, "save_project", lambda project: elsewhere)
    (tmp_path / "E1.mkv").write_bytes(b"")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_folder(tmp_path)
    w._autosave()
    w._autosave()
    assert w.banner.text().count(str(elsewhere)) == 1
