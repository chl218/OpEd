import json
import os
from pathlib import Path

import platformdirs
import pytest

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


def _readonly(monkeypatch, folder):
    """Make writes into ``folder`` fail like a read-only share (reads still work)."""
    folder = Path(folder).resolve()
    real_text, real_bytes = Path.write_text, Path.write_bytes

    def guard(real):
        def write(self, *a, **k):
            if Path(self).resolve().parent == folder:
                raise PermissionError(13, "Permission denied", str(self))
            return real(self, *a, **k)
        return write

    monkeypatch.setattr(Path, "write_text", guard(real_text))
    monkeypatch.setattr(Path, "write_bytes", guard(real_bytes))


def _data_dir(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setattr(platformdirs, "user_data_dir", lambda app: str(data))
    return data


def test_readonly_folder_saves_to_data_dir(tmp_path, monkeypatch):
    data = _data_dir(monkeypatch, tmp_path)
    season_dir = tmp_path / "season"
    season_dir.mkdir()
    (season_dir / "E1.mkv").write_bytes(b"")
    _readonly(monkeypatch, season_dir)
    p = load_project(discover(season_dir))
    saved = save_project(p)
    assert saved.parent == data / "projects" and saved.exists()
    assert sorted(x.name for x in season_dir.iterdir()) == ["E1.mkv"]


def test_existing_project_is_read_from_readonly_folder(tmp_path, monkeypatch):
    """S2: a season shared read-only with its oped-project.json must not open empty."""
    data = _data_dir(monkeypatch, tmp_path)
    season_dir = tmp_path / "season"
    season_dir.mkdir()
    (season_dir / "E1.mkv").write_bytes(b"")
    p = load_project(discover(season_dir))
    p.references.append(Reference("r1", "OP", "OP 1", "E1.mkv", 10.0, 20.0))
    assert save_project(p) == season_dir / PROJECT_FILE
    old = (season_dir / PROJECT_FILE).stat().st_mtime - 60
    os.utime(season_dir / PROJECT_FILE, (old, old))

    _readonly(monkeypatch, season_dir)
    q = load_project(discover(season_dir))
    assert q.references == p.references
    # Edits made while read-only go to the data dir and win on the next open.
    q.episodes["E1.mkv"].cuts.append(Cut("c", 10.0, 20.0, "approved", "r1"))
    assert save_project(q).parent == data / "projects"
    r = load_project(discover(season_dir))
    assert r.approved_intervals("E1.mkv") == [(10.0, 20.0)]
    assert json.loads((season_dir / PROJECT_FILE).read_text(encoding="utf-8"))["episodes"]["E1.mkv"]["cuts"] == []


def test_only_the_project_file_is_written_into_the_season_folder(tmp_path, monkeypatch):
    """S4: no write-probe files; the only name ever created is the atomic-save temp."""
    _data_dir(monkeypatch, tmp_path)
    (tmp_path / "E1.mkv").write_bytes(b"")
    created = []
    real_text, real_bytes = Path.write_text, Path.write_bytes

    def spy(real):
        def write(self, *a, **k):
            created.append(Path(self).name)
            return real(self, *a, **k)
        return write

    monkeypatch.setattr(Path, "write_text", spy(real_text))
    monkeypatch.setattr(Path, "write_bytes", spy(real_bytes))
    p = load_project(discover(tmp_path))
    for _ in range(3):
        save_project(p)
        load_project(discover(tmp_path))
    assert set(created) == {PROJECT_FILE + ".tmp"}
    assert sorted(x.name for x in tmp_path.iterdir()) == ["E1.mkv", PROJECT_FILE]


def test_failed_save_leaves_no_tmp(tmp_path, monkeypatch):
    data = _data_dir(monkeypatch, tmp_path)
    season_dir = tmp_path / "season"
    season_dir.mkdir()
    (season_dir / "E1.mkv").write_bytes(b"")
    p = load_project(discover(season_dir))
    monkeypatch.setattr(project.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError(28, "No space left")))
    with pytest.raises(OSError):
        save_project(p)
    assert sorted(x.name for x in season_dir.iterdir()) == ["E1.mkv"]
    assert not list(data.rglob("*.tmp"))
