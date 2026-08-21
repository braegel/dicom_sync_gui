"""
The dashboard's Series Queue table.

Extracted from ``gui.dashboard``, which had grown to hold the service
controls, the restart/watchdog state machine, the sound handling, the
study-rate section AND every detail of rendering this table.  Everything
here is presentation: given a queue (a list of ``SeriesJob.to_dict()``
mappings) it decides which cells to (re)build and what colour they get.

The performance notes below are load-bearing — the table is redrawn
after every completed series, on queues of several hundred rows, on the
GUI thread.  Two rules follow from that and must not be undone:

* never use ``QHeaderView.ResizeToContents`` on a data column (it
  re-measures every row on every ``dataChanged``, i.e. on every
  ``setText`` — O(rows²) per update, which pinned the GUI thread at
  100% and froze the app);
* prefer ``setText`` on an existing item over allocating a fresh
  ``QTableWidgetItem``, and wrap multi-cell passes in
  ``setUpdatesEnabled(False)`` so Qt coalesces one repaint.

The view deliberately knows nothing about the active transfer: callers
pass a ``pending_for`` callable, so the "discount images that already
arrived" rule stays with the dashboard, which owns that state.
"""

import itertools
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.dicom_time import format_date_time, format_duration
from core.transfer_engine import TERMINAL_STATUSES
from gui.study_rate import COLOR_GREEN, COLOR_RED
from gui.styles import (
    COLOR_BLUE_ACCENT, COLOR_MUTED, COLOR_ORANGE, TEXT_DEFAULT,
)

# Placeholder shown wherever a value is unknown or not applicable.
_DASH = "—"

# Column indices.  The order is load-bearing across every render path
# here, so name them rather than scattering bare integers: "Series
# Created" in particular is appended LAST on purpose, so that adding it
# could not shift the indices the other paths already used.
COL_CHECK = 0        # hidden outside manual-selection mode
COL_PATIENT = 1
COL_STUDY = 2
COL_SERIES = 3
COL_MODALITY = 4
COL_IMAGES = 5
COL_PENDING = 6
COL_IPM = 7
COL_STATUS = 8
COL_ETE = 9
COL_GROUP = 10       # hidden while institution filtering is off
COL_CREATED = 11     # appended last on purpose — see above

# Sensible one-time default widths (px) for the non-stretching columns.
# Up here with the COL_* indices rather than buried in the builder, so
# the column metadata reads as one block.
DEFAULT_COLUMN_WIDTHS = (
    (COL_CHECK, 28), (COL_MODALITY, 70), (COL_IMAGES, 70),
    (COL_PENDING, 80), (COL_IPM, 70), (COL_STATUS, 120),
    (COL_ETE, 80), (COL_GROUP, 110), (COL_CREATED, 130),
)
COLUMN_COUNT = 12

HEADERS = [
    "☑", "Patient", "Study", "Series", "Modality",
    "Images", "Pending", "img/min", "Status", "ETE", "Group",
    "Series Created",
]


# ── Pure formatters ──────────────────────────────────────────────────────
# Free functions, not methods: they need no widget and are unit-tested
# directly.  ``SourceDashboard`` re-exports them as staticmethods so the
# historical ``dashboard._format_ete(...)`` call sites keep working.

def format_ete(seconds: float) -> str:
    """Remaining time as ``m:ss`` / ``h:mm:ss``, or a dash when there is
    nothing to count down."""
    if seconds <= 0:
        return _DASH
    return format_duration(seconds)


def format_series_created(date_digits: str, time_digits: str) -> str:
    """When the series was acquired, as ``DD.MM.YYYY HH:MM``, dash when
    the PACS sent neither part."""
    return format_date_time(date_digits, time_digits, empty=_DASH)


def ipm_text(job: dict) -> str:
    """The img/min cell's display text for *job* — used both to render
    the cell and as the change-detection key for the in-place update
    cache."""
    ipm = job.get("images_per_minute", 0.0)
    return f"{ipm:.0f}" if job["status"] == "done" and ipm > 0 else _DASH


def status_text(status: str) -> str:
    return {
        "queued": "⏳ Queued",
        "transferring": "▶ Transferring...",
        "done": "✓ Done",
        "error": "✗ Error",
        "skipped": "— Skipped",
        "unavailable": "⊘ Not available",
    }.get(status, status)


def status_color(status: str) -> QColor:
    return {
        "queued": QColor(COLOR_MUTED),
        "transferring": QColor(COLOR_ORANGE),
        "done": QColor(COLOR_GREEN),
        "error": QColor(COLOR_RED),
        "skipped": QColor(COLOR_MUTED),
        "unavailable": QColor(COLOR_RED),
    }.get(status, QColor(TEXT_DEFAULT))


