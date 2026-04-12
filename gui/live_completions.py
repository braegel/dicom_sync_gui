"""
Live download completions window.

Shows in-memory data from running transfer engines: PatientName,
StudyDescription, Time Acquired, Time Download Completed, Institution,
and the delay between acquisition and download with color coding.
"""

import math
from datetime import datetime, timedelta
from typing import Optional

from core.i18n import tr

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QPushButton, QHeaderView, QApplication,
)

_RED = QColor("#e74c3c")
_GREEN = QColor("#2ecc71")


def _parse_time(s: str):
    """Parse HH:MM:SS or HHMMSS into (hours, minutes, seconds)."""
    s = s.strip()
    if ":" in s:
        parts = s.split(":")
    elif len(s) >= 6:
        parts = [s[:2], s[2:4], s[4:6]]
    elif len(s) >= 4:
        parts = [s[:2], s[2:4], "0"]
    else:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return None


def _time_to_seconds(h, m, s):
    return h * 3600 + m * 60 + s


def _format_delay(total_seconds: int) -> str:
    m, s = divmod(abs(total_seconds), 60)
    return f"{m}:{s:02d}"


class LiveCompletionsWindow(QWidget):

    def __init__(self, parent=None, language: str = "en"):
        super().__init__(parent)
        self.setWindowTitle("Download Completions")
        self.setWindowFlags(Qt.Window)
        self.setMinimumSize(600, 300)
        self._language = language
        self._delays: list[float] = []
        self._durations: list[Optional[float]] = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.completions_table = QTableWidget()
        self.completions_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.completions_table.setAlternatingRowColors(True)
        headers = ["Patient", "Study Description", "Institution",
                    "Time Acquired", "Download Completed",
                    "Download Duration", "Images", "img/min",
                    "Delay", ""]
        self.completions_table.setColumnCount(len(headers))
        self.completions_table.setHorizontalHeaderLabels(headers)
        self.completions_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents)
        # Row height must auto-fit the per-row Copy button — the global
        # dark theme forces all QPushButtons to min-height: 22 + 12px of
        # vertical padding, which is taller than the default fixed row.
        self.completions_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents)
        self.completions_table.verticalHeader().setVisible(False)
        layout.addWidget(self.completions_table, 1)

        bottom = QHBoxLayout()
        self.lbl_median_delay = QLabel("—")
        bottom.addWidget(QLabel("Median delay:"))
        bottom.addWidget(self.lbl_median_delay)
        bottom.addStretch()
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self._clear)
        bottom.addWidget(self.btn_clear)
        layout.addLayout(bottom)

    def add_completion(self, *, patient_name: str, study_description: str,
                       study_time: str, completed_time: str,
                       institution_name: str = "",
                       download_duration_seconds: Optional[float] = None,
                       image_count: Optional[int] = None):
        # Format study_time HHMMSS → HH:MM:SS
        acq = _parse_time(study_time)
        comp = _parse_time(completed_time)

        if acq and len(study_time) >= 4:
            formatted_acq = f"{acq[0]:02d}:{acq[1]:02d}:{acq[2]:02d}"
        else:
            formatted_acq = study_time

        if comp:
            formatted_comp = f"{comp[0]:02d}:{comp[1]:02d}:{comp[2]:02d}"
        else:
            formatted_comp = completed_time

        # Compute delay in seconds
        delay_seconds = None
        if acq and comp:
            delay_seconds = _time_to_seconds(*comp) - _time_to_seconds(*acq)

        delay_text = _format_delay(delay_seconds) if delay_seconds is not None else "—"

        if delay_seconds is not None:
            self._delays.insert(0, delay_seconds)

        self._durations.insert(0, download_duration_seconds)

        if download_duration_seconds is not None:
            dl_text = _format_delay(int(download_duration_seconds))
        else:
            dl_text = "—"

        images_text = str(image_count) if image_count is not None else "—"
        if (image_count is not None
                and download_duration_seconds is not None
                and download_duration_seconds > 0):
            ipm = image_count * 60.0 / download_duration_seconds
            ipm_text = f"{int(round(ipm))}"
        else:
            ipm_text = "—"

        self.completions_table.insertRow(0)
        self.completions_table.setItem(0, 0, QTableWidgetItem(patient_name))
        self.completions_table.setItem(0, 1, QTableWidgetItem(study_description))
        self.completions_table.setItem(0, 2, QTableWidgetItem(institution_name))
        self.completions_table.setItem(0, 3, QTableWidgetItem(formatted_acq))
        self.completions_table.setItem(0, 4, QTableWidgetItem(formatted_comp))
        self.completions_table.setItem(0, 5, QTableWidgetItem(dl_text))
        self.completions_table.setItem(0, 6, QTableWidgetItem(images_text))
        self.completions_table.setItem(0, 7, QTableWidgetItem(ipm_text))
        self.completions_table.setItem(0, 8, QTableWidgetItem(delay_text))

        copy_btn = QPushButton("Copy")
        # Override the global dark-theme QPushButton padding/min-height
        # so the button fits comfortably inside a table row.
        copy_btn.setStyleSheet(
            "QPushButton { padding: 2px 8px; min-height: 0; }")
        prefix = tr("image_transfer_completed", self._language)
        copy_btn.clicked.connect(
            lambda checked=False, t=formatted_comp, p=prefix:
                QApplication.clipboard().setText(f"{p}: {t}"))
        self.completions_table.setCellWidget(0, 9, copy_btn)

        self._update_stats()

    def _update_stats(self):
        self._update_duration_colors()

        if not self._delays:
            self.lbl_median_delay.setText("—")
            return

        s = sorted(self._delays)
        n = len(s)
        mid = n // 2
        median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
        self.lbl_median_delay.setText(_format_delay(int(median)))

        if n < 2:
            return

        mean = sum(self._delays) / n
        variance = sum((v - mean) ** 2 for v in self._delays) / n
        stddev = math.sqrt(variance)

        if stddev < 0.001:
            return

        delay_col = 8
        for row in range(self.completions_table.rowCount()):
            idx = row  # _delays[0] = newest = row 0
            if idx >= len(self._delays):
                break
            d = self._delays[idx]
            item = self.completions_table.item(row, delay_col)
            if item is None:
                continue
            if d > median + stddev:
                item.setForeground(QBrush(_RED))
            elif d < median - stddev:
                item.setForeground(QBrush(_GREEN))
            else:
                item.setForeground(QBrush(QColor("white")))

    def _update_duration_colors(self):
        """Color the Download Duration column: red if > median + 2σ,
        green if < median - 2σ. Uses 2σ (vs 1σ for Delay) because
        download time varies more with study size."""
        durations = [d for d in self._durations if d is not None]
        if len(durations) < 2:
            return

        s = sorted(durations)
        n = len(s)
        mid = n // 2
        median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
        mean = sum(durations) / n
        variance = sum((v - mean) ** 2 for v in durations) / n
        stddev = math.sqrt(variance)
        if stddev < 0.001:
            return

        duration_col = 5
        hi = median + 2 * stddev
        lo = median - 2 * stddev
        for row in range(self.completions_table.rowCount()):
            if row >= len(self._durations):
                break
            d = self._durations[row]
            item = self.completions_table.item(row, duration_col)
            if item is None or d is None:
                continue
            if d > hi:
                item.setForeground(QBrush(_RED))
            elif d < lo:
                item.setForeground(QBrush(_GREEN))
            else:
                item.setForeground(QBrush(QColor("white")))

    def _clear(self):
        self.completions_table.setRowCount(0)
        self._delays.clear()
        self._durations.clear()
        self.lbl_median_delay.setText("—")
