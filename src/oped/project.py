"""Project model and persistence for a season folder."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import platformdirs

from .discovery import Season

Kind = Literal["OP", "ED"]
Status = Literal["proposed", "approved", "rejected"]

PROJECT_FILE = "oped-project.json"
SCHEMA_VERSION = 1
HIGH = 0.85
WEAK = 0.65


@dataclass
class Reference:
    id: str
    kind: Kind
    label: str
    episode: str
    start: float
    end: float
    confirmed: bool = True


@dataclass
class Cut:
    id: str
    start: float
    end: float
    status: Status = "proposed"
    reference_id: str | None = None
    confidence: float | None = None


@dataclass
class EpisodeState:
    file: str
    cuts: list[Cut] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    scanned: bool = False


@dataclass
class Project:
    folder: Path
    references: list[Reference]
    episodes: dict[str, EpisodeState]

    def approved_intervals(self, file: str) -> list[tuple[float, float]]:
        state = self.episodes.get(file)
        if state is None:
            return []
        return sorted((c.start, c.end) for c in state.cuts if c.status == "approved")

    def to_json(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "references": [asdict(r) for r in self.references],
            "episodes": {name: asdict(st) for name, st in self.episodes.items()},
        }

    @classmethod
    def from_json(cls, folder: Path, d: dict) -> "Project":
        refs = [Reference(**r) for r in d.get("references", [])]
        eps = {}
        for name, st in d.get("episodes", {}).items():
            eps[name] = EpisodeState(
                file=st.get("file", name),
                cuts=[Cut(**c) for c in st.get("cuts", [])],
                flags=list(st.get("flags", [])),
                scanned=bool(st.get("scanned", False)),
            )
        return cls(Path(folder), refs, eps)


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def confidence_level(c: float | None) -> Literal["manual", "high", "weak", "none"]:
    if c is None:
        return "manual"
    if c >= HIGH:
        return "high"
    if c >= WEAK:
        return "weak"
    return "none"


def _writable(folder: Path) -> bool:
    probe = Path(folder) / f".oped-write-test-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def project_path(folder: Path) -> Path:
    folder = Path(folder)
    if _writable(folder):
        return folder / PROJECT_FILE
    digest = hashlib.sha1(str(folder.resolve()).encode("utf-8")).hexdigest()
    return Path(platformdirs.user_data_dir("oped")) / "projects" / f"{digest}.json"


def load_project(season: Season) -> Project:
    path = project_path(season.folder)
    if path.exists():
        project = Project.from_json(season.folder, json.loads(path.read_text(encoding="utf-8")))
    else:
        project = Project(Path(season.folder), [], {})
    for ep in season.episodes:
        name = ep.video.name
        if name not in project.episodes:
            project.episodes[name] = EpisodeState(file=name)
    return project


def save_project(project: Project) -> Path:
    path = project_path(project.folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(project.to_json(), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
