"""
Transfer Dashboard Widget.
Shows real-time transfer progress with throughput statistics.
Color-coded: green (>1 std above mean), red (<1 std below mean), white (normal).
Includes download service controls (hours, max images, interval, start/stop).
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QProgressBar, QGroupBox, QGridLayout, QHeaderView,
    QFrame, QSizePolicy, QPushButton, QSpinBox, QFormLayout,
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont

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
    """Real-time transfer monitoring dashboard with service controls."""

    # Signals emitted to main window
    start_requested = Signal(dict)   # {hours, max_images, sync_interval}
    stop_requested = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._current_stats: TransferStats = None
        self._series_rows = {}  # series_uid -> row index
        self._service_running = False
        self._settings_dirty = False
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
        self.interval_spin.setToolTip("Seconds to wait between query cycles when no images are found")
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

        # ── Restart Required Banner ──
        self.restart_banner = QLabel(
            "\u26a0  Settings changed. Restart the service for changes to take effect.")
        self.restart_banner.setStyleSheet(
            "QLabel { background: #7f6000; color: #fff; padding: 8px; "
            "border-radius: 4px; font-weight: bold; }")
        self.restart_banner.setAlignment(Qt.AlignCenter)
        self.restart_banner.setVisible(False)
        layout.addWidget(self.restart_banner)

        # ── Current Transfer Info ──
        current_group = QGroupBox("Current Transfer")
        cl = QGridLayout()

        self.lbl_patient = QLabel("\u2014")
        self.lbl_patient.setFont(QFont("", 12, QFont.Bold))
        self.lbl_study = QLabel("\u2014")
        self.lbl_series = QLabel("\u2014")
        self.lbl_progress_text = QLabel("\u2014")
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setTextVisible(True)

        cl.addWidget(QLabel("Patient:"), 0, 0)
        cl.addWidget(self.lbl_patient, 0, 1, 1, 3)
        cl.addWidget(QLabel("Study:"), 1, 0)
        cl.addWidget(self.lbl_study, 1, 1, 1, 3)
        cl.addWidget(QLabel("Series:"), 2, 0)
        cl.addWidget(self.lbl_series, 2, 1, 1, 3)
        cl.addWidget(QLabel("Progress:"), 3, 0)
        cl.addWidget(self.progress_bar, 3, 1, 1, 3)
        cl.addWidget(self.lbl_progress_text, 4, 1, 1, 3)

        current_group.setLayout(cl)
        layout.addWidget(current_group)

        # ── Throughput Statistics ──
        stats_group = QGroupBox("Transfer Speed (images / minute)")
        sl = QGridLayout()

        for col, label in enumerate(["1 min", "10 min", "15 min", "Overall"], 1):
            lbl = QLabel(label)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFont(QFont("", 10, QFont.Bold))
            sl.addWidget(lbl, 0, col)

        sl.addWidget(QLabel("Rate:"), 1, 0)

        self.stat_1min = StatsLabel()
        self.stat_10min = StatsLabel()
        self.stat_15min = StatsLabel()
        self.stat_total = StatsLabel()

        sl.addWidget(self.stat_1min, 1, 1)
        sl.addWidget(self.stat_10min, 1, 2)
        sl.addWidget(self.stat_15min, 1, 3)
        sl.addWidget(self.stat_total, 1, 4)

        self.lbl_mean = QLabel("Mean: \u2014")
        self.lbl_std = QLabel("Std Dev: \u2014")
        self.lbl_mean.setAlignment(Qt.AlignCenter)
        self.lbl_std.setAlignment(Qt.AlignCenter)
        sl.addWidget(self.lbl_mean, 2, 1, 1, 2)
        sl.addWidget(self.lbl_std, 2, 3, 1, 2)

        stats_group.setLayout(sl)
        layout.addWidget(stats_group)

        # ── Series Table ──
        table_group = QGroupBox("Series Overview")
        tl = QVBoxLayout()

        self.series_table = QTableWidget()
        self.series_table.setColumnCount(7)
        self.series_table.setHorizontalHeaderLabels([
            "Patient", "Study", "Series", "Modality",
            "Downloaded", "Pending", "Status"
        ])
        header = self.series_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.series_table.setAlternatingRowColors(True)
        self.series_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.series_table.setSelectionBehavior(QTableWidget.SelectRows)

        tl.addWidget(self.series_table)
        table_group.setLayout(tl)
        layout.addWidget(table_group, 1)

        # ── Summary ──
        summary = QHBoxLayout()
        self.lbl_total_images = QLabel("Total: 0 images")
        self.lbl_total_images.setFont(QFont("", 11, QFont.Bold))
        self.lbl_total_series = QLabel("Series: 0")
        self.lbl_status = QLabel("Idle")
        self.lbl_status.setFont(QFont("", 11))
        summary.addWidget(self.lbl_total_images)
        summary.addWidget(self.lbl_total_series)
        summary.addStretch()
        summary.addWidget(self.lbl_status)
        layout.addLayout(summary)

    # ── Service control handlers ──

    def _on_settings_changed(self):
        """Called when any download parameter spinbox changes."""
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
        """Update button states based on service status."""
        self._service_running = running
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        if not running:
            self._settings_dirty = False
            self.restart_banner.setVisible(False)

    def get_service_params(self) -> dict:
        return {
            "hours": self.hours_spin.value(),
            "max_images": self.max_images_spin.value(),
            "sync_interval": self.interval_spin.value(),
        }

    # ── Public update methods (called from signals) ──

    def on_series_started(self, info: dict):
        series_uid = info["series_uid"]

        self.lbl_patient.setText(f"{info['patient_name']} ({info['patient_id']})")
        self.lbl_study.setText(info["study_description"])
        modality = info.get("modality", "")
        snum = info.get("series_number", "")
        self.lbl_series.setText(f"[{modality}] {info['series_description']} (#{snum})")

        total = info["remote_count"]
        done = info["local_count"]
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(done)
        pending = total - done
        self.lbl_progress_text.setText(f"{done} downloaded / {pending} pending")

        row = self.series_table.rowCount()
        self.series_table.insertRow(row)
        self._series_rows[series_uid] = row

        self.series_table.setItem(row, 0, QTableWidgetItem(info["patient_name"]))
        self.series_table.setItem(row, 1, QTableWidgetItem(info["study_description"]))
        self.series_table.setItem(row, 2, QTableWidgetItem(info["series_description"]))
        self.series_table.setItem(row, 3, QTableWidgetItem(modality))
        self.series_table.setItem(row, 4, QTableWidgetItem(str(done)))
        self.series_table.setItem(row, 5, QTableWidgetItem(str(pending)))

        status_item = QTableWidgetItem("\u23f3 Transferring...")
        status_item.setForeground(QColor("#f39c12"))
        self.series_table.setItem(row, 6, status_item)

        self.series_table.scrollToBottom()
        self.lbl_status.setText("Transferring...")

    def on_series_progress(self, series_uid: str, transferred: int, total: int):
        self.progress_bar.setValue(self.progress_bar.value() + 1)
        pending = total - transferred
        self.lbl_progress_text.setText(
            f"{self.progress_bar.value()} downloaded / {pending} pending")

        row = self._series_rows.get(series_uid)
        if row is not None:
            self.series_table.setItem(row, 4, QTableWidgetItem(str(transferred)))
            self.series_table.setItem(row, 5, QTableWidgetItem(str(pending)))

    def on_series_completed(self, series_uid: str, total_images: int):
        row = self._series_rows.get(series_uid)
        if row is not None:
            status_item = QTableWidgetItem(f"\u2713 {total_images} images")
            status_item.setForeground(QColor("#2ecc71"))
            self.series_table.setItem(row, 6, status_item)
            self.series_table.setItem(row, 5, QTableWidgetItem("0"))

    def on_series_error(self, series_uid: str, error_msg: str):
        row = self._series_rows.get(series_uid)
        if row is not None:
            status_item = QTableWidgetItem("\u2717 Error")
            status_item.setForeground(QColor("#e74c3c"))
            status_item.setToolTip(error_msg)
            self.series_table.setItem(row, 6, status_item)

    def on_stats_updated(self, stats: TransferStats):
        self._current_stats = stats
        self._refresh_stats_display()

    def on_job_started(self, total_series: int):
        self.series_table.setRowCount(0)
        self._series_rows.clear()
        self.lbl_total_series.setText(f"Series: 0 / {total_series}")
        self.lbl_total_images.setText("Total: 0 images")
        self.lbl_status.setText("Transfer started...")

    def on_job_finished(self, total_images: int):
        self.lbl_total_images.setText(f"Total: {total_images} images")
        self.lbl_status.setText("Done" if total_images > 0 else "No images transferred")
        self.progress_bar.setValue(self.progress_bar.maximum())

        completed = sum(1 for uid, row in self._series_rows.items()
                        if self.series_table.item(row, 6) and
                        "\u2713" in (self.series_table.item(row, 6).text() or ""))
        total = len(self._series_rows)
        self.lbl_total_series.setText(f"Series: {completed} / {total}")

    def _refresh_stats_display(self):
        stats = self._current_stats
        if not stats or stats.total_images == 0:
            return

        mean, std = stats.overall_mean_and_std()

        rate_1 = stats.images_per_minute(1)
        rate_10 = stats.images_per_minute(10)
        rate_15 = stats.images_per_minute(15)
        rate_total = stats.overall_images_per_minute()

        self.stat_1min.set_value(rate_1, mean, std)
        self.stat_10min.set_value(rate_10, mean, std)
        self.stat_15min.set_value(rate_15, mean, std)
        self.stat_total.set_value(rate_total, mean, std)

        self.lbl_mean.setText(f"Mean: {mean:.0f} img/min")
        self.lbl_std.setText(f"Std Dev: {std:.0f} img/min")
        self.lbl_total_images.setText(f"Total: {stats.total_images} images")

    def reset(self):
        self.series_table.setRowCount(0)
        self._series_rows.clear()
        self._current_stats = None
        self.lbl_patient.setText("\u2014")
        self.lbl_study.setText("\u2014")
        self.lbl_series.setText("\u2014")
        self.lbl_progress_text.setText("\u2014")
        self.progress_bar.setValue(0)
        self.stat_1min.setText("\u2014")
        self.stat_10min.setText("\u2014")
        self.stat_15min.setText("\u2014")
        self.stat_total.setText("\u2014")
        self.lbl_mean.setText("Mean: \u2014")
        self.lbl_std.setText("Std Dev: \u2014")
        self.lbl_total_images.setText("Total: 0 images")
        self.lbl_total_series.setText("Series: 0")
        self.lbl_status.setText("Idle")
