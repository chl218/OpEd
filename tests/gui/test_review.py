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


# ---- Player (with an injected fake media player) ----
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402

from oped.gui import player as player_mod  # noqa: E402
from oped.gui.player import Player  # noqa: E402
from oped.media import Tools  # noqa: E402

S = QMediaPlayer.MediaStatus


class FakeMP(QObject):
    positionChanged = Signal("qlonglong")
    errorOccurred = Signal(object, str)
    mediaStatusChanged = Signal(object)

    def __init__(self):
        super().__init__()
        self.positions, self.sources, self.played, self.paused = [], [], 0, 0
        self._err = QMediaPlayer.Error.NoError
        self._status = S.NoMedia

    def setPosition(self, ms):
        self.positions.append(ms)

    def setSource(self, url):
        if self.sources and self.sources[-1] == url.toLocalFile():
            return  # like Qt: same source, no status emitted
        self._status = S.NoMedia
        self.sources.append(url.toLocalFile())

    def play(self):
        self.played += 1

    def pause(self):
        self.paused += 1

    def stop(self):
        pass

    def position(self):
        return 0

    def error(self):
        return self._err

    def mediaStatus(self):
        return self._status

    def emit_status(self, status):
        self._status = status
        self.mediaStatusChanged.emit(status)


def make_player(qtbot, loaded=True):
    pl = Player()
    qtbot.addWidget(pl)
    mp = FakeMP()
    pl._attach(mp)
    pl.load(Path("ep.mkv"))
    if loaded:
        mp.emit_status(S.LoadedMedia)
    return pl, mp


def test_player_sequences_spans_and_pauses_after_last(qtbot):
    pl, mp = make_player(qtbot)
    pl.play_spans([(0.0, 2.0), (60.0, 65.0)])
    assert mp.positions == [0]
    mp.positionChanged.emit(500)
    assert mp.positions == [0]
    mp.positionChanged.emit(2000)
    assert mp.positions == [0, 60000]
    mp.positionChanged.emit(2100)  # stale, pre-seek position: ignored
    assert mp.positions == [0, 60000] and mp.paused == 0
    mp.positionChanged.emit(60100)
    mp.positionChanged.emit(65000)
    assert mp.paused == 1 and pl._spans == []
    mp.positionChanged.emit(70000)  # later seeks do not re-trigger sequencing
    assert mp.paused == 1


def test_player_ignores_stale_position_from_previous_run(qtbot):
    pl, mp = make_player(qtbot)
    pl.play_spans([(149.0, 154.0), (225.0, 230.0)])
    mp.positionChanged.emit(226000)  # left over from the earlier "after" span
    assert mp.positions == [149000]
    mp.positionChanged.emit(149500)
    mp.positionChanged.emit(154000)
    assert mp.positions == [149000, 225000]


def test_player_waits_for_media_to_load(qtbot):
    pl, mp = make_player(qtbot, loaded=False)
    pl.play_spans([(10.0, 15.0)])
    assert mp.positions == [] and mp.played == 0
    mp.mediaStatusChanged.emit(S.LoadedMedia)
    assert mp.positions == [10000] and mp.played == 1


def test_player_end_of_media_clears_spans(qtbot):
    pl, mp = make_player(qtbot)
    pl.play_spans([(95.0, 100.0)])
    mp.mediaStatusChanged.emit(S.EndOfMedia)
    assert pl._spans == []
    mp.positionChanged.emit(96000)
    assert mp.positions == [95000]


def fake_run(cmds):
    def _run(cmd, **kw):
        cmds.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"x")

    return _run


def test_player_error_before_play_then_fallback_generates_previews(qtbot, monkeypatch):
    cmds = []
    monkeypatch.setattr(player_mod, "run", fake_run(cmds))
    pl, mp = make_player(qtbot)
    pl.tools = Tools("ffmpeg", "ffprobe")
    errors = []
    pl.error_occurred.connect(errors.append)
    mp._err = QMediaPlayer.Error.ResourceError
    mp.errorOccurred.emit(QMediaPlayer.Error.ResourceError, "cannot decode")  # no spans yet
    assert errors and not cmds
    pl.play_spans([(5.0, 8.0), (60.0, 80.0)])  # goes straight to previews
    assert len(cmds) == 2
    for c in cmds:
        for flag in ("-sn", "-dn", "-pix_fmt", "yuv420p", "libx264", "veryfast", "aac", "0:v:0", "0:a:0?"):
            assert flag in c
    assert cmds[0][cmds[0].index("-t") + 1] == "3.000"
    assert cmds[1][cmds[1].index("-t") + 1] == "10.000"
    tmp = pl._tmp
    assert tmp.exists() and mp.sources[-1].endswith("clip0.mp4")
    mp.mediaStatusChanged.emit(S.EndOfMedia)
    assert mp.sources[-1].endswith("clip1.mp4")
    mp.mediaStatusChanged.emit(S.EndOfMedia)
    assert mp.paused == 1 and not tmp.exists() and not pl._in_preview


def test_player_error_during_play_falls_back(qtbot, monkeypatch):
    cmds = []
    monkeypatch.setattr(player_mod, "run", fake_run(cmds))
    pl, mp = make_player(qtbot)
    pl.tools = Tools("ffmpeg", "ffprobe")
    pl.play_spans([(5.0, 8.0)])
    mp.errorOccurred.emit(QMediaPlayer.Error.FormatError, "bad")
    assert len(cmds) == 1 and pl._in_preview


