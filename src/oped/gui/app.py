"""GUI entry point."""
from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.resize(1100, 700)
    window.show()
    code = app.exec()
    # A worker cancelled on quit stops at its next progress report; let it finish its
    # cleanup (.part / work dir removal) before the interpreter shuts down.
    QThreadPool.globalInstance().waitForDone()
    return code


if __name__ == "__main__":
    sys.exit(main())
