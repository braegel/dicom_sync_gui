"""
Transfer Dashboard — the main and only view of the application.
Shows service controls (Start/Stop, hours, max images, interval),
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


class StatsLabel(QLabel):
    """A label that is color-coded based on statistical deviation."""

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

    def set_value(self, value: float, mean: float, std: float):
        self.setText(f"{value:.0f}")
        if std < 1 or mean < 1:
            self.setStyleSheet(self._style("white"))
            return
        if value > mean + std:
            self.setStyleSheet(self._style("#2ecc71"))  # green
        elif value < mean - std:
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


class TransferDashboard(QWidget):
    """The one and only main view — service controls + live progress."""

    # Signals to main window
    start_requested = Signal(dict)   # {hours, max_images, sync_interval}
    stop_requested = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
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

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── Service Controls ──
        ctrl_group = QGroupBox("Download Service")
        ctrl_layout = QHBoxLayout()

        form = QFormLayout()
        self.hours_spin = QSpinBox()
        self.hours_spin.setRange(1, 168)
        self.hours_spin.setValue(self.config.default_hours)
        self.hours_spin.setSuffix(" hours")
        self.hours_spin.valueChanged.connect(self._on_settings_changed)
        form.addRow("Download last:", self.hours_spin)

        self.max_images_spin = QSpinBox()
        self.max_images_spin.setRange(0, 99999)
        self.max_images_spin.setSpecialValueText("No limit")
        self.max_images_spin.setSuffix(" images")
        self.max_images_spin.setValue(self.config.max_images)
        self.max_images_spin.valueChanged.connect(self._on_settings_changed)
        form.addRow("Max images / series:", self.max_images_spin)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(10, 600)
        self.interval_spin.setValue(self.config.sync_interval)
        self.interval_spin.setSuffix(" sec")
        self.interval_spin.setToolTip(
            "Seconds to wait between query cycles when no images are found")
        self.interval_spin.valueChanged.connect(self._on_settings_changed)
        form.addRow("Query interval:", self.interval_spin)

        ctrl_layout.addLayout(form)
        ctrl_layout.addStretch()

        # Start / Stop buttons
        btn_layout = QVBoxLayout()
        self.btn_start = QPushButton("  Start Service  ")
        self.btn_start.setFont(QFont("", 11, QFont.Bold))
        self.btn_start.setStyleSheet(
            "QPushButton { background: #27ae60; color: white; padding: 10px 24px; "
            "border-radius: 4px; } QPushButton:hover { background: #2ecc71; } "
            "QPushButton:disabled { background: #7f8c8d; }")
        self.btn_start.clicked.connect(self._on_start_clicked)

        self.btn_stop = QPushButton("  Stop Service  ")
        self.btn_stop.setFont(QFont("", 11, QFont.Bold))
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton { background: #c0392b; color: white; padding: 10px 24px; "
            "border-radius: 4px; } QPushButton:hover { background: #e74c3c; } "
            "QPushButton:disabled { background: #7f8c8d; }")
        self.btn_stop.clicked.connect(self._on_stop_clicked)

        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_stop)
        ctrl_layout.addLayout(btn_layout)

        ctrl_group.setLayout(ctrl_layout)
        layout.addWidget(ctrl_group)

        # ── Filter Groups ──
        filter_group = QGroupBox("Institution Filter")
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

        filter_group.setLayout(fl)
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

        for col, label in enumerate(["1 min", "5 min", "10 min", "Overall"], 1):
            lbl = QLabel(label)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFont(QFont("", 10, QFont.Bold))
            sl.addWidget(lbl, 0, col)

        sl.addWidget(QLabel("Rate:"), 1, 0)

        self.stat_1min = StatsLabel()
        self.stat_5min = StatsLabel()
        self.stat_10min = StatsLabel()
        self.stat_total = StatsLabel()

        sl.addWidget(self.stat_1min, 1, 1)
        sl.addWidget(self.stat_5min, 1, 2)
        sl.addWidget(self.stat_10min, 1, 3)
        sl.addWidget(self.stat_total, 1, 4)

        self.lbl_mean = QLabel("Mean: \u2014")
        self.lbl_std = QLabel("Std Dev: \u2014")
        self.lbl_mean.setAlignment(Qt.AlignCenter)
        self.lbl_std.setAlignment(Qt.AlignCenter)
        sl.addWidget(self.lbl_mean, 2, 1, 1, 2)
        sl.addWidget(self.lbl_std, 2, 3, 1, 2)

        stats_group.setLayout(sl)
        layout.addWidget(stats_group)

        # ── Series Queue Table ──
        table_group = QGroupBox("Series Queue")
        tl = QVBoxLayout()

        self.series_table = QTableWidget()
        self.series_table.setColumnCount(8)
        self.series_table.setHorizontalHeaderLabels([
            "Patient", "Study", "Series", "Modality",
            "Images", "Pending", "Status", "ETE"
        ])
        header = self.series_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self.series_table.setAlternatingRowColors(True)
        self.series_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.series_table.setSelectionBehavior(QTableWidget.SelectRows)

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

    def _update_filter_enabled_state(self):
        enabled = self.filter_enable_check.isChecked()
        self.filter_btn.setEnabled(enabled)
        if enabled:
            self.lbl_filter_info.setText("")
        else:
            self.lbl_filter_info.setText(
                "Filtering disabled — all studies will be downloaded.")

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
        params = {
            "hours": self.hours_spin.value(),
            "max_images": self.max_images_spin.value(),
            "sync_interval": self.interval_spin.value(),
        }
        self.start_requested.emit(params)

    def _on_stop_clicked(self):
        self._settings_dirty = False
        self.restart_banner.setVisible(False)
        self.stop_requested.emit()

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

    # ── ETE calculation ───────────────────────────────────────────────────

    def _get_rate(self) -> float:
        """Current transfer rate in images per second. 0 if unknown."""
        if not self._current_stats or self._current_stats.total_images == 0:
            return 0.0
        ipm = self._current_stats.overall_images_per_minute()
        return ipm / 60.0 if ipm > 0 else 0.0

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
        rate = self._get_rate()

        self.series_table.setRowCount(0)
        done_count = 0
        cumulative_pending = 0  # images still to transfer from here down

        # First pass: count total pending for cumulative ETE
        # We accumulate from the current item downward
        # So we need to know which items are not yet done
        pending_list = []
        for job in queue:
            if job["status"] in ("done", "error", "skipped"):
                pending_list.append(0)
            else:
                pending = job["remote_count"] - job["local_count"]
                pending_list.append(max(pending, 0))

        # Cumulative from bottom: for each row, ETE = time to finish
        # this row + all rows below it that are still pending.
        # Actually: cumulative from top for "when will THIS series be done"
        # = sum of pending images from current transferring item through this row
        cumulative = []
        running_sum = 0
        for p in pending_list:
            running_sum += p
            cumulative.append(running_sum)

        for i, job in enumerate(queue):
            row = self.series_table.rowCount()
            self.series_table.insertRow(row)

            self.series_table.setItem(
                row, 0, QTableWidgetItem(job["patient_name"]))
            self.series_table.setItem(
                row, 1, QTableWidgetItem(job["study_description"]))
            self.series_table.setItem(
                row, 2, QTableWidgetItem(job["series_description"]))
            self.series_table.setItem(
                row, 3, QTableWidgetItem(job["modality"]))
            self.series_table.setItem(
                row, 4, QTableWidgetItem(str(job["remote_count"])))

            pending = job["remote_count"] - job["local_count"]
            pending_item = QTableWidgetItem(str(max(pending, 0)))
            self.series_table.setItem(row, 5, pending_item)

            status = job["status"]
            status_item = QTableWidgetItem(self._status_text(status))
            status_item.setForeground(self._status_color(status))
            self.series_table.setItem(row, 6, status_item)

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
            self.series_table.setItem(row, 7, ete_item)

            if status == "done":
                done_count += 1

        self.lbl_total_series.setText(
            f"Series: {done_count} / {len(queue)}")

    def on_series_started(self, info: dict):
        self.lbl_status.setText(
            f"Transferring: {info['patient_name']} \u2014 "
            f"[{info.get('modality', '')}] {info['series_description']}")

    def on_series_progress(self, series_uid: str, transferred: int,
                           total: int):
        pass  # Queue table is updated via queue_updated signal

    def on_series_completed(self, series_uid: str, total_images: int):
        pass  # Queue table is updated via queue_updated signal

    def on_series_error(self, series_uid: str, error_msg: str):
        pass  # Queue table is updated via queue_updated signal

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
        if not stats or stats.total_images == 0:
            return

        mean, std = stats.overall_mean_and_std()

        rate_1 = stats.images_per_minute(1)
        rate_5 = stats.images_per_minute(5)
        rate_10 = stats.images_per_minute(10)
        rate_total = stats.overall_images_per_minute()

        self.stat_1min.set_value(rate_1, mean, std)
        self.stat_5min.set_value(rate_5, mean, std)
        self.stat_10min.set_value(rate_10, mean, std)
        self.stat_total.set_value(rate_total, mean, std)

        self.lbl_mean.setText(f"Mean: {mean:.0f} img/min")
        self.lbl_std.setText(f"Std Dev: {std:.0f} img/min")
        self.lbl_total_images.setText(f"Total: {stats.total_images} images")

    def _update_ete_column(self):
        """Update only the ETE column without rebuilding the whole table."""
        rate = self._get_rate()
        queue = self._last_queue

        # Recalculate cumulative pending
        pending_list = []
        for job in queue:
            if job["status"] in ("done", "error", "skipped"):
                pending_list.append(0)
            else:
                pending = job["remote_count"] - job["local_count"]
                pending_list.append(max(pending, 0))

        running_sum = 0
        cumulative = []
        for p in pending_list:
            running_sum += p
            cumulative.append(running_sum)

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
            self.series_table.setItem(i, 7, ete_item)

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
        self.stat_1min.setText("\u2014")
        self.stat_5min.setText("\u2014")
        self.stat_10min.setText("\u2014")
        self.stat_total.setText("\u2014")
        self.lbl_mean.setText("Mean: \u2014")
        self.lbl_std.setText("Std Dev: \u2014")
        self.lbl_total_images.setText("Total: 0 images")
        self.lbl_total_series.setText("Series: 0")
        self.lbl_cycle.setText("Cycle: \u2014")
        self.lbl_status.setText("Idle")
