"""Review panel: timeline strip, cut rows, edge editing, reference marking."""
from __future__ import annotations

import re

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..project import Cut, Project, Reference, confidence_level, new_id

MIN_GAP = 0.05
ADD_CUT_SECONDS = 60.0
DEFAULT_FPS = 24.0
STATUS_COLORS = {
    "proposed": QColor("#e0a800"),
    "approved": QColor("#2e9e4f"),
    "rejected": QColor("#c0392b"),
}

_TIME_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")


def edge_plan(cut: Cut, duration: float, lead: float = 5.0) -> list[tuple[float, float]]:
    spans = [(max(0, cut.start - lead), cut.start), (cut.end, min(duration, cut.end + lead))]
    return [s for s in spans if s[1] > s[0]]


def parse_time(s: str) -> float:
    m = _TIME_RE.match(s.strip())
    if m is None:
        raise ValueError(f"not a time: {s!r}")
    hours, minutes, seconds = m.group(1), int(m.group(2)), float(m.group(3))
    if seconds >= 60 or (hours is not None and minutes >= 60):
        raise ValueError(f"out of range: {s!r}")
    return (int(hours) * 3600 if hours is not None else 0) + minutes * 60 + seconds


def format_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    if h:
        return f"{h}:{m:02d}:{s:02d}.{ms:03d}"
    return f"{m:02d}:{s:02d}.{ms:03d}"


