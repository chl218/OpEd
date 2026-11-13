import json

import platformdirs

from oped import project
from oped.discovery import discover
from oped.project import (
    PROJECT_FILE,
    Cut,
    Project,
    Reference,
    confidence_level,
    load_project,
    new_id,
    project_path,
    save_project,
)


def test_confidence_levels():
    assert [confidence_level(c) for c in (None, 0.9, 0.7, 0.3)] == ["manual", "high", "weak", "none"]
    assert confidence_level(0.85) == "high" and confidence_level(0.65) == "weak"


def test_new_id():
    a, b = new_id(), new_id()
    assert len(a) == 8 and a != b


def test_roundtrip_and_new_episodes(tmp_path):
    (tmp_path / "E1.mkv").write_bytes(b""); season = discover(tmp_path)
    p = load_project(season)
    p.references.append(Reference("r1", "OP", "OP 1", "E1.mkv", 154.0, 225.0))
    p.episodes["E1.mkv"].cuts.append(Cut("c1", 154.0, 225.0, "approved", "r1", 0.97))
    assert save_project(p) == tmp_path / PROJECT_FILE
    (tmp_path / "E2.mkv").write_bytes(b"")
    q = load_project(discover(tmp_path))
    assert q.references == p.references
    assert q.approved_intervals("E1.mkv") == [(154.0, 225.0)]
    assert q.episodes["E2.mkv"].cuts == []


def test_missing_episode_state_kept(tmp_path):
    (tmp_path / "E1.mkv").write_bytes(b"")
    (tmp_path / "E2.mkv").write_bytes(b"")
    p = load_project(discover(tmp_path))
    p.episodes["E2.mkv"].cuts.append(Cut("c", 1.0, 2.0, "approved"))
    p.episodes["E2.mkv"].flags.append("f")
    p.episodes["E2.mkv"].scanned = True
    save_project(p)
    (tmp_path / "E2.mkv").unlink()
    q = load_project(discover(tmp_path))
    assert q.episodes["E2.mkv"].cuts == p.episodes["E2.mkv"].cuts
    assert q.episodes["E2.mkv"].scanned and q.episodes["E2.mkv"].flags == ["f"]


def test_approved_intervals_only_approved_sorted(tmp_path):
    (tmp_path / "E1.mkv").write_bytes(b"")
    p = load_project(discover(tmp_path))
    cuts = p.episodes["E1.mkv"].cuts
    cuts += [Cut("a", 50, 60, "approved"), Cut("b", 1, 2, "proposed"),
             Cut("c", 5, 6, "rejected"), Cut("d", 10, 20, "approved")]
    assert p.approved_intervals("E1.mkv") == [(10, 20), (50, 60)]


def test_json_shape_and_atomic_write(tmp_path):
    (tmp_path / "E1.mkv").write_bytes(b"")
    p = load_project(discover(tmp_path))
    path = save_project(p)
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d["version"] == project.SCHEMA_VERSION
    assert not list(tmp_path.glob("*.tmp"))
    assert isinstance(Project.from_json(tmp_path, d), Project)


def test_readonly_folder_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(project, "_writable", lambda folder: False)
    monkeypatch.setattr(platformdirs, "user_data_dir", lambda app: str(tmp_path / "data"))
    assert project_path(tmp_path / "season").parent == tmp_path / "data" / "projects"
