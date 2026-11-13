"""Main window: folder open, episode list, banner, autosave."""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

import platformdirs

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QApplication,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..discovery import Season, discover
from ..media import Tools, ToolsMissing, find_tools, probe, run
from ..project import EpisodeState, Project, load_project, project_path, save_project
from .cut_panel import CutPanel
from .episode_list import EpisodeList
from .player import Player

AUTOSAVE_MS = 500
TOOL_CHECK_TIMEOUT = 5.0


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
        self.action_scan.triggered.connect(self._scan)
        self.action_approve.triggered.connect(self._approve_all_high)
        self.action_export.triggered.connect(self._export)
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

    def set_tool_paths(self, ffmpeg: str, ffprobe: str) -> None:
        if not (_tool_works(ffmpeg) and _tool_works(ffprobe)):
            self._try_find_tools(None, None)
            self._tools_refusal = (
                "The selected files were not accepted (they must exist and run "
                f"'-version' within {TOOL_CHECK_TIMEOUT:g} s): {ffmpeg}, {ffprobe}."
            )
            self._update_actions()
            self._update_banner()
            return
        self._tools_refusal = ""
        s = QSettings("oped", "oped")
        s.setValue("ffmpeg_path", ffmpeg)
        s.setValue("ffprobe_path", ffprobe)
        s.sync()
        self._try_find_tools(ffmpeg, ffprobe)
        self._update_actions()
        self._update_banner()

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
        if self.season is not None:
            lines.extend(self.season.warnings)
        self.banner.setText("\n".join(lines))
        self.choose_ffmpeg_button.setVisible(bool(self._tools_error or self._tools_refusal))
        self.banner_frame.setVisible(bool(lines))

    def _update_actions(self) -> None:
        has_work = (
            self.tools is not None and self.project is not None and bool(self.project.episodes)
        )
        self.action_scan.setEnabled(has_work)
        self.action_export.setEnabled(has_work)
        self.action_approve.setEnabled(has_work)

    # ---- folder / project ----
    def _choose_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Open season folder")
        if d:
            self.open_folder(Path(d))

    def open_folder(self, path: Path) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._autosave()
        self._notices = []
        self._episode_notice = ""
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
        if isinstance(QApplication.focusWidget(), QLineEdit):
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

    # ---- Task 13 stubs ----
    def _scan(self) -> None:
        """TODO(Task 13): run the project scan."""

    def _approve_all_high(self) -> None:
        """TODO(Task 13): approve every high-confidence cut."""

    def _export(self) -> None:
        """TODO(Task 13): export trimmed copies."""
