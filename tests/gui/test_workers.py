import functools
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool

from conftest import FakePlayer, raise_
from fixtures import episode, episode_with
from oped.export import EpisodeResult, output_folder
from oped.fingerprint import features_from_pcm
from oped.gui import main_window
from oped.gui.main_window import CandidatesDialog, EpisodePickDialog, MainWindow, summarize
from oped.gui.workers import Worker
from oped.media import MediaInfo, ToolsMissing
from oped.project import Cut, Reference


# ---------------------------------------------------------------- plan acceptance tests

def test_approve_all_high_only_touches_high_and_manual(qtbot, window_with_project):
    w, p = window_with_project
    cuts = [Cut("a", 1, 2, confidence=0.95), Cut("b", 3, 4, confidence=0.7), Cut("c", 5, 6, confidence=None),
            Cut("d", 7, 8, "rejected", confidence=0.99)]
    p.episodes["E1.mkv"].cuts = cuts
    w.approve_all_high()
    assert [c.status for c in cuts] == ["approved", "proposed", "approved", "rejected"]


def test_fallback_prompt_reruns_with_chosen_mode(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    calls = []
    monkeypatch.setattr(main_window, "export_season", lambda *a, **k: [EpisodeResult("E1.mkv", "needs_fallback", message="x")])
    monkeypatch.setattr(main_window, "export_episode", lambda *a, mode, **k: calls.append(mode) or EpisodeResult("E1.mkv", "trimmed", mode))
    monkeypatch.setattr(main_window, "ask_fallback", lambda parent, file, msg: "reencode")
    monkeypatch.setattr(main_window, "confirm_export", lambda *a: ["E1.mkv"])
    w.start_export(); qtbot.waitUntil(lambda: calls == ["reencode"], timeout=5000)


# ---------------------------------------------------------------- Worker

def test_worker_reports_progress_and_result(qtbot):
    seen = []
    worker = Worker(lambda progress: (progress("E1.mkv", 0.5), 42)[1])
    worker.signals.progress.connect(lambda n, x: seen.append((n, x)))
    with qtbot.waitSignal(worker.signals.finished, timeout=5000) as blocker:
        QThreadPool.globalInstance().start(worker)
    assert blocker.args == [42]
    assert seen == [("E1.mkv", 0.5)]


def test_worker_failure_emits_failed_message(qtbot):
    worker = Worker(raise_(ValueError("boom")))
    with qtbot.waitSignal(worker.signals.failed, timeout=5000) as blocker:
        QThreadPool.globalInstance().start(worker)
    assert blocker.args == ["boom"]


# ---------------------------------------------------------------- scan

@functools.lru_cache(maxsize=None)
def ed_feats(op_at, ed_at, seed):
    return features_from_pcm(episode_with(op_at, ed_at, seed, total=400))


@functools.lru_cache(maxsize=None)
def noise_feats(seed):
    return features_from_pcm(episode(None, total=200, seed=seed))


REPEATING = {"E1.mkv": (30, 320, 1), "E2.mkv": (50, 300, 2), "E3.mkv": (40, 330, 3)}


def fake_source(monkeypatch, fn):
    monkeypatch.setattr(main_window, "make_feature_source", lambda tools, season, cache: fn)


def idle(w):
    return w.action_scan.isEnabled() and w.action_export.isEnabled()


def status_log(w):
    log = []
    w.statusBar().messageChanged.connect(log.append)
    return log


def test_scan_end_to_end_confirms_references_and_proposes_cuts(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    fake_source(monkeypatch, lambda name: ed_feats(*REPEATING[name]))
    offered = []

    def confirm(parent, candidates, play):
        offered.extend(candidates)
        op = next(ref for ref, _support in candidates if ref.kind == "OP")
        play(op)
        from dataclasses import replace
        return [replace(op, label="Opening", confirmed=True)]

    monkeypatch.setattr(main_window, "confirm_candidates", confirm)
    log = status_log(w)
    w.start_scan()
    assert not w.action_scan.isEnabled() and not w.action_export.isEnabled()
    assert not w.action_open.isEnabled()
    qtbot.waitUntil(lambda: idle(w), timeout=20000)
    assert {ref.kind for ref, _ in offered} == {"OP", "ED"}
    assert all(support >= 2 for _, support in offered)
    (ref,) = p.references  # the unchecked ED candidate is not kept
    assert ref.confirmed and ref.label == "Opening" and ref.kind == "OP"
    assert w.player.spans == [(ref.start, ref.end)]
    assert all(st.scanned for st in p.episodes.values())
    for name, st in p.episodes.items():
        assert len(st.cuts) == 1 and st.cuts[0].status == "proposed" and st.cuts[0].reference_id == ref.id
    assert any("Scanning" in m and "%" in m for m in log)
    assert w.cut_panel.isEnabled()
    qtbot.waitUntil(lambda: (Path(p.folder) / "oped-project.json").exists(), timeout=3000)


def test_scan_with_confirmed_reference_skips_proposal(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    fake_source(monkeypatch, lambda name: ed_feats(*REPEATING[name]))
    monkeypatch.setattr(main_window, "confirm_candidates", raise_(AssertionError("asked")))
    p.references.append(Reference("r", "OP", "OP 1", "E1.mkv", 30, 90))
    w.start_scan()
    qtbot.waitUntil(lambda: idle(w), timeout=20000)
    assert all(st.scanned for st in p.episodes.values())
    assert "asked" not in w.banner.text()


def test_no_candidates_shows_message(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    fake_source(monkeypatch, lambda name: noise_feats(int(name[1])))
    monkeypatch.setattr(main_window, "confirm_candidates", raise_(AssertionError("asked")))
    w.start_scan()
    qtbot.waitUntil(lambda: idle(w), timeout=20000)
    assert ("No repeating opening/ending found — mark one by hand: select an episode, "
            "Ctrl+N, then Mark as reference.") in w.banner.text()
    assert p.references == [] and not any(st.scanned for st in p.episodes.values())


@pytest.mark.parametrize("answer", [[], None])
def test_unchecked_or_cancelled_candidates_add_nothing(qtbot, window_with_project, monkeypatch, answer):
    w, p = window_with_project
    fake_source(monkeypatch, lambda name: ed_feats(*REPEATING[name]))
    monkeypatch.setattr(main_window, "confirm_candidates", lambda parent, c, play: answer)
    w.start_scan()
    qtbot.waitUntil(lambda: idle(w), timeout=20000)
    assert p.references == []
    assert not any(st.scanned for st in p.episodes.values())


def test_unreadable_episode_during_reference_search_is_reported(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project

    def feats(name):
        if name == "E2.mkv":
            raise RuntimeError("moov atom not found")
        return ed_feats(*REPEATING[name])

    fake_source(monkeypatch, feats)
    w.start_scan()
    qtbot.waitUntil(lambda: idle(w), timeout=20000)
    text = w.banner.text()
    assert "E2.mkv" in text and "moov atom not found" in text
    assert p.references == []


def test_ui_locked_while_scan_runs(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    gate = threading.Event()

    def feats(name):
        gate.wait(10)
        return ed_feats(*REPEATING[name])

    fake_source(monkeypatch, feats)
    p.references.append(Reference("r", "OP", "OP 1", "E1.mkv", 30, 90))
    p.episodes["E2.mkv"].cuts.append(Cut("m", 1, 2, confidence=None))
    w.start_scan()
    try:
        assert not w.cut_panel.isEnabled()
        assert w.trigger_shortcut("A") is False
        w.approve_all_high()  # ignored while the scan owns the project
        assert p.episodes["E2.mkv"].cuts[0].status == "proposed"
        w.start_export()  # ignored: no dialog, no crash
    finally:
        gate.set()
    qtbot.waitUntil(lambda: idle(w), timeout=20000)
    assert w.cut_panel.isEnabled() and w.action_open.isEnabled()


def test_candidates_dialog_defaults_play_and_choice(qtbot):
    played = []
    cands = [(Reference("a", "OP", "OP?", "E1.mkv", 30, 90, confirmed=False), 3),
             (Reference("b", "ED", "ED?", "E2.mkv", 300, 390, confirmed=False), 2),
             (Reference("c", "OP", "OP?", "E3.mkv", 10, 70, confirmed=False), 2)]
    dlg = CandidatesDialog(None, cands, played.append)
    qtbot.addWidget(dlg)
    assert [row.label.text() for row in dlg.rows] == ["OP 1", "ED 1", "OP 2"]
    assert all(row.check.isChecked() for row in dlg.rows)
    assert "E2.mkv" in dlg.rows[1].text() and "05:00.000" in dlg.rows[1].text() and "2" in dlg.rows[1].text()
    qtbot.mouseClick(dlg.rows[1].play, Qt.LeftButton)
    assert played == [cands[1][0]]
    dlg.rows[2].check.setChecked(False)
    dlg.rows[0].label.setText("Opening")
    chosen = dlg.chosen()
    assert [(r.id, r.label, r.confirmed) for r in chosen] == [("a", "Opening", True), ("b", "ED 1", True)]
    assert cands[0][0].confirmed is False  # the offered objects are not mutated


# ---------------------------------------------------------------- export

def test_episode_pick_dialog_all_checked(qtbot):
    dlg = EpisodePickDialog(None, ["E1.mkv", "E2.mkv"])
    qtbot.addWidget(dlg)
    assert dlg.selected() == ["E1.mkv", "E2.mkv"]
    dlg.list.item(0).setCheckState(Qt.Unchecked)
    assert dlg.selected() == ["E2.mkv"]


def test_unreviewed_warning_and_only_approved_cuts_exported(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    p.episodes["E1.mkv"].cuts = [Cut("a", 0, 10, "approved"), Cut("b", 20, 30, "proposed", confidence=0.9)]
    p.episodes["E2.mkv"].cuts = [Cut("c", 0, 10, "proposed", confidence=0.7)]
    p.episodes["E3.mkv"].cuts = [Cut("d", 0, 10, "proposed")]  # not selected
    monkeypatch.setattr(main_window, "confirm_export", lambda parent, names: ["E1.mkv", "E2.mkv"])
    asked, exported = [], []
    monkeypatch.setattr(main_window, "confirm_unreviewed", lambda parent, n: asked.append(n) or False)
    monkeypatch.setattr(main_window, "export_season",
                        lambda tools, proj, season, files, **k: exported.append(
                            {f: proj.approved_intervals(f) for f in files}) or [])
    w.start_export()
    assert asked == [2] and exported == [] and idle(w)
    monkeypatch.setattr(main_window, "confirm_unreviewed", lambda parent, n: True)
    w.start_export()
    qtbot.waitUntil(lambda: idle(w) and bool(exported), timeout=5000)
    assert exported == [{"E1.mkv": [(0, 10)], "E2.mkv": []}]


def test_overwrite_cancel_exports_nothing(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    out = output_folder(p.folder)
    out.mkdir()
    for i in range(1, 4):
        (out / f"E{i}.mkv").write_bytes(b"old")
    monkeypatch.setattr(main_window, "confirm_export", lambda parent, names: list(names))
    asked = []
    monkeypatch.setattr(main_window, "confirm_overwrite", lambda parent, names: asked.append(names) or False)
    monkeypatch.setattr(main_window, "export_season", raise_(AssertionError("exported")))
    w.start_export()
    assert asked == [["E1.mkv", "E2.mkv", "E3.mkv"]]
    assert idle(w) and (out / "E1.mkv").read_bytes() == b"old"


def test_cancelled_episode_selection_exports_nothing(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    monkeypatch.setattr(main_window, "confirm_export", lambda parent, names: None)
    monkeypatch.setattr(main_window, "export_season", raise_(AssertionError("exported")))
    w.start_export()
    assert idle(w)


def test_skip_choice_does_not_rerun_and_summary_lists_all(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    monkeypatch.setattr(main_window, "confirm_export", lambda parent, names: list(names))
    monkeypatch.setattr(main_window, "export_season", lambda *a, **k: [
        EpisodeResult("E1.mkv", "trimmed", "smart"),
        EpisodeResult("E2.mkv", "needs_fallback", "smart", "no keyframe"),
        EpisodeResult("E3.mkv", "failed", "smart", "disk on fire")])
    asked = []
    monkeypatch.setattr(main_window, "ask_fallback", lambda parent, f, msg: asked.append((f, msg)) or "skip")
    monkeypatch.setattr(main_window, "export_episode", raise_(AssertionError("rerun")))
    opened = []
    monkeypatch.setattr(main_window, "open_url", opened.append)
    w.start_export()
    qtbot.waitUntil(lambda: w.summary_dialog is not None, timeout=5000)
    assert asked == [("E2.mkv", "no keyframe")]
    text = w.summary_dialog.text()
    assert "1 trimmed" in text and "1 failed" in text and "1 skipped" in text
    assert "E3.mkv: disk on fire" in text and "E2.mkv" in text
    qtbot.mouseClick(w.open_output_button, Qt.LeftButton)
    assert opened == [output_folder(p.folder)]
    assert idle(w)


def test_fallback_rerun_uses_same_inputs(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    p.episodes["E2.mkv"].cuts = [Cut("a", 5, 15, "approved")]
    monkeypatch.setattr(main_window, "confirm_export", lambda parent, names: ["E2.mkv"])
    monkeypatch.setattr(main_window, "export_season",
                        lambda *a, **k: [EpisodeResult("E2.mkv", "needs_fallback", "smart", "x")])
    monkeypatch.setattr(main_window, "ask_fallback", lambda parent, f, msg: "keyframe")
    seen = []

    def rerun(tools, ep, season_folder, cuts, out_dir, mode, progress=None):
        seen.append((tools, ep.video.name, Path(season_folder), cuts, Path(out_dir), mode))
        return EpisodeResult(ep.video.name, "trimmed", mode)

    monkeypatch.setattr(main_window, "export_episode", rerun)
    w.start_export()
    qtbot.waitUntil(lambda: w.summary_dialog is not None, timeout=5000)
    assert seen == [("TOOLS", "E2.mkv", Path(p.folder), [(5, 15)], output_folder(p.folder), "keyframe")]
    assert "1 trimmed" in w.summary_dialog.text()


def test_export_worker_failure_goes_to_banner(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    monkeypatch.setattr(main_window, "confirm_export", lambda parent, names: list(names))
    monkeypatch.setattr(main_window, "export_season", raise_(OSError("disk full")))
    w.start_export()
    qtbot.waitUntil(lambda: idle(w), timeout=5000)
    assert "disk full" in w.banner.text()
    assert w.summary_dialog is None


def test_summarize_counts():
    text = summarize([EpisodeResult("A", "trimmed"), EpisodeResult("B", "copied"),
                      EpisodeResult("C", "failed", message="bad"),
                      EpisodeResult("D", "needs_fallback", message="still"),
                      EpisodeResult("E", "needs_fallback", message="smart failed")], {"E"})
    assert "1 trimmed, 1 copied unchanged, 2 failed, 1 skipped" in text
    assert "C: bad" in text and "D: still" in text
    assert "smart failed" not in text and text.rstrip().endswith("E")


# ---------------------------------------------------------------- closing while busy

def test_worker_cancel_stops_at_next_progress(qtbot):
    cancel = threading.Event()
    reached = []

    def fn(progress):
        progress("a", 0.5)
        reached.append("a")
        cancel.set()
        progress("b", 1.0)
        reached.append("b")

    worker = Worker(fn, cancel)
    with qtbot.waitSignal(worker.signals.failed, timeout=5000) as blocker:
        QThreadPool.globalInstance().start(worker)
    assert blocker.args == ["cancelled"] and reached == ["a"]


def test_close_while_busy_wait_keeps_window_and_scan_finishes(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    gate = threading.Event()

    def feats(name):
        gate.wait(10)
        return ed_feats(*REPEATING[name])

    fake_source(monkeypatch, feats)
    asked = []
    monkeypatch.setattr(main_window, "confirm_quit_while_busy",
                        lambda parent, task: asked.append(task) or False)
    p.references.append(Reference("r", "OP", "OP 1", "E1.mkv", 30, 90))
    w.show()
    w.start_scan()
    try:
        assert w.close() is False
        assert asked == ["Scanning"] and w.isVisible()
    finally:
        gate.set()
    qtbot.waitUntil(lambda: idle(w), timeout=20000)
    assert all(st.scanned for st in p.episodes.values())


def test_quit_anyway_during_export_cancels_and_shows_nothing(qtbot, window_with_project, monkeypatch):
    import oped.export as export_mod

    w, p = window_with_project
    gate = threading.Event()
    started = []

    def fake_episode(tools, ep, season_folder, cuts, out_dir, mode="smart", progress=None):
        progress(0.0)  # like the real export_episode, which reports 0.0 before writing
        started.append(ep.video.name)
        gate.wait(10)
        return EpisodeResult(ep.video.name, "needs_fallback", mode, "x")

    monkeypatch.setattr(export_mod, "export_episode", fake_episode)
    monkeypatch.setattr(main_window, "confirm_export", lambda parent, names: list(names))
    monkeypatch.setattr(main_window, "confirm_quit_while_busy", lambda parent, task: True)
    monkeypatch.setattr(main_window, "ask_fallback", raise_(AssertionError("dialog after close")))
    w.show()
    w.start_export()
    qtbot.waitUntil(lambda: started == ["E1.mkv"], timeout=5000)
    assert w.close() is True and not w.isVisible()
    gate.set()
    assert QThreadPool.globalInstance().waitForDone(10000)
    qtbot.wait(50)  # deliver any late signals
    assert started == ["E1.mkv"]  # cancelled before the next episode
    assert w.summary_dialog is None
    assert "dialog after close" not in w.banner.text()


def test_close_when_idle_is_not_asked(qtbot, window_with_project, monkeypatch):
    w, p = window_with_project
    monkeypatch.setattr(main_window, "confirm_quit_while_busy", raise_(AssertionError("asked")))
    w.show()
    assert w.close() is True


# ---------------------------------------------------------------- carry-over

def test_current_episode_reprobed_after_tools_found(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(main_window, "find_tools", lambda ffmpeg=None, ffprobe=None:
                        "TOOLS" if ffmpeg else raise_(ToolsMissing(["ffmpeg"]))())
    monkeypatch.setattr(main_window, "_tool_works", lambda p: True)
    monkeypatch.setattr(main_window, "probe", lambda tools, path: MediaInfo(
        path=path, duration=100.0, container="mkv", streams=(), chapters=(), fps=None))
    (tmp_path / "E1.mkv").write_bytes(b"")
    w = MainWindow(player=FakePlayer())
    qtbot.addWidget(w)
    w.open_folder(tmp_path)
    w.episode_list.setCurrentRow(0)
    assert "Cannot review E1.mkv" in w.banner.text()
    w.set_tool_paths("a/ffmpeg", "a/ffprobe")
    assert "Cannot review" not in w.banner.text()
    assert w.cut_panel.file == "E1.mkv"


# ---------------------------------------------------------------- rescan keeps user decisions

def test_rescan_keeps_decisions_edits_and_a_deleted_reference_stays_deleted(qtbot, window_with_project, monkeypatch):
    """Through the real worker deep-copy/copy-back: approved, rejected, hand-added and
    edited cuts survive a re-scan, an edited reference cut moves the reference, and a
    deleted reference is not resurrected (nor approved by Approve all high)."""
    from oped.gui import cut_panel as cut_panel_mod

    w, p = window_with_project
    fake_source(monkeypatch, lambda name: ed_feats(*REPEATING[name]))
    monkeypatch.setattr(main_window, "probe", lambda tools, path: MediaInfo(
        path=path, duration=400.0, container="mkv", streams=(), chapters=(), fps=24.0))
    monkeypatch.setattr(cut_panel_mod, "confirm_delete_reference", lambda *a: True)
    p.references += [Reference("op", "OP", "OP 1", "E1.mkv", 30, 90),
                     Reference("ed", "ED", "ED 1", "E1.mkv", 320, 390)]
    w.start_scan()
    qtbot.waitUntil(lambda: idle(w), timeout=20000)
    for st in p.episodes.values():
        assert [c.reference_id for c in st.cuts] == ["op", "ed"]

    cp, rows = w.cut_panel, {n: i for i, n in enumerate(p.episodes)}

    def open_episode(name):
        w.episode_list.setCurrentRow(rows[name])
        assert cp.file == name

    open_episode("E2.mkv")
    cp.select(0); cp.approve()            # E2 OP approved
    cp.select(1); cp.reject()             # E2 ED rejected
    open_episode("E3.mkv")
    cp.select(0)
    cp.toggle_edge(); cp.nudge(-1.0)      # E3 OP: edited proposal (end - 1 s)
    open_episode("E1.mkv")
    cp.select(0)
    cp.nudge(1.0)                         # edit the OP reference's own cut
    w.player.seek(200.0)
    cp.add_cut()                          # hand-added cut at 200..260
    cp.select([c.reference_id for c in cp._cuts()].index("ed"))
    cp.delete(cp.selected)                # delete the ED reference (E3's ED proposal goes)
    assert [c.reference_id for c in p.episodes["E3.mkv"].cuts] == ["op"]
    before = {n: [(c.id, c.start, c.end, c.status, c.reference_id) for c in st.cuts]
              for n, st in p.episodes.items()}

    w.start_scan()                        # confirmed reference exists: straight to the worker
    assert not w.action_scan.isEnabled()
    qtbot.waitUntil(lambda: idle(w), timeout=20000)

    after = {n: [(c.id, c.start, c.end, c.status, c.reference_id) for c in st.cuts]
             for n, st in p.episodes.items()}
    assert after == before
    assert [r.id for r in p.references] == ["op"]
    assert (p.references[0].start, p.references[0].end) == (31, 90)
    assert [c[4] for c in after["E2.mkv"]] == ["op", None]           # rejected ED kept, unlinked
    assert len(after["E1.mkv"]) == 2 and after["E1.mkv"][1][1:3] == (200.0, 260.0)
    for cuts in after.values():
        assert [c[1] for c in cuts] == sorted(c[1] for c in cuts)
    assert not any("ED 1" in f for st in p.episodes.values() for f in st.flags)

    w.approve_all_high()
    assert all(not (319 < c.start < 321) for st in p.episodes.values() for c in st.cuts
               if c.status == "approved")
