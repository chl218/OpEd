"""Episode list widget with per-episode status badges."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from ..project import Project


def badge_text(project: Project, file: str) -> str:
    state = project.episodes.get(file)
    if state is None:
        return ""
    kinds = {r.id: r.kind for r in project.references}
    parts: list[str] = []
    approved = [c for c in state.cuts if c.status == "approved"]
    for c in approved:
        kind = kinds.get(c.reference_id or "")
        parts.append(f"✓ {kind}" if kind else "✓")
    parts.extend(f"⚠ {f}" for f in state.flags)
    if state.scanned and not approved:
        parts.append("copied unchanged")
    if any(c.status == "proposed" for c in state.cuts):
        parts.append("●")
    return " ".join(parts)


class EpisodeList(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._sidecars: dict[str, list[str]] = {}

    def set_sidecars(self, sidecars: dict[str, list[str]]) -> None:
        self._sidecars = sidecars

    def refresh(self, project: Project) -> None:
        cur = self.currentItem()
        current = cur.data(Qt.UserRole) if cur else None
        self.blockSignals(True)
        self.clear()
        for name in project.episodes:
            badges = badge_text(project, name)
            item = QListWidgetItem(f"{name}  {badges}" if badges else name)
            item.setData(Qt.UserRole, name)
            side = self._sidecars.get(name, [])
            item.setToolTip("Subtitles: " + ", ".join(side) if side else "No sidecar subtitle files")
            self.addItem(item)
            if name == current:
                self.setCurrentItem(item)
        self.blockSignals(False)
