"""GUI entry point."""
from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.resize(1100, 700)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
