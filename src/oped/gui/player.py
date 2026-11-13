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
LANDING_SLACK = 0.5  # positions below span start minus this are stale (pre-seek)


class Player(QWidget):
    error_occurred = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tools: Tools | None = None
        self._path: Path | None = None
        self._spans: list[tuple[float, float]] = []
        self._index = -1
        self._landing: tuple[float, float] | None = None  # window a fresh seek must land in
        self._pending = False  # play_spans requested before the media finished loading
        self._loaded = False
        self._source_failed = False
        self._mp = None
        self._video = None
        self._audio = None
        self._backend_failed = False
        self._tmp: Path | None = None
        self._clips: list[tuple[float, Path]] = []
        self._clip_i = 0
        self._preview_offset = 0.0  # source time of the loaded preview clip's start
        self._in_preview = False
        self._last_pos = 0.0
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._placeholder = QLabel("No video loaded")
        self._placeholder.setMinimumHeight(180)
        self._layout.addWidget(self._placeholder)

    # ---- backend (lazy) ----
    def _create_backend(self):
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget

        mp = QMediaPlayer(self)
        audio = QAudioOutput(self)
        video = QVideoWidget(self)
        mp.setAudioOutput(audio)
        mp.setVideoOutput(video)
        return mp, video, audio

    def _attach(self, mp) -> None:
        """Adopt a media player object (real or a test double) and connect its signals once."""
        self._mp = mp
        mp.positionChanged.connect(self._on_position)
        mp.errorOccurred.connect(self._on_error)
        mp.mediaStatusChanged.connect(self._on_status)

    def _ensure_backend(self) -> bool:
        if self._mp is not None:
            return True
        if self._backend_failed:
            return False
        try:
            mp, video, audio = self._create_backend()
        except Exception as e:  # missing/broken multimedia backend
            self._backend_failed = True
            self._placeholder.setText(f"Video playback is unavailable: {e}")
            self.error_occurred.emit(f"Video playback is unavailable: {e}")
            return False
        self._video, self._audio = video, audio
        self._attach(mp)
        self._placeholder.hide()
        self._layout.addWidget(video, 1)
        video.setMinimumHeight(180)
        return True

    # ---- public API ----
    def load(self, path: Path) -> None:
        self._path = Path(path)
        self._reset_sequence()
        self._reset_preview_state()
        self._loaded = False
        self._source_failed = False
        self._last_pos = 0.0
        if not self._ensure_backend():
            return
        self._mp.stop()
        self._mp.setSource(QUrl.fromLocalFile(str(self._path)))

    def position(self) -> float:
        if self._mp is None:
            return self._last_pos
        p = self._mp.position() / 1000.0
        return p + self._preview_offset if self._in_preview else p

    def seek(self, t: float) -> None:
        self._last_pos = float(t)
        self._reset_sequence()
        if self._mp is None:
            return
        if self._in_preview:
            self._restore_source()
        self._mp.setPosition(int(max(0.0, t) * 1000))

    def play_spans(self, spans: list[tuple[float, float]]) -> None:
        spans = [s for s in spans if s[1] > s[0]]
        self._reset_sequence()
        if not spans or not self._ensure_backend():
            return
        if self._in_preview:
            self._restore_source()
        if self._source_failed or self._mp_errored():
            self._source_failed = True
            self._play_previews(spans)
            return
        self._spans = spans
        if not self._loaded:
            self._pending = True  # started from _on_status once the media is ready
            return
        self._start_next()

    # ---- span sequencing ----
    def _reset_sequence(self) -> None:
        self._spans = []
        self._index = -1
        self._landing = None
        self._pending = False

    def _mp_errored(self) -> bool:
        try:
            from PySide6.QtMultimedia import QMediaPlayer

            return self._mp.error() != QMediaPlayer.Error.NoError
        except Exception:
            return False

    def _start_next(self) -> None:
        self._index += 1
        if self._index >= len(self._spans):
            self._mp.pause()
            self._reset_sequence()
            return
        start, end = self._spans[self._index]
        self._landing = (start - LANDING_SLACK, end)
        self._mp.setPosition(int(start * 1000))
        self._mp.play()

    def _on_position(self, ms: int) -> None:
        if self._in_preview or not self._spans or not 0 <= self._index < len(self._spans):
            return
        pos = ms / 1000.0
        end = self._spans[self._index][1]
        if self._landing is not None:
            lo, hi = self._landing
            if not lo <= pos < hi:
                return  # stale position from before the seek took effect
            self._landing = None
        if pos >= end:
            self._start_next()

    def _on_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        S = QMediaPlayer.MediaStatus
        if self._in_preview:
            if status == S.EndOfMedia:
                self._clip_i += 1
                if self._clip_i < len(self._clips):
                    self._load_clip()
                else:
                    self._mp.pause()
                    self._restore_source()
            return
        if status in (S.LoadedMedia, S.BufferedMedia, S.BufferingMedia, S.StalledMedia):
            self._loaded = True
            if self._pending:
                self._pending = False
                self._start_next()
        elif status in (S.NoMedia, S.LoadingMedia, S.InvalidMedia):
            self._loaded = False
        elif status == S.EndOfMedia:
            self._reset_sequence()

    # ---- error fallback ----
    def _on_error(self, *args) -> None:
        msg = args[1] if len(args) > 1 else "playback error"
        if self._in_preview:
            self.error_occurred.emit(f"Preview clip playback failed: {msg}")
            return
        self._source_failed = True
        spans = self._spans
        pending_spans = bool(spans) or self._pending
        if pending_spans and self.tools is not None and self._path is not None:
            self._reset_sequence()
            self.error_occurred.emit(f"Playback failed ({msg}); using generated preview clips.")
            self._play_previews(spans)
            return
        self._reset_sequence()
        self.error_occurred.emit(f"Playback failed: {msg}")

    def _play_previews(self, spans: list[tuple[float, float]]) -> None:
        if self.tools is None or self._path is None:
            self.error_occurred.emit("Playback failed and ffmpeg is unavailable for preview clips.")
            return
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
                        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                        "-pix_fmt", "yuv420p",
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
        self._in_preview = True
        self._load_clip()

    def _load_clip(self) -> None:
        start, path = self._clips[self._clip_i]
        self._preview_offset = start
        self._mp.setSource(QUrl.fromLocalFile(str(path)))
        self._mp.play()

    def _reset_preview_state(self) -> None:
        self._in_preview = False
        self._preview_offset = 0.0
        self._clips = []
        self._clip_i = 0
        self._cleanup_tmp()

    def _restore_source(self) -> None:
        self._reset_preview_state()
        self._reset_sequence()
        self._loaded = False
        if self._mp is not None and self._path is not None and not self._source_failed:
            self._mp.setSource(QUrl.fromLocalFile(str(self._path)))

    def _cleanup_tmp(self) -> None:
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def closeEvent(self, event) -> None:
        if self._mp is not None:
            self._mp.stop()
        self._cleanup_tmp()
        super().closeEvent(event)