# A callable that returns the pending image count for one queue job.
PendingFor = Callable[[dict], int]


class QueueTableView(QWidget):
    """The Series Queue table plus the render logic that keeps it cheap.

    ``table`` is exposed as a plain attribute: the dashboard publishes it
    as ``series_table`` so existing call sites (and a large body of
    tests) keep addressing the underlying ``QTableWidget`` directly.
    """

    def __init__(self, config: Any,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        # series_uid sequence currently rendered.  Lets the next render
        # choose between a cheap in-place cell update (uid sequence
        # unchanged — the common per-series progress case) and a full
        # rebuild.  ``None`` means "no valid normal-mode render" and
        # forces a rebuild; it is reset whenever the table content comes
        # from somewhere else (selection mode, ``clear()``).
        self._rendered_uids: Optional[list[str]] = None
        # Per-row cache of the last-rendered Status and img/min text,
        # keyed by series_uid.  The in-place update only rewrites those
        # two (brush-carrying) cells when the cached value actually
        # changed, avoiding a setItem storm on every completed series.
        self._cell_cache: dict = {}
        self.table = self._build_table()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)

    # ── Construction ─────────────────────────────────────────────────

    def _build_table(self) -> QTableWidget:
        table = QTableWidget()
        table.setColumnCount(COLUMN_COUNT)
        table.setHorizontalHeaderLabels(HEADERS)
        header = table.horizontalHeader()
        # Resize strategy is performance-critical — see the module
        # docstring.  The data columns use fixed/Interactive widths
        # (measured once, never re-measured on update); only the three
        # text columns Stretch, which just distributes spare width and
        # does NOT trigger per-row measuring.
        header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
        for col in (COL_PATIENT, COL_STUDY, COL_SERIES):
            header.setSectionResizeMode(col, QHeaderView.Stretch)
        for col in (COL_MODALITY, COL_IMAGES, COL_PENDING, COL_IPM,
                    COL_STATUS, COL_ETE, COL_GROUP, COL_CREATED):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
        # One-time defaults; the user can drag them afterwards.
        for col, width in DEFAULT_COLUMN_WIDTHS:
            table.setColumnWidth(col, width)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setColumnHidden(COL_CHECK, True)
        table.setColumnHidden(
            COL_GROUP, not self.config.filter_groups_enabled)
        return table

    # ── Column visibility ────────────────────────────────────────────

    def set_group_column_visible(self, visible: bool) -> None:
        self.table.setColumnHidden(COL_GROUP, not visible)

    def set_check_column_visible(self, visible: bool) -> None:
        self.table.setColumnHidden(COL_CHECK, not visible)

    # ── Cell builders ────────────────────────────────────────────────
    # Single source of truth per mutable column — shared by the
    # full-rebuild and in-place paths so text and colour can never drift
    # between them.

    @staticmethod
    def _make_pending_item(pending: int) -> QTableWidgetItem:
        return QTableWidgetItem(str(pending))

    @staticmethod
    def _make_ipm_item(job: dict) -> QTableWidgetItem:
        """img/min column: rate in blue once the series is done."""
        ipm = job.get("images_per_minute", 0.0)
        if job["status"] == "done" and ipm > 0:
            item = QTableWidgetItem(f"{ipm:.0f}")
            item.setForeground(QColor(COLOR_BLUE_ACCENT))
        else:
            item = QTableWidgetItem(_DASH)
            item.setForeground(QColor(COLOR_MUTED))
        item.setTextAlignment(Qt.AlignCenter)
        return item

    @staticmethod
    def _make_status_item(status: str) -> QTableWidgetItem:
        """Status column: human-readable text + status colour."""
        item = QTableWidgetItem(status_text(status))
        item.setForeground(status_color(status))
        return item

    @staticmethod
    def _make_series_created_item(job: dict) -> QTableWidgetItem:
        """Series Created column: when the series was acquired on the
        modality (SeriesDate/SeriesTime, with study date/time fallback)."""
        item = QTableWidgetItem(format_series_created(
            job.get("series_date", ""), job.get("series_time", "")))
        item.setForeground(QColor(COLOR_MUTED))
        return item

    @staticmethod
    def _make_ete_item(status: str, cumulative_pending: int,
                       rate: float) -> QTableWidgetItem:
        """ETE column: check-mark when done, dash when dead or unknown,
        otherwise the cumulative pending image count for this and all
        preceding rows divided by the current rate."""
        if status in TERMINAL_STATUSES:
            item = QTableWidgetItem("✓" if status == "done" else _DASH)
            item.setForeground(QColor(
                COLOR_GREEN if status == "done" else COLOR_MUTED))
        elif rate > 0:
            item = QTableWidgetItem(format_ete(cumulative_pending / rate))
            item.setForeground(QColor(COLOR_ORANGE))
        else:
            item = QTableWidgetItem(_DASH)
            item.setForeground(QColor(COLOR_MUTED))
        item.setTextAlignment(Qt.AlignCenter)
        return item

    def _set_static_cells(self, row: int, job: dict) -> None:
        """Fill the columns keyed by series_uid, which therefore cannot
        change between emits.  Shared by the full rebuild and the
        selection-mode render, which populated them identically."""
        t = self.table
        t.setItem(row, COL_PATIENT, QTableWidgetItem(job["patient_name"]))
        t.setItem(row, COL_STUDY,
                  QTableWidgetItem(job["study_description"]))
        t.setItem(row, COL_SERIES,
                  QTableWidgetItem(job["series_description"]))
        t.setItem(row, COL_MODALITY, QTableWidgetItem(job["modality"]))
        t.setItem(row, COL_IMAGES,
                  QTableWidgetItem(str(job["remote_count"])))
        group = self.config.institution_assignments.get(
            job.get("institution_name", ""), "")
        t.setItem(row, COL_GROUP, QTableWidgetItem(group))
        t.setItem(row, COL_CREATED, self._make_series_created_item(job))

    def _set_pending_text(self, row: int, pending: int) -> None:
        """Update the Pending cell's TEXT in place when an item already
        exists, building a fresh one otherwise."""
        existing = self.table.item(row, COL_PENDING)
        if existing is not None:
            existing.setText(str(pending))
        else:
            self.table.setItem(
                row, COL_PENDING, self._make_pending_item(pending))

    def _set_ete_text(self, row: int, status: str,
                      cumulative_pending: int, rate: float) -> None:
        """Update only the ETE cell's TEXT in place when an item already
        exists, falling back to building a fresh item otherwise.  Avoids
        re-creating the item (and re-setting its brush/alignment) on
        every 1 Hz tick."""
        existing = self.table.item(row, COL_ETE)
        if existing is None:
            self.table.setItem(
                row, COL_ETE,
                self._make_ete_item(status, cumulative_pending, rate))
            return
        if status in TERMINAL_STATUSES or rate <= 0:
            # Terminal / unknown-rate cells don't count down; leave the
            # item the queue path already rendered untouched.
            return
        existing.setText(format_ete(cumulative_pending / rate))

    # ── Render paths ─────────────────────────────────────────────────

    @staticmethod
    def _cumulative_pending(queue: list, pending_for: PendingFor) -> list:
        """Cumulative pending-image counts per queue row."""
        return list(itertools.accumulate(pending_for(j) for j in queue))

    def render(self, queue: list, rate: float,
               pending_for: PendingFor) -> None:
        """Render *queue*, choosing the cheapest path.

        The engine emits the full queue after EVERY completed series, so
        unconditionally tearing the table down and rebuilding it is O(n²)
        widget churn per cycle, flickers visibly, and destroys the user's
        row selection on each emit.  Instead compare the incoming
        series_uid sequence with the one currently rendered:

        * sequence differs (new cycle, selection filtering, first
          render) → full rebuild;
        * sequence identical (the common per-series progress emit) →
          update only the mutable cells in place.  Row selection
          intentionally survives these updates.
        """
        self.set_check_column_visible(False)
        uids = [job["series_uid"] for job in queue]
        if uids == self._rendered_uids:
            self._update_in_place(queue, rate, pending_for)
        else:
            self._rebuild(queue, rate, pending_for)
            self._rendered_uids = uids

    def _rebuild(self, queue: list, rate: float,
                 pending_for: PendingFor) -> None:
        """Full rebuild (uid sequence changed).  Wrapped in
        ``setUpdatesEnabled(False)`` so the whole rebuild repaints once.
        Resets the cell cache since every row is re-rendered fresh."""
        cumulative = self._cumulative_pending(queue, pending_for)
        self._cell_cache.clear()

        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(0)
            for i, job in enumerate(queue):
                row = self.table.rowCount()
                self.table.insertRow(row)
                self._set_static_cells(row, job)
                self.table.setItem(
                    row, COL_PENDING,
                    self._make_pending_item(pending_for(job)))
                self.table.setItem(row, COL_IPM, self._make_ipm_item(job))
                self.table.setItem(
                    row, COL_STATUS, self._make_status_item(job["status"]))
                self.table.setItem(
                    row, COL_ETE,
                    self._make_ete_item(job["status"], cumulative[i], rate))
                self._cell_cache[job["series_uid"]] = (
                    job["status"], ipm_text(job))
        finally:
            self.table.setUpdatesEnabled(True)

    def _update_in_place(self, queue: list, rate: float,
                         pending_for: PendingFor) -> None:
        """Same uid sequence as currently rendered — refresh only the
        cells that can change between per-series progress emits.  The
        static columns are keyed by series_uid and cannot have changed.

        This runs after EVERY completed series, so it must be cheap: see
        the module docstring for why the whole pass is wrapped in
        ``setUpdatesEnabled(False)`` and why only genuinely-changed
        brush-carrying cells are rebuilt."""
        cumulative = self._cumulative_pending(queue, pending_for)

        self.table.setUpdatesEnabled(False)
        try:
            for i, job in enumerate(queue):
                uid = job["series_uid"]
                cached = self._cell_cache.get(uid, (None, None))

                self._set_pending_text(i, pending_for(job))

                # img/min and Status carry a colour brush, so a plain
                # setText would not recolour them — rebuild only on an
                # actual change.
                status = job["status"]
                text = ipm_text(job)
                if cached != (status, text):
                    self.table.setItem(i, COL_IPM,
                                       self._make_ipm_item(job))
                    self.table.setItem(i, COL_STATUS,
                                       self._make_status_item(status))
                    self._cell_cache[uid] = (status, text)

                self._set_ete_text(i, status, cumulative[i], rate)
        finally:
            self.table.setUpdatesEnabled(True)

    def render_for_selection(self, queue: list) -> None:
        """Render the manual-selection view: a checkbox per row and
        "Waiting" statuses, with no rate-derived columns."""
        # The table now holds selection-mode rows — force the next
        # ``render`` to do a full rebuild even if the uid sequence
        # happens to match.
        self._rendered_uids = None
        self._cell_cache.clear()
        self.table.setRowCount(0)
        self.set_check_column_visible(True)

        for job in queue:
            row = self.table.rowCount()
            self.table.insertRow(row)

            cb_item = QTableWidgetItem()
            cb_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            cb_item.setCheckState(Qt.Checked)
            cb_item.setData(Qt.UserRole, job["series_uid"])
            self.table.setItem(row, COL_CHECK, cb_item)

            self._set_static_cells(row, job)
            pending = job["remote_count"] - job["local_count"]
            self.table.setItem(row, COL_PENDING,
                               QTableWidgetItem(str(max(pending, 0))))
            self.table.setItem(row, COL_IPM, QTableWidgetItem(_DASH))
            status_item = QTableWidgetItem("⏳ Waiting")
            status_item.setForeground(QColor(COLOR_ORANGE))
            self.table.setItem(row, COL_STATUS, status_item)
            self.table.setItem(row, COL_ETE, QTableWidgetItem(_DASH))

        # Allow checking/unchecking in the table.
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

    def refresh_pending_and_ete(self, queue: list, rate: float,
                                pending_for: PendingFor) -> None:
        """Refresh just the Pending and ETE columns so both count down
        smoothly between queue/stats signals.  Updates the TEXT of
        existing cells rather than allocating fresh items every tick — at
        1 Hz across a large queue (and several source tabs) that churn
        alone made the UI unresponsive during a download."""
        rows = self.table.rowCount()
        cumulative = self._cumulative_pending(queue, pending_for)
        for i, job in enumerate(queue):
            if i >= rows:
                break
            self._set_pending_text(i, pending_for(job))
            self._set_ete_text(i, job["status"], cumulative[i], rate)

    def update_ete_column(self, queue: list, rate: float,
                          pending_for: PendingFor) -> None:
        """Update only the ETE column, without rebuilding the table."""
        rows = self.table.rowCount()
        cumulative = self._cumulative_pending(queue, pending_for)
        for i, job in enumerate(queue):
            if i >= rows:
                break
            self._set_ete_text(i, job["status"], cumulative[i], rate)

    # ── Selection helpers ────────────────────────────────────────────

    def set_all_checked(self, checked: bool) -> None:
        """Check or uncheck every row of the selection table."""
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.table.rowCount()):
            cb_item = self.table.item(row, COL_CHECK)
            if cb_item is not None:
                cb_item.setCheckState(state)

    def checked_series_uids(self) -> list:
        """The series UIDs the user left ticked in selection mode."""
        selected = []
        for row in range(self.table.rowCount()):
            cb_item = self.table.item(row, COL_CHECK)
            if cb_item and cb_item.checkState() == Qt.Checked:
                uid = cb_item.data(Qt.UserRole)
                if uid:
                    selected.append(uid)
        return selected

    # ── Teardown ─────────────────────────────────────────────────────

    def clear(self) -> None:
        """Empty the table and drop the render caches, so the next
        ``render`` rebuilds from scratch."""
        self.table.setRowCount(0)
        self._cell_cache.clear()
        self._rendered_uids = None
