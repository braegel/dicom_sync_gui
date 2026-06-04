"""
Live download completions window.

Shows in-memory data from running transfer engines: PatientName,
StudyDescription, Time Acquired, Time Download Completed, Institution,
and the delay between acquisition and download with color coding.
"""

import math
from datetime import datetime, timedelta
from typing import Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QPushButton, QHeaderView, QApplication,
)

from core.i18n import tr

_RED = QColor("#e74c3c")
_GREEN = QColor("#2ecc71")

# Placeholder shown in empty / unknown numeric cells.
_DASH = "—"
# Per-row button label + window button label — pinned here so a
# future i18n pass has a single source of truth to translate.
_COPY_LABEL = "Copy"
_CLEAR_LABEL = "Clear"
# Font tuning for the dense 10-column completions table.
_FONT_REDUCTION_PT = 3
_FONT_MIN_PT = 8
# How many standard deviations from the median define the
# red / green colour bands for the Delay and Download Duration
# columns.  ±2σ keeps only the strong outliers visible.
_STDDEV_BAND_MULTIPLIER = 2.0

# Column layout — kept here so add_completion, _update_existing_row,
# and the stat-colour helpers all agree on which cell is which.
_COL_COPY = 0
_COL_PATIENT = 1
_COL_STUDY_DESC = 2
_COL_INSTITUTION = 3
_COL_ACQ = 4
_COL_COMPLETED = 5
_COL_DURATION = 6
_COL_IMAGES = 7
_COL_IPM = 8
_COL_DELAY = 9


