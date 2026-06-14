"""
Live download completions window.

Shows in-memory data from running transfer engines: PatientName,
StudyDescription, Time Acquired, Time Download Completed, Institution,
and the delay between acquisition and download with color coding.
"""

from datetime import datetime, timedelta
from typing import Dict, Optional

from core.stats_utils import median, median_and_pstdev

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

# Source-PACS tag stored on the Delay and Download Duration items.
# The stat colouring and the median-delay label group by this value so
# a slow source PACS doesn't paint a fast one's rows red (and vice
# versa).  Qt.UserRole itself holds the raw numeric cell value.
_ROLE_SOURCE = Qt.UserRole + 1

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


def _time_to_seconds(h: int, m: int, s: int) -> int:
    return h * 3600 + m * 60 + s


def _format_delay(total_seconds: int) -> str:
    # Callers pass non-negative values (cross-midnight delays are
    # normalized into [0, 24h) in add_completion); abs() stays as a
    # defensive guard against any future negative input.
    m, s = divmod(abs(total_seconds), 60)
    return f"{m}:{s:02d}"


def _datetime_sort_key(date_digits: str,
                       parsed_time: Optional[tuple]) -> str:
    """Build a lexically-sortable ``YYYYMMDDHHMMSS`` key from a DICOM
    date (``YYYYMMDD``; may be empty) and a parsed ``(h, m, s)`` tuple.

    The Time Acquired / Download Completed cells only *display* the
    time of day, so sorting on their text alone would interleave
    studies from different days.  Sorting on this key instead orders
    them chronologically.  A missing date sorts before any dated row;
    a missing time sorts before any timed row within the same date —
    both edge cases only arise for legacy/test rows that omit a date.
    """
    date_part = (date_digits or "").strip()
    if parsed_time is not None:
        time_part = (f"{parsed_time[0]:02d}{parsed_time[1]:02d}"
                     f"{parsed_time[2]:02d}")
    else:
        time_part = ""
    return f"{date_part}{time_part}"


