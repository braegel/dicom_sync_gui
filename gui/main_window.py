"""
Main window for DICOM Sync GUI.
Per-source architecture: each configured source PACS gets its own tab
with an independent download service, queue, and statistics.
"""

import logging
from datetime import datetime
from typing import Dict, Optional, Tuple

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QMessageBox, QApplication,
    QTabWidget, QLabel,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont

from core.config import AppConfig
from core.dicom_ops import DicomOperations
from core.storage_scp import StorageSCP
from core.transfer_engine import TransferEngine
from gui.settings_dialog import SettingsDialog
from gui.dashboard import SourceDashboard
from gui.log_window import LogWindow
from gui.filter_groups_dialog import FilterGroupsDialog
from gui.unknown_institution_popup import UnknownInstitutionPopup

logger = logging.getLogger("dicom_sync")


class MainWindow(QMainWindow):
    """Main application window — per-source tabs, fully automatic."""

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        # Per-source SCPs keyed by (ae_title, port) tuple
        self.storage_scps: Dict[Tuple[str, int], StorageSCP] = {}
        # Per-source engines and dashboards
        self.engines: Dict[str, TransferEngine] = {}
        self.dashboards: Dict[str, SourceDashboard] = {}

        self.setWindowTitle("DICOM Sync")
        self.setMinimumSize(1000, 750)
        self.resize(1100, 850)

        self._setup_menu()
        self._setup_ui()
        self._setup_statusbar()

    # ── Menu ──────────────────────────────────────────────────────────────

    def _setup_menu(self):
        menubar = self.menuBar()

        settings_menu = menubar.addMenu("Settings")
        settings_action = QAction("PACS Configuration...", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._open_settings)
        settings_menu.addAction(settings_action)

        filter_action = QAction("Manage Filter Groups...", self)
        filter_action.triggered.connect(self._open_filter_groups)
        settings_menu.addAction(filter_action)

        settings_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        settings_menu.addAction(quit_action)

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

        # Tab widget — one tab per source PACS
        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        self._rebuild_tabs()

        # Log window (created once, shown/hidden on demand)
        self.log_window = LogWindow(self)

    def _rebuild_tabs(self):
        """Create one SourceDashboard tab per configured source PACS."""
        self.tab_widget.clear()
        self.dashboards.clear()

        if not self.config.remote_nodes:
            # Show a placeholder when no sources are configured
            placeholder = QLabel(
                "No source PACS configured.\n"
                "Go to Settings \u2192 PACS Configuration to add one.")
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setFont(QFont("", 12))
            placeholder.setStyleSheet("QLabel { color: #888; }")
            self.tab_widget.addTab(placeholder, "No Sources")
            return

        for key in self.config.remote_nodes:
            node = self.config.remote_nodes[key]
            dashboard = SourceDashboard(
                config=self.config, remote_key=key)
            dashboard.start_requested.connect(self._on_start_service)
            dashboard.stop_requested.connect(self._on_stop_service)
            self.dashboards[key] = dashboard
            tab_label = f"{node.name} ({key})"
            self.tab_widget.addTab(dashboard, tab_label)

    def _setup_statusbar(self):
        self.statusBar().showMessage("Ready")

    # ── Settings ──────────────────────────────────────────────────────────

    def _open_settings(self):
        any_running = any(
            e.is_running for e in self.engines.values())
        if any_running:
            QMessageBox.information(
                self, "Service Running",
                "Please stop all services before changing settings.")
            return

        dlg = SettingsDialog(self.config, self)
        if dlg.exec() == SettingsDialog.Accepted:
            # Rebuild tabs to reflect added/removed sources
            self._rebuild_tabs()
            self._log("Settings saved.")

    def _open_filter_groups(self):
        dlg = FilterGroupsDialog(self.config, self)
        if dlg.exec() == FilterGroupsDialog.Accepted:
            # Refresh filter dropdowns in all dashboards
            for dashboard in self.dashboards.values():
                dashboard.refresh_filter_groups()
            self._log("Filter groups updated.")

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
            local_config = self.config.get_local_dict_for(key)
            ops = DicomOperations(local_config, node.to_dict(), key)
            ok = ops.c_echo(target='remote')
            results.append(
                f"  {key} ({node.name}): "
                f"{'Reachable' if ok else 'Not reachable'}")

        # Test each unique local destination
        tested_locals = set()
        for key, node in self.config.remote_nodes.items():
            local_key = (node.local_ae_title, node.local_port)
            if local_key in tested_locals:
                continue
            tested_locals.add(local_key)
            local_config = self.config.get_local_dict_for(key)
            ops = DicomOperations(local_config, node.to_dict(), key)
            local_ok = ops.c_echo(target='local')
            results.append(
                f"\n  Local [{node.local_ae_title}:{node.local_port}]: "
                f"{'Reachable' if local_ok else 'Not reachable'}")
            if not local_ok and node.fallback_folder:
                results.append(
                    f"  Fallback: {node.fallback_folder}")

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

    # ── Service Start / Stop (per source) ─────────────────────────────────

    def _on_start_service(self, remote_key: str, params: dict):
        """Start the download service for one source PACS."""
        if remote_key not in self.config.remote_nodes:
            return

        dashboard = self.dashboards.get(remote_key)
        if not dashboard:
            return

        # Ensure per-source SCP if local PACS is not reachable
        self._ensure_storage_scp_for(remote_key)

        # Create engine for this source
        engine = TransferEngine(self.config, remote_key)
        self.engines[remote_key] = engine
        self._connect_engine_signals(remote_key, engine, dashboard)

        dashboard.reset()
        dashboard.set_service_running(True)
        self.statusBar().showMessage(
            f"Service started: {remote_key}")

        engine.start(
            hours=params["hours"],
            max_images=params["max_images"],
            sync_interval=params["sync_interval"],
            selection_mode=params.get("selection_mode", False),
        )

    def _on_stop_service(self, remote_key: str):
        """Stop the download service for one source PACS."""
        engine = self.engines.get(remote_key)
        if engine and engine.is_running:
            engine.stop()
            self._log(f"Stopping service: {remote_key}...")

    def _on_service_stopped(self, remote_key: str):
        """Called when an engine thread has actually stopped."""
        dashboard = self.dashboards.get(remote_key)
        if dashboard:
            dashboard.set_service_running(False)
        self.statusBar().showMessage(f"Service stopped: {remote_key}")

    # ── Per-source Storage SCP ────────────────────────────────────────────

    def _ensure_storage_scp_for(self, remote_key: str):
        """Start a built-in SCP for this source if its local PACS is
        not reachable and a fallback folder is configured."""
        node = self.config.remote_nodes.get(remote_key)
        if not node:
            return

        scp_key = (node.local_ae_title, node.local_port)

        # Already running for this AE/port combo?
        if scp_key in self.storage_scps and self.storage_scps[scp_key].running:
            return

        # Quick echo test against this source's local PACS
        local_config = self.config.get_local_dict_for(remote_key)
        ops = DicomOperations(local_config, node.to_dict(), remote_key)
        local_reachable = ops.c_echo(target='local')

        if not local_reachable:
            fallback = node.fallback_folder
            if fallback:
                import os
                # Use a per-source subdirectory under the fallback folder
                storage_path = os.path.join(fallback, remote_key)
                self._log(
                    f"Local PACS [{node.local_ae_title}:{node.local_port}] "
                    f"not reachable for {remote_key}. "
                    f"Starting built-in SCP — saving to: {storage_path}")
                scp = StorageSCP(
                    node.local_ae_title,
                    node.local_port,
                    storage_path,
                )
                scp.start()
                self.storage_scps[scp_key] = scp
            else:
                self._log(
                    f"Local PACS [{node.local_ae_title}:{node.local_port}] "
                    f"not reachable for {remote_key}. "
                    f"No fallback folder configured.")

    # ── Engine signal wiring ──────────────────────────────────────────────

    def _connect_engine_signals(self, remote_key: str,
                                engine: TransferEngine,
                                dashboard: SourceDashboard):
        e = engine
        # Dashboard updates
        e.signals.queue_updated.connect(dashboard.on_queue_updated)
        e.signals.series_started.connect(dashboard.on_series_started)
        e.signals.stats_updated.connect(dashboard.on_stats_updated)
        e.signals.cycle_started.connect(dashboard.on_cycle_started)
        e.signals.cycle_finished.connect(dashboard.on_cycle_finished)
        # Service lifecycle — use a lambda to pass the remote_key
        e.signals.service_stopped.connect(
            lambda rk=remote_key: self._on_service_stopped(rk))
        # Manual series selection
        e.signals.queue_ready_for_selection.connect(
            dashboard.on_queue_ready_for_selection)
        dashboard.selection_confirmed.connect(
            lambda rk, uids, eng=engine: eng.confirm_selection(uids))
        # Log
        e.signals.log_message.connect(self._log)
        e.signals.unknown_institution.connect(
            self._on_unknown_institution)

    # ── Unknown institution handling ──────────────────────────────────────

    def _on_unknown_institution(self, institution_name: str):
        """Show popup when an unknown institution is encountered."""
        popup = UnknownInstitutionPopup(
            institution_name,
            self.config.filter_group_names,
            self,
        )
        if popup.exec() == UnknownInstitutionPopup.Accepted:
            if popup.assigned_group:
                self.config.institution_assignments[
                    institution_name] = popup.assigned_group
                self.config.save()
                self._log(
                    f"Assigned \"{institution_name}\" "
                    f"to group \"{popup.assigned_group}\".")
            else:
                # Still register it as known but unassigned
                if institution_name not in self.config.institution_assignments:
                    self.config.institution_assignments[
                        institution_name] = ""
                    self.config.save()

    # ── Window close ──────────────────────────────────────────────────────

    def closeEvent(self, event):
        any_running = any(
            e.is_running for e in self.engines.values())
        if any_running:
            reply = QMessageBox.question(
                self, "Quit",
                "One or more download services are still running. "
                "Quit anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                event.ignore()
                return
            for engine in self.engines.values():
                engine.stop()

        for scp in self.storage_scps.values():
            if scp.running:
                scp.stop()

        self.log_window.close()
        event.accept()
