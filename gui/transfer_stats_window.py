"""
Transfer Performance Statistics window.

Displays statistical analysis of the SQLite transfer performance log,
accessible via the View menu.
"""

from collections import defaultdict
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtCharts import QChart, QChartView, QBoxPlotSeries, QBoxSet
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QGroupBox, QGridLayout, QHeaderView,
    QPushButton, QComboBox, QTabWidget,
)
from PySide6.QtGui import QFont, QPainter

from core.transfer_log import TransferLog


def _quartiles(vals):
    """Return (min, Q1, median, Q3, max) for a sorted list of floats."""
    n = len(vals)
    if n == 0:
        return (0, 0, 0, 0, 0)
    s = sorted(vals)
    mid = n // 2
    median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
    lower = s[:mid]
    upper = s[mid + 1:] if n % 2 else s[mid:]
    q1 = lower[len(lower) // 2] if lower else s[0]
    q3 = upper[len(upper) // 2] if upper else s[-1]
    return (s[0], q1, median, q3, s[-1])


class TransferStatsWindow(QWidget):

    def __init__(self, db_path: str, parent=None):
        super().__init__(parent)
        self._db_path = db_path
        self.setWindowTitle("Transfer Performance Statistics")
        self.setWindowFlags(Qt.Window)
        self.setMinimumSize(800, 500)
        self._setup_ui()
        self._refresh()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── Filters ──────────────────────────────────────────────────
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

        # ── Summary ──────────────────────────────────────────────────
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

        # ── Boxplot chart ────────────────────────────────────────────
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

        # ── Breakdown tables ─────────────────────────────────────────
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

        # ── Detail tabs ──────────────────────────────────────────────
        tabs = QTabWidget()
        study_tab = QWidget()
        stl = QVBoxLayout(study_tab)
        self.study_table = QTableWidget()
        self.study_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.study_table.setAlternatingRowColors(True)
        stl.addWidget(self.study_table)
        tabs.addTab(study_tab, "Studies")

        series_tab = QWidget()
        sel = QVBoxLayout(series_tab)
        self.series_table = QTableWidget()
        self.series_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.series_table.setAlternatingRowColors(True)
        sel.addWidget(self.series_table)
        tabs.addTab(series_tab, "Series")

        layout.addWidget(tabs, 1)

    # ── Data loading ─────────────────────────────────────────────────

    def _get_filters(self):
        source = None
        modality = None
        if self.filter_source.currentIndex() > 0:
            source = self.filter_source.currentText()
        if self.filter_modality.currentIndex() > 0:
            modality = self.filter_modality.currentText()
        return source, modality

    def _on_refresh_clicked(self):
        self._rebuild_filters()
        self._refresh()

    def _rebuild_filters(self):
        log = TransferLog(self._db_path)
        all_series = log.query_series()
        log.close()

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

    def _refresh(self):
        log = TransferLog(self._db_path)

        # Build filter lists on first load or refresh
        if self.filter_source.count() == 0:
            all_series = log.query_series()
            sources = sorted({r["source_pacs"] for r in all_series})
            modalities = sorted({r["modality"] for r in all_series})
            self.filter_source.blockSignals(True)
            self.filter_modality.blockSignals(True)
            self.filter_source.addItem("All")
            self.filter_source.addItems(sources)
            self.filter_modality.addItem("All")
            self.filter_modality.addItems(modalities)
            self.filter_source.blockSignals(False)
            self.filter_modality.blockSignals(False)

        source, modality = self._get_filters()
        kw = {}
        if source:
            kw["source_pacs"] = source
        if modality:
            kw["modality"] = modality

        series = log.query_series(**kw)
        studies = log.query_studies(**kw)
        log.close()

        self._update_summary(series, studies)
        self._update_boxplot(series)
        self._update_source_table(series, studies)
        self._update_modality_table(series)
        self._update_study_table(studies)
        self._update_series_table(series)

    # ── Summary ──────────────────────────────────────────────────────

    def _update_summary(self, series, studies):
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
                mbps_vals.sort()
                mid = len(mbps_vals) // 2
                median = (mbps_vals[mid] if len(mbps_vals) % 2
                          else (mbps_vals[mid - 1] + mbps_vals[mid]) / 2)
                self.lbl_median_mbps.setText(f"{median:.1f}")
            else:
                self.lbl_median_mbps.setText("—")
        else:
            self.lbl_date_range.setText("—")
            self.lbl_median_mbps.setText("—")

    # ── Boxplot ──────────────────────────────────────────────────────

    def _bucket_key(self, study_date: str, study_time: str) -> str:
        """Return a bucket key based on current aggregation level."""
        agg = self.combo_aggregation.currentText().lower()
        # study_date = YYYYMMDD, study_time = HHMMSS
        if agg == "hour":
            return study_time[:2] + ":00"
        if agg == "day":
            return study_date
        if agg == "week":
            try:
                dt = datetime.strptime(study_date, "%Y%m%d")
                iso = dt.isocalendar()
                return f"{iso[0]}-W{iso[1]:02d}"
            except ValueError:
                return study_date
        if agg == "month":
            return study_date[:6]
        return study_date

    def _update_boxplot(self, series):
        self._chart.removeAllSeries()
        for axis in self._chart.axes():
            self._chart.removeAxis(axis)

        if not series:
            return

        buckets = defaultdict(list)
        for r in series:
            key = self._bucket_key(r["study_date"], r["study_time"])
            if r["estimated_mbps"] > 0:
                buckets[key].append(r["estimated_mbps"])

        if not buckets:
            return

        bp_series = QBoxPlotSeries()
        for key in sorted(buckets.keys()):
            vals = buckets[key]
            lo, q1, med, q3, hi = _quartiles(vals)
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

    def _update_source_table(self, series, studies):
        headers = ["Source", "Studies", "Series", "Images", "Median Mbit/s"]
        sources = {}
        for r in studies:
            s = r["source_pacs"]
            if s not in sources:
                sources[s] = {"studies": 0, "images": 0}
            sources[s]["studies"] += 1
            sources[s]["images"] += r["total_images"]
        for r in series:
            s = r["source_pacs"]
            if s not in sources:
                sources[s] = {"studies": 0, "images": 0}
            sources[s].setdefault("series", 0)
            sources[s]["series"] = sources[s].get("series", 0) + 1
            sources[s].setdefault("mbps_vals", [])
            if r["estimated_mbps"] > 0:
                sources[s]["mbps_vals"].append(r["estimated_mbps"])

        self.source_table.setColumnCount(len(headers))
        self.source_table.setHorizontalHeaderLabels(headers)
        self.source_table.setRowCount(len(sources))
        for row, (src, d) in enumerate(sorted(sources.items())):
            self.source_table.setItem(row, 0, QTableWidgetItem(src))
            self.source_table.setItem(row, 1, QTableWidgetItem(str(d["studies"])))
            self.source_table.setItem(row, 2, QTableWidgetItem(str(d.get("series", 0))))
            self.source_table.setItem(row, 3, QTableWidgetItem(str(d["images"])))
            vals = d.get("mbps_vals", [])
            if vals:
                vals.sort()
                mid = len(vals) // 2
                med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
                self.source_table.setItem(row, 4, QTableWidgetItem(f"{med:.1f}"))
            else:
                self.source_table.setItem(row, 4, QTableWidgetItem("—"))

    # ── Modality table ───────────────────────────────────────────────

    def _update_modality_table(self, series):
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
                vals.sort()
                mid = len(vals) // 2
                med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
                self.modality_table.setItem(row, 3, QTableWidgetItem(f"{med:.1f}"))
            else:
                self.modality_table.setItem(row, 3, QTableWidgetItem("—"))

    # ── Study table ──────────────────────────────────────────────────

    def _update_study_table(self, studies):
        headers = ["Date", "Time", "Source", "Modality", "Description",
                    "Series", "Images", "Wall Clock (s)", "Mbit/s"]
        self.study_table.setColumnCount(len(headers))
        self.study_table.setHorizontalHeaderLabels(headers)
        self.study_table.setRowCount(len(studies))
        for row, r in enumerate(studies):
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

    def _update_series_table(self, series):
        headers = ["Date", "Time", "Source", "Modality", "Study",
                    "Series", "Images", "Duration (s)", "img/min", "Mbit/s"]
        self.series_table.setColumnCount(len(headers))
        self.series_table.setHorizontalHeaderLabels(headers)
        self.series_table.setRowCount(len(series))
        for row, r in enumerate(series):
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
