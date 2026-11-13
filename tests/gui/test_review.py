from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from conftest import FakePlayer
from oped.gui import cut_panel, main_window
from oped.gui.cut_panel import CutPanel, edge_plan, parse_time
from oped.gui.main_window import MainWindow
from oped.media import MediaInfo
from oped.project import Cut, EpisodeState, Project


def project_with(names):
    return Project(Path("."), [], {n: EpisodeState(file=n) for n in names})


def info(p, duration=100.0, fps=None):
    return MediaInfo(path=p, duration=duration, container="mkv", streams=(), chapters=(), fps=fps)


def test_edge_plan_clamps():
    assert edge_plan(Cut("c", 2, 60), 62) == [(0, 2), (60, 62)]
    assert edge_plan(Cut("c", 0, 60), 100) == [(60, 65)]


def test_parse_time():
    assert parse_time("2:34.12") == pytest.approx(154.12)
    assert parse_time("1:02:03.5") == pytest.approx(3723.5)
    for bad in ("2:7x", "", "abc", "1:2:3:4", "-1:00", "1:75.0"):
        with pytest.raises(ValueError):
            parse_time(bad)


def make_panel(qtbot, cuts, pos=0.0, duration=1400, fps=23.976):
    p = project_with(["E1.mkv"])
    p.episodes["E1.mkv"].cuts.extend(cuts)
    fp = FakePlayer(position=pos)
    panel = CutPanel(player=fp)
    qtbot.addWidget(panel)
    panel.set_episode(p, "E1.mkv", duration, fps)
    return p, fp, panel


def test_nudge_approve_and_mark_reference(qtbot, monkeypatch):
    p, fp, panel = make_panel(qtbot, [Cut("c", 154.0, 225.0)], pos=150.0)
    panel.select(0)
    panel.nudge(-1.0)
    panel.toggle_edge()
    panel.nudge(0.1)
    c = p.episodes["E1.mkv"].cuts[0]
    assert (c.start, c.end) == (pytest.approx(153.0), pytest.approx(225.1))
    panel.toggle_edge()
    panel.set_in()
    assert c.start == 150.0
    panel.approve()
    assert c.status == "approved"
    monkeypatch.setattr(cut_panel, "ask_kind", lambda parent, default: ("OP", "OP 1"))
    panel.mark_reference()
    assert p.references[0].kind == "OP" and c.reference_id == p.references[0].id
    assert p.references[0].episode == "E1.mkv" and p.references[0].confirmed


def test_nudge_cannot_invert_or_leave_file(qtbot):
    p, _, panel = make_panel(qtbot, [Cut("c", 0.05, 0.2)], duration=100, fps=None)
    panel.select(0)
    panel.nudge(-1.0)
    c = p.episodes["E1.mkv"].cuts[0]
    assert c.start == 0.0
    panel.nudge(5.0)
    assert c.start < c.end
    panel.toggle_edge()
    panel.nudge(500.0)
    assert c.end == 100


def test_changed_emitted_and_add_cut_and_delete(qtbot):
    p, fp, panel = make_panel(qtbot, [Cut("a", 10, 20), Cut("b", 30, 40)], pos=95.0, duration=100)
    cuts = p.episodes["E1.mkv"].cuts
    with qtbot.waitSignal(panel.changed, timeout=1000):
        panel.add_cut()
    new = cuts[-1]
    assert (new.start, new.end) == (95.0, 100) and new.reference_id is None
    assert new.status == "proposed" and new.confidence is None
    assert panel.selected == 2
    with qtbot.waitSignal(panel.changed, timeout=1000):
        panel.delete(2)
    assert len(cuts) == 2 and panel.selected == 1
    panel.delete(0)
    assert panel.selected == 0 and cuts[0].id == "b"
    panel.delete(0)
    assert panel.selected == -1
    panel.nudge(1.0)  # no cuts: no crash
    panel.approve()
    panel.play_edge()


def test_reject_and_row_text(qtbot):
    p, fp, panel = make_panel(qtbot, [Cut("a", 154.0, 225.0, confidence=0.9)])
    panel.select(0)
    panel.reject()
    assert p.episodes["E1.mkv"].cuts[0].status == "rejected"
    text = panel.row_texts()[0]
    assert "02:34.000" in text and "03:45.000" in text and "high" in text


def test_play_edge_uses_edge_plan(qtbot):
    p, fp, panel = make_panel(qtbot, [Cut("a", 2, 60)], duration=62)
    panel.select(0)
    panel.play_edge()
    assert fp.spans == [(0, 2), (60, 62)]


