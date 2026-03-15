"""
Source Dashboard — one widget per configured source PACS.
Shows per-source service controls (Start/Stop, hours, max images, interval),
the series queue with ETE (estimated time to completion),
and real-time throughput statistics with color-coded indicators.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QGroupBox, QGridLayout, QHeaderView,
    QPushButton, QSpinBox, QFormLayout, QCheckBox, QComboBox,
    QListWidget, QListWidgetItem, QMenu, QToolButton, QFrame,
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QAction

from core.transfer_engine import TransferStats
from gui.styles import BTN_START, BTN_STOP, BTN_DOWNLOAD_SELECTED


class StatsLabel(QLabel):
    """A label that is color-coded relative to the median-all baseline."""

    def __init__(self, text="\u2014", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setFamilies(["Menlo", "Consolas", "Courier New"])
        font.setPointSize(16)
        font.setBold(True)
        self.setFont(font)
        self.setMinimumWidth(120)
        self.setMinimumHeight(50)
        self.setStyleSheet(self._style("white"))

    def set_value(self, value: float, median_all: float):
        """Update text and colour.

        *median_all* is the overall-median baseline.  A value more than
        20 % above baseline is green; more than 20 % below is red;
        everything else stays white.  If baseline is < 1 we don't
        colour-code (not enough data).
        """
        self.setText(f"{value:.0f}")
        if median_all < 1 or value < 1:
            self.setStyleSheet(self._style("white"))
            return
        if value > median_all * 1.2:
            self.setStyleSheet(self._style("#2ecc71"))  # green
        elif value < median_all * 0.8:
            self.setStyleSheet(self._style("#e74c3c"))  # red
        else:
            self.setStyleSheet(self._style("white"))

    @staticmethod
    def _style(color: str) -> str:
        bg = "#2c2c2c" if color == "white" else (
            "#1a3a1a" if "#2ecc71" in color else
            "#3a1a1a" if "#e74c3c" in color else "#2c2c2c"
        )
        return (
            f"QLabel {{ color: {color}; background: {bg}; "
            f"border: 1px solid #555; border-radius: 6px; padding: 6px; }}"
        )


class SourceDashboard(QWidget):
    """Dashboard widget for a single source PACS — controls + live progress."""

    # Signals to main window
    start_requested = Signal(str, dict)   # remote_key, {hours, max_images, sync_interval, selection_mode}
    stop_requested = Signal(str)          # remote_key
    selection_confirmed = Signal(str, list)  # remote_key, [series_uid, ...]

    def __init__(self, config, remote_key: str, parent=None):
        super().__init__(parent)
        self.config = config
        self.remote_key = remote_key
        self._current_stats: TransferStats = None
        self._last_queue: list = []
        self._service_running = False
        self._settings_dirty = False
        self._filter_popup_visible = False
        self._setup_ui()

        # Refresh stats display every 2 seconds
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_stats_display)
        self._timer.start(2000)

    @property
    def _remote_node(self):
        return self.config.remote_nodes.get(self.remote_key)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        node = self._remote_node

        # ── Service Controls ──
        ctrl_group = QGroupBox("Download Service")
        ctrl_layout = QHBoxLayout()

        form = QFormLayout()
        self.hours_spin = QSpinBox()
        self.hours_spin.setRange(1, 168)
        self.hours_spin.setValue(node.hours if node else 3)
        self.hours_spin.setSuffix(" hours")
        self.hours_spin.valueChanged.connect(self._on_settings_changed)
        form.addRow("Download last:", self.hours_spin)

        self.max_images_spin = QSpinBox()
        self.max_images_spin.setRange(0, 99999)
        self.max_images_spin.setSpecialValueText("No limit")
        self.max_images_spin.setSuffix(" images")
        self.max_images_spin.setValue(node.max_images if node else 0)
        self.max_images_spin.valueChanged.connect(self._on_settings_changed)
        form.addRow("Max images / series:", self.max_images_spin)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(10, 600)
        self.interval_spin.setValue(node.sync_interval if node else 60)
        self.interval_spin.setSuffix(" sec")
        self.interval_spin.setToolTip(
            "Seconds to wait between query cycles when no images are found")
        self.interval_spin.valueChanged.connect(self._on_settings_changed)
        form.addRow("Query interval:", self.interval_spin)

        self.manual_selection_check = QCheckBox("Manual series selection")
        self.manual_selection_check.setToolTip(
            "After each query, pause and let you choose which series to download.")
        form.addRow("", self.manual_selection_check)

        ctrl_layout.addLayout(form)
        ctrl_layout.addStretch()

        # Start / Stop buttons
        btn_layout = QVBoxLayout()
        self.btn_start = QPushButton("  Start Service  ")
        self.btn_start.setFont(QFont("", 11, QFont.Bold))
        self.btn_start.setStyleSheet(BTN_START)
        self.btn_start.clicked.connect(self._on_start_clicked)

        self.btn_stop = QPushButton("  Stop Service  ")
        self.btn_stop.setFont(QFont("", 11, QFont.Bold))
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(BTN_STOP)
        self.btn_stop.clicked.connect(self._on_stop_clicked)

        self.btn_download_selected = QPushButton("  Download Selected  ")
        self.btn_download_selected.setFont(QFont("", 11, QFont.Bold))
        self.btn_download_selected.setStyleSheet(BTN_DOWNLOAD_SELECTED)
        self.btn_download_selected.setVisible(False)
        self.btn_download_selected.clicked.connect(
            self._on_download_selected_clicked)

        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_stop)
        btn_layout.addWidget(self.btn_download_selected)
        ctrl_layout.addLayout(btn_layout)

        ctrl_group.setLayout(ctrl_layout)
        layout.addWidget(ctrl_group)

        # ── Filter Groups ──
        filter_group = QGroupBox("Institution Filter")
        filter_vbox = QVBoxLayout()

        fl = QHBoxLayout()

        self.filter_enable_check = QCheckBox("Enable group filtering")
        self.filter_enable_check.setChecked(
            self.config.filter_groups_enabled)
        self.filter_enable_check.toggled.connect(
            self._on_filter_toggled)
        fl.addWidget(self.filter_enable_check)

        fl.addWidget(QLabel("Active groups:"))

        # Multi-select dropdown button
        self.filter_btn = QToolButton()
        self.filter_btn.setText("Select Groups...")
        self.filter_btn.setPopupMode(QToolButton.InstantPopup)
        self.filter_btn.setStyleSheet(
            "QToolButton { padding: 5px 12px; border: 1px solid #555; "
            "border-radius: 4px; background: #2c2c2c; min-width: 200px; "
            "text-align: left; }"
            "QToolButton::menu-indicator { subcontrol-position: right center; }")

        self.filter_menu = QMenu(self.filter_btn)
        self.filter_btn.setMenu(self.filter_menu)

        fl.addWidget(self.filter_btn)

        self.lbl_filter_info = QLabel("")
        self.lbl_filter_info.setStyleSheet(
            "QLabel { color: #f39c12; font-style: italic; }")
        fl.addWidget(self.lbl_filter_info)

        fl.addStretch()
        filter_vbox.addLayout(fl)

        # Small-series exception row
        fl2 = QHBoxLayout()

        self.small_series_check = QCheckBox(
            "Allow small series (other groups)")
        self.small_series_check.setToolTip(
            "Download series with few images even if the institution "
            "is not in an active filter group.")
        self.small_series_check.setChecked(
            self.config.filter_allow_small_series)
        self.small_series_check.toggled.connect(
            self._on_small_series_toggled)
        fl2.addWidget(self.small_series_check)

        self.lbl_small_max = QLabel("Max images/series:")
        fl2.addWidget(self.lbl_small_max)

        self.small_series_spin = QSpinBox()
        self.small_series_spin.setRange(1, 999)
        self.small_series_spin.setValue(
            self.config.filter_small_series_max)
        self.small_series_spin.valueChanged.connect(
            self._on_small_series_max_changed)
        fl2.addWidget(self.small_series_spin)

        fl2.addStretch()
        filter_vbox.addLayout(fl2)

        filter_group.setLayout(filter_vbox)
        layout.addWidget(filter_group)

        self._populate_filter_menu()
        self._update_filter_button_text()
        self._update_filter_enabled_state()

        # ── Restart Required Banner ──
        self.restart_banner = QLabel(
            "\u26a0  Settings changed. Restart the service for "
            "changes to take effect.")
        self.restart_banner.setStyleSheet(
            "QLabel { background: #7f6000; color: #fff; padding: 8px; "
            "border-radius: 4px; font-weight: bold; }")
        self.restart_banner.setAlignment(Qt.AlignCenter)
        self.restart_banner.setVisible(False)
        layout.addWidget(self.restart_banner)

        # ── Throughput Statistics ──
        stats_group = QGroupBox("Transfer Speed (images / minute)")
        sl = QGridLayout()

        for col, label in enumerate(
                ["Last Series", "Median 5", "Median 10", "Median All"], 1):
            lbl = QLabel(label)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFont(QFont("", 10, QFont.Bold))
            sl.addWidget(lbl, 0, col)

        sl.addWidget(QLabel("Rate:"), 1, 0)

        self.stat_last = StatsLabel()
        self.stat_med5 = StatsLabel()
        self.stat_med10 = StatsLabel()
        self.stat_medall = StatsLabel()

        sl.addWidget(self.stat_last, 1, 1)
        sl.addWidget(self.stat_med5, 1, 2)
        sl.addWidget(self.stat_med10, 1, 3)
        sl.addWidget(self.stat_medall, 1, 4)

        stats_group.setLayout(sl)
        layout.addWidget(stats_group)

        # ── Series Queue Table ──
        table_group = QGroupBox("Series Queue")
        tl = QVBoxLayout()

        self.series_table = QTableWidget()
        self.series_table.setColumnCount(10)
        self.series_table.setHorizontalHeaderLabels([
            "☑", "Patient", "Study", "Series", "Modality",
            "Images", "Pending", "img/min", "Status", "ETE"
        ])
        header = self.series_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(9, QHeaderView.ResizeToContents)
        self.series_table.setAlternatingRowColors(True)
        self.series_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.series_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.series_table.setColumnHidden(0, True)

        tl.addWidget(self.series_table)
        table_group.setLayout(tl)
        layout.addWidget(table_group, 1)  # stretch factor 1 = takes all space

        # ── Summary bar ──
        summary = QHBoxLayout()
        self.lbl_total_images = QLabel("Total: 0 images")
        self.lbl_total_images.setFont(QFont("", 11, QFont.Bold))
        self.lbl_total_series = QLabel("Series: 0")
        self.lbl_cycle = QLabel("Cycle: \u2014")
        self.lbl_status = QLabel("Idle")
        self.lbl_status.setFont(QFont("", 11))
        summary.addWidget(self.lbl_total_images)
        summary.addWidget(self.lbl_total_series)
        summary.addWidget(self.lbl_cycle)
        summary.addStretch()
        summary.addWidget(self.lbl_status)
        layout.addLayout(summary)

    # ── Filter group handling ─────────────────────────────────────────

    def _populate_filter_menu(self):
        """Build the checkable menu items for each filter group."""
        self.filter_menu.clear()
        active = set(self.config.active_filter_groups)

        for name in self.config.filter_group_names:
            action = QAction(name, self.filter_menu)
            action.setCheckable(True)
            action.setChecked(name in active)
            action.toggled.connect(self._on_filter_group_toggled)
            self.filter_menu.addAction(action)

        if not self.config.filter_group_names:
            empty_action = QAction(
                "(no groups configured)", self.filter_menu)
            empty_action.setEnabled(False)
            self.filter_menu.addAction(empty_action)

    def _on_filter_group_toggled(self, checked: bool):
        """Update active groups when a menu item is toggled."""
        active = []
        for action in self.filter_menu.actions():
            if action.isCheckable() and action.isChecked():
                active.append(action.text())
        self.config.active_filter_groups = active
        self.config.filter_groups_enabled = (
            self.filter_enable_check.isChecked())
        self.config.save()
        self._update_filter_button_text()

    def _on_filter_toggled(self, enabled: bool):
        """Master switch for filtering."""
        self.config.filter_groups_enabled = enabled
        self.config.save()
        self._update_filter_enabled_state()
        self._update_filter_button_text()
        self._on_settings_changed()

    def _on_small_series_toggled(self, checked: bool):
        self.config.filter_allow_small_series = checked
        self.config.save()
        self._update_filter_enabled_state()
        self._on_settings_changed()

    def _on_small_series_max_changed(self, value: int):
        self.config.filter_small_series_max = value
        self.config.save()
        self._on_settings_changed()

    def _update_filter_enabled_state(self):
        enabled = self.filter_enable_check.isChecked()
        self.filter_btn.setEnabled(enabled)
        # Small-series controls: visible only when filtering is enabled
        small_visible = enabled and self.small_series_check.isChecked()
        self.small_series_check.setVisible(enabled)
        self.lbl_small_max.setVisible(small_visible)
        self.small_series_spin.setVisible(small_visible)
        if enabled:
            self.lbl_filter_info.setText("")
        else:
            self.lbl_filter_info.setText(
                "Filtering disabled \u2014 all studies will be downloaded.")

    def _update_filter_button_text(self):
        active = self.config.active_filter_groups
        if not active:
            self.filter_btn.setText("Select Groups...")
        elif len(active) <= 3:
            self.filter_btn.setText(", ".join(active))
        else:
            self.filter_btn.setText(
                f"{len(active)} groups selected")

        if self.filter_enable_check.isChecked() and active:
            count = sum(
                1 for inst, grp
                in self.config.institution_assignments.items()
                if grp in set(active))
            self.lbl_filter_info.setText(
                f"Filtering active: {len(active)} group(s), "
                f"{count} institution(s)")

    def refresh_filter_groups(self):
        """Called when filter groups are edited in the dialog."""
        # Remove active selections that no longer exist
        valid_names = set(self.config.filter_group_names)
        self.config.active_filter_groups = [
            g for g in self.config.active_filter_groups
            if g in valid_names]
        self.config.save()
        self._populate_filter_menu()
        self._update_filter_button_text()
        self._update_filter_enabled_state()

    # ── Service control handlers ──────────────────────────────────────────

    def _on_settings_changed(self):
        if self._service_running:
            self._settings_dirty = True
            self.restart_banner.setVisible(True)

    def _on_start_clicked(self):
        self._settings_dirty = False
        self.restart_banner.setVisible(False)
        # Save per-source params to config
        node = self._remote_node
        if node:
            node.hours = self.hours_spin.value()
            node.max_images = self.max_images_spin.value()
            node.sync_interval = self.interval_spin.value()
            self.config.save()
        params = {
            "hours": self.hours_spin.value(),
            "max_images": self.max_images_spin.value(),
            "sync_interval": self.interval_spin.value(),
            "selection_mode": self.manual_selection_check.isChecked(),
        }
        self.start_requested.emit(self.remote_key, params)

    def _on_stop_clicked(self):
        self._settings_dirty = False
        self.restart_banner.setVisible(False)
        self.stop_requested.emit(self.remote_key)

    def set_service_running(self, running: bool):
        self._service_running = running
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.hours_spin.setEnabled(not running)
        self.max_images_spin.setEnabled(not running)
        self.interval_spin.setEnabled(not running)
        if not running:
            self._settings_dirty = False
            self.restart_banner.setVisible(False)
            self.lbl_status.setText("Stopped")
            self.btn_download_selected.setVisible(False)
            self.series_table.setColumnHidden(0, True)

    # ── ETE calculation ───────────────────────────────────────────────────

    def _get_rate(self) -> float:
        """Current transfer rate in images per second. 0 if unknown."""
        if not self._current_stats or self._current_stats.total_images == 0:
            return 0.0
        ipm = self._current_stats.overall_images_per_minute()
        return ipm / 60.0 if ipm > 0 else 0.0

    @staticmethod
    def _compute_cumulative_pending(queue: list) -> list:
        """Return a list of cumulative pending-image counts per queue row."""
        running_sum = 0
        cumulative = []
        for job in queue:
            if job["status"] in ("done", "error", "skipped"):
                running_sum += 0
            else:
                pending = job["remote_count"] - job["local_count"]
                running_sum += max(pending, 0)
            cumulative.append(running_sum)
        return cumulative

    @staticmethod
    def _format_ete(seconds: float) -> str:
        """Format seconds into mm:ss or hh:mm:ss."""
        if seconds <= 0:
            return "\u2014"
        seconds = int(seconds)
        if seconds >= 3600:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            return f"{h}:{m:02d}:{s:02d}"
        m = seconds // 60
        s = seconds % 60
        return f"{m}:{s:02d}"

    # ── Slots called by engine signals ────────────────────────────────────

    def on_queue_updated(self, queue: list):
        """Rebuild the series table from the full queue list."""
        self._last_queue = queue
        # Hide selection UI — engine is now downloading or idle
        self.btn_download_selected.setVisible(False)
        self.series_table.setColumnHidden(0, True)

        rate = self._get_rate()

        self.series_table.setRowCount(0)
        done_count = 0
        cumulative = self._compute_cumulative_pending(queue)

        for i, job in enumerate(queue):
            row = self.series_table.rowCount()
            self.series_table.insertRow(row)

            self.series_table.setItem(
                row, 1, QTableWidgetItem(job["patient_name"]))
            self.series_table.setItem(
                row, 2, QTableWidgetItem(job["study_description"]))
            self.series_table.setItem(
                row, 3, QTableWidgetItem(job["series_description"]))
            self.series_table.setItem(
                row, 4, QTableWidgetItem(job["modality"]))
            self.series_table.setItem(
                row, 5, QTableWidgetItem(str(job["remote_count"])))

            pending = job["remote_count"] - job["local_count"]
            pending_item = QTableWidgetItem(str(max(pending, 0)))
            self.series_table.setItem(row, 6, pending_item)

            # img/min column
            ipm = job.get("images_per_minute", 0.0)
            status = job["status"]
            if status == "done" and ipm > 0:
                ipm_item = QTableWidgetItem(f"{ipm:.0f}")
                ipm_item.setForeground(QColor("#3498db"))
            else:
                ipm_item = QTableWidgetItem("\u2014")
                ipm_item.setForeground(QColor("#969696"))
            ipm_item.setTextAlignment(Qt.AlignCenter)
            self.series_table.setItem(row, 7, ipm_item)

            status_item = QTableWidgetItem(self._status_text(status))
            status_item.setForeground(self._status_color(status))
            self.series_table.setItem(row, 8, status_item)

            # ETE column
            if status in ("done", "error", "skipped"):
                ete_text = "\u2014" if status != "done" else "\u2713"
                ete_item = QTableWidgetItem(ete_text)
                if status == "done":
                    ete_item.setForeground(QColor("#2ecc71"))
                else:
                    ete_item.setForeground(QColor("#969696"))
            elif rate > 0:
                ete_seconds = cumulative[i] / rate
                ete_item = QTableWidgetItem(self._format_ete(ete_seconds))
                ete_item.setForeground(QColor("#f39c12"))
            else:
                ete_item = QTableWidgetItem("\u2014")
                ete_item.setForeground(QColor("#969696"))
            ete_item.setTextAlignment(Qt.AlignCenter)
            self.series_table.setItem(row, 9, ete_item)

            if status == "done":
                done_count += 1

        self.lbl_total_series.setText(
            f"Series: {done_count} / {len(queue)}")

    def on_queue_ready_for_selection(self, queue: list):
        """Engine paused after query — show checkboxes for manual selection."""
        self._last_queue = queue
        self.series_table.setRowCount(0)
        self.series_table.setColumnHidden(0, False)

        for job in queue:
            row = self.series_table.rowCount()
            self.series_table.insertRow(row)

            cb_item = QTableWidgetItem()
            cb_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            cb_item.setCheckState(Qt.Checked)
            cb_item.setData(Qt.UserRole, job["series_uid"])
            self.series_table.setItem(row, 0, cb_item)

            self.series_table.setItem(
                row, 1, QTableWidgetItem(job["patient_name"]))
            self.series_table.setItem(
                row, 2, QTableWidgetItem(job["study_description"]))
            self.series_table.setItem(
                row, 3, QTableWidgetItem(job["series_description"]))
            self.series_table.setItem(
                row, 4, QTableWidgetItem(job["modality"]))
            self.series_table.setItem(
                row, 5, QTableWidgetItem(str(job["remote_count"])))
            pending = job["remote_count"] - job["local_count"]
            self.series_table.setItem(
                row, 6, QTableWidgetItem(str(max(pending, 0))))
            self.series_table.setItem(row, 7, QTableWidgetItem("\u2014"))
            status_item = QTableWidgetItem("\u23f3 Waiting")
            status_item.setForeground(QColor("#f39c12"))
            self.series_table.setItem(row, 8, status_item)
            self.series_table.setItem(row, 9, QTableWidgetItem("\u2014"))

        total = sum(max(j["remote_count"] - j["local_count"], 0) for j in queue)
        self.lbl_total_series.setText(f"Series: 0 / {len(queue)}")
        self.lbl_total_images.setText(f"Pending: {total} images")
        self.lbl_status.setText(
            f"Awaiting selection — {len(queue)} series found")
        self.btn_download_selected.setVisible(True)
        # Allow checking/unchecking in the table
        self.series_table.setEditTriggers(QTableWidget.NoEditTriggers)

    def _on_download_selected_clicked(self):
        """Collect checked series UIDs and confirm selection to the engine."""
        selected_uids = []
        for row in range(self.series_table.rowCount()):
            cb_item = self.series_table.item(row, 0)
            if cb_item and cb_item.checkState() == Qt.Checked:
                uid = cb_item.data(Qt.UserRole)
                if uid:
                    selected_uids.append(uid)
        self.selection_confirmed.emit(self.remote_key, selected_uids)

    def on_series_started(self, info: dict):
        self.lbl_status.setText(
            f"Transferring: {info['patient_name']} \u2014 "
            f"[{info.get('modality', '')}] {info['series_description']}")

    def on_stats_updated(self, stats: TransferStats):
        self._current_stats = stats
        self.lbl_total_images.setText(f"Total: {stats.total_images} images")
        self._refresh_stats_display()
        # Re-render ETE values with updated rate
        if self._last_queue:
            self._update_ete_column()

    def on_cycle_started(self, cycle: int):
        self.lbl_cycle.setText(f"Cycle: {cycle}")
        self.lbl_status.setText(f"Cycle {cycle} \u2014 querying...")

    def on_cycle_finished(self, cycle: int, images: int):
        if images > 0:
            self.lbl_status.setText(
                f"Cycle {cycle} done \u2014 {images} images")
        else:
            self.lbl_status.setText(
                f"Cycle {cycle} \u2014 waiting...")

    # ── Stats display ─────────────────────────────────────────────────────

    def _refresh_stats_display(self):
        stats = self._current_stats
        if not stats or stats.completed_count == 0:
            return

        median_all = stats.median_all_ipm()
        last = stats.last_series_ipm()
        med5 = stats.median_n_ipm(5)
        med10 = stats.median_n_ipm(10)

        self.stat_last.set_value(last, median_all)
        self.stat_med5.set_value(med5, median_all)
        self.stat_med10.set_value(med10, median_all)
        self.stat_medall.set_value(median_all, median_all)

        self.lbl_total_images.setText(f"Total: {stats.total_images} images")

    def _update_ete_column(self):
        """Update only the ETE column without rebuilding the whole table."""
        rate = self._get_rate()
        queue = self._last_queue
        cumulative = self._compute_cumulative_pending(queue)

        for i, job in enumerate(queue):
            if i >= self.series_table.rowCount():
                break
            status = job["status"]
            if status in ("done", "error", "skipped"):
                ete_text = "\u2713" if status == "done" else "\u2014"
                ete_item = QTableWidgetItem(ete_text)
                if status == "done":
                    ete_item.setForeground(QColor("#2ecc71"))
                else:
                    ete_item.setForeground(QColor("#969696"))
            elif rate > 0:
                ete_seconds = cumulative[i] / rate
                ete_item = QTableWidgetItem(self._format_ete(ete_seconds))
                ete_item.setForeground(QColor("#f39c12"))
            else:
                ete_item = QTableWidgetItem("\u2014")
                ete_item.setForeground(QColor("#969696"))
            ete_item.setTextAlignment(Qt.AlignCenter)
            self.series_table.setItem(i, 9, ete_item)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _status_text(status: str) -> str:
        return {
            "queued": "\u23f3 Queued",
            "transferring": "\u25b6 Transferring...",
            "done": "\u2713 Done",
            "error": "\u2717 Error",
            "skipped": "\u2014 Skipped",
        }.get(status, status)

    @staticmethod
    def _status_color(status: str) -> QColor:
        return {
            "queued": QColor("#969696"),
            "transferring": QColor("#f39c12"),
            "done": QColor("#2ecc71"),
            "error": QColor("#e74c3c"),
            "skipped": QColor("#969696"),
        }.get(status, QColor("#d4d4d4"))

    def reset(self):
        self.series_table.setRowCount(0)
        self._current_stats = None
        self._last_queue = []
        self.stat_last.setText("\u2014")
        self.stat_med5.setText("\u2014")
        self.stat_med10.setText("\u2014")
        self.stat_medall.setText("\u2014")
        self.lbl_total_images.setText("Total: 0 images")
        self.lbl_total_series.setText("Series: 0")
        self.lbl_cycle.setText("Cycle: \u2014")
        self.lbl_status.setText("Idle")

    def sync_from_config(self):
        """Update spinboxes from the current config node values."""
        node = self._remote_node
        if node:
            self.hours_spin.setValue(node.hours)
            self.max_images_spin.setValue(node.max_images)
            self.interval_spin.setValue(node.sync_interval)
