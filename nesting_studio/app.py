from __future__ import annotations

import sys
from pathlib import Path
import logging

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .logging_config import configure_logging
from .main_window import MainWindow
from .theme import apply_dark_theme
from .version import __version__


def main() -> int:
    configure_logging()
    QCoreApplication.setOrganizationName("NestingStudio")
    QCoreApplication.setApplicationName("NestingStudio")
    QCoreApplication.setApplicationVersion(__version__)
    app = QApplication(sys.argv)
    app.setApplicationDisplayName(f"NestingStudio {__version__} - 板材自动套裁")
    icon_path = Path(__file__).resolve().parent / "assets" / "icon.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    apply_dark_theme(app)
    window = MainWindow()
    if "--smoke-test" not in sys.argv:
        window.show()
        app.aboutToQuit.connect(logging.shutdown)
    else:
        QTimer.singleShot(500, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