def _parse_time(s: str) -> Optional[tuple[int, int, int]]:
    """Parse HH:MM:SS or HHMMSS into (hours, minutes, seconds).

    Returns ``None`` when *s* cannot be parsed; never raises.
    """
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
        h = int(parts[0])
        m = int(parts[1])
        sec = int(parts[2]) if len(parts) > 2 else 0
        return h, m, sec
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
        # Per-row state (study_uid, image_count, duration, delay) lives
        # on the table items themselves via ``Qt.UserRole`` so:
        #   1. Aggregation for repeat study_uid emits stays sort-stable
        #      (lookup scans rows; the data follows the item).
        #   2. Stat-colour helpers read the raw values straight from the
        #      cells they're colouring, which is also sort-stable.
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.completions_table = QTableWidget()
        self.completions_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.completions_table.setAlternatingRowColors(True)
        # Slightly smaller font: the default UI size is too dense on
        # high-DPI macOS displays once we add a 10-column layout with
        # a per-row Copy button.  Drop ~3 pt (clamped to a sane min)
        # for both the table cells and the header.
        #
        # ``font.pointSize()`` may return -1 on platforms where Qt
        # chose a pixel-sized default; ``max(_FONT_MIN_PT, …)`` then
        # falls back to the floor instead of producing a negative size.
        font = self.completions_table.font()
        font.setPointSize(
            max(_FONT_MIN_PT, font.pointSize() - _FONT_REDUCTION_PT))
        self.completions_table.setFont(font)
        headers = ["", "Patient", "Study Description", "Institution",
                    "Time Acquired", "Download Completed",
                    "Download Duration", "Images", "img/min",
                    "Delay"]
        self.completions_table.setColumnCount(len(headers))
        self.completions_table.setHorizontalHeaderLabels(headers)
        self.completions_table.setSortingEnabled(True)
        header = self.completions_table.horizontalHeader()
        header.setFont(font)
        header.setSortIndicatorShown(True)
        # Interactive resize mode so the user can drag column borders
        # to widen / narrow individual columns (Excel / LibreOffice
        # style).  Size to contents ONCE so initial widths fit the
        # header labels (default 100 px would truncate "Download
        # Completed" et al.); from then on the user is in control.
        self.completions_table.resizeColumnsToContents()
        header.setSectionResizeMode(QHeaderView.Interactive)
        # When the window is wider than the natural content, let the
        # last column eat the remaining space so the layout fills the
        # viewport instead of leaving an empty grey strip on the right.
        # If the natural content is wider than the viewport, Qt shows
        # a horizontal scrollbar — no column gets crushed.
        header.setStretchLastSection(True)
        # Row height must auto-fit the per-row Copy button — the global
        # dark theme forces all QPushButtons to min-height: 22 + 12px of
        # vertical padding, which is taller than the default fixed row.
        self.completions_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents)
        self.completions_table.verticalHeader().setVisible(False)
        layout.addWidget(self.completions_table, 1)

        # ── ETE countdown row ──
        ete_row = QHBoxLayout()
        ete_row.addWidget(QLabel("Remaining:"))
        self.lbl_remaining_time = QLabel(_DASH)
        ete_row.addWidget(self.lbl_remaining_time)
        ete_row.addWidget(QLabel("  Expected completion:"))
        self.lbl_expected_completion = QLabel(_DASH)
        ete_row.addWidget(self.lbl_expected_completion)
        ete_row.addStretch()
        layout.addLayout(ete_row)

        self._remaining_seconds: float = 0.0
        self._ete_timer = QTimer(self)
        self._ete_timer.setInterval(1000)
        self._ete_timer.timeout.connect(self._tick_countdown)

        # ── Bottom bar ──
        bottom = QHBoxLayout()
        self.lbl_median_delay = QLabel(_DASH)
        bottom.addWidget(QLabel("Median delay:"))
        bottom.addWidget(self.lbl_median_delay)
        bottom.addStretch()
        self.btn_clear = QPushButton(_CLEAR_LABEL)
        self.btn_clear.clicked.connect(self._clear)
        bottom.addWidget(self.btn_clear)
        layout.addLayout(bottom)

    @staticmethod
    def _format_row_strings(image_count: Optional[int],
                            download_duration_seconds: Optional[float],
                            delay_seconds: Optional[float]
                            ) -> Dict[str, str]:
        """Build the four user-visible strings (duration, images,
        img/min, delay) shared by ``add_completion`` and the
        aggregation update path.

        Keeping the formatting in one place means a future change to
        a column's text format only has to be made once.
        """
        dl_text = (_format_delay(int(download_duration_seconds))
                   if (download_duration_seconds is not None
                       and download_duration_seconds > 0)
                   else _DASH)
        images_text = (str(image_count)
                       if image_count is not None and image_count > 0
                       else _DASH)
        if (image_count is not None and image_count > 0
                and download_duration_seconds is not None
                and download_duration_seconds > 0):
            ipm = image_count * 60.0 / download_duration_seconds
            ipm_text = f"{int(round(ipm))}"
        else:
            ipm_text = _DASH
        delay_text = (_format_delay(round(delay_seconds))
                      if delay_seconds is not None else _DASH)
        return {
            "duration": dl_text,
            "images": images_text,
            "ipm": ipm_text,
            "delay": delay_text,
        }

    def add_completion(self, *,
                       study_uid: Optional[str] = None,
                       patient_name: str, study_description: str,
                       study_time: str, completed_time: str,
                       institution_name: str = "",
                       download_duration_seconds: Optional[float] = None,
                       image_count: Optional[int] = None,
                       min_images_threshold: Optional[int] = None):
        """Add (or aggregate) a completed-study entry.

        When *study_uid* is supplied and a row with the same UID
        already exists in the table, the new values fold into that
        row: ``image_count`` and ``download_duration_seconds`` are
        summed, ``completed_time`` advances to the latest, and the
        Copy button is rebound to the latest timestamp.

        When *study_uid* is ``None`` (the legacy / unit-test case),
        no aggregation is attempted and a fresh row is inserted.

        *min_images_threshold* (optional) lets the caller suppress
        tiny entries: when the cumulative image count (existing on
        the row + ``image_count``) is below the threshold, the call
        is a no-op.  This is checked AFTER aggregation lookup so a
        first-wave 8-image emit can still be followed by a 50-image
        second wave that crosses the threshold and creates the row.
        """
        acq = _parse_time(study_time)
        comp = _parse_time(completed_time)

        formatted_acq = (
            f"{acq[0]:02d}:{acq[1]:02d}:{acq[2]:02d}"
            if acq is not None else study_time
        )
        formatted_comp = (
            f"{comp[0]:02d}:{comp[1]:02d}:{comp[2]:02d}"
            if comp is not None else completed_time
        )

        delay_seconds: Optional[float] = None
        if acq and comp:
            delay_seconds = _time_to_seconds(*comp) - _time_to_seconds(*acq)

        if study_uid is not None:
            existing_row = self._find_row_by_study_uid(study_uid)
            if existing_row >= 0:
                # Cumulative cutoff: skip the update if the running
                # total would still be below the threshold (rare —
                # if the row already passed the cutoff once, adding
                # to it keeps it above).
                if min_images_threshold is not None:
                    img_item = self.completions_table.item(
                        existing_row, _COL_IMAGES)
                    existing = (img_item.data(Qt.UserRole) or 0
                                if img_item else 0)
                    if (existing + (image_count or 0)
                            < min_images_threshold):
                        return
                self._update_existing_row(
                    existing_row,
                    formatted_comp=formatted_comp,
                    new_image_count=image_count,
                    new_duration=download_duration_seconds,
                    delay_seconds=delay_seconds,
                )
                return

        # New row path: a single emit below the threshold is dropped.
        if (min_images_threshold is not None
                and (image_count or 0) < min_images_threshold):
            return

        texts = self._format_row_strings(
            image_count, download_duration_seconds, delay_seconds)

        # Disable sorting during the insert so Qt doesn't re-sort while
        # we're still populating the row.
        was_sorting = self.completions_table.isSortingEnabled()
        self.completions_table.setSortingEnabled(False)

        row = 0
        self.completions_table.insertRow(row)

        pat_item = QTableWidgetItem(patient_name)
        if study_uid is not None:
            pat_item.setData(Qt.UserRole, study_uid)
        self.completions_table.setItem(row, _COL_PATIENT, pat_item)
        self.completions_table.setItem(
            row, _COL_STUDY_DESC, QTableWidgetItem(study_description))
        self.completions_table.setItem(
            row, _COL_INSTITUTION, QTableWidgetItem(institution_name))
        self.completions_table.setItem(
            row, _COL_ACQ, QTableWidgetItem(formatted_acq))
        self.completions_table.setItem(
            row, _COL_COMPLETED, QTableWidgetItem(formatted_comp))

        dur_item = QTableWidgetItem(texts["duration"])
        dur_item.setData(Qt.UserRole, download_duration_seconds)
        self.completions_table.setItem(row, _COL_DURATION, dur_item)

        img_item = QTableWidgetItem(texts["images"])
        img_item.setData(Qt.UserRole, image_count or 0)
        self.completions_table.setItem(row, _COL_IMAGES, img_item)

        self.completions_table.setItem(
            row, _COL_IPM, QTableWidgetItem(texts["ipm"]))

        delay_item = QTableWidgetItem(texts["delay"])
        delay_item.setData(Qt.UserRole, delay_seconds)
        self.completions_table.setItem(row, _COL_DELAY, delay_item)

        self._install_copy_button(row=row, formatted_comp=formatted_comp)

        if was_sorting:
            self.completions_table.setSortingEnabled(True)

        self._update_stats()

    def _find_row_by_study_uid(self, study_uid: str) -> int:
        """Return the table row that carries *study_uid* in its
        Patient-cell UserRole, or ``-1`` if no row matches.  Sort-
        stable: scans the table directly, no parallel list."""
        t = self.completions_table
        for row in range(t.rowCount()):
            item = t.item(row, _COL_PATIENT)
            if item is not None and item.data(Qt.UserRole) == study_uid:
                return row
        return -1

    def _install_copy_button(self, *, row: int, formatted_comp: str):
        """Place a Copy button in column 0 of *row* that copies the
        localized "Image transfer completed: HH:MM:SS" line to the
        clipboard.  Replaces any existing button at that cell so the
        captured timestamp is always the latest."""
        copy_btn = QPushButton(_COPY_LABEL)
        # Override the global dark-theme QPushButton padding/min-height
        # so the button fits comfortably inside a table row.
        copy_btn.setStyleSheet(
            "QPushButton { padding: 2px 8px; min-height: 0; }")
        prefix = tr("image_transfer_completed", self._language)
        copy_btn.clicked.connect(
            lambda checked=False, t=formatted_comp, p=prefix:
                QApplication.clipboard().setText(f"{p}: {t}"))
        self.completions_table.setCellWidget(row, 0, copy_btn)

    def _update_existing_row(self, row: int, *,
                             formatted_comp: str,
                             new_image_count: Optional[int],
                             new_duration: Optional[float],
                             delay_seconds: Optional[float]):
        """Fold a repeat ``study_completed`` emit into the existing row.

        Sums ``image_count`` and ``download_duration_seconds`` against
        what's already on the cells (stored in ``Qt.UserRole``),
        advances completed_time to the latest, re-derives delay and
        img/min from the new totals, and re-installs the Copy button
        so it grabs the latest timestamp.

        Reads/writes go through the items directly (not parallel
        lists), so this is sort-stable.
        """
        t = self.completions_table
        # Snapshot the study_uid lookup token FIRST — before any
        # mutation runs.  Future editors of this method must not be
        # able to lose the token by reordering a setText/setData call
        # ahead of the read.
        pat_item = t.item(row, _COL_PATIENT)
        study_uid = pat_item.data(Qt.UserRole) if pat_item else None

        was_sorting = t.isSortingEnabled()
        t.setSortingEnabled(False)

        dur_item = t.item(row, _COL_DURATION)
        existing_dur = dur_item.data(Qt.UserRole) or 0.0
        total_dur = existing_dur + (new_duration or 0.0)

        img_item = t.item(row, _COL_IMAGES)
        existing_images = img_item.data(Qt.UserRole) or 0
        total_images = existing_images + (new_image_count or 0)

        texts = self._format_row_strings(
            total_images if total_images > 0 else None,
            total_dur if total_dur > 0 else None,
            delay_seconds,
        )

        t.item(row, _COL_COMPLETED).setText(formatted_comp)

        dur_item.setText(texts["duration"])
        dur_item.setData(Qt.UserRole,
                         total_dur if total_dur > 0 else None)

        img_item.setText(texts["images"])
        img_item.setData(Qt.UserRole, total_images)

        t.item(row, _COL_IPM).setText(texts["ipm"])

        delay_item = t.item(row, _COL_DELAY)
        delay_item.setText(texts["delay"])
        delay_item.setData(Qt.UserRole, delay_seconds)

        if was_sorting:
            t.setSortingEnabled(True)

        # Re-resolve the row via study_uid AFTER the resort so the
        # Copy button always lands on the right study, regardless of
        # which column the user has sorted by.
        target_row = (self._find_row_by_study_uid(study_uid)
                      if study_uid is not None else row)
        if target_row < 0:
            target_row = row
        self._install_copy_button(
            row=target_row, formatted_comp=formatted_comp)

        self._update_stats()

    def _collect_column_values(self, col: int) -> list:
        """Return all non-None ``Qt.UserRole`` values stored in *col*."""
        t = self.completions_table
        out = []
        for row in range(t.rowCount()):
            item = t.item(row, col)
            if item is None:
                continue
            v = item.data(Qt.UserRole)
            if v is not None:
                out.append(v)
        return out

    def _update_stats(self):
        self._update_duration_colors()

        delays = self._collect_column_values(_COL_DELAY)
        if not delays:
            self.lbl_median_delay.setText(_DASH)
            return

        s = sorted(delays)
        n = len(s)
        mid = n // 2
        median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
        self.lbl_median_delay.setText(_format_delay(int(median)))

        if n < 2:
            return

        mean = sum(delays) / n
        variance = sum((v - mean) ** 2 for v in delays) / n
        stddev = math.sqrt(variance)
        if stddev < 0.001:
            return

        self._colour_column_by_bands(
            _COL_DELAY,
            high=median + _STDDEV_BAND_MULTIPLIER * stddev,
            low=median - _STDDEV_BAND_MULTIPLIER * stddev,
        )

    def _update_duration_colors(self):
        """Color the Download Duration column: red if > median + 2σ,
        green if < median - 2σ. Uses 2σ (vs 1σ for Delay) because
        download time varies more with study size."""
        durations = self._collect_column_values(_COL_DURATION)
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

        self._colour_column_by_bands(
            _COL_DURATION,
            high=median + _STDDEV_BAND_MULTIPLIER * stddev,
            low=median - _STDDEV_BAND_MULTIPLIER * stddev,
        )

    def _colour_column_by_bands(self, col: int, *,
                                high: float, low: float):
        """Paint the cells in *col* red above *high*, green below
        *low*, white otherwise.  Reads the raw value from each cell's
        ``Qt.UserRole``, so this is sort-stable."""
        t = self.completions_table
        for row in range(t.rowCount()):
            item = t.item(row, col)
            if item is None:
                continue
            d = item.data(Qt.UserRole)
            if d is None:
                continue
            if d > high:
                item.setForeground(QBrush(_RED))
            elif d < low:
                item.setForeground(QBrush(_GREEN))
            else:
                item.setForeground(QBrush(QColor("white")))

    def update_transfer_progress(self, pending_images: int,
                                 images_per_minute: float):
        """Update the ETE countdown from the current queue state."""
        if pending_images <= 0 or images_per_minute <= 0:
            self._remaining_seconds = 0.0
            self._ete_timer.stop()
            self.lbl_remaining_time.setText(_DASH)
            self.lbl_expected_completion.setText(_DASH)
            return
        self._remaining_seconds = pending_images / images_per_minute * 60.0
        self._refresh_countdown_labels()
        if not self._ete_timer.isActive():
            self._ete_timer.start()

    def _tick_countdown(self):
        self._remaining_seconds = max(self._remaining_seconds - 1.0, 0.0)
        if self._remaining_seconds <= 0:
            self._ete_timer.stop()
            self.lbl_remaining_time.setText(_DASH)
            self.lbl_expected_completion.setText(_DASH)
            return
        self._refresh_countdown_labels()

    def _refresh_countdown_labels(self):
        secs = int(self._remaining_seconds)
        if secs >= 3600:
            h = secs // 3600
            m = (secs % 3600) // 60
            s = secs % 60
            self.lbl_remaining_time.setText(f"{h}:{m:02d}:{s:02d}")
        else:
            m = secs // 60
            s = secs % 60
            self.lbl_remaining_time.setText(f"{m}:{s:02d}")
        expected = datetime.now() + timedelta(seconds=secs)
        self.lbl_expected_completion.setText(expected.strftime("%H:%M:%S"))

    def _clear(self):
        self.completions_table.setRowCount(0)
        self.lbl_median_delay.setText(_DASH)
        self._remaining_seconds = 0.0
        self._ete_timer.stop()
        self.lbl_remaining_time.setText(_DASH)
        self.lbl_expected_completion.setText(_DASH)
