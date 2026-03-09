#!/usr/bin/env python3
"""
DICOM Sync GUI — Cross-platform DICOM transfer tool with real-time dashboard.

Usage:
    python main.py
    python -m dicom_sync_gui

Dependencies:
    pip install PySide6 pydicom pynetdicom
"""

import logging
import os
import sys

# Ensure 'core' and 'gui' are importable regardless of how the script is launched
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Setup logging before imports
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('dicom_sync_gui.log'),
    ]
)
logger = logging.getLogger("dicom_sync")


def check_dependencies():
    """Check that required packages are installed."""
    missing = []
    try:
        import PySide6
    except ImportError:
        missing.append("PySide6")
    try:
        import pydicom
    except ImportError:
        missing.append("pydicom")
    try:
        import pynetdicom
    except ImportError:
        missing.append("pynetdicom")

    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print(f"Install with: pip install {' '.join(missing)}")
        sys.exit(1)


def main():
    check_dependencies()

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont

    from core.config import AppConfig
    from gui.main_window import MainWindow
    from gui.settings_dialog import SettingsDialog

    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("DICOM Sync")
    app.setOrganizationName("DicomSync")

    # Apply dark theme
    app.setStyleSheet(_dark_theme())

    # Load or create config
    config = AppConfig()
    if not config.load():
        logger.info("No configuration found. Opening settings...")
        # Show settings dialog for first-time setup
        dlg = SettingsDialog(config)
        dlg.setWindowTitle("Initial Setup — DICOM Sync")
        if dlg.exec() != SettingsDialog.Accepted:
            sys.exit(0)

    # Auto-update local IP
    config.update_local_ip()

    # Create and show main window
    window = MainWindow(config)
    window.show()

    sys.exit(app.exec())


def _dark_theme() -> str:
    """Return a dark theme stylesheet for the application."""
    return """
    QMainWindow, QDialog, QWidget {
        background-color: #1e1e1e;
        color: #d4d4d4;
    }
    QMenuBar {
        background-color: #2d2d2d;
        color: #d4d4d4;
        border-bottom: 1px solid #3e3e3e;
    }
    QMenuBar::item:selected {
        background-color: #3e3e3e;
    }
    QMenu {
        background-color: #2d2d2d;
        color: #d4d4d4;
        border: 1px solid #3e3e3e;
    }
    QMenu::item:selected {
        background-color: #094771;
    }
    QTabWidget::pane {
        border: 1px solid #3e3e3e;
        background-color: #252526;
    }
    QTabBar::tab {
        background-color: #2d2d2d;
        color: #d4d4d4;
        padding: 8px 16px;
        border: 1px solid #3e3e3e;
        border-bottom: none;
        margin-right: 2px;
    }
    QTabBar::tab:selected {
        background-color: #252526;
        color: #ffffff;
        border-bottom: 2px solid #2980b9;
    }
    QTabBar::tab:hover {
        background-color: #3e3e3e;
    }
    QGroupBox {
        border: 1px solid #3e3e3e;
        border-radius: 4px;
        margin-top: 8px;
        padding-top: 16px;
        font-weight: bold;
        color: #d4d4d4;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
    }
    QTableWidget {
        background-color: #1e1e1e;
        alternate-background-color: #252526;
        gridline-color: #3e3e3e;
        color: #d4d4d4;
        border: 1px solid #3e3e3e;
        selection-background-color: #094771;
    }
    QTableWidget::item {
        padding: 4px;
    }
    QHeaderView::section {
        background-color: #2d2d2d;
        color: #d4d4d4;
        padding: 6px;
        border: 1px solid #3e3e3e;
        font-weight: bold;
    }
    QLineEdit, QSpinBox, QComboBox, QDateEdit {
        background-color: #3c3c3c;
        color: #d4d4d4;
        border: 1px solid #555;
        border-radius: 3px;
        padding: 5px;
        min-height: 22px;
    }
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QDateEdit:focus {
        border: 1px solid #2980b9;
    }
    QComboBox::drop-down {
        border: none;
        padding-right: 8px;
    }
    QComboBox QAbstractItemView {
        background-color: #3c3c3c;
        color: #d4d4d4;
        selection-background-color: #094771;
    }
    QPushButton {
        background-color: #3c3c3c;
        color: #d4d4d4;
        border: 1px solid #555;
        border-radius: 4px;
        padding: 6px 14px;
        min-height: 22px;
    }
    QPushButton:hover {
        background-color: #4a4a4a;
    }
    QPushButton:pressed {
        background-color: #555;
    }
    QPushButton:disabled {
        background-color: #2d2d2d;
        color: #666;
    }
    QProgressBar {
        background-color: #3c3c3c;
        border: 1px solid #555;
        border-radius: 4px;
        text-align: center;
        color: #d4d4d4;
        min-height: 20px;
    }
    QProgressBar::chunk {
        background-color: #2980b9;
        border-radius: 3px;
    }
    QTextEdit {
        background-color: #1e1e1e;
        color: #969696;
        border: 1px solid #3e3e3e;
        font-family: "Menlo", "Consolas", "Courier New", monospace;
    }
    QStatusBar {
        background-color: #007acc;
        color: white;
    }
    QSplitter::handle {
        background-color: #3e3e3e;
        height: 3px;
    }
    QCheckBox {
        color: #d4d4d4;
        spacing: 6px;
    }
    QCheckBox::indicator {
        width: 16px;
        height: 16px;
        border: 1px solid #555;
        border-radius: 3px;
        background-color: #3c3c3c;
    }
    QCheckBox::indicator:checked {
        background-color: #2980b9;
        border-color: #2980b9;
    }
    QLabel {
        color: #d4d4d4;
    }
    QScrollBar:vertical {
        background-color: #1e1e1e;
        width: 12px;
        border: none;
    }
    QScrollBar::handle:vertical {
        background-color: #555;
        border-radius: 4px;
        min-height: 30px;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0px;
    }
    QListWidget {
        background-color: #1e1e1e;
        color: #d4d4d4;
        border: 1px solid #3e3e3e;
        alternate-background-color: #252526;
    }
    QListWidget::item:selected {
        background-color: #094771;
    }
    QDialogButtonBox QPushButton {
        min-width: 80px;
    }
    """


if __name__ == "__main__":
    main()
