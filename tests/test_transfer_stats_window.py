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
