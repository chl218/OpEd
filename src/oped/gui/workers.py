"""Background work for the GUI: run ``fn(progress)`` on the global thread pool.

The worker never touches widgets or the live Project. It reports back only through
``WorkerSignals``; the signals object lives in the GUI thread, so the connected slots
run there (queued connections).
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

Progress = Callable[[str, float], None]


class WorkerSignals(QObject):
    progress = Signal(str, float)
    finished = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn: Callable[[Progress], Any]):
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()
        # The Python side owns the object (the caller keeps a reference until it is done).
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self.fn(lambda name, frac: self.signals.progress.emit(str(name), float(frac)))
        except BaseException as e:  # noqa: BLE001 - every failure is reported, never raised
            self.signals.failed.emit(str(e) or type(e).__name__)
            return
        self.signals.finished.emit(result)

    def start(self) -> None:
        QThreadPool.globalInstance().start(self)
