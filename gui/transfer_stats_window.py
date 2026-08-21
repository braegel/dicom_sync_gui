"""
Transfer Performance Statistics window.

Displays statistical analysis of the SQLite transfer performance log,
accessible via the View menu.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtCharts import QChart, QChartView, QBoxPlotSeries, QBoxSet
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QGroupBox, QGridLayout, QHeaderView,
    QPushButton, QComboBox, QTabWidget,
)
from PySide6.QtGui import QFont, QPainter

from core.stats_utils import median, tukey_quartiles
from core.transfer_log import TransferLog
from gui.async_helpers import run_in_background

logger = logging.getLogger("dicom_sync")

# Cap on the rows rendered into the two browsable detail tables.
#
# ``_refresh`` runs the QUERY on a worker thread, but the table
# population below is unavoidably GUI-thread work: one QTableWidgetItem
# per cell.  After months of 24/7 logging the log holds >10^5 series
# rows, so rendering all of them allocated ~10^6 items in a single event
# loop slice and froze the window on every filter change.
#
# Only these two tables are capped, and they show the MOST RECENT rows.
# The summary, the boxplot, and the per-source / per-modality breakdowns
# keep aggregating the FULL result set, so no statistic changes.
MAX_DETAIL_ROWS = 500


@dataclass
class _SourceTotals:
    """Per-source accumulator for the "By Source" table.

    Deliberately a fixed shape: the study rows and the series rows are
    accumulated in two separate loops, and when this was a plain dict
    grown key-by-key a source that only ever appeared in one of the two
    loops ended up without the other loop's keys.  Every read then had
    to guess a fallback (``d.get("series", 0)``), and the guess was the
    only thing keeping the render loop from a KeyError.  One dataclass
    means every source carries all four fields from the start.
    """
    studies: int = 0
    images: int = 0
    series: int = 0
    mbps_vals: list[float] = field(default_factory=list)


class TransferStatsWindow(QWidget):

    def __init__(self, db_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        # Open one TransferLog for the window's lifetime instead of
        # re-creating (with the three CREATE TABLE IF NOT EXISTS round
        # trips) on every filter change.
        self._log = TransferLog(db_path)
        # Monotonic refresh counter: queries run on a worker thread,
        # so a slow result must not overwrite a newer one.
        self._refresh_seq = 0
        self.setWindowTitle("Transfer Performance Statistics")
        self.setWindowFlags(Qt.Window)
        self.setMinimumSize(800, 500)
        self._setup_ui()
        self._refresh()

    def shutdown(self) -> None:
        """Release the SQLite connection.  Call this when the window is
        really finished with — NOT from ``closeEvent``.

        The owner (``MainWindow``) keeps one instance and re-``show()``s
        it, so closing the log on every window close left the cached
        window pointing at a dead connection: each later query raised
        ``sqlite3.ProgrammingError``, which ``_refresh``'s worker catches
        and turns into empty results — the user saw blank statistics with
        no error.  Closing is therefore tied to the owner's lifetime, not
        to the window being hidden.
        """
        try:
            self._log.close()
        except Exception:
            logger.debug("TransferStatsWindow: closing log failed",
                         exc_info=True)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        self._build_filters(layout)
        self._build_summary(layout)
        self._build_chart(layout)
        self._build_breakdown_tables(layout)
        self._build_detail_tabs(layout)

    def _build_filters(self, layout: QVBoxLayout) -> None:
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Source:"))
        self.filter_source = QComboBox()
        self.filter_source.currentIndexChanged.connect(self._refresh)
        filter_row.addWidget(self.filter_source)
        filter_row.addWidget(QLabel("Modality:"))
        self.filter_modality = QComboBox()
        self.filter_modality.currentIndexChanged.connect(self._refresh)
        filter_row.addWidget(self.filter_modality)
        filter_row.addStretch()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)
        filter_row.addWidget(self.btn_refresh)
        layout.addLayout(filter_row)

    def _build_summary(self, layout: QVBoxLayout) -> None:
        self.summary_group = QGroupBox("Summary")
        sg = QGridLayout(self.summary_group)
        self.lbl_total_studies = QLabel("—")
        self.lbl_total_series = QLabel("—")
        self.lbl_total_images = QLabel("—")
        self.lbl_date_range = QLabel("—")
        self.lbl_median_mbps = QLabel("—")
        sg.addWidget(QLabel("Studies:"), 0, 0)
        sg.addWidget(self.lbl_total_studies, 0, 1)
        sg.addWidget(QLabel("Series:"), 0, 2)
        sg.addWidget(self.lbl_total_series, 0, 3)
        sg.addWidget(QLabel("Images:"), 0, 4)
        sg.addWidget(self.lbl_total_images, 0, 5)
        sg.addWidget(QLabel("Date range:"), 1, 0)
        sg.addWidget(self.lbl_date_range, 1, 1, 1, 3)
        sg.addWidget(QLabel("Median Mbit/s:"), 1, 4)
        sg.addWidget(self.lbl_median_mbps, 1, 5)
        layout.addWidget(self.summary_group)

    def _build_chart(self, layout: QVBoxLayout) -> None:
        chart_row = QHBoxLayout()
        chart_row.addWidget(QLabel("Aggregate by:"))
        self.combo_aggregation = QComboBox()
        self.combo_aggregation.addItems(["Hour", "Day", "Week", "Month"])
        self.combo_aggregation.setCurrentIndex(1)  # Day
        self.combo_aggregation.currentIndexChanged.connect(self._refresh)
        chart_row.addWidget(self.combo_aggregation)
        chart_row.addStretch()
        layout.addLayout(chart_row)

        self._chart = QChart()
        self._chart.setTitle("Estimated Mbit/s")
        self._chart.setAnimationOptions(QChart.NoAnimation)
        self.chart_view = QChartView(self._chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        self.chart_view.setMinimumHeight(400)
        layout.addWidget(self.chart_view)

    def _build_breakdown_tables(self, layout: QVBoxLayout) -> None:
        breakdown = QHBoxLayout()

        source_group = QGroupBox("Per Source")
        sl = QVBoxLayout(source_group)
        self.source_table = QTableWidget()
        self.source_table.setEditTriggers(QTableWidget.NoEditTriggers)
        sl.addWidget(self.source_table)
        breakdown.addWidget(source_group)

        modality_group = QGroupBox("Per Modality")
        ml = QVBoxLayout(modality_group)
        self.modality_table = QTableWidget()
        self.modality_table.setEditTriggers(QTableWidget.NoEditTriggers)
        ml.addWidget(self.modality_table)
        breakdown.addWidget(modality_group)

        layout.addLayout(breakdown)

    def _build_detail_tabs(self, layout: QVBoxLayout) -> None:
        tabs = QTabWidget()
        study_tab = QWidget()
        stl = QVBoxLayout(study_tab)
        self.study_table = QTableWidget()
        self.study_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.study_table.setAlternatingRowColors(True)
        stl.addWidget(self.study_table)
        # Tells the user when the table is a capped view (see
        # MAX_DETAIL_ROWS) so a truncated list is never mistaken for the
        # whole history.
        self.lbl_study_rows = QLabel("—")
        stl.addWidget(self.lbl_study_rows)
        tabs.addTab(study_tab, "Studies")

        series_tab = QWidget()
        sel = QVBoxLayout(series_tab)
        self.series_table = QTableWidget()
        self.series_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.series_table.setAlternatingRowColors(True)
        sel.addWidget(self.series_table)
        self.lbl_series_rows = QLabel("—")
        sel.addWidget(self.lbl_series_rows)
        tabs.addTab(series_tab, "Series")

        layout.addWidget(tabs, 1)

    # ── Detail-table row capping ─────────────────────────────────────

    @staticmethod
    def _detail_rows(rows: list[dict]) -> list[dict]:
        """The most recent ``MAX_DETAIL_ROWS`` of *rows*.

        ``TransferLog._query`` orders by ``id`` ascending, so the tail is
        the newest.  The slice keeps that ascending order — the newest
        row stays at the bottom, exactly where it was before the cap.
        """
        return rows[-MAX_DETAIL_ROWS:]

    @staticmethod
    def _row_count_text(shown: int, total: int) -> str:
        if shown >= total:
            return f"{total} rows"
        return f"Showing the {shown} most recent of {total} rows"

    # ── Data loading ─────────────────────────────────────────────────

    def _get_filters(self) -> tuple[str | None, str | None]:
        source = None
        modality = None
        if self.filter_source.currentIndex() > 0:
            source = self.filter_source.currentText()
        if self.filter_modality.currentIndex() > 0:
            modality = self.filter_modality.currentText()
        return source, modality

    def _on_refresh_clicked(self) -> None:
        """Re-enumerate the filter combos from the full log on a
        worker thread, then refresh the views."""
        def job() -> list[dict]:
            try:
                return self._log.query_series()
            except Exception:
                logger.exception(
                    "TransferStatsWindow: filter rebuild query failed")
                return []

        def apply(all_series: list[dict]) -> None:
            self._populate_filter_combos(all_series)
            self._refresh()

        run_in_background(self, job, apply, label="stats_filters")

    def _populate_filter_combos(self, all_series: list[dict]) -> None:
        """Rebuild the Source/Modality combos from *all_series* rows,
        preserving the current selection where still valid."""
        sources = sorted({r["source_pacs"] for r in all_series})
        modalities = sorted({r["modality"] for r in all_series})

        old_src = self.filter_source.currentText()
        old_mod = self.filter_modality.currentText()

        self.filter_source.blockSignals(True)
        self.filter_modality.blockSignals(True)

        self.filter_source.clear()
        self.filter_source.addItem("All")
        self.filter_source.addItems(sources)

        self.filter_modality.clear()
        self.filter_modality.addItem("All")
        self.filter_modality.addItems(modalities)

        # Restore selection if still valid
        idx_s = self.filter_source.findText(old_src)
        if idx_s >= 0:
            self.filter_source.setCurrentIndex(idx_s)
        idx_m = self.filter_modality.findText(old_mod)
        if idx_m >= 0:
            self.filter_modality.setCurrentIndex(idx_m)

        self.filter_source.blockSignals(False)
        self.filter_modality.blockSignals(False)

    def _refresh(self) -> None:
        """Query the log on a worker thread and update all views.

        The full-history ``SELECT *`` used to run synchronously on
        the GUI thread; with months of 24/7 logging that freezes the
        window on every filter change.  ``TransferLog`` is safe to
        call from the worker: its connection is created with
        ``check_same_thread=False`` and every access goes through the
        instance lock, and WAL mode keeps the engine's writers
        unblocked while we read.
        """
        source, modality = self._get_filters()
        kw = {}
        if source:
            kw["source_pacs"] = source
        if modality:
            kw["modality"] = modality
        # First load: combos are still empty, which also means no
        # filter was active — *series* below then covers the full
        # table and can seed the combos without an extra query.
        need_filters = self.filter_source.count() == 0

        self._refresh_seq += 1
        seq = self._refresh_seq

        def job() -> tuple[list[dict], list[dict]]:
            try:
                series = self._log.query_series(**kw)
                studies = self._log.query_studies(**kw)
                return series, studies
            except Exception:
                logger.exception(
                    "TransferStatsWindow: refresh query failed")
                return [], []

        def apply(payload: tuple[list[dict], list[dict]]) -> None:
            if seq != self._refresh_seq:
                return  # superseded by a newer refresh
            series, studies = payload
            if need_filters:
                self._populate_filter_combos(series)
            self._update_summary(series, studies)
            self._update_boxplot(series)
            self._update_source_table(series, studies)
            self._update_modality_table(series)
            self._update_study_table(studies)
            self._update_series_table(series)

        run_in_background(self, job, apply, label="stats_refresh")

    # ── Summary ──────────────────────────────────────────────────────

    def _update_summary(self, series: list[dict],
                        studies: list[dict]) -> None:
        n_studies = len(studies)
        n_series = len(series)
        n_images = sum(r["image_count"] for r in series)
        self.lbl_total_studies.setText(str(n_studies))
        self.lbl_total_series.setText(str(n_series))
        self.lbl_total_images.setText(str(n_images))

        if series:
            dates = [r["study_date"] for r in series]
            d_min, d_max = min(dates), max(dates)
            self.lbl_date_range.setText(f"{d_min} — {d_max}")
            mbps_vals = [r["estimated_mbps"] for r in series
                         if r["estimated_mbps"] > 0]
            if mbps_vals:
                self.lbl_median_mbps.setText(f"{median(mbps_vals):.1f}")
            else:
                self.lbl_median_mbps.setText("—")
        else:
            self.lbl_date_range.setText("—")
            self.lbl_median_mbps.setText("—")

    # ── Boxplot ──────────────────────────────────────────────────────

    def _bucket_key(self, timestamp: str) -> str:
        """Return a bucket key based on the actual download timestamp.

        ``timestamp`` is the ISO-formatted download completion time
        recorded in the SQLite log (not the DICOM acquisition date —
        that would put prior studies into the wrong bucket).
        """
        agg = self.combo_aggregation.currentText().lower()
        try:
            dt = datetime.fromisoformat(timestamp)
        except (ValueError, TypeError):
            return timestamp
        if agg == "hour":
            return dt.strftime("%Y-%m-%d %H:00")
        if agg == "day":
            return dt.strftime("%Y-%m-%d")
        if agg == "week":
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        if agg == "month":
            return dt.strftime("%Y-%m")
        return dt.strftime("%Y-%m-%d")

    def _update_boxplot(self, series: list[dict]) -> None:
        self._chart.removeAllSeries()
        for axis in self._chart.axes():
            self._chart.removeAxis(axis)

        if not series:
            return

        buckets = defaultdict(list)
        for r in series:
            key = self._bucket_key(r["timestamp"])
            if r["estimated_mbps"] > 0:
                buckets[key].append(r["estimated_mbps"])

        if not buckets:
            return

        bp_series = QBoxPlotSeries()
        for key in sorted(buckets.keys()):
            vals = buckets[key]
            lo, q1, med, q3, hi = tukey_quartiles(vals)
            box = QBoxSet(key)
            box.setValue(QBoxSet.LowerExtreme, lo)
            box.setValue(QBoxSet.LowerQuartile, q1)
            box.setValue(QBoxSet.Median, med)
            box.setValue(QBoxSet.UpperQuartile, q3)
            box.setValue(QBoxSet.UpperExtreme, hi)
            bp_series.append(box)

        self._chart.addSeries(bp_series)
        self._chart.createDefaultAxes()
        for axis in self._chart.axes(Qt.Vertical):
            axis.setTitleText("Mbit/s")

    # ── Source table ─────────────────────────────────────────────────

    def _update_source_table(self, series: list[dict],
                             studies: list[dict]) -> None:
        headers = ["Source", "Studies", "Series", "Images", "Median Mbit/s"]
        sources: dict[str, _SourceTotals] = defaultdict(_SourceTotals)
        for r in studies:
            totals = sources[r["source_pacs"]]
            totals.studies += 1
            totals.images += r["total_images"]
        for r in series:
            totals = sources[r["source_pacs"]]
            totals.series += 1
            # A zero rate means the series was too small / too fast to
            # time — keep it out of the median instead of dragging it
            # towards zero.
            if r["estimated_mbps"] > 0:
                totals.mbps_vals.append(r["estimated_mbps"])

        self.source_table.setColumnCount(len(headers))
        self.source_table.setHorizontalHeaderLabels(headers)
        self.source_table.setRowCount(len(sources))
        for row, src in enumerate(sorted(sources)):
            totals = sources[src]
            self.source_table.setItem(row, 0, QTableWidgetItem(src))
            self.source_table.setItem(
                row, 1, QTableWidgetItem(str(totals.studies)))
            self.source_table.setItem(
                row, 2, QTableWidgetItem(str(totals.series)))
            self.source_table.setItem(
                row, 3, QTableWidgetItem(str(totals.images)))
            if totals.mbps_vals:
                self.source_table.setItem(
                    row, 4, QTableWidgetItem(f"{median(totals.mbps_vals):.1f}"))
            else:
                self.source_table.setItem(row, 4, QTableWidgetItem("—"))

    # ── Modality table ───────────────────────────────────────────────

    def _update_modality_table(self, series: list[dict]) -> None:
        headers = ["Modality", "Series", "Images", "Median Mbit/s"]
        mods = {}
        for r in series:
            m = r["modality"]
            if m not in mods:
                mods[m] = {"series": 0, "images": 0, "mbps_vals": []}
            mods[m]["series"] += 1
            mods[m]["images"] += r["image_count"]
            if r["estimated_mbps"] > 0:
                mods[m]["mbps_vals"].append(r["estimated_mbps"])

        self.modality_table.setColumnCount(len(headers))
        self.modality_table.setHorizontalHeaderLabels(headers)
        self.modality_table.setRowCount(len(mods))
        for row, (mod, d) in enumerate(sorted(mods.items())):
            self.modality_table.setItem(row, 0, QTableWidgetItem(mod))
            self.modality_table.setItem(row, 1, QTableWidgetItem(str(d["series"])))
            self.modality_table.setItem(row, 2, QTableWidgetItem(str(d["images"])))
            vals = d["mbps_vals"]
            if vals:
                self.modality_table.setItem(
                    row, 3, QTableWidgetItem(f"{median(vals):.1f}"))
            else:
                self.modality_table.setItem(row, 3, QTableWidgetItem("—"))

    # ── Study table ──────────────────────────────────────────────────

    def _update_study_table(self, studies: list[dict]) -> None:
        headers = ["Date", "Time", "Source", "Modality", "Description",
                    "Series", "Images", "Wall Clock (s)", "Mbit/s"]
        shown = self._detail_rows(studies)
        self.lbl_study_rows.setText(
            self._row_count_text(len(shown), len(studies)))
        self.study_table.setColumnCount(len(headers))
        self.study_table.setHorizontalHeaderLabels(headers)
        self.study_table.setRowCount(len(shown))
        for row, r in enumerate(shown):
            self.study_table.setItem(row, 0, QTableWidgetItem(r["study_date"]))
            self.study_table.setItem(row, 1, QTableWidgetItem(r["study_time"]))
            self.study_table.setItem(row, 2, QTableWidgetItem(r["source_pacs"]))
            self.study_table.setItem(row, 3, QTableWidgetItem(r["modality"]))
            self.study_table.setItem(row, 4, QTableWidgetItem(r["study_description"]))
            self.study_table.setItem(row, 5, QTableWidgetItem(str(r["total_series"])))
            self.study_table.setItem(row, 6, QTableWidgetItem(str(r["total_images"])))
            self.study_table.setItem(row, 7, QTableWidgetItem(f"{r['wall_clock_seconds']:.1f}"))
            self.study_table.setItem(row, 8, QTableWidgetItem(f"{r['estimated_mbps']:.1f}"))

    # ── Series table ─────────────────────────────────────────────────

    def _update_series_table(self, series: list[dict]) -> None:
        headers = ["Date", "Time", "Source", "Modality", "Study",
                    "Series", "Images", "Duration (s)", "img/min", "Mbit/s"]
        shown = self._detail_rows(series)
        self.lbl_series_rows.setText(
            self._row_count_text(len(shown), len(series)))
        self.series_table.setColumnCount(len(headers))
        self.series_table.setHorizontalHeaderLabels(headers)
        self.series_table.setRowCount(len(shown))
        for row, r in enumerate(shown):
            self.series_table.setItem(row, 0, QTableWidgetItem(r["study_date"]))
            self.series_table.setItem(row, 1, QTableWidgetItem(r["study_time"]))
            self.series_table.setItem(row, 2, QTableWidgetItem(r["source_pacs"]))
            self.series_table.setItem(row, 3, QTableWidgetItem(r["modality"]))
            self.series_table.setItem(row, 4, QTableWidgetItem(r["study_description"]))
            self.series_table.setItem(row, 5, QTableWidgetItem(r["series_description"]))
            self.series_table.setItem(row, 6, QTableWidgetItem(str(r["image_count"])))
            self.series_table.setItem(row, 7, QTableWidgetItem(f"{r['duration_seconds']:.1f}"))
            self.series_table.setItem(row, 8, QTableWidgetItem(f"{r['images_per_minute']:.0f}"))
            self.series_table.setItem(row, 9, QTableWidgetItem(f"{r['estimated_mbps']:.1f}"))
