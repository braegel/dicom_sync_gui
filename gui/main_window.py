"""
Main window for DICOM Sync GUI.
Pure service-based: Start/Stop downloads all series in the configured time
window automatically. No manual study selection.
"""

import logging
from datetime import datetime
from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QMessageBox, QApplication,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction

from core.config import AppConfig
from core.dicom_ops import DicomOperations
from core.storage_scp import StorageSCP
from core.transfer_engine import TransferEngine
from gui.settings_dialog import SettingsDialog
from gui.dashboard import TransferDashboard
from gui.log_window import LogWindow

logger = logging.getLogger("dicom_sync")


class MainWindow(QMainWindow):
    """Main application window — dashboard-only, fully automatic."""

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.storage_scp: Optional[StorageSCP] = None
        self.engine: Optional[TransferEngine] = None

        self.setWindowTitle("DICOM Sync")
        self.setMinimumSize(1000, 750)
        self.resize(1100, 850)

        self._setup_menu()
        self._setup_ui()
        self._setup_statusbar()

    # ── Menu ──────────────────────────────────────────────────────────────

    def _setup_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("File")
        settings_action = QAction("Settings...", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menubar.addMenu("View")
        log_action = QAction("Show Log Window", self)
        log_action.setShortcut("Ctrl+L")
        log_action.triggered.connect(self._show_log_window)
        view_menu.addAction(log_action)

        tools_menu = menubar.addMenu("Tools")
        echo_action = QAction("C-ECHO Test...", self)
        echo_action.triggered.connect(self._test_echo)
        tools_menu.addAction(echo_action)

    # ── Central UI ────────────────────────────────────────────────────────

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)

        # Dashboard is the entire main view
        self.dashboard = TransferDashboard(config=self.config)
        self.dashboard.start_requested.connect(self._on_start_service)
        self.dashboard.stop_requested.connect(self._on_stop_service)
        layout.addWidget(self.dashboard)

        # Log window (created once, shown/hidden on demand)
        self.log_window = LogWindow(self)

    def _setup_statusbar(self):
        self.statusBar().showMessage("Ready")

    # ── Settings ──────────────────────────────────────────────────────────

    def _open_settings(self):
        was_running = self.engine and self.engine.is_running
        if was_running:
            QMessageBox.information(
                self, "Service Running",
                "Please stop the service before changing settings.")
            return

        dlg = SettingsDialog(self.config, self)
        if dlg.exec() == SettingsDialog.Accepted:
            # Sync dashboard spinboxes with saved config
            self.dashboard.hours_spin.setValue(self.config.default_hours)
            self.dashboard.max_images_spin.setValue(self.config.max_images)
            self.dashboard.interval_spin.setValue(self.config.sync_interval)
            self._log("Settings saved.")

    # ── C-ECHO ────────────────────────────────────────────────────────────

    def _test_echo(self):
        if not self.config.remote_nodes:
            QMessageBox.warning(
                self, "Warning",
                "No source PACS configured. Open Settings first.")
            return

        self.statusBar().showMessage("C-ECHO test running...")
        QApplication.processEvents()

        results = []
        for key, node in self.config.remote_nodes.items():
            ops = DicomOperations(
                self.config.get_local_dict(), node.to_dict(), key)
            ok = ops.c_echo(target='remote')
            results.append(
                f"  {key} ({node.name}): "
                f"{'Reachable' if ok else 'Not reachable'}")

        # Test local PACS
        if self.config.remote_nodes:
            first_key = next(iter(self.config.remote_nodes))
            first_node = self.config.remote_nodes[first_key]
            ops = DicomOperations(
                self.config.get_local_dict(), first_node.to_dict(), first_key)
            local_ok = ops.c_echo(target='local')
            results.append(
                f"\n  Local PACS: "
                f"{'Reachable' if local_ok else 'Not reachable'}")
            if not local_ok and self.config.fallback_storage_enabled:
                results.append(
                    f"  Fallback storage: {self.config.fallback_storage_path}")

        QMessageBox.information(
            self, "C-ECHO Results", "Results:\n" + "\n".join(results))
        self.statusBar().showMessage("Ready")

    # ── Log ───────────────────────────────────────────────────────────────

    def _show_log_window(self):
        self.log_window.show()
        self.log_window.raise_()
        self.log_window.activateWindow()

    def _log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        self.log_window.append_log(line)
        self.statusBar().showMessage(msg)

    # ── Service Start / Stop ──────────────────────────────────────────────

    def _on_start_service(self, params: dict):
        """Start the automatic download service."""
        if not self.config.remote_nodes:
            QMessageBox.warning(
                self, "Warning",
                "No source PACS configured. Open Settings first.")
            return

        # Ensure local storage / SCP
        self._ensure_storage_scp()

        # Create engine
        self.engine = TransferEngine(self.config)
        self._connect_engine_signals()

        self.dashboard.reset()
        self.dashboard.set_service_running(True)
        self.statusBar().showMessage("Service running...")

        self.engine.start(
            hours=params["hours"],
            max_images=params["max_images"],
            sync_interval=params["sync_interval"],
        )

    def _on_stop_service(self):
        """Stop the download service."""
        if self.engine and self.engine.is_running:
            self.engine.stop()
            self._log("Stopping service...")
        # Button states are updated when service_stopped signal fires

    def _on_service_stopped(self):
        """Called when the engine thread has actually stopped."""
        self.dashboard.set_service_running(False)
        self.statusBar().showMessage("Service stopped.")

    # ── Storage SCP ───────────────────────────────────────────────────────

    def _ensure_storage_scp(self):
        """Start built-in SCP if no local DICOM server is reachable."""
        if self.storage_scp and self.storage_scp.running:
            return

        # Quick echo test against local PACS
        local_reachable = False
        if self.config.remote_nodes:
            first_key = next(iter(self.config.remote_nodes))
            first_node = self.config.remote_nodes[first_key]
            ops = DicomOperations(
                self.config.get_local_dict(), first_node.to_dict(), first_key)
            local_reachable = ops.c_echo(target='local')

        if not local_reachable:
            if self.config.fallback_storage_enabled:
                storage_path = self.config.fallback_storage_path
                self._log(f"Local PACS not reachable. "
                          f"Saving to: {storage_path}")
            else:
                storage_path = self.config.fallback_storage_path
                self._log("Local PACS not reachable. "
                          "Starting built-in Storage SCP...")

            local = self.config.get_local_dict()
            self.storage_scp = StorageSCP(
                local.get('ae_title', 'LOCAL_AE'),
                local.get('port', 11112),
                storage_path,
            )
            self.storage_scp.start()

    # ── Engine signal wiring ──────────────────────────────────────────────

    def _connect_engine_signals(self):
        e = self.engine
        # Dashboard updates
        e.signals.queue_updated.connect(self.dashboard.on_queue_updated)
        e.signals.series_started.connect(self.dashboard.on_series_started)
        e.signals.series_progress.connect(self.dashboard.on_series_progress)
        e.signals.series_completed.connect(self.dashboard.on_series_completed)
        e.signals.series_error.connect(self.dashboard.on_series_error)
        e.signals.stats_updated.connect(self.dashboard.on_stats_updated)
        e.signals.cycle_started.connect(self.dashboard.on_cycle_started)
        e.signals.cycle_finished.connect(self.dashboard.on_cycle_finished)
        # Service lifecycle
        e.signals.service_stopped.connect(self._on_service_stopped)
        # Log
        e.signals.log_message.connect(self._log)

    # ── Window close ──────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self.engine and self.engine.is_running:
            reply = QMessageBox.question(
                self, "Quit",
                "The download service is still running. Quit anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                event.ignore()
                return
            self.engine.stop()

        if self.storage_scp and self.storage_scp.running:
            self.storage_scp.stop()

        self.log_window.close()
        event.accept()