class _DateTimeItem(QTableWidgetItem):
    """Table item whose sort order follows a comparable key stored in
    ``Qt.UserRole`` rather than its displayed text.

    Used for the Time Acquired / Download Completed columns so they
    sort by the full ``YYYYMMDDHHMMSS`` timestamp (date + time) while
    still showing only ``HH:MM:SS``.  Falls back to the default
    text comparison when either item lacks a key (e.g. an empty
    placeholder cell)."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        own = self.data(Qt.UserRole)
        their = other.data(Qt.UserRole)
        if own is not None and their is not None:
            return own < their
        return super().__lt__(other)


class LiveCompletionsWindow(QWidget):

    def __init__(self, parent: Optional[QWidget] = None,
                 language: str = "en") -> None:
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

    def set_language(self, language: str) -> None:
        """Update the window's UI language.  The per-row Copy buttons
        resolve their localized prefix at click time, so existing rows
        pick up the new language without rebuilding."""
        self._language = language

    def _setup_ui(self) -> None:
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
                       study_date: str = "", completed_date: str = "",
                       institution_name: str = "",
                       download_duration_seconds: Optional[float] = None,
                       image_count: Optional[int] = None,
                       min_images_threshold: Optional[int] = None,
                       source: str = "") -> None:
        """Add (or aggregate) a completed-study entry.

        *study_date* / *completed_date* (DICOM ``YYYYMMDD``) make the
        Time Acquired / Download Completed columns sort chronologically
        across days: the cells show only the time of day, but sorting
        keys on the full date+time so an exam acquired late yesterday
        does not sort ahead of one acquired this morning.

        *source* names the source PACS the study came from.  Delay /
        duration colour bands and the median-delay readout are computed
        per source, so entries from different PACS never share
        statistics.

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

        acq_sort = _datetime_sort_key(study_date, acq)
        comp_sort = _datetime_sort_key(completed_date, comp)

        delay_seconds: Optional[float] = None
        if acq and comp:
            delay_seconds = _time_to_seconds(*comp) - _time_to_seconds(*acq)
            # Both timestamps are time-of-day only (no date).  A
            # download completion can only happen AFTER acquisition,
            # so a negative diff means midnight was crossed (e.g.
            # acquired 23:55, completed 00:05) — wrap it forward by
            # 24 h to get the true delay.  acq == comp stays 0.
            # Limitation: with time-of-day-only inputs a genuine
            # >24 h delay cannot be represented; it aliases into
            # [0, 24h).  Acceptable — same-day (or overnight)
            # completions are the only realistic case here.
            if delay_seconds < 0:
                delay_seconds += 24 * 3600

        if study_uid is not None:
            existing_row = self._find_row_by_study_uid(study_uid, source)
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
                    comp_sort=comp_sort,
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
        acq_item = _DateTimeItem(formatted_acq)
        acq_item.setData(Qt.UserRole, acq_sort)
        self.completions_table.setItem(row, _COL_ACQ, acq_item)

        comp_item = _DateTimeItem(formatted_comp)
        comp_item.setData(Qt.UserRole, comp_sort)
        self.completions_table.setItem(row, _COL_COMPLETED, comp_item)

        dur_item = QTableWidgetItem(texts["duration"])
        dur_item.setData(Qt.UserRole, download_duration_seconds)
        dur_item.setData(_ROLE_SOURCE, source)
        self.completions_table.setItem(row, _COL_DURATION, dur_item)

        img_item = QTableWidgetItem(texts["images"])
        img_item.setData(Qt.UserRole, image_count or 0)
        self.completions_table.setItem(row, _COL_IMAGES, img_item)

        self.completions_table.setItem(
            row, _COL_IPM, QTableWidgetItem(texts["ipm"]))

        delay_item = QTableWidgetItem(texts["delay"])
        delay_item.setData(Qt.UserRole, delay_seconds)
        delay_item.setData(_ROLE_SOURCE, source)
        self.completions_table.setItem(row, _COL_DELAY, delay_item)

        self._install_copy_button(row=row, formatted_comp=formatted_comp)

        if was_sorting:
            self.completions_table.setSortingEnabled(True)

        self._update_stats()

    def _find_row_by_study_uid(self, study_uid: str,
                               source: str = "") -> int:
        """Return the table row that carries *study_uid* in its
        Patient-cell UserRole AND was added by *source*, or ``-1`` if
        no row matches.  Matching on the source too keeps repeat emits
        from a second source PACS (same study on two PACS) out of the
        first source's row — they get their own row, so the per-source
        statistics stay clean.  Sort-stable: scans the table directly,
        no parallel list."""
        t = self.completions_table
        for row in range(t.rowCount()):
            item = t.item(row, _COL_PATIENT)
            if item is None or item.data(Qt.UserRole) != study_uid:
                continue
            delay_item = t.item(row, _COL_DELAY)
            row_source = (delay_item.data(_ROLE_SOURCE) or ""
                          if delay_item else "")
            if row_source == (source or ""):
                return row
        return -1

    def _install_copy_button(self, *, row: int,
                             formatted_comp: str) -> None:
        """Place a Copy button in column 0 of *row* that copies the
        localized "Image transfer completed: HH:MM:SS" line to the
        clipboard.  Replaces any existing button at that cell so the
        captured timestamp is always the latest."""
        copy_btn = QPushButton(_COPY_LABEL)
        # Override the global dark-theme QPushButton padding/min-height
        # so the button fits comfortably inside a table row.
        copy_btn.setStyleSheet(
            "QPushButton { padding: 2px 8px; min-height: 0; }")
        # Resolve the localized prefix at CLICK time (not button-build
        # time) by reading ``self._language`` inside the slot — so a
        # language change after the row was added still copies the right
        # wording, and the window's current language always wins.
        copy_btn.clicked.connect(
            lambda checked=False, t=formatted_comp:
                QApplication.clipboard().setText(
                    f"{tr('image_transfer_completed', self._language)}: {t}"))
        self.completions_table.setCellWidget(row, 0, copy_btn)

    def _update_existing_row(self, row: int, *,
                             formatted_comp: str,
                             comp_sort: str,
                             new_image_count: Optional[int],
                             new_duration: Optional[float],
                             delay_seconds: Optional[float]) -> None:
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
        delay_for_source = t.item(row, _COL_DELAY)
        row_source = (delay_for_source.data(_ROLE_SOURCE) or ""
                      if delay_for_source else "")

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

        comp_item = t.item(row, _COL_COMPLETED)
        comp_item.setText(formatted_comp)
        # Keep the chronological sort key in step with the advanced
        # completion time (the row may now be a later day than at
        # insert).
        comp_item.setData(Qt.UserRole, comp_sort)

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
        target_row = (self._find_row_by_study_uid(study_uid, row_source)
                      if study_uid is not None else row)
        if target_row < 0:
            target_row = row
        self._install_copy_button(
            row=target_row, formatted_comp=formatted_comp)

        self._update_stats()

    def _collect_column_items_by_source(self, col: int) -> Dict[str, list]:
        """Return ``{source: [(item, value), …]}`` for all cells in
        *col* whose ``Qt.UserRole`` value is non-None.  Rows added
        without a source (legacy callers, unit tests) group under
        ``""``."""
        t = self.completions_table
        groups: Dict[str, list] = {}
        for row in range(t.rowCount()):
            item = t.item(row, col)
            if item is None:
                continue
            v = item.data(Qt.UserRole)
            if v is None:
                continue
            src = item.data(_ROLE_SOURCE) or ""
            groups.setdefault(src, []).append((item, v))
        return groups

    def _update_stats(self) -> None:
        # Colour both stat columns: red if > median + 2σ, green if
        # < median − 2σ (±2σ keeps only the strong outliers visible;
        # since 1.0.12 Delay and Download Duration share the same
        # threshold).  Bands and medians are computed per source PACS
        # so one slow source doesn't skew another's statistics.
        self._colour_column_by_stat_bands(_COL_DURATION)

        by_source = self._collect_column_items_by_source(_COL_DELAY)
        if not by_source:
            self.lbl_median_delay.setText(_DASH)
            return
        medians = {src: median([v for _, v in items])
                   for src, items in by_source.items()}
        if len(medians) == 1:
            # Single source (or untagged rows): plain value, no prefix.
            med = next(iter(medians.values()))
            self.lbl_median_delay.setText(_format_delay(int(med)))
        else:
            self.lbl_median_delay.setText("   ".join(
                f"{src or _DASH}: {_format_delay(int(med))}"
                for src, med in sorted(medians.items())))

        self._colour_column_by_stat_bands(_COL_DELAY)

    def _colour_column_by_stat_bands(self, col: int) -> None:
        """Per source PACS, compute median ± 2σ over the raw
        (``Qt.UserRole``) values of *col* and paint that source's
        cells red above the band, green below it, white otherwise.
        Reads raw values straight from the cells, so it's sort-stable.

        A source with fewer than two values or (near) zero spread is
        skipped — identical values get no colouring.  Shared by the
        Delay and Download Duration columns."""
        for items in self._collect_column_items_by_source(col).values():
            if len(items) < 2:
                continue
            med, stddev = median_and_pstdev([v for _, v in items])
            if stddev < 0.001:
                continue
            high = med + _STDDEV_BAND_MULTIPLIER * stddev
            low = med - _STDDEV_BAND_MULTIPLIER * stddev
            for item, v in items:
                if v > high:
                    item.setForeground(QBrush(_RED))
                elif v < low:
                    item.setForeground(QBrush(_GREEN))
                else:
                    item.setForeground(QBrush(QColor("white")))

    def update_transfer_progress(self, pending_images: int,
                                 images_per_minute: float) -> None:
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

    def _tick_countdown(self) -> None:
        self._remaining_seconds = max(self._remaining_seconds - 1.0, 0.0)
        if self._remaining_seconds <= 0:
            self._ete_timer.stop()
            self.lbl_remaining_time.setText(_DASH)
            self.lbl_expected_completion.setText(_DASH)
            return
        self._refresh_countdown_labels()

    def _refresh_countdown_labels(self) -> None:
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

    def _clear(self) -> None:
        self.completions_table.setRowCount(0)
        self.lbl_median_delay.setText(_DASH)
        self._remaining_seconds = 0.0
        self._ete_timer.stop()
        self.lbl_remaining_time.setText(_DASH)
        self.lbl_expected_completion.setText(_DASH)
