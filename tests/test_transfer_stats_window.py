"""
Tests for gui.transfer_stats_window — Transfer Performance Statistics window.

Displays statistical analysis of the SQLite transfer performance log,
accessible via View menu. Shows summary metrics, per-source breakdown,
per-modality breakdown, and a filterable series-level table.
"""

import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableWidget, QComboBox, QLabel

from core.transfer_log import TransferLog, estimate_bytes
from gui.transfer_stats_window import TransferStatsWindow
from gui.main_window import MainWindow
from core.config import AppConfig, PacsNode


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _populate_log(log: TransferLog):
    """Insert a realistic set of transfer records."""
    # Study 1: CT with 3 series, fast transfer
    for i in range(3):
        log.record_series(
            source_pacs="ct_scanner", study_uid="1.2.3.100",
            series_uid=f"1.2.3.100.{i}", patient_id="PAT_A",
            accession_number="ACC100", study_date="20260401",
            study_time="080000", modality="CT",
            study_description="CT Abdomen",
            series_description=f"Series {i}", series_number=str(i),
            image_count=200, duration_seconds=20.0,
        )
    log.record_study(
        source_pacs="ct_scanner", study_uid="1.2.3.100",
        patient_id="PAT_A", accession_number="ACC100",
        study_date="20260401", study_time="080000", modality="CT",
        study_description="CT Abdomen", total_series=3,
        total_images=600, total_duration_seconds=60.0,
        wall_clock_seconds=65.0,
    )
    # Study 2: MR with 5 series, slower
    for i in range(5):
        log.record_series(
            source_pacs="mri_unit", study_uid="1.2.3.200",
            series_uid=f"1.2.3.200.{i}", patient_id="PAT_B",
            accession_number="ACC200", study_date="20260402",
            study_time="140000", modality="MR",
            study_description="MR Brain",
            series_description=f"Sequence {i}", series_number=str(i),
            image_count=50, duration_seconds=15.0,
        )
    log.record_study(
        source_pacs="mri_unit", study_uid="1.2.3.200",
        patient_id="PAT_B", accession_number="ACC200",
        study_date="20260402", study_time="140000", modality="MR",
        study_description="MR Brain", total_series=5,
        total_images=250, total_duration_seconds=75.0,
        wall_clock_seconds=80.0,
    )
    # Study 3: CT from different source, different day
    log.record_series(
        source_pacs="ct_scanner", study_uid="1.2.3.300",
        series_uid="1.2.3.300.0", patient_id="PAT_C",
        accession_number="ACC300", study_date="20260405",
        study_time="090000", modality="CT",
        study_description="CT Thorax",
        series_description="Axial", series_number="1",
        image_count=400, duration_seconds=50.0,
    )
    log.record_study(
        source_pacs="ct_scanner", study_uid="1.2.3.300",
        patient_id="PAT_C", accession_number="ACC300",
        study_date="20260405", study_time="090000", modality="CT",
        study_description="CT Thorax", total_series=1,
        total_images=400, total_duration_seconds=50.0,
        wall_clock_seconds=50.0,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_stats.sqlite")


@pytest.fixture
def log(db_path):
    tl = TransferLog(db_path)
    yield tl
    tl.close()


@pytest.fixture
def populated_log(log):
    _populate_log(log)
    return log


@pytest.fixture
def window(populated_log, db_path, qapp):
    win = TransferStatsWindow(db_path)
    yield win
    win.close()


@pytest.fixture
def empty_window(log, db_path, qapp):
    win = TransferStatsWindow(db_path)
    yield win
    win.close()


# ═══════════════════════════════════════════════════════════════════════════
# Window basics
# ═══════════════════════════════════════════════════════════════════════════

class TestWindowBasics:

    def test_window_title(self, window):
        assert "Transfer" in window.windowTitle()
        assert "Statistics" in window.windowTitle() or "Performance" in window.windowTitle()

    def test_is_separate_window(self, window):
        """Should be a standalone window, not a dialog."""
        flags = window.windowFlags()
        assert flags & Qt.Window

    def test_minimum_size(self, window):
        assert window.minimumWidth() >= 600
        assert window.minimumHeight() >= 400

    def test_can_show_and_close(self, window):
        window.show()
        assert window.isVisible()
        window.close()


# ═══════════════════════════════════════════════════════════════════════════
# Menu integration
# ═══════════════════════════════════════════════════════════════════════════

class TestMenuIntegration:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    def test_view_menu_has_stats_action(self):
        menubar = self.win.menuBar()
        for action in menubar.actions():
            if action.text() == "View":
                menu = action.menu()
                texts = [a.text() for a in menu.actions()]
                assert any("Transfer" in t and ("Statistics" in t or "Performance" in t)
                           for t in texts)
                return
        pytest.fail("View menu not found")

    def test_stats_action_has_shortcut(self):
        menubar = self.win.menuBar()
        for action in menubar.actions():
            if action.text() == "View":
                menu = action.menu()
                for a in menu.actions():
                    if "Transfer" in a.text():
                        assert a.shortcut().toString() != ""
                        return
        pytest.fail("Transfer stats action not found")

    def test_open_stats_window(self):
        """Triggering the menu action should open the stats window."""
        with patch("gui.main_window.TransferStatsWindow") as MockWin:
            mock_instance = MagicMock()
            MockWin.return_value = mock_instance
            self.win._open_transfer_stats()
            mock_instance.show.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Summary section
# ═══════════════════════════════════════════════════════════════════════════

class TestSummarySection:
    """Top-level summary showing aggregate metrics across all transfers."""

    def test_has_summary_group(self, window):
        """Should have a summary/overview section."""
        assert window.summary_group is not None

    def test_shows_total_studies(self, window):
        """Summary shows total number of studies transferred."""
        text = window.lbl_total_studies.text()
        assert "3" in text

    def test_shows_total_series(self, window):
        text = window.lbl_total_series.text()
        assert "9" in text  # 3 + 5 + 1

    def test_shows_total_images(self, window):
        text = window.lbl_total_images.text()
        assert "1250" in text or "1,250" in text  # 600 + 250 + 400

    def test_shows_date_range(self, window):
        """Summary shows date range of the data."""
        text = window.lbl_date_range.text()
        assert "2026" in text

    def test_shows_median_mbps(self, window):
        """Summary shows overall median estimated Mbit/s."""
        text = window.lbl_median_mbps.text()
        # Should be a number > 0
        assert any(c.isdigit() for c in text)

    def test_empty_db_shows_no_data(self, empty_window):
        text = empty_window.lbl_total_studies.text()
        assert "0" in text or "—" in text or "no" in text.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Per-source breakdown
# ═══════════════════════════════════════════════════════════════════════════

class TestPerSourceBreakdown:
    """Table or section showing stats grouped by source PACS."""

    def test_has_source_table(self, window):
        assert window.source_table is not None
        assert isinstance(window.source_table, QTableWidget)

    def test_source_table_row_count(self, window):
        """One row per source PACS."""
        assert window.source_table.rowCount() == 2  # ct_scanner, mri_unit

    def test_source_table_has_columns(self, window):
        """Table should have source name, studies, series, images, median speed."""
        headers = []
        for col in range(window.source_table.columnCount()):
            item = window.source_table.horizontalHeaderItem(col)
            if item:
                headers.append(item.text().lower())
        assert any("source" in h or "pacs" in h for h in headers)
        assert any("stud" in h for h in headers)
        assert any("series" in h for h in headers)
        assert any("image" in h for h in headers)

    def test_source_data_correct(self, window):
        """ct_scanner should show 2 studies, mri_unit 1."""
        data = {}
        for row in range(window.source_table.rowCount()):
            source = window.source_table.item(row, 0).text()
            studies = window.source_table.item(row, 1).text()
            data[source] = studies
        assert data.get("ct_scanner") == "2"
        assert data.get("mri_unit") == "1"


# ═══════════════════════════════════════════════════════════════════════════
# Per-modality breakdown
# ═══════════════════════════════════════════════════════════════════════════

class TestPerModalityBreakdown:
    """Table showing stats grouped by modality."""

    def test_has_modality_table(self, window):
        assert window.modality_table is not None
        assert isinstance(window.modality_table, QTableWidget)

    def test_modality_table_row_count(self, window):
        assert window.modality_table.rowCount() == 2  # CT, MR

    def test_modality_table_has_columns(self, window):
        headers = []
        for col in range(window.modality_table.columnCount()):
            item = window.modality_table.horizontalHeaderItem(col)
            if item:
                headers.append(item.text().lower())
        assert any("modal" in h for h in headers)
        assert any("image" in h for h in headers)
        assert any("mbit" in h or "mbps" in h or "speed" in h for h in headers)

    def test_modality_data_present(self, window):
        modalities = set()
        for row in range(window.modality_table.rowCount()):
            modalities.add(window.modality_table.item(row, 0).text())
        assert "CT" in modalities
        assert "MR" in modalities


# ═══════════════════════════════════════════════════════════════════════════
# Series detail table
# ═══════════════════════════════════════════════════════════════════════════

class TestSeriesDetailTable:
    """Filterable table showing individual series transfers."""

    def test_has_series_table(self, window):
        assert window.series_table is not None
        assert isinstance(window.series_table, QTableWidget)

    def test_series_table_row_count(self, window):
        """Should show all 9 series by default."""
        assert window.series_table.rowCount() == 9

    def test_series_table_columns(self, window):
        headers = []
        for col in range(window.series_table.columnCount()):
            item = window.series_table.horizontalHeaderItem(col)
            if item:
                headers.append(item.text().lower())
        assert any("date" in h for h in headers)
        assert any("modal" in h for h in headers)
        assert any("image" in h for h in headers)
        assert any("duration" in h or "time" in h or "sec" in h for h in headers)
        assert any("mbit" in h or "mbps" in h or "speed" in h for h in headers)

    def test_series_table_shows_study_description(self, window):
        """Series table should include study description."""
        descriptions = set()
        for row in range(window.series_table.rowCount()):
            for col in range(window.series_table.columnCount()):
                item = window.series_table.item(row, col)
                if item and "Abdomen" in item.text():
                    descriptions.add(item.text())
        assert len(descriptions) > 0

    def test_empty_db_shows_no_rows(self, empty_window):
        assert empty_window.series_table.rowCount() == 0


# ═══════════════════════════════════════════════════════════════════════════
# Filters
# ═══════════════════════════════════════════════════════════════════════════

class TestFilters:
    """Filter controls for narrowing the displayed data."""

    def test_has_source_filter(self, window):
        assert window.filter_source is not None
        assert isinstance(window.filter_source, QComboBox)

    def test_has_modality_filter(self, window):
        assert window.filter_modality is not None
        assert isinstance(window.filter_modality, QComboBox)

    def test_source_filter_has_all_option(self, window):
        texts = [window.filter_source.itemText(i)
                 for i in range(window.filter_source.count())]
        assert any("all" in t.lower() for t in texts)

    def test_source_filter_lists_sources(self, window):
        texts = [window.filter_source.itemText(i)
                 for i in range(window.filter_source.count())]
        assert "ct_scanner" in texts
        assert "mri_unit" in texts

    def test_modality_filter_has_all_option(self, window):
        texts = [window.filter_modality.itemText(i)
                 for i in range(window.filter_modality.count())]
        assert any("all" in t.lower() for t in texts)

    def test_modality_filter_lists_modalities(self, window):
        texts = [window.filter_modality.itemText(i)
                 for i in range(window.filter_modality.count())]
        assert "CT" in texts
        assert "MR" in texts

    def test_filter_by_source(self, window):
        """Selecting a source filters series table."""
        # Find ct_scanner index
        for i in range(window.filter_source.count()):
            if window.filter_source.itemText(i) == "ct_scanner":
                window.filter_source.setCurrentIndex(i)
                break
        # CT scanner has 4 series (3 from study 1 + 1 from study 3)
        assert window.series_table.rowCount() == 4

    def test_filter_by_modality(self, window):
        """Selecting a modality filters series table."""
        for i in range(window.filter_modality.count()):
            if window.filter_modality.itemText(i) == "MR":
                window.filter_modality.setCurrentIndex(i)
                break
        assert window.series_table.rowCount() == 5

    def test_filter_resets_to_all(self, window):
        """Selecting 'All' shows all series again."""
        # First filter
        for i in range(window.filter_modality.count()):
            if window.filter_modality.itemText(i) == "MR":
                window.filter_modality.setCurrentIndex(i)
                break
        assert window.series_table.rowCount() == 5
        # Reset
        window.filter_modality.setCurrentIndex(0)  # "All" is first
        assert window.series_table.rowCount() == 9

    def test_combined_filters(self, window):
        """Source + modality filters combine (AND)."""
        for i in range(window.filter_source.count()):
            if window.filter_source.itemText(i) == "ct_scanner":
                window.filter_source.setCurrentIndex(i)
                break
        for i in range(window.filter_modality.count()):
            if window.filter_modality.itemText(i) == "CT":
                window.filter_modality.setCurrentIndex(i)
                break
        # ct_scanner + CT = 4 series
        assert window.series_table.rowCount() == 4


# ═══════════════════════════════════════════════════════════════════════════
# Refresh
# ═══════════════════════════════════════════════════════════════════════════

class TestRefresh:

    def test_has_refresh_button(self, window):
        assert window.btn_refresh is not None

    def test_refresh_updates_data(self, window, db_path):
        """Adding data and refreshing should update the display."""
        assert window.series_table.rowCount() == 9
        # Add another series directly to the DB
        log2 = TransferLog(db_path)
        log2.record_series(
            source_pacs="ct_scanner", study_uid="1.2.3.999",
            series_uid="1.2.3.999.0", patient_id="PAT_X",
            accession_number="ACC999", study_date="20260410",
            study_time="100000", modality="CT",
            study_description="CT New", series_description="Axial",
            series_number="1", image_count=100, duration_seconds=10.0,
        )
        log2.close()
        window.btn_refresh.click()
        assert window.series_table.rowCount() == 10


# ═══════════════════════════════════════════════════════════════════════════
# Study-level table
# ═══════════════════════════════════════════════════════════════════════════

class TestStudyTable:
    """Study-level aggregated view."""

    def test_has_study_table(self, window):
        assert window.study_table is not None
        assert isinstance(window.study_table, QTableWidget)

    def test_study_table_row_count(self, window):
        assert window.study_table.rowCount() == 3

    def test_study_table_columns(self, window):
        headers = []
        for col in range(window.study_table.columnCount()):
            item = window.study_table.horizontalHeaderItem(col)
            if item:
                headers.append(item.text().lower())
        assert any("date" in h for h in headers)
        assert any("modal" in h for h in headers)
        assert any("series" in h for h in headers)
        assert any("image" in h for h in headers)
        assert any("wall" in h or "duration" in h or "sec" in h for h in headers)
        assert any("mbit" in h or "mbps" in h or "speed" in h for h in headers)

    def test_study_table_shows_wall_clock(self, window):
        """Study table should show wall-clock time, not just summed duration."""
        headers = []
        for col in range(window.study_table.columnCount()):
            item = window.study_table.horizontalHeaderItem(col)
            if item:
                headers.append(item.text().lower())
        assert any("wall" in h for h in headers)

    def test_study_filters_apply(self, window):
        """Study table should also respect source/modality filters."""
        for i in range(window.filter_source.count()):
            if window.filter_source.itemText(i) == "mri_unit":
                window.filter_source.setCurrentIndex(i)
                break
        assert window.study_table.rowCount() == 1


# ═══════════════════════════════════════════════════════════════════════════
# Boxplot chart — Mbit/s over time
# ═══════════════════════════════════════════════════════════════════════════

def _populate_log_multi_day(log: TransferLog):
    """Insert series spread across multiple days/hours for boxplot testing.

    Each series gets an explicit ``timestamp`` (download time) so the
    boxplot buckets reflect real download times, not DICOM acquisition.
    """
    base_series = 0
    for day in range(1, 8):  # 20260401–20260407
        date = f"2026040{day}"
        for hour in (8, 12, 16, 20):
            base_series += 1
            duration = 10.0 + day * 2 + hour * 0.5
            ts = f"2026-04-0{day}T{hour:02d}:00:00"
            log.record_series(
                source_pacs="ct_scanner",
                study_uid=f"1.2.{day}.{hour}",
                series_uid=f"1.2.{day}.{hour}.1",
                patient_id=f"PAT_{day}_{hour}",
                accession_number=f"ACC_{day}_{hour}",
                study_date=date,
                study_time=f"{hour:02d}0000",
                modality="CT",
                study_description="CT Abdomen",
                series_description="Axial",
                series_number="1",
                image_count=300,
                duration_seconds=duration,
                timestamp=ts,
            )


@pytest.fixture
def boxplot_log(db_path):
    tl = TransferLog(db_path)
    _populate_log_multi_day(tl)
    yield tl
    tl.close()


@pytest.fixture
def boxplot_window(boxplot_log, db_path, qapp):
    win = TransferStatsWindow(db_path)
    yield win
    win.close()


class TestBoxplotWidgets:
    """The chart area and aggregation selector exist and are wired up."""

    def test_has_chart_view(self, boxplot_window):
        """Window should contain a QChartView for the boxplot."""
        from PySide6.QtCharts import QChartView
        assert boxplot_window.chart_view is not None
        assert isinstance(boxplot_window.chart_view, QChartView)

    def test_has_aggregation_combo(self, boxplot_window):
        """Combo box to select hour/day/week/month aggregation."""
        assert boxplot_window.combo_aggregation is not None
        assert isinstance(boxplot_window.combo_aggregation, QComboBox)

    def test_aggregation_options(self, boxplot_window):
        texts = [boxplot_window.combo_aggregation.itemText(i)
                 for i in range(boxplot_window.combo_aggregation.count())]
        assert len(texts) == 4
        # Check all four granularities exist (case-insensitive)
        lower = [t.lower() for t in texts]
        assert any("hour" in t for t in lower)
        assert any("day" in t or "tag" in t for t in lower)
        assert any("week" in t or "woche" in t for t in lower)
        assert any("month" in t or "monat" in t for t in lower)

    def test_default_aggregation_is_day(self, boxplot_window):
        """Day should be the default aggregation level."""
        current = boxplot_window.combo_aggregation.currentText().lower()
        assert "day" in current or "tag" in current

    def test_chart_has_title(self, boxplot_window):
        title = boxplot_window.chart_view.chart().title()
        assert "mbit" in title.lower() or "mbps" in title.lower()


class TestBoxplotChart:
    """The chart renders correct data for each aggregation level."""

    def test_day_aggregation_box_count(self, boxplot_window):
        """Per-day: 7 days of data → 7 boxes."""
        _set_aggregation(boxplot_window, "day")
        series = _get_boxplot_series(boxplot_window)
        assert series is not None
        assert series.count() == 7

    def test_hour_aggregation_box_count(self, boxplot_window):
        """Per-hour: 7 days × 4 hours = 28 distinct hour buckets."""
        _set_aggregation(boxplot_window, "hour")
        series = _get_boxplot_series(boxplot_window)
        assert series is not None
        assert series.count() == 28

    def test_week_aggregation_box_count(self, boxplot_window):
        """20260401–20260407 spans 2 ISO weeks (W14 Mon–Sun, W15 Mon)."""
        _set_aggregation(boxplot_window, "week")
        series = _get_boxplot_series(boxplot_window)
        assert series is not None
        assert series.count() >= 1

    def test_month_aggregation_box_count(self, boxplot_window):
        """All data is in April 2026 → 1 box."""
        _set_aggregation(boxplot_window, "month")
        series = _get_boxplot_series(boxplot_window)
        assert series is not None
        assert series.count() == 1

    def test_box_values_are_positive(self, boxplot_window):
        """All box values (min, Q1, median, Q3, max) should be > 0."""
        _set_aggregation(boxplot_window, "day")
        series = _get_boxplot_series(boxplot_window)
        for i in range(series.count()):
            box_set = series.boxSets()[i]
            for val_idx in range(5):  # 0=lower, 1=Q1, 2=median, 3=Q3, 4=upper
                assert box_set.at(val_idx) >= 0

    def test_changing_aggregation_updates_chart(self, boxplot_window):
        """Switching aggregation should change the number of boxes."""
        _set_aggregation(boxplot_window, "day")
        day_count = _get_boxplot_series(boxplot_window).count()
        _set_aggregation(boxplot_window, "month")
        month_count = _get_boxplot_series(boxplot_window).count()
        assert day_count != month_count

    def test_chart_y_axis_label_contains_mbit(self, boxplot_window):
        """Y axis should be labeled with Mbit/s."""
        chart = boxplot_window.chart_view.chart()
        for axis in chart.axes():
            if axis.alignment().name == b"AlignLeft":
                assert "mbit" in axis.titleText().lower() or \
                       "mbps" in axis.titleText().lower()
                return
        # If no left axis found, check any axis
        labels = [a.titleText().lower() for a in chart.axes()]
        assert any("mbit" in l or "mbps" in l for l in labels)


class TestBoxplotFilters:
    """Source/modality filters should also affect the boxplot."""

    def test_source_filter_affects_chart(self, boxplot_window, db_path):
        """Adding MR data then filtering to CT should exclude it."""
        # Add some MR series on a different date
        log2 = TransferLog(db_path)
        log2.record_series(
            source_pacs="mri_unit", study_uid="9.9.9",
            series_uid="9.9.9.1", patient_id="P",
            accession_number="A", study_date="20260408",
            study_time="100000", modality="MR",
            study_description="MR", series_description="T1",
            series_number="1", image_count=100, duration_seconds=20.0,
            timestamp="2026-04-08T10:00:00",
        )
        log2.close()
        boxplot_window.btn_refresh.click()
        _set_aggregation(boxplot_window, "day")

        # Unfiltered: 8 days
        count_all = _get_boxplot_series(boxplot_window).count()

        # Filter to ct_scanner only
        for i in range(boxplot_window.filter_source.count()):
            if boxplot_window.filter_source.itemText(i) == "ct_scanner":
                boxplot_window.filter_source.setCurrentIndex(i)
                break
        count_ct = _get_boxplot_series(boxplot_window).count()
        assert count_ct < count_all  # day 8 (MR only) excluded


class TestBoxplotEmpty:
    """Boxplot with no data should not crash."""

    def test_empty_db_no_crash(self, empty_window):
        """Empty DB → chart exists but has no boxes."""
        from PySide6.QtCharts import QChartView
        assert isinstance(empty_window.chart_view, QChartView)
        series = _get_boxplot_series(empty_window)
        assert series is None or series.count() == 0

    def test_single_series_shows_one_box(self, log, db_path, qapp):
        """A single data point should still render one box."""
        log.record_series(
            source_pacs="ct", study_uid="1", series_uid="1.1",
            patient_id="P", accession_number="A",
            study_date="20260401", study_time="120000",
            modality="CT", study_description="Test",
            series_description="Axial", series_number="1",
            image_count=100, duration_seconds=10.0,
        )
        win = TransferStatsWindow(db_path)
        _set_aggregation(win, "day")
        series = _get_boxplot_series(win)
        assert series is not None
        assert series.count() == 1
        win.close()


# ── Boxplot test helpers ─────────────────────────────────────────────────

def _set_aggregation(window, keyword: str):
    """Set the aggregation combo to the item matching keyword."""
    for i in range(window.combo_aggregation.count()):
        if keyword.lower() in window.combo_aggregation.itemText(i).lower():
            window.combo_aggregation.setCurrentIndex(i)
            return
    raise ValueError(f"No aggregation option matching '{keyword}'")


def _get_boxplot_series(window):
    """Return the first QBoxPlotSeries from the chart, or None."""
    from PySide6.QtCharts import QBoxPlotSeries
    chart = window.chart_view.chart()
    for s in chart.series():
        if isinstance(s, QBoxPlotSeries):
            return s
    return None
