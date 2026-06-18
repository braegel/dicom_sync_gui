#!/usr/bin/env python3
"""
DICOM Sync GUI — Cross-platform DICOM transfer tool with real-time dashboard.

Usage:
    python main.py
    python -m dicom_sync_gui

Dependencies:
    pip install PySide6 pydicom pynetdicom
"""

import gc
import logging
import logging.handlers
import os
import platform
import sys

# Ensure 'core' and 'gui' are importable regardless of how the script is launched
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _log_file_path() -> str:
    """Return a writable path for the log file."""
    system = platform.system()
    if system == "Darwin":
        log_dir = os.path.expanduser("~/Library/Logs")
    elif system == "Windows":
        log_dir = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        log_dir = os.environ.get(
            "XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "dicom_sync_gui.log")


# Setup logging before imports.  Rotating handler because the app
# runs 24/7 as a service — an unbounded plain FileHandler would grow
# the log file without limit on the host.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            _log_file_path(),
            maxBytes=2 * 1024 * 1024,  # 2 MB per file
            backupCount=3,             # keep ~8 MB of history
        ),
    ]
)
logger = logging.getLogger("dicom_sync")


def check_dependencies() -> None:
    """Check that required packages are installed."""
    import importlib.util

    missing = [
        name for name in ("PySide6", "pydicom", "pynetdicom")
        if importlib.util.find_spec(name) is None
    ]

    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print(f"Install with: pip install {' '.join(missing)}")
        sys.exit(1)


def _ensure_config(config) -> None:
    """Load the config, running the first-run setup dialog if none exists.

    Exits the process if the user cancels the initial-setup dialog.
    """
    if not config.load():
        logger.info("No configuration found. Opening settings...")
        # Show settings dialog for first-time setup
        from gui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(config)
        dlg.setWindowTitle("Initial Setup — DICOM Sync")
        if dlg.exec() != SettingsDialog.Accepted:
            sys.exit(0)


def _configure_gc(app) -> None:
    """Install the GC workaround for the Python 3.14 cross-thread crash.

    Python 3.14's incremental GC runs mark_stacks at any bytecode
    safepoint on any thread.  When the service-loop thread triggers a
    collection while the Qt thread is mid-dealloc of a PySide wrapper,
    the stack walk races the dealloc and SIGSEGVs in mark_stacks.
    Mitigation: freeze all startup objects so they're never scanned,
    disable automatic GC entirely, and drive collection ourselves from
    a QTimer that only ever fires on the main thread.  With automatic
    GC off, mark_stacks can never run on the service-loop thread, so
    the cross-thread race becomes unreachable.  Refcount cleanup is
    unaffected.

    The QTimer is parented to *app* so it outlives this function
    (``main`` never returns, but parenting keeps it explicit).
    """
    from PySide6.QtCore import QTimer

    gc.freeze()
    gc.disable()
    gc_timer = QTimer(app)
    gc_timer.setInterval(60 * 1000)
    gc_timer.timeout.connect(lambda: gc.collect())
    gc_timer.start()


def main() -> None:
    check_dependencies()

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    from core.config import AppConfig
    from gui.main_window import MainWindow
    from gui.styles import DARK_THEME

    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("DICOM Sync")
    app.setOrganizationName("DicomSync")

    # Apply dark theme
    app.setStyleSheet(DARK_THEME)

    # Load or create config
    config = AppConfig()
    _ensure_config(config)

    # Auto-update local IP
    config.update_local_ip()

    # Create and show main window
    window = MainWindow(config)
    window.show()

    _configure_gc(app)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()