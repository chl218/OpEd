"""Background work for the GUI: run ``fn(progress)`` on the global thread pool.

The worker never touches widgets or the live Project. It reports back only through
``WorkerSignals``; the signals object lives in the GUI thread, so the connected slots
run there (queued connections).
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

Progress = Callable[[str, float], None]


class Cancelled(Exception):
    """Raised from ``progress`` once the worker's cancel event is set."""


class WorkerSignals(QObject):
    progress = Signal(str, float)
    finished = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn: Callable[[Progress], Any], cancel: threading.Event | None = None):
        super().__init__()
        self.fn = fn
        self.cancel = cancel
        self.signals = WorkerSignals()
        # The Python side owns the object (the caller keeps a reference until it is done).
        self.setAutoDelete(False)

    def run(self) -> None:
        def progress(name: str, frac: float) -> None:
            # Cooperative cancel: ``fn`` stops at its next progress report. Nothing is
            # killed mid-step, so the callee's own cleanup (``finally`` blocks) still runs.
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled("cancelled")
            self.signals.progress.emit(str(name), float(frac))

        try:
            result = self.fn(progress)
        except BaseException as e:  # noqa: BLE001 - every failure is reported, never raised
            self.signals.failed.emit(str(e) or type(e).__name__)
            return
        self.signals.finished.emit(result)

    def start(self) -> None:
        QThreadPool.globalInstance().start(self)
