"""Video player widget. QtMultimedia is imported lazily so that importing this module,
the cut panel or the main window never needs a working multimedia backend."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..media import Tools, run

PREVIEW_SECONDS = 10.0
PREVIEW_TIMEOUT = 120.0


class Player(QWidget):
    error_occurred = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tools: Tools | None = None
        self._path: Path | None = None
        self._spans: list[tuple[float, float]] = []
        self._index = -1
        self._mp = None
        self._video = None
        self._audio = None
        self._backend_failed = False
        self._tmp: Path | None = None
        self._preview_offset = 0.0  # source time of the loaded preview clip's start
        self._in_preview = False
        self._last_pos = 0.0
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._placeholder = QLabel("No video loaded")
        self._placeholder.setMinimumHeight(180)
        self._layout.addWidget(self._placeholder)

    # ---- backend (lazy) ----
    def _ensure_backend(self) -> bool:
        if self._mp is not None:
            return True
        if self._backend_failed:
            return False
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget

            self._mp = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._video = QVideoWidget(self)
            self._mp.setAudioOutput(self._audio)
            self._mp.setVideoOutput(self._video)
            self._mp.positionChanged.connect(self._on_position)
            self._mp.errorOccurred.connect(self._on_error)
        except Exception as e:  # missing/broken multimedia backend
            self._mp = None
            self._backend_failed = True
            self._placeholder.setText(f"Video playback is unavailable: {e}")
            self.error_occurred.emit(f"Video playback is unavailable: {e}")
            return False
        self._placeholder.hide()
        self._layout.addWidget(self._video, 1)
        self._video.setMinimumHeight(180)
        return True

    # ---- public API ----
    def load(self, path: Path) -> None:
        self._path = Path(path)
        self._spans = []
        self._index = -1
        self._in_preview = False
        self._last_pos = 0.0
        if not self._ensure_backend():
            return
        self._mp.stop()
        self._mp.setSource(QUrl.fromLocalFile(str(self._path)))

    def position(self) -> float:
        if self._mp is None:
            return self._last_pos
        return self._source_pos()

    def _source_pos(self) -> float:
        p = self._mp.position() / 1000.0
        return p + self._preview_offset if self._in_preview else p

    def seek(self, t: float) -> None:
        self._last_pos = float(t)
        if self._mp is None:
            return
        if self._in_preview:
            self._restore_source()
        self._mp.setPosition(int(max(0.0, t) * 1000))

    def play_spans(self, spans: list[tuple[float, float]]) -> None:
        self._spans = [s for s in spans if s[1] > s[0]]
        self._index = -1
        if self._in_preview:
            self._restore_source()
        if not self._spans or not self._ensure_backend():
            return
        self._start_next()

    # ---- span sequencing ----
    def _start_next(self) -> None:
        self._index += 1
        if self._index >= len(self._spans):
            self._mp.pause()
            self._spans = []
            return
        start, _ = self._spans[self._index]
        self._mp.setPosition(int(start * 1000))
        self._mp.play()

    def _on_position(self, ms: int) -> None:
        if not self._spans or self._index < 0 or self._index >= len(self._spans):
            return
        if self._in_preview:
            return  # preview clips advance on EndOfMedia
        end = self._spans[self._index][1]
        pos = ms / 1000.0
        if pos >= end:
            self._start_next()

    # ---- error fallback ----
    def _on_error(self, *args) -> None:
        msg = args[1] if len(args) > 1 else "playback error"
        if self._in_preview or not self._spans or self._path is None or self.tools is None:
            self.error_occurred.emit(f"Playback failed: {msg}")
            return
        spans = self._spans
        self.error_occurred.emit(f"Playback failed ({msg}); using generated preview clips.")
        self._play_previews(spans)

    def _play_previews(self, spans: list[tuple[float, float]]) -> None:
        self._cleanup_tmp()
        self._tmp = Path(tempfile.mkdtemp(prefix="oped-preview-"))
        clips: list[tuple[float, Path]] = []
        try:
            for i, (start, end) in enumerate(spans):
                out = self._tmp / f"clip{i}.mp4"
                run(
                    [
                        self.tools.ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(self._path),
                        "-t", f"{min(PREVIEW_SECONDS, end - start):.3f}",
                        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(out),
                    ],
                    timeout=PREVIEW_TIMEOUT,
                )
                clips.append((start, out))
        except Exception as e:
            self.error_occurred.emit(f"Could not generate preview clips: {e}")
            self._cleanup_tmp()
            return
        self._clips = clips
        self._clip_i = 0
        self._spans = [(0.0, min(PREVIEW_SECONDS, e - s)) for s, e in spans]
        self._in_preview = True
        self._index = -1
        self._load_clip()

    def _load_clip(self) -> None:
        start, path = self._clips[self._clip_i]
        self._preview_offset = start
        self._mp.setSource(QUrl.fromLocalFile(str(path)))
        self._index = self._clip_i - 1
        self._start_next_clip()

    def _start_next_clip(self) -> None:
        self._index += 1
        self._mp.setPosition(0)
        self._mp.play()
        self._mp.mediaStatusChanged.connect(self._on_clip_status)

    def _on_clip_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        self._mp.mediaStatusChanged.disconnect(self._on_clip_status)
        self._clip_i += 1
        if self._clip_i < len(self._clips):
            self._load_clip()
        else:
            self._mp.pause()
            self._restore_source()

    def _restore_source(self) -> None:
        self._in_preview = False
        self._preview_offset = 0.0
        self._spans = []
        if self._mp is not None and self._path is not None:
            self._mp.setSource(QUrl.fromLocalFile(str(self._path)))
        self._cleanup_tmp()

    def _cleanup_tmp(self) -> None:
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def closeEvent(self, event) -> None:
        if self._mp is not None:
            self._mp.stop()
        self._cleanup_tmp()
        super().closeEvent(event)
