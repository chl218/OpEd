"""Main window: folder open, episode list, banner, autosave."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import shutil
import time
from pathlib import Path
from typing import Callable

import platformdirs

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QApplication,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..discovery import Season, discover
from ..export import EpisodeResult, existing_outputs, export_episode, export_season, output_folder
from ..fingerprint import default_cache_dir
from ..media import Tools, ToolsMissing, find_tools, probe, run
from ..project import (EpisodeState, Project, Reference, confidence_level, load_project,
                       project_path, save_project)
from ..scan import make_feature_source, reference_candidates, scan
from .cut_panel import CutPanel, format_time
from .episode_list import EpisodeList
from .player import Player
from .workers import Worker

AUTOSAVE_MS = 500
TOOL_CHECK_TIMEOUT = 5.0
NO_CANDIDATES_MESSAGE = ("No repeating opening/ending found — mark one by hand: select an "
                         "episode, Ctrl+N, then Mark as reference.")
OVERWRITE_LIST_MAX = 10
# Shortcuts that only navigate or play; everything else edits cuts and is blocked while busy.
_SAFE_WHILE_BUSY = {"Space", "Tab", "Up", "Down", "PageUp", "PageDown"}
FALLBACK_CHOICES = (
    ("reencode", "Exact re-encode (slow)"),
    ("keyframe", "Fast keyframe cut (may keep ~2 s)"),
    ("skip", "Skip"),
)


# ---------------------------------------------------------------- dialogs
# Module-level so tests can monkeypatch them on ``main_window``.

@dataclasses.dataclass
class CandidateRow:
    ref: Reference
    support: int
    check: QCheckBox
    label: QLineEdit
    play: QPushButton
    default_label: str

    def text(self) -> str:
        return (f"{self.ref.kind}  {self.ref.episode}  "
                f"{format_time(self.ref.start)}–{format_time(self.ref.end)}  "
                f"found in {self.support} episodes")


class CandidatesDialog(QDialog):
    """Lists reference candidates; the checked ones become confirmed references."""

    def __init__(self, parent, candidates: list[tuple[Reference, int]],
                 play: Callable[[Reference], None]):
        super().__init__(parent)
        self.setWindowTitle("Confirm opening/ending")
        col = QVBoxLayout(self)
        col.addWidget(QLabel("These pieces repeat across episodes. Tick the ones to cut "
                             "everywhere; Play previews one."))
        grid = QGridLayout()
        for c, h in enumerate(("Use", "Kind", "Episode", "Span", "Support", "Label", "")):
            grid.addWidget(QLabel(f"<b>{h}</b>"), 0, c)
        counts: dict[str, int] = {}
        self.rows: list[CandidateRow] = []
        for i, (ref, support) in enumerate(candidates, start=1):
            counts[ref.kind] = counts.get(ref.kind, 0) + 1
            default = f"{ref.kind} {counts[ref.kind]}"
            check = QCheckBox()
            check.setChecked(True)
            label = QLineEdit(default)
            play_button = QPushButton("Play")
            play_button.clicked.connect(lambda _checked=False, r=ref: play(r))
            row = CandidateRow(ref, support, check, label, play_button, default)
            self.rows.append(row)
            for c, wdg in enumerate((check, QLabel(ref.kind), QLabel(ref.episode),
                                     QLabel(f"{format_time(ref.start)}–{format_time(ref.end)}"),
                                     QLabel(f"{support} episodes"), label, play_button)):
                grid.addWidget(wdg, i, c)
        col.addLayout(grid)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Scan")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        col.addWidget(buttons)

    def chosen(self) -> list[Reference]:
        """Copies of the checked candidates, confirmed and labelled (inputs untouched)."""
        return [dataclasses.replace(r.ref, label=r.label.text().strip() or r.default_label,
                                    confirmed=True)
                for r in self.rows if r.check.isChecked()]


def confirm_candidates(parent, candidates: list[tuple[Reference, int]],
                       play: Callable[[Reference], None]) -> list[Reference] | None:
    """The confirmed references, or None if the user cancelled."""
    dlg = CandidatesDialog(parent, candidates, play)
    return dlg.chosen() if dlg.exec() == QDialog.Accepted else None


class EpisodePickDialog(QDialog):
    def __init__(self, parent, names: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Export episodes")
        col = QVBoxLayout(self)
        col.addWidget(QLabel("Episodes to export:"))
        self.list = QListWidget()
        for n in names:
            item = QListWidgetItem(n)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.list.addItem(item)
        col.addWidget(self.list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Export")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        col.addWidget(buttons)

    def selected(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


def confirm_export(parent, names: list[str]) -> list[str] | None:
    """The episodes to export, or None if the user cancelled."""
    dlg = EpisodePickDialog(parent, names)
    return dlg.selected() if dlg.exec() == QDialog.Accepted else None


def _cuts_word(n: int) -> str:
    return f"{n} cut" if n == 1 else f"{n} cuts"


def confirm_unreviewed(parent, n: int) -> bool:
    return QMessageBox.question(
        parent, "Cuts not reviewed",
        f"{_cuts_word(n)} not reviewed. Only approved cuts are applied, so these will not "
        "be cut. Export anyway?",
    ) == QMessageBox.Yes


def confirm_overwrite(parent, names: list[str]) -> bool:
    shown = names[:OVERWRITE_LIST_MAX]
    more = len(names) - len(shown)
    listing = "\n".join(shown) + (f"\n… and {more} more" if more else "")
    return QMessageBox.question(
        parent, "Overwrite exported files?",
        f"These files already exist in the output folder and will be replaced:\n\n{listing}",
    ) == QMessageBox.Yes


def ask_fallback(parent, file: str, msg: str) -> str:
    """'reencode', 'keyframe' or 'skip' for an episode whose smart cut failed."""
    box = QMessageBox(parent)
    box.setWindowTitle("Smart cut failed")
    box.setText(f"Smart cut failed for {file}:\n{msg}\n\nHow should this episode be cut?")
    buttons = {box.addButton(text, QMessageBox.AcceptRole): key for key, text in FALLBACK_CHOICES}
    skip = next(b for b, k in buttons.items() if k == "skip")
    box.setEscapeButton(skip)
    box.exec()
    return buttons.get(box.clickedButton(), "skip")


def open_url(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def summarize(results: list[EpisodeResult], skipped: set[str]) -> str:
    trimmed = [r for r in results if r.status == "trimmed"]
    copied = [r for r in results if r.status == "copied"]
    failed = [r for r in results if r.status not in ("trimmed", "copied") and r.file not in skipped]
    skip = [r.file for r in results if r.file in skipped]
    lines = [f"Export finished: {len(trimmed)} trimmed, {len(copied)} copied unchanged, "
             f"{len(failed)} failed, {len(skip)} skipped."]
    if failed:
        lines += ["", "Failed:"] + [f"  {r.file}: {r.message or r.status}" for r in failed]
    if skip:
        lines += ["", "Skipped:"] + [f"  {f}" for f in skip]
    return "\n".join(lines)


def _tool_works(path: str) -> bool:
    """True if `path` exists and answers `-version`."""
    try:
        if not Path(path).is_file():
            return False
        run([path, "-version"], timeout=TOOL_CHECK_TIMEOUT)
        return True
    except Exception:
        return False


class MainWindow(QMainWindow):
    episode_selected = Signal(str)
    project_changed = Signal()

    def __init__(self, player=None):
        super().__init__()
        self.setWindowTitle("OpEd")
        self.tools: Tools | None = None
        self.season: Season | None = None
        self.project: Project | None = None
        self._tools_error = ""
        self._tools_refusal = ""
        self._notices: list[str] = []
        self._episode_notice = ""
        self._task_notice = ""
        self._busy = ""  # non-empty while a background scan/export owns the project
        self._failure_prefix = ""
        self._worker: Worker | None = None
        self._feats = None
        self._export_ctx: dict = {}
        self.summary_dialog: QMessageBox | None = None
        self.open_output_button: QPushButton | None = None

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(AUTOSAVE_MS)
        self._save_timer.timeout.connect(self._autosave)

        # Banner area
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.choose_ffmpeg_button = QPushButton("Choose ffmpeg…")
        self.choose_ffmpeg_button.clicked.connect(self.choose_ffmpeg)
        self.banner_frame = QWidget()
        row = QHBoxLayout(self.banner_frame)
        row.setContentsMargins(6, 4, 6, 4)
        row.addWidget(self.banner, 1)
        row.addWidget(self.choose_ffmpeg_button)
        self.banner_frame.setStyleSheet("background: #fff3cd; color: #533f03;")
        self.banner_frame.hide()

        # Body
        self.episode_list = EpisodeList()
        self.episode_list.currentItemChanged.connect(self._on_current_changed)
        self.episode_selected.connect(self._on_episode_selected)
        self.player = player if player is not None else Player()
        if hasattr(self.player, "error_occurred"):
            self.player.error_occurred.connect(self._on_player_error)
        self.cut_panel = CutPanel(player=self.player)
        self.cut_panel.changed.connect(self._on_cuts_changed)
        right = QWidget()
        right_col = QVBoxLayout(right)
        right_col.setContentsMargins(0, 0, 0, 0)
        if isinstance(self.player, QWidget):
            right_col.addWidget(self.player, 2)
        right_col.addWidget(self.cut_panel, 3)
        split = QSplitter()
        split.addWidget(self.episode_list)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

        central = QWidget()
        col = QVBoxLayout(central)
        col.setContentsMargins(0, 0, 0, 0)
        col.addWidget(self.banner_frame)
        col.addWidget(split, 1)
        self.setCentralWidget(central)

        # Toolbar
        bar = QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(bar)
        self.action_open = QAction("Open folder", self)
        self.action_scan = QAction("Scan", self)
        self.action_approve = QAction("Approve all high", self)
        self.action_export = QAction("Export", self)
        self.action_open.triggered.connect(self._choose_folder)
        self.action_scan.triggered.connect(self.start_scan)
        self.action_approve.triggered.connect(self.approve_all_high)
        self.action_export.triggered.connect(self.start_export)
        for a in (self.action_open, self.action_scan, self.action_approve, self.action_export):
            bar.addAction(a)

        self._shortcuts: dict[str, object] = {}
        self._install_shortcuts()

        self._try_find_tools()
        self._update_actions()
        self._update_banner()

    # ---- tools ----
    def _try_find_tools(self, ffmpeg: str | None = None, ffprobe: str | None = None) -> None:
        """Use the given (or stored) paths if they work, else search PATH."""
        if ffmpeg is None and ffprobe is None:
            s = QSettings("oped", "oped")
            ffmpeg = s.value("ffmpeg_path", None) or None
            ffprobe = s.value("ffprobe_path", None) or None
        if ffmpeg or ffprobe:
            if not (ffmpeg and ffprobe and _tool_works(ffmpeg) and _tool_works(ffprobe)):
                ffmpeg = ffprobe = None
        try:
            self.tools = find_tools(ffmpeg, ffprobe)
            self._tools_error = ""
        except ToolsMissing as e:
            self.tools = None
            self._tools_error = str(e)
        except Exception as e:  # unexpected failure while locating tools
            self.tools = None
            self._tools_error = f"Could not use ffmpeg: {e}"
        self.player.tools = self.tools

    def set_tool_paths(self, ffmpeg: str, ffprobe: str) -> None:
        if not (_tool_works(ffmpeg) and _tool_works(ffprobe)):
            self._try_find_tools(None, None)
            self._tools_refusal = (
                "The selected files were not accepted (they must exist and run "
                f"'-version' within {TOOL_CHECK_TIMEOUT:g} s): {ffmpeg}, {ffprobe}."
            )
            self._update_actions()
            self._update_banner()
            self._reload_current_episode()
            return
        self._tools_refusal = ""
        s = QSettings("oped", "oped")
        s.setValue("ffmpeg_path", ffmpeg)
        s.setValue("ffprobe_path", ffprobe)
        s.sync()
        self._try_find_tools(ffmpeg, ffprobe)
        self._update_actions()
        self._update_banner()
        self._reload_current_episode()

    def _reload_current_episode(self) -> None:
        """Re-probe the selected episode (e.g. once ffmpeg has become available)."""
        cur = self.episode_list.currentItem()
        if cur is not None and self.tools is not None:
            self._on_episode_selected(cur.data(Qt.UserRole))

    def choose_ffmpeg(self) -> None:
        ff, _ = QFileDialog.getOpenFileName(self, "Locate ffmpeg")
        if not ff:
            return
        fp, _ = QFileDialog.getOpenFileName(self, "Locate ffprobe", str(Path(ff).parent))
        if not fp:
            return
        self.set_tool_paths(ff, fp)

    # ---- banner / actions ----
    def _update_banner(self) -> None:
        lines: list[str] = []
        if self._tools_error:
            lines.append(self._tools_error)
        if self._tools_refusal:
            lines.append(self._tools_refusal)
        lines.extend(self._notices)
        if self._episode_notice:
            lines.append(self._episode_notice)
        if self._task_notice:
            lines.append(self._task_notice)
        if self.season is not None:
            lines.extend(self.season.warnings)
        self.banner.setText("\n".join(lines))
        self.choose_ffmpeg_button.setVisible(bool(self._tools_error or self._tools_refusal))
        self.banner_frame.setVisible(bool(lines))

    def _update_actions(self) -> None:
        has_work = (
            self.tools is not None and self.project is not None and bool(self.project.episodes)
        )
        has_work = has_work and not self._busy
        self.action_scan.setEnabled(has_work)
        self.action_export.setEnabled(has_work)
        self.action_approve.setEnabled(has_work)
        self.action_open.setEnabled(not self._busy)

    # ---- folder / project ----
    def _choose_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Open season folder")
        if d:
            self.open_folder(Path(d))

    def open_folder(self, path: Path) -> None:
        if self._busy:
            return
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._autosave()
        self._notices = []
        self._episode_notice = ""
        self._task_notice = ""
        self.cut_panel.clear()
        path = Path(path)
        try:
            self.season = discover(path)
        except OSError as e:
            self.season = None
            self.project = None
            self.episode_list.clear()
            self._notices.append(f"Could not read folder {path}: {e}")
            self._update_actions()
            self._update_banner()
            return
        if not self.season.episodes:
            self._notices.append(f"No video files found in {path}.")
        try:
            self.project = load_project(self.season)
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as e:
            self.project = self._fresh_project()
            self._notices.append(
                f"The saved project file was unreadable ({type(e).__name__}); "
                f"starting a fresh project. {self._backup_corrupt_project()}"
            )
        self.episode_list.set_sidecars(
            {ep.video.name: [s.name for s in ep.sidecars] for ep in self.season.episodes}
        )
        self.episode_list.refresh(self.project)
        self.setWindowTitle(f"OpEd - {path.name}")
        self._update_actions()
        self._update_banner()
        self.project_changed.emit()

    def _fresh_project(self) -> Project:
        assert self.season is not None
        project = Project(Path(self.season.folder), [], {})
        for ep in self.season.episodes:
            project.episodes[ep.video.name] = EpisodeState(file=ep.video.name)
        return project

    def _backup_corrupt_project(self) -> str:
        """Copy the unreadable project file outside the season folder; report the real outcome."""
        assert self.season is not None
        try:
            src = project_path(self.season.folder)
            digest = hashlib.sha1(str(Path(self.season.folder).resolve()).encode("utf-8")).hexdigest()
            dest_dir = Path(platformdirs.user_data_dir("oped")) / "backups"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{digest}-{time.strftime('%Y%m%d-%H%M%S')}.json"
            shutil.copy2(src, dest)
            return f"The old file was backed up to {dest}."
        except OSError as e:
            return f"Backing up the old file failed ({e}); it will be overwritten on the next save."

    def mark_dirty(self) -> None:
        self._save_timer.start()

    def _autosave(self) -> None:
        if self.project is None:
            return
        try:
            save_project(self.project)
        except OSError as e:
            self._notices = [n for n in self._notices if not n.startswith("Could not save")]
            self._notices.append(f"Could not save project: {e}")
            self._update_banner()

    def closeEvent(self, event) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._autosave()
        super().closeEvent(event)

    def _on_current_changed(self, cur, _prev) -> None:
        if cur is not None:
            self.episode_selected.emit(cur.data(Qt.UserRole))
        else:
            self._set_episode_notice("")
            self.cut_panel.clear()

    def _set_episode_notice(self, text: str) -> None:
        if text != self._episode_notice:
            self._episode_notice = text
            self._update_banner()

    def _on_episode_selected(self, name: str) -> None:
        if self.project is None or self.season is None or name not in self.project.episodes:
            self.cut_panel.clear()
            return
        path = Path(self.season.folder) / name
        self.cut_panel.clear()  # never keep the previous episode's state
        try:
            self.player.load(path)
        except Exception as e:
            self._set_episode_notice(f"Could not load {name} into the player: {e}")
            return
        if self.tools is None:
            self._set_episode_notice(f"Cannot review {name}: ffmpeg/ffprobe not available.")
            return
        try:
            info = probe(self.tools, path)
        except Exception as e:
            self._set_episode_notice(f"Could not read {name}: {e}")
            return
        self._set_episode_notice("")
        self.cut_panel.set_episode(self.project, name, info.duration, info.fps)

    def _on_cuts_changed(self) -> None:
        self.mark_dirty()
        if self.project is not None:
            keep = self.episode_list.currentRow()
            self.episode_list.blockSignals(True)
            try:
                self.episode_list.refresh(self.project)
                if keep >= 0:
                    self.episode_list.setCurrentRow(keep)
            finally:
                self.episode_list.blockSignals(False)

    def _on_player_error(self, msg: str) -> None:
        self._set_episode_notice(msg)

    # ---- shortcuts ----
    def _install_shortcuts(self) -> None:
        cp = self.cut_panel
        table = {
            "Space": cp.play_edge,
            "A": cp.approve,
            "X": cp.reject,
            "Tab": cp.toggle_edge,
            "Left": lambda: cp.nudge(-0.1),
            "Right": lambda: cp.nudge(0.1),
            "Shift+Left": lambda: cp.nudge(-1.0),
            "Shift+Right": lambda: cp.nudge(1.0),
            "Ctrl+Left": lambda: cp.nudge(-cp.frame_step()),
            "Ctrl+Right": lambda: cp.nudge(cp.frame_step()),
            "Up": lambda: cp.select_relative(-1),
            "Down": lambda: cp.select_relative(1),
            "PageUp": lambda: self._step_episode(-1),
            "PageDown": lambda: self._step_episode(1),
            "I": cp.set_in,
            "O": cp.set_out,
            "Ctrl+N": cp.add_cut,
        }
        self._shortcuts = table
        for key in table:
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(lambda key=key: self.trigger_shortcut(key))

    def trigger_shortcut(self, key: str) -> bool:
        """Run a shortcut action unless a text field has focus. Returns whether it ran."""
        if self._busy and key not in _SAFE_WHILE_BUSY:
            return False
        fw = QApplication.focusWidget()
        if isinstance(fw, QLineEdit) and not fw.isReadOnly():
            return False
        fn = self._shortcuts.get(key)
        if fn is None:
            return False
        fn()
        return True

    def _step_episode(self, step: int) -> None:
        n = self.episode_list.count()
        if n == 0:
            return
        row = self.episode_list.currentRow()
        self.episode_list.setCurrentRow(min(n - 1, max(0, row + step if row >= 0 else 0)))

    # ---- background work ----
    # Threading model: a worker never sees the live Project. Scan and export run on a
    # deep copy taken on the GUI thread; scan results are copied back on the GUI thread
    # when the worker finishes. While a worker runs the window is "busy": Open/Scan/
    # Approve/Export are disabled, the cut panel is disabled and editing shortcuts are
    # ignored, so the live project cannot change under the copy and the copy-back loses
    # no edits. Navigation (episode list, playback) keeps working.
    def _ready(self) -> bool:
        return (not self._busy and self.tools is not None and self.season is not None
                and self.project is not None)

    def _set_busy(self, text: str) -> None:
        self._busy = text
        self.cut_panel.setEnabled(not text)
        self._update_actions()
        if text:
            self.statusBar().showMessage(text + "…")

    def _set_task_notice(self, text: str) -> None:
        self._task_notice = text
        self._update_banner()

    def _start_worker(self, fn, on_finished, failure_prefix: str) -> None:
        worker = Worker(fn)
        worker.signals.progress.connect(self._on_worker_progress)
        worker.signals.finished.connect(on_finished)
        worker.signals.failed.connect(self._on_worker_failed)
        self._failure_prefix = failure_prefix
        self._worker = worker  # keep it alive until its signals have been delivered
        worker.start()

    def _on_worker_progress(self, name: str, frac: float) -> None:
        if self._busy:
            self.statusBar().showMessage(f"{self._busy} {name} — {round(frac * 100)}%")

    def _on_worker_failed(self, msg: str) -> None:
        self._set_busy("")
        self.statusBar().showMessage(f"{self._failure_prefix}.", 10000)
        self._set_task_notice(f"{self._failure_prefix}: {msg}")

    def _refresh_views(self) -> None:
        """Re-read the project into the episode list and the cut panel."""
        if self.project is None:
            return
        self.episode_list.refresh(self.project)
        cp = self.cut_panel
        if cp.project is self.project and cp.file in self.project.episodes:
            keep = cp.selected
            cp.set_episode(self.project, cp.file, cp.duration, cp.fps)
            if keep >= 0:
                cp.select(keep)

    # ---- scan ----
    def start_scan(self) -> None:
        if not self._ready():
            return
        self._set_task_notice("")
        feats = make_feature_source(self.tools, self.season, default_cache_dir())
        self._feats = feats
        if any(r.confirmed for r in self.project.references):
            self._run_scan()
            return
        names = [ep.video.name for ep in self.season.episodes if ep.video.name in self.project.episodes]
        view = Project(self.project.folder, [], {n: self.project.episodes[n] for n in names})

        def find(progress):
            done = [0]

            def named(name: str):
                try:
                    f = feats(name)
                except Exception as e:  # noqa: BLE001 - name the episode that broke the search
                    reason = e.args[0] if isinstance(e, KeyError) and e.args else e
                    raise RuntimeError(f"could not read {name}: {reason}") from e
                done[0] += 1
                progress(name, done[0] / len(names))
                return f

            return reference_candidates(view, named)

        self._set_busy("Looking for the opening/ending:")
        self._start_worker(find, self._on_candidates, "Looking for the opening/ending failed")

    def _on_candidates(self, candidates) -> None:
        if not candidates:
            self._set_busy("")
            self.statusBar().clearMessage()
            self._set_task_notice(NO_CANDIDATES_MESSAGE)
            return
        self._played_candidate = False
        chosen = confirm_candidates(self, list(candidates), self._play_candidate)
        if self._played_candidate:
            self._restore_player()
        if not chosen:
            self._set_busy("")
            self.statusBar().showMessage(
                "Scan cancelled." if chosen is None else "No reference ticked; scan not run.", 10000)
            return
        self.project.references.extend(chosen)
        self.mark_dirty()
        self._run_scan()

    def _play_candidate(self, ref: Reference) -> None:
        self._played_candidate = True
        try:
            self.player.load(Path(self.season.folder) / ref.episode)
            self.player.play_spans([(ref.start, ref.end)])
        except Exception as e:  # noqa: BLE001
            self._set_episode_notice(f"Could not play {ref.episode}: {e}")

    def _restore_player(self) -> None:
        """Put the selected episode back into the player after a candidate preview."""
        cur = self.episode_list.currentItem()
        if cur is None or self.season is None:
            return
        name = cur.data(Qt.UserRole)
        try:
            self.player.load(Path(self.season.folder) / name)
        except Exception as e:  # noqa: BLE001
            self._set_episode_notice(f"Could not load {name} into the player: {e}")

    def _run_scan(self) -> None:
        snapshot = copy.deepcopy(self.project)
        feats = self._feats

        def work(progress):
            scan(snapshot, feats, progress)
            return snapshot

        self._set_busy("Scanning")
        self._start_worker(work, self._on_scanned, "Scan failed")

    def _on_scanned(self, scanned: Project) -> None:
        for name, st in scanned.episodes.items():
            live = self.project.episodes.get(name)
            if live is not None:
                live.cuts, live.flags, live.scanned = st.cuts, st.flags, st.scanned
        self._set_busy("")
        self._refresh_views()
        self.mark_dirty()
        flagged = sum(1 for st in scanned.episodes.values() if st.flags)
        self.statusBar().showMessage(
            f"Scan finished: {len(scanned.episodes)} episodes, {flagged} flagged.", 10000)

    # ---- approve ----
    def approve_all_high(self) -> None:
        if self.project is None or self._busy:
            return
        n = 0
        for st in self.project.episodes.values():
            for c in st.cuts:
                if c.status == "proposed" and confidence_level(c.confidence) in ("high", "manual"):
                    c.status = "approved"
                    n += 1
        if n:
            self.mark_dirty()
            self._refresh_views()
        self.statusBar().showMessage(f"Approved {_cuts_word(n)}.", 10000)

    # ---- export ----
    def start_export(self) -> None:
        if not self._ready():
            return
        names = [ep.video.name for ep in self.season.episodes if ep.video.name in self.project.episodes]
        files = confirm_export(self, names)
        if not files:
            return
        unreviewed = sum(1 for f in files for c in self.project.episodes[f].cuts
                         if c.status == "proposed")
        if unreviewed and not confirm_unreviewed(self, unreviewed):
            return
        out_dir = output_folder(self.season.folder)
        existing = existing_outputs(self.season, files)
        if existing and not confirm_overwrite(self, [str(p.relative_to(out_dir)) for p in existing]):
            return
        self._set_task_notice("")
        snapshot = copy.deepcopy(self.project)
        tools, season = self.tools, self.season
        self._export_ctx = {"tools": tools, "season": season, "project": snapshot,
                            "out_dir": out_dir, "results": [], "skipped": set()}

        def work(progress):
            return export_season(tools, snapshot, season, list(files), progress=progress)

        self._set_busy("Exporting")
        self._start_worker(work, self._on_exported, "Export failed")

    def _on_exported(self, results) -> None:
        ctx = self._export_ctx
        ctx["results"] = list(results)
        reruns: dict[str, str] = {}
        for r in ctx["results"]:
            if r.status == "needs_fallback":
                choice = ask_fallback(self, r.file, r.message)
                if choice in ("reencode", "keyframe"):
                    reruns[r.file] = choice
                else:
                    ctx["skipped"].add(r.file)
        if not reruns:
            self._finish_export()
            return
        tools, season, snapshot, out_dir = ctx["tools"], ctx["season"], ctx["project"], ctx["out_dir"]
        by_name = {ep.video.name: ep for ep in season.episodes}

        def work(progress):
            out: dict[str, EpisodeResult] = {}
            for f, mode in reruns.items():
                ep = by_name.get(f)
                if ep is None:
                    out[f] = EpisodeResult(f, "failed", mode, "episode not found in the season folder")
                    continue
                out[f] = export_episode(tools, ep, season.folder, snapshot.approved_intervals(f),
                                        out_dir, mode=mode,
                                        progress=lambda x, f=f: progress(f, x))
            return out

        self._set_busy("Re-exporting")
        self._start_worker(work, self._on_reexported, "Export failed")

    def _on_reexported(self, out) -> None:
        ctx = self._export_ctx
        ctx["results"] = [out.get(r.file, r) for r in ctx["results"]]
        self._finish_export()

    def _finish_export(self) -> None:
        ctx = self._export_ctx
        results, skipped = ctx["results"], ctx["skipped"]
        self._set_busy("")
        text = summarize(results, skipped)
        self.statusBar().showMessage(text.splitlines()[0], 15000)
        failed = [r for r in results if r.status not in ("trimmed", "copied") and r.file not in skipped]
        if failed:
            self._set_task_notice(
                f"Export: {len(failed)} episode{'s' if len(failed) != 1 else ''} failed (see the summary).")
        box = QMessageBox(self)
        box.setWindowTitle("Export finished")
        box.setText(text)
        out_dir = ctx["out_dir"]
        button = box.addButton("Open output folder", QMessageBox.ActionRole)
        button.clicked.connect(lambda: open_url(out_dir))
        box.addButton(QMessageBox.Close)
        box.setModal(False)
        self.summary_dialog = box
        self.open_output_button = button
        box.show()

