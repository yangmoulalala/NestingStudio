from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#202124"))
    palette.setColor(QPalette.WindowText, QColor("#e8eaed"))
    palette.setColor(QPalette.Base, QColor("#292a2d"))
    palette.setColor(QPalette.AlternateBase, QColor("#303134"))
    palette.setColor(QPalette.ToolTipBase, QColor("#303134"))
    palette.setColor(QPalette.ToolTipText, QColor("#f1f3f4"))
    palette.setColor(QPalette.Text, QColor("#e8eaed"))
    palette.setColor(QPalette.Button, QColor("#303134"))
    palette.setColor(QPalette.ButtonText, QColor("#e8eaed"))
    palette.setColor(QPalette.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.Highlight, QColor("#3b82f6"))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#7f8489"))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#7f8489"))
    app.setPalette(palette)
    app.setStyleSheet(
        """
        QMainWindow, QDialog { background: #202124; }
        QToolBar { background: #292a2d; border: 0; spacing: 4px; padding: 4px; }
        QToolButton { padding: 7px 10px; border-radius: 5px; }
        QToolButton:hover { background: #3a3b3f; }
        QToolButton:checked { background: #315fa8; }
        QDockWidget { color: #e8eaed; }
        QDockWidget::title { background: #292a2d; padding: 7px; }
        QGroupBox {
            border: 1px solid #45474b;
            border-radius: 7px;
            margin-top: 12px;
            padding-top: 8px;
            font-weight: 600;
        }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
            background: #303134; border: 1px solid #4a4d52;
            border-radius: 4px; padding: 4px 6px; min-height: 22px;
        }
        QPushButton {
            background: #3a3b3f; border: 1px solid #55585d;
            border-radius: 5px; padding: 6px 12px;
        }
        QPushButton:hover { background: #484a4f; }
        QPushButton:disabled { color: #777b80; background: #2b2c2f; }
        QPushButton#primary { background: #2563eb; border-color: #3b82f6; color: white; font-weight: 600; }
        QPushButton#primary:hover { background: #3475ef; }
        QHeaderView::section { background: #303134; padding: 6px; border: 0; border-right: 1px solid #45474b; }
        QTableView, QTreeView, QTableWidget, QTreeWidget {
            background: #292a2d; alternate-background-color: #2e3033;
            gridline-color: #414348; border: 0;
        }
        QTabWidget::pane { border: 1px solid #45474b; }
        QTabBar::tab { background: #303134; padding: 7px 12px; }
        QTabBar::tab:selected { background: #2563eb; }
        QSplitter::handle { background: #3a3b3f; }
        QProgressBar { border: 1px solid #4a4d52; border-radius: 4px; text-align: center; }
        QProgressBar::chunk { background: #2563eb; border-radius: 3px; }
        """
    )
