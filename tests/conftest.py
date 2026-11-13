import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from oped.media import ToolsMissing, find_tools


@pytest.fixture(scope="session")
def tools():
    try:
        return find_tools()
    except ToolsMissing as e:
        pytest.skip(str(e))


class FakePlayer:
    """Stand-in for oped.gui.player.Player (no QtMultimedia needed)."""

    def __init__(self, position: float = 0.0):
        self._pos = position
        self.loaded = None
        self.spans = None
        self.seeks = []

    def position(self) -> float:
        return self._pos

    def seek(self, t: float) -> None:
        self._pos = t
        self.seeks.append(t)

    def play_spans(self, spans) -> None:
        self.spans = list(spans)

    def load(self, path) -> None:
        self.loaded = path


def raise_(exc):
    """Return a callable that raises exc (for monkeypatching)."""

    def _raiser(*args, **kwargs):
        raise exc

    return _raiser


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path_factory):
    """Keep QSettings (saved ffmpeg paths) out of the user's real profile."""
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        yield
        return
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path_factory.mktemp("qsettings")),
    )
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings("oped", "oped").clear()
    yield
    QSettings("oped", "oped").clear()
