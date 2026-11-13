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
