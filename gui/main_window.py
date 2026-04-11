"""
Main window for DICOM Sync GUI.
Per-source architecture: each configured source PACS gets its own tab
with an independent download service, queue, and statistics.
"""

import logging
import os
import threading
from datetime import datetime
from typing import Dict, Optional, Tuple

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QMessageBox,
    QTabWidget, QLabel, QApplication,
)
from PySide6.QtCore import Qt, Signal
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
from gui.transfer_stats_window import TransferStatsWindow
from gui.examination_lookup import ExaminationLookupDialog
from gui.live_completions import LiveCompletionsWindow

logger = logging.getLogger("dicom_sync")


class MainWindow(QMainWindow):
    """Main application window — per-source tabs, fully automatic."""

    _echo_results_ready = Signal(list)
    _scp_check_done = Signal(str, bool, dict)

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        # Per-source SCPs keyed by (ae_title, port) tuple
        self.storage_scps: Dict[Tuple[str, int], StorageSCP] = {}
        # Per-source engines and dashboards
        self.engines: Dict[str, TransferEngine] = {}
        self.dashboards: Dict[str, SourceDashboard] = {}
        # Pending start params keyed by remote_key (supports concurrent starts)
        self._pending_start_params: Dict[str, dict] = {}

        self.setWindowTitle("DICOM Sync")
        self.setMinimumSize(1000, 750)
        self.resize(1100, 850)

        self._setup_menu()
        self._setup_ui()
        self._setup_statusbar()
        self._echo_results_ready.connect(self._on_echo_results)
        self._scp_check_done.connect(self._on_scp_check_done)

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

        stats_action = QAction("Transfer Performance Statistics...", self)
        stats_action.setShortcut("Ctrl+T")
        stats_action.triggered.connect(self._open_transfer_stats)
        view_menu.addAction(stats_action)

        completions_action = QAction("Download Completions", self)
        completions_action.triggered.connect(self._show_completions_window)
        view_menu.addAction(completions_action)

        tools_menu = menubar.addMenu("Tools")
        self._echo_action = QAction("C-ECHO Test...", self)
        self._echo_action.triggered.connect(self._test_echo)
        tools_menu.addAction(self._echo_action)

        lookup_action = QAction("Examination Lookup...", self)
        lookup_action.triggered.connect(self._open_examination_lookup)
        tools_menu.addAction(lookup_action)

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
        self.completions_window = LiveCompletionsWindow(
            self, language=getattr(self.config, "language", "en"))

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

        self._echo_action.setEnabled(False)
        self.statusBar().showMessage("C-ECHO test running...")

        def run_echo():
            results = []
            for key, node in self.config.remote_nodes.items():
                local_config = self.config.get_local_dict_for(key)
                ops = DicomOperations(local_config, node.to_dict(), key)
                ok = ops.c_echo(target='remote')
                results.append(
                    f"  {key} ({node.name}): "
                    f"{'Reachable' if ok else 'Not reachable'}")

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

            self._echo_results_ready.emit(results)

        threading.Thread(target=run_echo, daemon=True).start()

    def _on_echo_results(self, results: list):
        self._echo_action.setEnabled(True)
        QMessageBox.information(
            self, "C-ECHO Results", "Results:\n" + "\n".join(results))
        self.statusBar().showMessage("Ready")

    # ── Log ───────────────────────────────────────────────────────────────

    def _open_transfer_stats(self):
        from core.transfer_log import default_db_path
        if not hasattr(self, '_stats_window') or self._stats_window is None:
            self._stats_window = TransferStatsWindow(default_db_path(), self)
        self._stats_window.show()
        self._stats_window.raise_()
        self._stats_window.activateWindow()

    def _open_examination_lookup(self):
        from core.transfer_log import default_db_path
        dlg = ExaminationLookupDialog(default_db_path(), self)
        dlg.exec()

    def _show_completions_window(self):
        self.completions_window.show()
        self.completions_window.raise_()
        self.completions_window.activateWindow()

    def _show_log_window(self):
        self.log_window.show()
        self.log_window.raise_()
        self.log_window.activateWindow()

    def _on_study_completed_live(self, engine, study_uid: str,
                                   fully_complete: bool):
        """Add a completion entry to the live completions window."""
        if not fully_complete:
            return
        study_jobs = [j for j in engine._queue
                       if j.study_uid == study_uid and j.status == "done"]
        if not study_jobs:
            return
        first = study_jobs[0]
        wall_clock = engine._study_wall_clock.pop(study_uid, None)
        self.completions_window.add_completion(
            patient_name=first.patient_name,
            study_description=first.study_description,
            study_time=first.study_time,
            completed_time=datetime.now().strftime("%H:%M:%S"),
            institution_name=first.institution_name,
            download_duration_seconds=wall_clock,
        )

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

        # Check local PACS reachability in a background thread,
        # then start the engine once the check completes.
        self._pending_start_params[remote_key] = params
        self._ensure_storage_scp_for(remote_key)

    def _start_engine(self, remote_key: str, params: dict):
        """Actually create and start the engine (called on main thread)."""
        dashboard = self.dashboards.get(remote_key)
        if not dashboard:
            return

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
            # No node — skip SCP check, start engine directly
            params = self._pending_start_params.pop(remote_key, {})
            self._start_engine(remote_key, params)
            return

        scp_key = (node.local_ae_title, node.local_port)

        # Already running for this AE/port combo?
        if scp_key in self.storage_scps and self.storage_scps[scp_key].running:
            params = self._pending_start_params.pop(remote_key, {})
            self._start_engine(remote_key, params)
            return

        # Run echo test in background thread to avoid blocking the UI
        # and causing GC/threading conflicts with PySide6.
        def check_local():
            local_config = self.config.get_local_dict_for(remote_key)
            ops = DicomOperations(local_config, node.to_dict(), remote_key)
            reachable = ops.c_echo(target='local')
            self._scp_check_done.emit(
                remote_key, reachable, node.to_dict())

        threading.Thread(target=check_local, daemon=True).start()

    def _on_scp_check_done(self, remote_key: str, local_reachable: bool,
                           node_dict: dict):
        """Handle SCP check result on the main thread."""
        node = self.config.remote_nodes.get(remote_key)
        if not node:
            return

        if not local_reachable:
            fallback = node.fallback_folder
            if fallback:
                storage_path = os.path.join(fallback, remote_key)
                self._log(
                    f"Local PACS [{node.local_ae_title}:{node.local_port}] "
                    f"not reachable for {remote_key}. "
                    f"Starting built-in SCP — saving to: {storage_path}")
                scp_key = (node.local_ae_title, node.local_port)
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

        # Now start the engine
        params = self._pending_start_params.pop(remote_key, {})
        self._start_engine(remote_key, params)

    # ── Engine signal wiring ──────────────────────────────────────────────

    def _connect_engine_signals(self, remote_key: str,
                                engine: TransferEngine,
                                dashboard: SourceDashboard):
        e = engine
        # Dashboard updates
        e.signals.queue_updated.connect(dashboard.on_queue_updated)
        e.signals.studies_queried.connect(dashboard.on_studies_queried)
        e.signals.study_completed.connect(
            dashboard.on_study_completed)
        e.signals.study_completed.connect(
            lambda uid, inst, full, eng=engine:
                self._on_study_completed_live(eng, uid, full))
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
            # Wait for the in-flight C-MOVE to finish so the SQLite
            # transfer log reflects what actually arrived locally.
            # Daemon threads would otherwise be killed mid-stream.
            self.statusBar().showMessage(
                "Waiting for current downloads to finish...")
            QApplication.processEvents()
            for engine in self.engines.values():
                thread = getattr(engine, "_thread", None)
                if thread is not None and thread.is_alive():
                    thread.join(timeout=30)

        for scp in self.storage_scps.values():
            if scp.running:
                scp.stop()

        self.log_window.close()
        event.accept()