def test_player_interrupted_preview_leaves_no_stale_state(qtbot, monkeypatch):
    cmds = []
    monkeypatch.setattr(player_mod, "run", fake_run(cmds))
    pl, mp = make_player(qtbot)
    pl.tools = Tools("ffmpeg", "ffprobe")
    pl._source_failed = True
    pl.play_spans([(5.0, 8.0), (20.0, 25.0)])
    tmp = pl._tmp
    pl.load(Path("other.mkv"))  # interrupts the preview
    assert not tmp.exists() and pl._clips == [] and not pl._in_preview
    n = len(mp.sources)
    mp.mediaStatusChanged.emit(S.EndOfMedia)  # old source's end must be harmless
    assert len(mp.sources) == n and pl._clip_i == 0
    pl._source_failed = True
    pl.play_spans([(1.0, 2.0)])
    pl.play_spans([(3.0, 4.0)])  # second fallback replaces the first
    mp.mediaStatusChanged.emit(S.EndOfMedia)
    assert not pl._in_preview


def test_player_missing_backend_reports_error(qtbot, monkeypatch):
    def boom(self):
        raise RuntimeError("no backend")

    monkeypatch.setattr(Player, "_create_backend", boom)
    pl = Player()
    qtbot.addWidget(pl)
    errors = []
    pl.error_occurred.connect(errors.append)
    pl.load(Path("ep.mkv"))
    pl.play_spans([(0, 1)])
    pl.seek(3)
    assert errors and "no backend" in errors[0]
    assert pl.position() == 3.0


def test_window_passes_tools_to_player(qtbot, tmp_path):
    w, fp = _window(qtbot, tmp_path)
    assert fp.tools is w.tools


# ---- real key events and focus behaviour ----
def _ready_window(qtbot, tmp_path, monkeypatch, cuts):
    w, fp = _window(qtbot, tmp_path)
    w.project.episodes["E1.mkv"].cuts.extend(cuts)
    monkeypatch.setattr(main_window, "probe", lambda t, p: info(p))
    w.tools = w.tools or object()
    w.show()
    qtbot.waitExposed(w)
    w.episode_list.setCurrentRow(0)
    return w, fp


def test_real_key_events_trigger_shortcuts(qtbot, tmp_path, monkeypatch):
    w, fp = _ready_window(qtbot, tmp_path, monkeypatch, [Cut("a", 10, 20)])
    cuts = w.project.episodes["E1.mkv"].cuts
    w.activateWindow()
    qtbot.waitUntil(w.isActiveWindow, timeout=3000)
    QTest.keyClick(w, Qt.Key_Tab)
    assert w.cut_panel.edge == "end"
    QTest.keyClick(w, Qt.Key_Right)
    assert cuts[0].end == pytest.approx(20.1)
    QTest.keyClick(w, Qt.Key_Space)
    assert fp.spans
    QTest.keyClick(w, Qt.Key_A)
    assert cuts[0].status == "approved"
    QTest.keyClick(w, Qt.Key_X)
    assert cuts[0].status == "rejected"


def test_clicking_a_time_selects_row_and_keeps_shortcuts(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QLineEdit

    w, fp = _ready_window(qtbot, tmp_path, monkeypatch, [Cut("a", 10, 20), Cut("b", 30, 40)])
    cuts = w.project.episodes["E1.mkv"].cuts
    assert w.cut_panel.selected == 0
    edit = w.cut_panel.time_edit_widget(1, "start")
    QTest.mouseClick(edit, Qt.LeftButton)
    assert w.cut_panel.selected == 1
    fw = QApplication.focusWidget()
    assert not (isinstance(fw, QLineEdit) and not fw.isReadOnly())
    assert w.trigger_shortcut("X") is True
    assert cuts[1].status == "rejected" and cuts[0].status == "proposed"


def test_mark_reference_twice_updates_not_duplicates(qtbot, monkeypatch):
    p, fp, panel = make_panel(qtbot, [Cut("c", 154.0, 225.0)])
    panel.select(0)
    answers = iter([("OP", "OP 1"), ("ED", "Renamed")])
    monkeypatch.setattr(cut_panel, "ask_kind", lambda parent, default: next(answers))
    panel.mark_reference()
    panel.nudge(1.0)
    panel.mark_reference()
    assert len(p.references) == 1
    r = p.references[0]
    assert (r.kind, r.label) == ("ED", "Renamed")
    c = p.episodes["E1.mkv"].cuts[0]
    assert c.reference_id == r.id and r.start == c.start


def test_player_reload_same_file_does_not_hang(qtbot):
    pl, mp = make_player(qtbot)
    pl.load(Path("ep.mkv"))  # same source: Qt emits nothing
    pl.play_spans([(10.0, 15.0)])
    assert mp.positions == [10000] and not pl._pending


def test_player_position_after_preview_is_source_time(qtbot, monkeypatch):
    cmds = []
    monkeypatch.setattr(player_mod, "run", fake_run(cmds))
    pl, mp = make_player(qtbot)
    pl.tools = Tools("ffmpeg", "ffprobe")
    pl._source_failed = True
    pl.play_spans([(5.0, 8.0), (225.0, 228.0)])
    mp.mediaStatusChanged.emit(S.EndOfMedia)
    mp.mediaStatusChanged.emit(S.EndOfMedia)
    assert not pl._in_preview
    assert pl.position() == 228.0
    n = len(mp.positions)
    pl.seek(100.0)
    assert pl.position() == 100.0 and len(mp.positions) == n
