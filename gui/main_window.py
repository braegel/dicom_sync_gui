"""
Main window for DICOM Sync GUI.
Per-source architecture: each configured source PACS gets its own tab
with an independent download service, queue, and statistics.

Threading model
---------------
The GUI runs on Qt's main thread.  Three categories of cross-thread
work flow into ``MainWindow``:

1. **TransferEngine signals** — each per-source ``TransferEngine``
   emits its progress/log/completion signals from its own service-
   loop thread.  ``Qt.AutoConnection`` marshals them onto the GUI
   thread before any slot in this file runs.  Slots may touch
   widgets freely.

2. **Built-in StorageSCP signals** — pynetdicom reactor thread, same
   marshalling rule.

3. **Direct calls into** ``MainWindow._log(msg)`` — happen from
   either the GUI thread (other handlers in this file) or background
   workers (e.g. ``_test_echo``'s daemon thread).  ``_log`` therefore
   forwards through the ``_log_message`` signal, whose
   ``_append_log`` slot is the only place that actually touches the
   ``LogWindow``.  No direct widget access from non-GUI threads.

Background work the file launches itself (``_test_echo``,
``_ensure_storage_scp_for``) uses plain ``threading.Thread(daemon=True)``
plus a private signal (``_echo_results_ready`` / ``_scp_check_done``)
to hop back to the GUI thread with the result.
"""

import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QMessageBox,
    QTabWidget, QLabel, QApplication,
)
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QFont
from PySide6.QtMultimedia import QSoundEffect

from core.config import AppConfig, PacsNode, local_ip_for
from core.dicom_ops import DicomOperations, TRANSFER_SYNTAXES
from core.storage_scp import StorageSCP
from core.transfer_engine import TransferEngine, TERMINAL_STATUSES
from core.transfer_log import TransferLog, default_db_path
from gui.settings_dialog import SettingsDialog
from gui.dashboard import SourceDashboard
from gui.log_window import LogWindow
from gui.filter_groups_dialog import FilterGroupsDialog
from gui.priority_series_dialog import PrioritySeriesDialog
from gui.unknown_institution_popup import UnknownInstitutionPopup
from gui.transfer_stats_window import TransferStatsWindow
from gui.examination_lookup import ExaminationLookupDialog
from gui.live_completions import LiveCompletionsWindow
from core.i18n import tr

logger = logging.getLogger("dicom_sync")

# Studies whose total downloaded image count is below this threshold
# are not added to the Download Completions window — they're too small
# to be meaningful exam records.
MIN_IMAGES_FOR_COMPLETIONS_ENTRY = 10

# When a watchdog-triggered auto-restart finds the source PACS
# unreachable, retry the reachability probe this often (ms) — sounding
# the siren each time — until the PACS answers and the engine starts.
_AUTO_RESTART_RETRY_MS = 5_000