def test_time_edit_rejects_bad_input(qtbot):
    p, fp, panel = make_panel(qtbot, [Cut("a", 154.0, 225.0)])
    assert panel.edit_time(0, "start", "2:30.5")
    assert p.episodes["E1.mkv"].cuts[0].start == pytest.approx(150.5)
    assert not panel.edit_time(0, "start", "garbage")
    assert "red" in panel.time_edit_widget(0, "start").styleSheet()
    assert not panel.edit_time(0, "start", "9:00.0")  # past end
    assert not panel.edit_time(0, "end", "99:00.0")  # past duration
    assert p.episodes["E1.mkv"].cuts[0].start == pytest.approx(150.5)
    assert "red" in panel.time_edit_widget(0, "end").styleSheet()


def test_set_episode_resets_selection(qtbot):
    p, fp, panel = make_panel(qtbot, [Cut("a", 1, 2), Cut("b", 3, 4)])
    panel.select(1)
    panel.toggle_edge()
    p.episodes["E2.mkv"] = EpisodeState(file="E2.mkv")
    panel.set_episode(p, "E2.mkv", 100, 24.0)
    assert panel.selected == -1 and panel.edge == "start"
    panel.select(0)  # out of range ignored
    assert panel.selected == -1


def _window(qtbot, tmp_path, names=("E1.mkv", "E2.mkv")):
    for n in names:
        (tmp_path / n).write_bytes(b"")
    fp = FakePlayer()
    w = MainWindow(player=fp)
    qtbot.addWidget(w)
    w.open_folder(tmp_path)
    return w, fp


def test_window_selection_loads_player_and_probes(qtbot, tmp_path, monkeypatch):
    w, fp = _window(qtbot, tmp_path)
    w.project.episodes["E1.mkv"].cuts.append(Cut("a", 1, 5))
    calls = []

    def fake_probe(tools, path):
        calls.append(path)
        return info(path, 90.0, 25.0)

    monkeypatch.setattr(main_window, "probe", fake_probe)
    w.tools = w.tools or object()
    w.episode_list.setCurrentRow(0)
    assert fp.loaded == tmp_path / "E1.mkv" and calls
    assert w.cut_panel.duration == 90.0 and w.cut_panel.selected == 0
    w.cut_panel.approve()
    qtbot.waitUntil(lambda: (tmp_path / "oped-project.json").exists(), timeout=2000)


def test_window_probe_failure_shows_message(qtbot, tmp_path, monkeypatch):
    w, fp = _window(qtbot, tmp_path)
    w.project.episodes["E1.mkv"].cuts.append(Cut("a", 1, 5))

    def boom(t, p):
        raise RuntimeError("bad file")

    monkeypatch.setattr(main_window, "probe", boom)
    w.tools = w.tools or object()
    w.episode_list.setCurrentRow(0)
    assert "bad file" in w.banner.text()
    assert w.cut_panel.selected == -1
    w.episode_list.setCurrentRow(1)  # still no crash


def test_shortcuts_drive_panel(qtbot, tmp_path, monkeypatch):
    w, fp = _window(qtbot, tmp_path)
    w.project.episodes["E1.mkv"].cuts.extend([Cut("a", 10, 20), Cut("b", 30, 40)])
    monkeypatch.setattr(main_window, "probe", lambda t, p: info(p))
    w.tools = w.tools or object()
    w.episode_list.setCurrentRow(0)
    panel = w.cut_panel
    cuts = w.project.episodes["E1.mkv"].cuts
    w.trigger_shortcut("Right")
    assert cuts[0].start == pytest.approx(10.1)
    w.trigger_shortcut("Shift+Left")
    assert cuts[0].start == pytest.approx(9.1)
    w.trigger_shortcut("Ctrl+Right")
    assert cuts[0].start == pytest.approx(9.1 + 1 / 24)
    w.trigger_shortcut("Tab")
    assert panel.edge == "end"
    w.trigger_shortcut("Down")
    assert panel.selected == 1
    w.trigger_shortcut("A")
    assert cuts[1].status == "approved"
    w.trigger_shortcut("Up")
    w.trigger_shortcut("X")
    assert cuts[0].status == "rejected"
    w.trigger_shortcut("Space")
    assert fp.spans
    w.trigger_shortcut("PageDown")
    assert w.episode_list.currentRow() == 1


def test_shortcuts_do_not_fire_while_typing(qtbot, tmp_path, monkeypatch):
    w, fp = _window(qtbot, tmp_path)
    w.project.episodes["E1.mkv"].cuts.append(Cut("a", 10, 20))
    monkeypatch.setattr(main_window, "probe", lambda t, p: info(p))
    w.tools = w.tools or object()
    w.show()
    qtbot.waitExposed(w)
    w.episode_list.setCurrentRow(0)
    edit = w.cut_panel.time_edit_widget(0, "start")
    edit.begin_edit()
    edit.setFocus()
    qtbot.waitUntil(lambda: edit.hasFocus(), timeout=2000)
    QTest.keyClick(edit, Qt.Key_A)
    assert w.project.episodes["E1.mkv"].cuts[0].status == "proposed"
    assert w.trigger_shortcut("A") is False
    assert w.project.episodes["E1.mkv"].cuts[0].status == "proposed"