def ask_kind(parent, default: tuple[str, str]) -> tuple[str, str] | None:
    """Small dialog: OP/ED and a label. Returns (kind, label) or None if cancelled."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("Mark as reference")
    kind = QComboBox()
    kind.addItems(["OP", "ED"])
    kind.setCurrentText(default[0])
    label = QLineEdit(default[1])
    state = {"edited": False}
    label.textEdited.connect(lambda _t: state.update(edited=True))

    def kind_changed(k):
        if not state["edited"]:
            n = default[1].rsplit(" ", 1)[-1]
            label.setText(f"{k} {n}")

    kind.currentTextChanged.connect(kind_changed)
    form = QFormLayout(dlg)
    form.addRow("Kind", kind)
    form.addRow("Label", label)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    form.addRow(buttons)
    if dlg.exec() != QDialog.Accepted:
        return None
    return kind.currentText(), label.text().strip() or default[1]


def confirm_delete_reference(parent, label: str, proposed: int, decided: int) -> bool:
    """Ask before deleting a reference together with its own cut. Module-level so tests
    can monkeypatch it."""
    lines = [f'This cut is the reference "{label}". Deleting it deletes the reference, '
             "so a re-scan no longer looks for it."]
    if proposed:
        lines.append(f"{proposed} unreviewed proposal(s) from it in other episodes will be removed.")
    if decided:
        lines.append(f"{decided} approved, rejected or edited cut(s) from it are kept as "
                     "hand-added cuts.")
    lines.append("Delete it?")
    return QMessageBox.question(parent, "Delete reference?", "\n\n".join(lines)) == QMessageBox.Yes


class TimeEdit(QLineEdit):
    """Read-only time field; double-click to edit. Reports text through `submitted`."""

    submitted = Signal(str)
    clicked = Signal()

    def __init__(self, text: str):
        super().__init__(text)
        self._orig = text
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.NoFocus)  # a read-only field must not take focus (or shortcuts)
        self.setFixedWidth(96)
        self.setFrame(False)
        self.editingFinished.connect(self._finished)

    def mousePressEvent(self, event) -> None:
        if self.isReadOnly():
            self.clicked.emit()
        super().mousePressEvent(event)

    def _end_edit(self) -> None:
        self.setReadOnly(True)
        self.setFrame(False)
        self.setFocusPolicy(Qt.NoFocus)

    def begin_edit(self) -> None:
        self.setFocusPolicy(Qt.StrongFocus)
        self.setReadOnly(False)
        self.setFrame(True)
        self.selectAll()

    def mouseDoubleClickEvent(self, event) -> None:
        self.begin_edit()
        self.setFocus()

    def mark_invalid(self, bad: bool) -> None:
        self.setStyleSheet("border: 2px solid red;" if bad else "")

    def _finished(self) -> None:
        if self.isReadOnly():
            return
        if self.text() == self._orig:
            self._end_edit()
            self.mark_invalid(False)
            return
        self.submitted.emit(self.text())


class Timeline(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(28)
        self.duration = 0.0
        self.cuts: list[Cut] = []
        self.selected = -1

    def set_data(self, duration: float, cuts: list[Cut], selected: int) -> None:
        self.duration, self.cuts, self.selected = duration, cuts, selected
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#8a8a8a"))
        if self.duration <= 0:
            return
        for i, c in enumerate(self.cuts):
            x0 = c.start / self.duration * w
            x1 = c.end / self.duration * w
            r = QRectF(x0, 0, max(2.0, x1 - x0), h)
            p.fillRect(r, STATUS_COLORS.get(c.status, QColor("#e0a800")))
            if i == self.selected:
                p.setPen(QPen(QColor("black"), 2))
                p.drawRect(r.adjusted(1, 1, -1, -1))
        p.end()


class CutPanel(QWidget):
    changed = Signal()

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.project: Project | None = None
        self.file = ""
        self.duration = 0.0
        self.fps: float | None = None
        self.selected = -1
        self.edge = "start"
        self._edits: dict[tuple[int, str], TimeEdit] = {}

        self.add_button = QPushButton("+ Add cut")
        self.ref_button = QPushButton("Mark as reference")
        self.add_button.clicked.connect(self.add_cut)
        self.ref_button.clicked.connect(self.mark_reference)
        self.timeline = Timeline()
        self.message = QLabel("")
        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.rows_host)

        top = QHBoxLayout()
        top.addWidget(self.add_button)
        top.addWidget(self.ref_button)
        top.addStretch(1)
        col = QVBoxLayout(self)
        col.addLayout(top)
        col.addWidget(self.timeline)
        col.addWidget(self.message)
        col.addWidget(scroll, 1)
        self._update_enabled()

    # ---- model access ----
    def _cuts(self) -> list[Cut]:
        if self.project is None or self.file not in self.project.episodes:
            return []
        return self.project.episodes[self.file].cuts

    def _current(self) -> Cut | None:
        cuts = self._cuts()
        return cuts[self.selected] if 0 <= self.selected < len(cuts) else None

    def set_episode(self, project: Project, file: str, duration: float, fps: float | None) -> None:
        self.project, self.file, self.duration, self.fps = project, file, duration, fps
        self.edge = "start"
        self.selected = -1
        self.message.setText("" if self._cuts() else "No cuts in this episode.")
        if self._cuts():
            self.selected = 0
        self._refresh()

    def clear(self, message: str = "") -> None:
        self.project, self.file, self.duration, self.fps = None, "", 0.0, None
        self.selected = -1
        self.edge = "start"
        self.message.setText(message)
        self._refresh()

    def _mutated(self) -> None:
        self._refresh()
        self.changed.emit()

    # ---- selection ----
    def select(self, index: int) -> None:
        if not 0 <= index < len(self._cuts()):
            return
        self.selected = index
        self._refresh()

    def select_relative(self, step: int) -> None:
        n = len(self._cuts())
        if n == 0:
            return
        self.select(min(n - 1, max(0, self.selected + step if self.selected >= 0 else 0)))

    def toggle_edge(self) -> None:
        self.edge = "end" if self.edge == "start" else "start"
        self._refresh()

    # ---- editing ----
    def _own_reference(self, c: Cut) -> Reference | None:
        """The reference whose own span ``c`` is (its cut in the reference episode)."""
        if self.project is None or not c.reference_id:
            return None
        return next((r for r in self.project.references
                     if r.id == c.reference_id and r.episode == self.file), None)

    def _edited(self, c: Cut) -> None:
        """Record a time edit. An edited proposal survives a re-scan (``Cut.edited``), and
        editing a reference's own cut moves the reference with it."""
        c.edited = True
        ref = self._own_reference(c)
        if ref is not None:
            ref.start, ref.end = c.start, c.end

    def _apply_edge(self, value: float) -> bool:
        c = self._current()
        if c is None:
            return False
        if self.edge == "start":
            c.start = min(max(0.0, value), c.end - MIN_GAP)
        else:
            c.end = max(min(self.duration, value), c.start + MIN_GAP)
        self._edited(c)
        return True

    def nudge(self, delta: float) -> None:
        c = self._current()
        if c is None:
            return
        if self._apply_edge((c.start if self.edge == "start" else c.end) + delta):
            self._mutated()

    def frame_step(self) -> float:
        return 1.0 / (self.fps if self.fps else DEFAULT_FPS)

    def set_in(self) -> None:
        self._set_edge_from_player("start")

    def set_out(self) -> None:
        self._set_edge_from_player("end")

    def _set_edge_from_player(self, edge: str) -> None:
        if self._current() is None:
            return
        self.edge = edge
        if self._apply_edge(self.player.position()):
            self._mutated()

    def _set_status(self, status: str) -> None:
        c = self._current()
        if c is not None:
            c.status = status
            self._mutated()

    def approve(self) -> None:
        self._set_status("approved")

    def reject(self) -> None:
        self._set_status("rejected")

    def add_cut(self) -> None:
        if self.project is None or self.file not in self.project.episodes:
            return
        start = min(max(0.0, self.player.position()), max(0.0, self.duration - MIN_GAP))
        end = min(self.duration, start + ADD_CUT_SECONDS)
        if end - start < MIN_GAP:
            return
        cuts = self._cuts()
        cuts.append(Cut(new_id(), start, end, "proposed", None, None))
        self.selected = len(cuts) - 1
        self._mutated()

    def delete(self, index: int) -> None:
        cuts = self._cuts()
        if not 0 <= index < len(cuts):
            return
        ref = self._own_reference(cuts[index])
        if ref is not None and not self._delete_reference(ref, cuts[index]):
            return
        del cuts[index]
        if not cuts:
            self.selected = -1
        elif self.selected == index:
            self.selected = min(index, len(cuts) - 1)
        elif self.selected > index:
            self.selected -= 1
        self._mutated()

    def _delete_reference(self, ref: Reference, own: Cut) -> bool:
        """Delete ``ref`` (after asking): its unreviewed, unedited proposals elsewhere go;
        approved, rejected and edited cuts stay as hand-added cuts. Its scan flags go too.
        Returns False if the user cancelled."""
        assert self.project is not None
        others = [c for st in self.project.episodes.values() for c in st.cuts
                  if c.reference_id == ref.id and c is not own]
        dropped = [c for c in others if c.status == "proposed" and not c.edited]
        if not confirm_delete_reference(self, ref.label, len(dropped), len(others) - len(dropped)):
            return False
        gone = {id(c) for c in dropped}
        stale_flags = {f"No {ref.label} found", f"{ref.label} match weak"}
        for st in self.project.episodes.values():
            st.cuts[:] = [c for c in st.cuts if id(c) not in gone]
            for c in st.cuts:
                if c.reference_id == ref.id and c is not own:
                    c.reference_id = None
            st.flags = [f for f in st.flags if f not in stale_flags]
        self.project.references.remove(ref)
        return True

    def play_edge(self) -> None:
        c = self._current()
        if c is None:
            return
        spans = edge_plan(c, self.duration)
        if spans:
            self.player.play_spans(spans)

    def mark_reference(self) -> None:
        c = self._current()
        if c is None or self.project is None:
            return
        refs = self.project.references
        existing = next((r for r in refs if r.id == c.reference_id), None) if c.reference_id else None
        if existing is not None:
            default = (existing.kind, existing.label)
        else:
            kind = "OP" if c.start < self.duration / 2 else "ED"
            n = sum(1 for r in refs if r.kind == kind) + 1
            default = (kind, f"{kind} {n}")
        answer = ask_kind(self, default)
        if answer is None:
            return
        kind, label = answer
        if existing is not None:  # re-marking updates the reference, never duplicates it
            existing.kind, existing.label = kind, label
            existing.episode, existing.start, existing.end = self.file, c.start, c.end
            existing.confirmed = True
        else:
            ref = Reference(new_id(), kind, label, self.file, c.start, c.end, True)
            refs.append(ref)
            c.reference_id = ref.id
        self._mutated()

    def edit_time(self, index: int, edge: str, text: str) -> bool:
        """Apply typed time; returns False (and flags the field red) on bad input."""
        cuts = self._cuts()
        ok = False
        if 0 <= index < len(cuts):
            c = cuts[index]
            try:
                t = parse_time(text)
                if edge == "start":
                    ok = 0.0 <= t <= c.end - MIN_GAP
                else:
                    ok = c.start + MIN_GAP <= t <= self.duration
            except ValueError:
                ok = False
            if ok:
                if edge == "start":
                    c.start = t
                else:
                    c.end = t
                self._edited(c)
                self.selected, self.edge = index, edge
                self._mutated()
                return True
        w = self._edits.get((index, edge))
        if w is not None:
            w.mark_invalid(True)
        return False

    def time_edit_widget(self, index: int, edge: str) -> TimeEdit:
        return self._edits[(index, edge)]

    # ---- view ----
    def _update_enabled(self) -> None:
        self.add_button.setEnabled(self.project is not None)
        self.ref_button.setEnabled(self._current() is not None)

    def row_texts(self) -> list[str]:
        out = []
        for i in range(self.rows_layout.count() - 1):
            w = self.rows_layout.itemAt(i).widget()
            parts = []
            for ch in w.findChildren(QWidget):
                if isinstance(ch, (QLabel, QLineEdit)) and ch.text():
                    parts.append(ch.text())
            out.append(" ".join(parts))
        return out

    def _refresh(self) -> None:
        while self.rows_layout.count() > 1:
            item = self.rows_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        self._edits = {}
        cuts = self._cuts()
        refs = {r.id: r for r in (self.project.references if self.project else [])}
        for i, c in enumerate(cuts):
            self.rows_layout.insertWidget(i, self._make_row(i, c, refs))
        self.timeline.set_data(self.duration, cuts, self.selected)
        self._update_enabled()

    def _make_row(self, i: int, c: Cut, refs: dict[str, Reference]) -> QWidget:
        row = QFrame()
        row.setFrameShape(QFrame.StyledPanel)
        if i == self.selected:
            row.setStyleSheet("QFrame { background: palette(midlight); }")
        lay = QHBoxLayout(row)
        ref = refs.get(c.reference_id) if c.reference_id else None
        lay.addWidget(QLabel(f"{ref.label if ref else f'Cut {i + 1}'} [{c.status}]"), 1)
        for edge, value in (("start", c.start), ("end", c.end)):
            te = TimeEdit(format_time(value))
            if i == self.selected and edge == self.edge:
                te.setStyleSheet("QLineEdit { border: 2px solid #2a6df4; }")
            te.submitted.connect(lambda text, i=i, edge=edge: self.edit_time(i, edge, text))
            te.clicked.connect(lambda i=i: self._click_row(i))
            self._edits[(i, edge)] = te
            lay.addWidget(te)
            if edge == "start":
                lay.addWidget(QLabel("–"))
        lay.addWidget(QLabel(confidence_level(c.confidence)))
        for text, fn in (
            ("Approve", lambda i=i: self._row_action(i, "approved")),
            ("Reject", lambda i=i: self._row_action(i, "rejected")),
            ("Delete", lambda i=i: self.delete(i)),
        ):
            b = QPushButton(text)
            b.clicked.connect(fn)
            lay.addWidget(b)
        row.mousePressEvent = lambda _e, i=i: self.select(i)
        return row

    def _click_row(self, i: int) -> None:
        if i != self.selected:
            self.select(i)

    def _row_action(self, i: int, status: str) -> None:
        cuts = self._cuts()
        if 0 <= i < len(cuts):
            self.selected = i
            cuts[i].status = status
            self._mutated()