class MainWindow(QMainWindow):
    """Main application window — per-source tabs, fully automatic."""

    _echo_results_ready = Signal(list)
    # remote_key, remote_reachable, local_reachable, node_dict
    _scp_check_done = Signal(str, bool, bool, dict)
    _log_message = Signal(str)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        # Per-source SCPs keyed by (ae_title, port) tuple
        self.storage_scps: Dict[Tuple[str, int], StorageSCP] = {}
        # Per-source engines and dashboards
        self.engines: Dict[str, TransferEngine] = {}
        self.dashboards: Dict[str, SourceDashboard] = {}
        # Pending start params keyed by remote_key (supports concurrent starts)
        self._pending_start_params: Dict[str, dict] = {}
        # Single shared TransferLog so per-instance write locks actually
        # serialize writers across sources (separate instances would each
        # carry their own lock and could interleave commits).
        self._transfer_log = TransferLog(default_db_path())
        self._stats_window: Optional[TransferStatsWindow] = None
        # One open (non-modal) unknown-institution popup per institution
        # name.  Two sources can emit the same institution; the dict
        # dedupes them — the second emit raises the existing popup
        # instead of stacking a duplicate.  Entries are removed in the
        # popup's ``finished`` handler.
        self._open_institution_popups: Dict[str, UnknownInstitutionPopup] = {}
        # Non-modal warning dialogs currently on screen, keyed by a
        # caller-chosen dedupe token (see _show_nonmodal_warning).  A
        # second signal for the same token raises the existing dialog
        # instead of stacking a duplicate.
        self._open_warnings: Dict[str, QMessageBox] = {}
        # remote_key → QTimer driving the auto-restart reachability
        # retry loop: when a watchdog-triggered restart hits an
        # unreachable PACS, we keep probing every
        # _AUTO_RESTART_RETRY_MS (with the siren alarm) instead of
        # aborting, until the PACS answers.
        self._auto_restart_retry_timers: Dict[str, QTimer] = {}
        # Lazy, single long-lived QSoundEffect for the recurring siren.
        self._siren_effect: Optional[QSoundEffect] = None

        self.setWindowTitle("DICOM Sync")
        self.setMinimumSize(1000, 750)
        self.resize(1100, 850)

        self._setup_menu()
        self._setup_ui()
        self._setup_statusbar()
        self._echo_results_ready.connect(self._on_echo_results)
        self._scp_check_done.connect(self._on_scp_check_done)
        # Queue log lines through this signal so direct callers from
        # any thread are marshalled onto the GUI thread.  ``Qt.AutoConnection``
        # already picks queued semantics for cross-thread emit, but
        # pin it explicitly so a future receiver move can't silently
        # turn this into a direct (=racy) call.
        self._log_message.connect(self._append_log, Qt.QueuedConnection)

        # Pre-generate notification WAVs after the event loop is up so
        # the first study completion doesn't pay the ~30k-sample DSP
        # cost synchronously on the GUI thread.
        QTimer.singleShot(0, self._prewarm_sounds)

    @staticmethod
    def _prewarm_sounds() -> None:
        try:
            from gui.notification_sound import (
                _generate_default_sound, _generate_siren_sound)
            _generate_default_sound(sad=False)
            _generate_default_sound(sad=True)
            _generate_siren_sound()
        except Exception as e:
            logger.debug(f"Notification prewarm failed: {e}")

    def _play_siren(self) -> None:
        """Play the recurring PACS-unreachable siren via a single
        long-lived QSoundEffect (lazy-created on first use)."""
        try:
            from gui.notification_sound import _generate_siren_sound
            path = _generate_siren_sound()
            if self._siren_effect is None:
                self._siren_effect = QSoundEffect(self)
            self._siren_effect.stop()
            self._siren_effect.setSource(QUrl.fromLocalFile(path))
            self._siren_effect.play()
        except Exception as e:
            logger.debug(f"Siren playback failed: {e}")

    # ── Menu ──────────────────────────────────────────────────────────────

    def _setup_menu(self) -> None:
        menubar = self.menuBar()

        settings_menu = menubar.addMenu("Settings")
        settings_action = QAction("PACS Configuration...", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._open_settings)
        settings_menu.addAction(settings_action)

        filter_action = QAction("Manage Filter Groups...", self)
        filter_action.setShortcut("Ctrl+Shift+F")
        filter_action.triggered.connect(self._open_filter_groups)
        settings_menu.addAction(filter_action)

        priority_action = QAction("Manage Priority Series...", self)
        priority_action.setShortcut("Ctrl+Shift+P")
        priority_action.triggered.connect(self._open_priority_series)
        settings_menu.addAction(priority_action)

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

    def _setup_ui(self) -> None:
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

    def _rebuild_tabs(self) -> None:
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

    def _setup_statusbar(self) -> None:
        self.statusBar().showMessage("Ready")

    # ── Settings ──────────────────────────────────────────────────────────

    def _open_settings(self) -> None:
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
            # Propagate a possibly-changed UI language to the live
            # completions window so its Copy buttons use the new wording.
            self.completions_window.set_language(
                getattr(self.config, "language", "en"))
            self._log("Settings saved.")

    def _open_filter_groups(self) -> None:
        dlg = FilterGroupsDialog(self.config, self)
        if dlg.exec() == FilterGroupsDialog.Accepted:
            # Refresh filter dropdowns in all dashboards
            for dashboard in self.dashboards.values():
                dashboard.refresh_filter_groups()
            self._log("Filter groups updated.")

    def _open_priority_series(self) -> None:
        dlg = PrioritySeriesDialog(self.config, self)
        if dlg.exec() == PrioritySeriesDialog.Accepted:
            self._log("Priority series terms updated.")
        else:
            self._log("Priority series dialog cancelled (no changes).")

    # ── C-ECHO ────────────────────────────────────────────────────────────

    def _test_echo(self) -> None:
        if not self.config.remote_nodes:
            QMessageBox.warning(
                self, "Warning",
                "No source PACS configured. Open Settings first.")
            return

        self._echo_action.setEnabled(False)
        self.statusBar().showMessage("C-ECHO test running...")

        threading.Thread(target=self._run_echo_thread, daemon=True).start()

    def _echo_one(self, key: str, node: PacsNode, target: str) -> bool:
        local_config = self.config.get_local_dict_for(key)
        ops = DicomOperations(local_config, node.to_dict(), key)
        # ``finally`` so the AE's threads are shut down before the
        # object is dropped — see DicomOperations.close().
        try:
            return ops.c_echo(target=target)
        finally:
            ops.close()

    def _run_echo_thread(self) -> None:
        """Run all C-ECHO probes on a daemon thread, then marshal the
        formatted results back to the GUI thread via the
        ``_echo_results_ready`` signal."""
        results = []
        for key, node in self.config.remote_nodes.items():
            ok = self._echo_one(key, node, target='remote')
            results.append(
                f"  {key} ({node.name}): "
                f"{'Reachable' if ok else 'Not reachable'}")

        tested_locals = set()
        for key, node in self.config.remote_nodes.items():
            local_key = (node.local_ae_title, node.local_port)
            if local_key in tested_locals:
                continue
            tested_locals.add(local_key)
            local_ok = self._echo_one(key, node, target='local')
            results.append(
                f"\n  Local [{node.local_ae_title}:{node.local_port}]: "
                f"{'Reachable' if local_ok else 'Not reachable'}")
            if not local_ok and node.fallback_folder:
                results.append(
                    f"  Fallback: {node.fallback_folder}")

        self._echo_results_ready.emit(results)

    def _on_echo_results(self, results: list) -> None:
        self._echo_action.setEnabled(True)
        QMessageBox.information(
            self, "C-ECHO Results", "Results:\n" + "\n".join(results))
        self.statusBar().showMessage("Ready")

    # ── Log ───────────────────────────────────────────────────────────────

    def _open_transfer_stats(self) -> None:
        if self._stats_window is None:
            self._stats_window = TransferStatsWindow(default_db_path(), self)
        self._stats_window.show()
        self._stats_window.raise_()
        self._stats_window.activateWindow()

    def _open_examination_lookup(self) -> None:
        dlg = ExaminationLookupDialog(default_db_path(), self)
        dlg.exec()

    def _show_completions_window(self) -> None:
        self.completions_window.show()
        self.completions_window.raise_()
        self.completions_window.activateWindow()

    def _show_log_window(self) -> None:
        self.log_window.show()
        self.log_window.raise_()
        self.log_window.activateWindow()

    def _update_completions_progress(self) -> None:
        """Aggregate pending images and transfer rate across all
        running engines and push the ETE to the completions window."""
        total_pending = 0
        total_ipm = 0.0
        for engine in self.engines.values():
            if not engine.is_running:
                continue
            for job in engine.queue_snapshot():
                if job.status not in TERMINAL_STATUSES:
                    total_pending += job.to_transfer
            total_ipm += engine.stats.raw_images_per_minute()
        self.completions_window.update_transfer_progress(
            total_pending, total_ipm)

    def _on_study_completed_live(self, engine: TransferEngine, study_uid: str,
                                 fully_complete: bool, total_images: int,
                                 source: str = "") -> None:
        """Add a completion entry to the live completions window.

        *source* is the remote_key of the source PACS the engine
        serves; the completions window separates its delay / duration
        statistics (colour bands, median readout) by it.

        *total_images* comes straight from the ``study_completed``
        signal — the engine already summed it when it decided the study
        was complete, so re-deriving it here could only ever disagree
        with the count the engine acted on.  The queue snapshot is still
        needed for the study's descriptive fields (and as the "did
        anything actually finish" guard).

        Threshold filtering (``MIN_IMAGES_FOR_COMPLETIONS_ENTRY``) is
        applied inside ``add_completion`` against the *cumulative*
        image count of any pre-existing row for this study, so a
        first-wave emit below the threshold does not erase a later
        emit that crosses it.
        """
        if not fully_complete:
            return
        study_jobs = [j for j in engine.queue_snapshot()
                      if j.study_uid == study_uid and j.status == "done"]
        if not study_jobs:
            return
        first = study_jobs[0]
        wall_clock = engine.pop_study_wall_clock(study_uid)
        now = datetime.now()
        self.completions_window.add_completion(
            study_uid=study_uid,
            patient_name=first.patient_name,
            study_description=first.study_description,
            study_time=first.study_time,
            study_date=first.study_date,
            completed_time=now.strftime("%H:%M:%S"),
            completed_date=now.strftime("%Y%m%d"),
            institution_name=first.institution_name,
            download_duration_seconds=wall_clock,
            image_count=total_images,
            min_images_threshold=MIN_IMAGES_FOR_COMPLETIONS_ENTRY,
            cumulative=True,
            source=source,
        )

    def _log(self, msg: str) -> None:
        """Log a message.  Safe to call from any thread — routed via
        the ``_log_message`` signal so the GUI work happens on the GUI
        thread.  Does NOT touch the status bar; lifecycle handlers
        update it explicitly."""
        self._log_message.emit(msg)

    def _append_log(self, msg: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_window.append_log(f"[{timestamp}] {msg}")

    # ── Service Start / Stop (per source) ─────────────────────────────────

    def _on_start_service(self, remote_key: str, params: dict) -> None:
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

    def _start_engine(self, remote_key: str, params: dict) -> None:
        """Actually create and start the engine (called on main thread)."""
        dashboard = self.dashboards.get(remote_key)
        if not dashboard:
            return

        engine = TransferEngine(
            self.config, remote_key, transfer_log=self._transfer_log)
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

    def _on_stop_service(self, remote_key: str) -> None:
        """Stop the download service for one source PACS."""
        # Abort any in-flight auto-restart retry loop: during it the
        # engine is already stopped, so the user pressing Stop must end
        # the retry/siren too (and drop its queued start params).
        was_retrying = remote_key in self._auto_restart_retry_timers
        self._stop_auto_restart_retry(remote_key)
        self._pending_start_params.pop(remote_key, None)
        engine = self.engines.get(remote_key)
        if engine and engine.is_running:
            engine.stop()
            self._log(f"Stopping service: {remote_key}...")
        elif was_retrying:
            # No live engine — the Stop ended a retry loop only.  Put the
            # dashboard back to its idle state (no service_stopped signal
            # will arrive to do it for us).
            self._log(
                f"Auto-restart for {remote_key} cancelled by user.")
            dashboard = self.dashboards.get(remote_key)
            if dashboard:
                dashboard.set_service_running(False)

    def _on_service_stopped(self, remote_key: str,
                            engine: Optional[TransferEngine] = None) -> None:
        """Called when an engine thread has actually stopped.

        Prunes the stopped engine from ``self.engines`` so stale
        ``TransferEngine`` objects (with their connected signal
        lambdas) don't accumulate over start/stop/restart cycles.

        The prune must happen BEFORE ``dashboard.set_service_running(False)``:
        the dashboard's restart flow re-emits ``start_requested`` from
        inside that call, which (re-)registers a fresh engine under the
        same key — popping afterwards could evict the new engine.

        Pruning is safe for the rest of the file: a pruned engine is by
        definition not running, so ``closeEvent``'s ``any_running``
        check and its join loop never needed it, and
        ``_update_completions_progress`` skips non-running engines
        anyway.
        """
        current = self.engines.get(remote_key)
        # Remove IF AND ONLY IF the registered engine is the one that
        # stopped and it has actually wound down.  ``engine is None``
        # (no emitter known — e.g. direct calls) falls back to pruning
        # whatever non-running engine is registered.
        if current is not None and not current.is_running and (
                engine is None or engine is current):
            self.engines.pop(remote_key, None)

        dashboard = self.dashboards.get(remote_key)
        if dashboard:
            dashboard.set_service_running(False)
        self.statusBar().showMessage(f"Service stopped: {remote_key}")
        self._update_completions_progress()

    def _show_nonmodal_warning(self, dedupe_key: str, title: str,
                               text: str) -> None:
        """Show a NON-modal warning dialog, at most one per *dedupe_key*.

        Never use the static ``QMessageBox.warning`` from a slot that
        engines emit into: it spins a nested event loop inside the slot,
        so every further queued signal (queue updates, progress, other
        sources' completions) is processed re-entrantly while the user
        stares at the dialog, and a second warning stacks another nested
        loop behind the first.  ``show()`` returns immediately; the
        dialog cleans itself up in ``_on_warning_closed``.

        Same pattern as ``_on_unknown_institution`` — a repeat call for a
        key that is already on screen just raises the existing dialog.
        """
        existing = self._open_warnings.get(dedupe_key)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle(title)
        msg.setText(text)
        msg.setStandardButtons(QMessageBox.Ok)
        # Explicitly non-modal so it can never block the event loop.
        msg.setWindowModality(Qt.NonModal)
        msg.setModal(False)
        msg.finished.connect(
            lambda _result, k=dedupe_key: self._on_warning_closed(k))
        self._open_warnings[dedupe_key] = msg
        msg.show()

    def _on_warning_closed(self, dedupe_key: str) -> None:
        """Free the dedupe slot once the user dismisses the dialog, so a
        later outage can warn again."""
        msg = self._open_warnings.pop(dedupe_key, None)
        if msg is not None:
            msg.deleteLater()

    def _on_connection_lost(self, remote_key: str, detail: str) -> None:
        """A running engine reported the PACS became unreachable during
        a query or download.  Tell the user once per outage; the engine
        keeps retrying and recovers on its own when the PACS returns.

        The engine already latches one emit per outage; the dedupe key
        additionally prevents a second dialog for the same source while
        the first is still on screen."""
        node = self.config.remote_nodes.get(remote_key)
        name = node.name if node and node.name else remote_key
        self._log(
            f"Connection to PACS lost for {remote_key}: {detail}")
        lang = getattr(self.config, "language", "en")
        self._show_nonmodal_warning(
            f"connection_lost:{remote_key}",
            tr("pacs_connection_lost_title", lang),
            tr("pacs_connection_lost_msg", lang, name=name))

    def _on_connection_restored(self, remote_key: str) -> None:
        """The engine re-established contact after a connection_lost.
        Just log it — the service kept running throughout."""
        self._log(f"Connection to PACS restored for {remote_key}.")

    # ── Per-source Storage SCP ────────────────────────────────────────────

    def _ensure_storage_scp_for(self, remote_key: str) -> None:
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
        # and causing GC/threading conflicts with PySide6.  Probe the
        # source PACS first: if it's down there's no point starting the
        # engine (and with the connection timeout this check itself now
        # fails fast instead of hanging).  Only bother with the local
        # probe when the remote is up, since we abort either way if it
        # isn't.
        def check_reachability():
            local_config = self.config.get_local_dict_for(remote_key)
            ops = DicomOperations(local_config, node.to_dict(), remote_key)
            try:
                remote_reachable = ops.c_echo(target='remote')
                local_reachable = (ops.c_echo(target='local')
                                   if remote_reachable else False)
            finally:
                ops.close()
            self._scp_check_done.emit(
                remote_key, remote_reachable, local_reachable,
                node.to_dict())

        threading.Thread(target=check_reachability, daemon=True).start()

    def _on_scp_check_done(self, remote_key: str, remote_reachable: bool,
                           local_reachable: bool, node_dict: dict) -> None:
        """Handle reachability check result on the main thread.

        Short orchestrator: guards against a removed node, dispatches the
        unreachable-remote and local-unreachable-fallback branches to
        their helpers, then brings the engine up once the path is clear.
        """
        node = self.config.remote_nodes.get(remote_key)
        if not node:
            # Source removed mid-flight — drop the queued start params
            # so they don't leak (and don't get accidentally consumed by
            # a future same-keyed re-add).
            self._pending_start_params.pop(remote_key, None)
            return

        if not remote_reachable:
            self._handle_unreachable_remote(remote_key, node)
            return

        # Three ways the receiving side can be arranged, in priority
        # order:
        #   1. the node explicitly wants the built-in SCP to receive
        #      (``receive_with_builtin_scp``) -- regardless of whether
        #      the local PACS answers, because the problem it solves is
        #      a local PACS that answers fine but then rejects this
        #      source's images;
        #   2. the local PACS is unreachable -> classic fallback SCP;
        #   3. nothing to do, C-MOVE goes straight to the local PACS.
        if node.receive_with_builtin_scp:
            scp_ready = self._ensure_builtin_receiver(remote_key, node)
        elif not local_reachable:
            scp_ready = self._ensure_fallback_scp(remote_key, node)
        else:
            scp_ready = True

        if not scp_ready:
            # SCP startup failed (e.g. the local port is still
            # transiently bound during the restart window).  A watchdog
            # auto-restart must not die silently on "Stopped" — leave the
            # start queued and retry with the siren, mirroring the
            # unreachable-remote path.  This branch owns the pending
            # params outright: ``_ensure_fallback_scp`` no longer touches
            # them, so there is nothing to snapshot and restore.
            if self._pending_start_params.get(
                    remote_key, {}).get("auto_restart"):
                self._log(
                    f"Built-in SCP for {remote_key} not ready — "
                    f"auto-restart retrying in "
                    f"{_AUTO_RESTART_RETRY_MS // 1000}s.")
                self._play_siren()
                self._show_awaiting_pacs(remote_key)
                self._schedule_auto_restart_retry(remote_key)
            else:
                self._abort_start_scp_failed(remote_key, node)
            return

        # PACS answered — cancel any pending auto-restart retry loop and
        # silence the siren before bringing the engine up.
        self._stop_auto_restart_retry(remote_key)
        # Now start the engine
        params = self._pending_start_params.pop(remote_key, {})
        self._start_engine(remote_key, params)

    def _abort_start_scp_failed(self, remote_key: str,
                                node: PacsNode) -> None:
        """A manual start whose built-in receiver could not bind.

        Mirrors ``_handle_unreachable_remote``'s manual-start branch:
        drop the queued start, tell the user, and put the dashboard back
        to "Stopped".  Without the reset the dashboard kept showing
        "Starting service…" indefinitely for a service that was never
        going to start, with the only evidence in the log window — the
        UI reported a state that was simply not true.
        """
        self._pending_start_params.pop(remote_key, None)
        self._log(
            f"Service for {remote_key} not started — the built-in "
            f"Storage SCP could not bind on port {node.local_port}.")
        self._show_nonmodal_warning(
            f"scp_bind_failed:{remote_key}",
            tr("scp_bind_failed_title",
               getattr(self.config, "language", "en")),
            tr("scp_bind_failed_msg",
               getattr(self.config, "language", "en"),
               name=node.name or remote_key, port=node.local_port))
        dashboard = self.dashboards.get(remote_key)
        if dashboard:
            dashboard.set_service_running(False)

    def _handle_unreachable_remote(self, remote_key: str,
                                   node: PacsNode) -> None:
        """The source PACS did not answer the reachability probe.

        Auto-restart (watchdog) starts keep retrying with the siren;
        manual starts abort with a warning dialog and reset the
        dashboard."""
        # Auto-restart (watchdog) path: the PACS is often just slow,
        # which is exactly what triggered the restart.  Don't abort —
        # keep probing every _AUTO_RESTART_RETRY_MS, sounding the
        # siren each time, until it answers.
        if self._pending_start_params.get(
                remote_key, {}).get("auto_restart"):
            self._log(
                f"Source PACS [{node.ae_title}@{node.ip_address}:"
                f"{node.port}] still unreachable for {remote_key} — "
                f"auto-restart retrying in "
                f"{_AUTO_RESTART_RETRY_MS // 1000}s.")
            self._play_siren()
            self._show_awaiting_pacs(remote_key)
            self._schedule_auto_restart_retry(remote_key)
            return
        # Manual start: abort, tell the user, and leave the dashboard
        # back in its idle ("Stopped") state.
        self._pending_start_params.pop(remote_key, None)
        self._log(
            f"Source PACS [{node.ae_title}@{node.ip_address}:"
            f"{node.port}] not reachable for {remote_key}. "
            f"Service not started.")
        lang = getattr(self.config, "language", "en")
        # Non-modal: this runs in the ``_scp_check_done`` slot, and on a
        # multi-source setup the other engines keep emitting into the
        # GUI thread while the dialog is up.
        self._show_nonmodal_warning(
            f"pacs_unreachable:{remote_key}",
            tr("pacs_unreachable_title", lang),
            tr("pacs_unreachable_msg", lang,
               name=node.name or remote_key,
               ip=node.ip_address, port=node.port))
        dashboard = self.dashboards.get(remote_key)
        if dashboard:
            dashboard.set_service_running(False)

    def _ensure_fallback_scp(self, remote_key: str,
                             node: PacsNode) -> bool:
        """Local PACS is unreachable — bring up the built-in StorageSCP
        if a fallback folder is configured.

        Returns ``True`` when the caller may proceed to start the engine
        (SCP started, or no fallback configured), ``False`` when SCP
        startup failed and the engine must not be started.

        Deliberately does NOT touch ``_pending_start_params``.  It used
        to pop them on failure, which forced the caller to snapshot and
        restore the queued start before every call just to survive that
        side effect; the caller owns them now.
        """
        if not node.fallback_folder:
            self._log(
                f"Local PACS [{node.local_ae_title}:{node.local_port}] "
                f"not reachable for {remote_key}. "
                f"No fallback folder configured.")
            return True
        self._log(
            f"Local PACS [{node.local_ae_title}:{node.local_port}] "
            f"not reachable for {remote_key}. Starting built-in SCP.")
        # Wildcard bind: the local PACS is down, so there is nothing to
        # share the port with and images may arrive on any interface.
        return self._start_builtin_scp(remote_key, node, "0.0.0.0")

    def _ensure_builtin_receiver(self, remote_key: str,
                                 node: PacsNode) -> bool:
        """The node is configured to have the built-in SCP receive its
        C-MOVE images even though the local PACS is up.

        Binds the SCP to the interface address that reaches THIS source
        rather than the wildcard, which is what lets it coexist with the
        local PACS: a socket bound to a specific address wins over
        another process's wildcard bind on the same port, so images from
        this source land here while the local PACS keeps serving its own
        address -- including the engine's local C-FIND, which is how it
        knows what has already arrived.

        Returns ``True`` when the caller may start the engine.
        """
        if not node.fallback_folder:
            # Without a folder the received images would have nowhere to
            # go.  Refuse rather than start an SCP that drops everything.
            self._log(
                f"{remote_key}: built-in receiver requested but no "
                f"fallback folder configured — engine not started.")
            return False
        bind_address = local_ip_for(node.ip_address)
        self._log(
            f"{remote_key}: receiving C-MOVE images with the built-in "
            f"SCP on {bind_address}:{node.local_port} "
            f"[{node.local_ae_title}].")
        return self._start_builtin_scp(remote_key, node, bind_address)

    def _start_builtin_scp(self, remote_key: str, node: PacsNode,
                           bind_address: str) -> bool:
        """Create, wire up and start a built-in StorageSCP for *node*.

        Shared by the unreachable-local fallback and the explicit
        built-in-receiver path; they differ only in *bind_address* and
        in what they log beforehand.  Returns ``False`` when the bind
        failed, in which case the caller must not start the engine.
        """
        storage_path = os.path.join(node.fallback_folder, remote_key)
        scp_key = (node.local_ae_title, node.local_port)
        scp = StorageSCP(
            node.local_ae_title,
            node.local_port,
            storage_path,
            bind_address=bind_address,
            # The node's "Preferred Syntax" is the receiving side's
            # preference, so it only ever had a meaning for the built-in
            # SCP -- when the local PACS receives, that negotiation
            # happens on an association this app is not part of.
            # Unknown names fall through to None, i.e. the default order.
            preferred_syntax=TRANSFER_SYNTAXES.get(node.local_syntax),
        )
        # Surface built-in-SCP activity in the log — without this the
        # user has no feedback that images actually land in the folder.
        # Throttled in the handler (first image, then every 25th).  The
        # signal is emitted from the pynetdicom reactor thread;
        # Qt.AutoConnection queues the slot onto the GUI thread.
        scp.image_received.connect(
            lambda count, k=scp_key:
                self._on_fallback_image_received(k, count))
        try:
            scp.start()
        except RuntimeError as e:
            # Port already in use, permission denied, etc.  Report the
            # failure so the engine doesn't fire C-MOVEs at a dead local
            # SCP, and surface it in the log instead of leaking the
            # traceback to stderr.  Whether the queued start is dropped
            # or retried is the caller's call.
            self._log(
                f"Storage SCP startup failed for {remote_key}: "
                f"{e}. Engine not started.")
            return False
        self.storage_scps[scp_key] = scp
        self._log(f"Images for {remote_key} are saved to: {storage_path}")
        return True

    def _show_awaiting_pacs(self, remote_key: str) -> None:
        """Put the source's dashboard into the "waiting for the PACS"
        state while the auto-restart retry loop keeps probing.

        The wording goes through ``tr()`` like every other user-facing
        string here — it used to be a German literal, so an English UI
        showed German at exactly the moment something was wrong.
        """
        dashboard = self.dashboards.get(remote_key)
        if not dashboard:
            return
        dashboard.show_awaiting_pacs(tr(
            "pacs_retry_status", getattr(self.config, "language", "en"),
            seconds=_AUTO_RESTART_RETRY_MS // 1000))

    def _schedule_auto_restart_retry(self, remote_key: str) -> None:
        """Re-run the reachability probe for *remote_key* after
        _AUTO_RESTART_RETRY_MS.  The start params stay in
        ``_pending_start_params`` so the retry reuses them; the loop ends
        when the PACS becomes reachable (``_on_scp_check_done`` calls
        ``_stop_auto_restart_retry``) or the user stops the service."""
        # Don't retry a source the user already stopped / removed.
        if remote_key not in self._pending_start_params:
            return
        timer = self._auto_restart_retry_timers.get(remote_key)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda rk=remote_key: self._ensure_storage_scp_for(rk))
            self._auto_restart_retry_timers[remote_key] = timer
        timer.start(_AUTO_RESTART_RETRY_MS)

    def _stop_auto_restart_retry(self, remote_key: str) -> None:
        """Cancel the auto-restart retry timer for *remote_key* and stop
        the siren if no other source is still retrying."""
        timer = self._auto_restart_retry_timers.pop(remote_key, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        if not self._auto_restart_retry_timers and self._siren_effect:
            self._siren_effect.stop()

    def _on_fallback_image_received(self, scp_key: Tuple[str, int],
                                    count: int) -> None:
        """Log built-in SCP receive progress.  The SCP already throttles
        the signal (first image + every Nth), so every delivered *count*
        is worth a log line."""
        scp = self.storage_scps.get(scp_key)
        if scp is None:
            return
        self._log(
            f"Built-in SCP [{scp_key[0]}:{scp_key[1]}] received "
            f"{count} image(s) — saving to {scp.storage_path}")

    # ── Engine signal wiring ──────────────────────────────────────────────

    def _connect_engine_signals(self, remote_key: str,
                                engine: TransferEngine,
                                dashboard: SourceDashboard) -> None:
        e = engine
        # Dashboard updates
        e.signals.queue_updated.connect(dashboard.on_queue_updated)
        e.signals.studies_queried.connect(dashboard.on_studies_queried)
        e.signals.study_completed.connect(
            dashboard.on_study_completed)
        e.signals.study_completed.connect(
            lambda uid, inst, full, images, eng=engine, rk=remote_key:
                self._on_study_completed_live(
                    eng, uid, full, images, source=rk))
        e.signals.series_started.connect(dashboard.on_series_started)
        e.signals.series_progress.connect(dashboard.on_series_progress)
        e.signals.series_completed.connect(dashboard.on_series_completed)
        e.signals.series_error.connect(dashboard.on_series_error)
        e.signals.stats_updated.connect(dashboard.on_stats_updated)
        # Forward queue/stats to the completions-window ETE countdown
        e.signals.queue_updated.connect(
            lambda _q: self._update_completions_progress())
        e.signals.stats_updated.connect(
            lambda _s: self._update_completions_progress())
        e.signals.cycle_started.connect(dashboard.on_cycle_started)
        e.signals.cycle_finished.connect(dashboard.on_cycle_finished)
        # Service lifecycle — use a lambda to pass the remote_key AND
        # the emitting engine, so the stopped-handler can prune exactly
        # the engine that stopped (a stale engine's late signal must
        # never evict a newer engine registered under the same key).
        e.signals.service_stopped.connect(
            lambda rk=remote_key, eng=engine:
                self._on_service_stopped(rk, eng))
        # Manual series selection
        e.signals.queue_ready_for_selection.connect(
            dashboard.on_queue_ready_for_selection)
        dashboard.selection_confirmed.connect(
            lambda rk, uids, eng=engine: eng.confirm_selection(uids))
        # Log
        e.signals.log_message.connect(self._log)
        e.signals.unknown_institution.connect(
            self._on_unknown_institution)
        # Connection lost / restored during a running service
        e.signals.connection_lost.connect(self._on_connection_lost)
        e.signals.connection_lost.connect(dashboard.on_connection_lost)
        e.signals.connection_restored.connect(
            self._on_connection_restored)
        e.signals.connection_restored.connect(
            dashboard.on_connection_restored)

    # ── Unknown institution handling ──────────────────────────────────────

    def _on_unknown_institution(self, institution_name: str) -> None:
        """Show a NON-modal popup when an unknown institution is
        encountered.

        Non-modal on purpose: this slot runs on the GUI thread while
        engines keep emitting.  A modal ``exec()`` would spin a nested
        event loop inside the slot, and several unknown institutions in
        one cycle would stack nested modal dialogs.  ``show()`` returns
        immediately; the assignment is applied in the ``finished``
        handler instead.

        Each engine emits at most once per institution per run (it
        keeps a notified set), but two sources can emit the same name —
        ``_open_institution_popups`` dedupes: re-emits for an already
        open popup just bring it to the front."""
        existing = self._open_institution_popups.get(institution_name)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return

        popup = UnknownInstitutionPopup(
            institution_name,
            self.config.filter_group_names,
            self,
        )
        self._open_institution_popups[institution_name] = popup
        # Capture name + popup in the closure so the finished handler
        # can read ``popup.assigned_group`` BEFORE the dialog object is
        # deleted (deletion happens via deleteLater in the handler, not
        # WA_DeleteOnClose, exactly so this read stays valid).
        popup.finished.connect(
            lambda result, name=institution_name, dlg=popup:
                self._on_institution_popup_finished(name, dlg, result))
        popup.show()

    def _on_institution_popup_finished(self, institution_name: str,
                                       popup: UnknownInstitutionPopup,
                                       result: int) -> None:
        """Apply the unknown-institution popup's outcome once the user
        dismisses it (OK or window close).  Runs on the GUI thread via
        the dialog's ``finished`` signal."""
        # Free the dedupe slot first so a later emit for the same name
        # can open a fresh popup.
        self._open_institution_popups.pop(institution_name, None)

        if result == UnknownInstitutionPopup.Accepted:
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

        # Deletion only AFTER assigned_group has been read above.
        popup.deleteLater()

    # ── Window close ──────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:
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
            self._join_engines_responsive(total_timeout=30)

        self._teardown_resources()
        event.accept()

    def _teardown_resources(self) -> None:
        """Release everything the window owns, in dependency order.

        Split out of ``closeEvent`` because the two halves have different
        contracts: the engine wind-down up there can still ABORT the
        close (the user answers "No" to the quit prompt), while
        everything here runs only once the close is certain and must not
        be able to refuse it.
        """
        # Stop any auto-restart retry loops and silence the siren so a
        # pending QTimer can't fire into a half-torn-down window.
        for rk in list(self._auto_restart_retry_timers):
            self._stop_auto_restart_retry(rk)

        # Non-modal warnings are independent top-level windows; close
        # them explicitly so none is left floating after the main window
        # goes away.
        for key in list(self._open_warnings):
            msg = self._open_warnings.pop(key)
            msg.close()
            msg.deleteLater()

        for scp in self.storage_scps.values():
            if scp.running:
                scp.stop()

        # Flush any debounced config writes the dashboards have queued
        # so closing the app never drops in-flight settings changes.
        for dashboard in self.dashboards.values():
            dashboard.flush_pending_save()

        # The stats window keeps one SQLite connection for as long as we
        # hold it (it survives being closed and re-shown), so release it
        # here — the window's own closeEvent deliberately does not.
        if self._stats_window is not None:
            self._stats_window.shutdown()

        self.log_window.close()

    def _join_engines_responsive(self, total_timeout: float) -> None:
        """Wait up to *total_timeout* seconds per engine while keeping the
        Qt event loop pumping so the status bar/window stays responsive.

        Calls ``engine.join(timeout=…)`` in short slices and processes
        pending Qt events between slices instead of a single 30-second
        blocking join per engine.  Uses the engine's public ``join``
        instead of reaching into its private ``_thread`` so the
        thread-lifecycle is owned in one place.

        Iterates over a snapshot: ``processEvents`` can deliver a
        queued ``service_stopped`` whose handler prunes the engine from
        ``self.engines`` — mutating the dict mid-iteration otherwise."""
        for engine in list(self.engines.values()):
            end = time.monotonic() + total_timeout
            while time.monotonic() < end:
                remaining = max(0.0, end - time.monotonic())
                if engine.join(timeout=min(0.1, remaining)):
                    break
                QApplication.processEvents()
