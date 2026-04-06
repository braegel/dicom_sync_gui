"""
Tests for gui.examination_lookup — Hash-based examination lookup dialog.

Allows the user to enter PatientID and/or AccessionNumber (+ optional
StudyDate) in cleartext, hashes them, and queries the transfer log DB
to show transfer details for that specific examination.
Warns if series were likely resent (effective Mbit/s < median - 2*stddev).
"""

import hashlib
import math
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QTableWidget

from core.transfer_log import TransferLog, estimate_bytes
from gui.examination_lookup import ExaminationLookupDialog
from gui.main_window import MainWindow


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _populate_normal(log: TransferLog):
    """Insert several studies with consistent, fast transfer speeds."""
    for day in range(1, 6):
        date = f"2026040{day}"
        log.record_series(
            source_pacs="ct", study_uid=f"1.2.{day}",
            series_uid=f"1.2.{day}.1", patient_id="PAT001",
            accession_number=f"ACC{day:03d}",
            study_date=date, study_time="080000",
            modality="CT", study_description="CT Abdomen",
            series_description="Axial", series_number="1",
            image_count=300, duration_seconds=30.0,
        )
        log.record_series(
            source_pacs="ct", study_uid=f"1.2.{day}",
            series_uid=f"1.2.{day}.2", patient_id="PAT001",
            accession_number=f"ACC{day:03d}",
            study_date=date, study_time="080000",
            modality="CT", study_description="CT Abdomen",
            series_description="Coronal", series_number="2",
            image_count=300, duration_seconds=30.0,
        )
    # Study with anomalously slow transfer (resend candidate)
    log.record_series(
        source_pacs="ct", study_uid="1.2.99",
        series_uid="1.2.99.1", patient_id="PAT001",
        accession_number="ACC099",
        study_date="20260406", study_time="100000",
        modality="CT", study_description="CT Thorax",
        series_description="Axial", series_number="1",
        image_count=300, duration_seconds=30.0,
    )
    # Second series arrived much later → huge wall clock
    log.record_series(
        source_pacs="ct", study_uid="1.2.99",
        series_uid="1.2.99.2", patient_id="PAT001",
        accession_number="ACC099",
        study_date="20260406", study_time="100000",
        modality="CT", study_description="CT Thorax",
        series_description="Coronal", series_number="2",
        image_count=300, duration_seconds=600.0,  # 10 minutes for same data
    )
    # Different patient
    log.record_series(
        source_pacs="ct", study_uid="2.2.1",
        series_uid="2.2.1.1", patient_id="PAT002",
        accession_number="ACC_OTHER",
        study_date="20260401", study_time="090000",
        modality="MR", study_description="MR Brain",
        series_description="T1", series_number="1",
        image_count=200, duration_seconds=25.0,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "lookup_test.sqlite")


@pytest.fixture
def log(db_path):
    tl = TransferLog(db_path)
    yield tl
    tl.close()


@pytest.fixture
def populated_log(log):
    _populate_normal(log)
    return log


@pytest.fixture
def dialog(populated_log, db_path, qapp):
    dlg = ExaminationLookupDialog(db_path)
    yield dlg
    dlg.close()


@pytest.fixture
def empty_dialog(log, db_path, qapp):
    dlg = ExaminationLookupDialog(db_path)
    yield dlg
    dlg.close()


# ═══════════════════════════════════════════════════════════════════════════
# Dialog basics
# ═══════════════════════════════════════════════════════════════════════════

class TestDialogBasics:

    def test_window_title(self, dialog):
        title = dialog.windowTitle().lower()
        assert "lookup" in title or "examination" in title or "search" in title

    def test_has_patient_id_input(self, dialog):
        assert dialog.input_patient_id is not None
        assert isinstance(dialog.input_patient_id, QLineEdit)

    def test_has_accession_input(self, dialog):
        assert dialog.input_accession is not None
        assert isinstance(dialog.input_accession, QLineEdit)

    def test_has_study_date_input(self, dialog):
        assert dialog.input_study_date is not None
        assert isinstance(dialog.input_study_date, QLineEdit)

    def test_has_search_button(self, dialog):
        assert dialog.btn_search is not None

    def test_has_results_table(self, dialog):
        assert dialog.results_table is not None
        assert isinstance(dialog.results_table, QTableWidget)


# ═══════════════════════════════════════════════════════════════════════════
# Menu integration
# ═══════════════════════════════════════════════════════════════════════════

class TestMenuIntegration:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    def test_tools_menu_has_lookup(self):
        menubar = self.win.menuBar()
        for action in menubar.actions():
            if action.text() == "Tools":
                menu = action.menu()
                texts = [a.text() for a in menu.actions()]
                assert any("lookup" in t.lower() or "examination" in t.lower()
                           for t in texts)
                return
        pytest.fail("Tools menu not found")


# ═══════════════════════════════════════════════════════════════════════════
# Search by PatientID
# ═══════════════════════════════════════════════════════════════════════════

class TestSearchByPatientID:

    def test_finds_patient(self, dialog):
        dialog.input_patient_id.setText("PAT001")
        dialog.btn_search.click()
        assert dialog.results_table.rowCount() > 0

    def test_excludes_other_patient(self, dialog):
        dialog.input_patient_id.setText("PAT001")
        dialog.btn_search.click()
        # All rows should belong to PAT001's hash
        for row in range(dialog.results_table.rowCount()):
            found_pat002_data = False
            for col in range(dialog.results_table.columnCount()):
                item = dialog.results_table.item(row, col)
                if item and "MR Brain" in item.text():
                    found_pat002_data = True
            assert not found_pat002_data

    def test_no_results_for_unknown_patient(self, dialog):
        dialog.input_patient_id.setText("NONEXISTENT")
        dialog.btn_search.click()
        assert dialog.results_table.rowCount() == 0


# ═══════════════════════════════════════════════════════════════════════════
# Search by AccessionNumber
# ═══════════════════════════════════════════════════════════════════════════

class TestSearchByAccession:

    def test_finds_accession(self, dialog):
        dialog.input_accession.setText("ACC001")
        dialog.btn_search.click()
        assert dialog.results_table.rowCount() > 0

    def test_single_study_by_accession(self, dialog):
        dialog.input_accession.setText("ACC001")
        dialog.btn_search.click()
        # ACC001 is day 1, 2 series
        assert dialog.results_table.rowCount() == 2


# ═══════════════════════════════════════════════════════════════════════════
# Search with StudyDate filter
# ═══════════════════════════════════════════════════════════════════════════

class TestSearchWithDate:

    def test_date_narrows_results(self, dialog):
        dialog.input_patient_id.setText("PAT001")
        dialog.btn_search.click()
        all_count = dialog.results_table.rowCount()

        dialog.input_study_date.setText("20260401")
        dialog.btn_search.click()
        filtered_count = dialog.results_table.rowCount()
        assert filtered_count < all_count

    def test_date_only_search(self, dialog):
        """StudyDate alone (no patient/accession) should also work."""
        dialog.input_study_date.setText("20260401")
        dialog.btn_search.click()
        assert dialog.results_table.rowCount() > 0


# ═══════════════════════════════════════════════════════════════════════════
# Results table content
# ═══════════════════════════════════════════════════════════════════════════

class TestResultsContent:

    def test_shows_timestamp(self, dialog):
        """Results should show when the transfer happened."""
        dialog.input_accession.setText("ACC001")
        dialog.btn_search.click()
        headers = _get_headers(dialog.results_table)
        assert any("timestamp" in h or "time" in h or "start" in h for h in headers)

    def test_shows_duration(self, dialog):
        dialog.input_accession.setText("ACC001")
        dialog.btn_search.click()
        headers = _get_headers(dialog.results_table)
        assert any("duration" in h or "sec" in h for h in headers)

    def test_shows_mbps(self, dialog):
        dialog.input_accession.setText("ACC001")
        dialog.btn_search.click()
        headers = _get_headers(dialog.results_table)
        assert any("mbit" in h or "mbps" in h for h in headers)

    def test_shows_modality(self, dialog):
        dialog.input_accession.setText("ACC001")
        dialog.btn_search.click()
        headers = _get_headers(dialog.results_table)
        assert any("modal" in h for h in headers)

    def test_shows_series_description(self, dialog):
        dialog.input_accession.setText("ACC001")
        dialog.btn_search.click()
        headers = _get_headers(dialog.results_table)
        assert any("series" in h for h in headers)

    def test_shows_image_count(self, dialog):
        dialog.input_accession.setText("ACC001")
        dialog.btn_search.click()
        headers = _get_headers(dialog.results_table)
        assert any("image" in h for h in headers)


# ═══════════════════════════════════════════════════════════════════════════
# Resend detection
# ═══════════════════════════════════════════════════════════════════════════

class TestResendDetection:
    """When a series has effective Mbit/s < median - 2*stddev of all series,
    the dialog should show a warning that series were likely resent."""

    def test_warning_shown_for_slow_study(self, dialog):
        """ACC099 has one series at 600s → extremely low Mbit/s → warning."""
        dialog.input_accession.setText("ACC099")
        dialog.btn_search.click()
        warning = dialog.lbl_resend_warning.text()
        assert len(warning) > 0
        lower = warning.lower()
        assert "resend" in lower or "resent" in lower or "nachgesendet" in lower

    def test_no_warning_for_normal_study(self, dialog):
        """ACC001 has normal speeds → no warning."""
        dialog.input_accession.setText("ACC001")
        dialog.btn_search.click()
        warning = dialog.lbl_resend_warning.text()
        assert warning == "" or "resend" not in warning.lower()

    def test_warning_label_exists(self, dialog):
        assert dialog.lbl_resend_warning is not None

    def test_warning_clears_on_new_search(self, dialog):
        """Warning from previous search should clear."""
        dialog.input_accession.setText("ACC099")
        dialog.btn_search.click()
        assert len(dialog.lbl_resend_warning.text()) > 0
        dialog.input_accession.setText("ACC001")
        dialog.btn_search.click()
        warning = dialog.lbl_resend_warning.text()
        assert "resend" not in warning.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Empty state
# ═══════════════════════════════════════════════════════════════════════════

class TestEmptyState:

    def test_no_crash_on_empty_db(self, empty_dialog):
        empty_dialog.input_patient_id.setText("PAT001")
        empty_dialog.btn_search.click()
        assert empty_dialog.results_table.rowCount() == 0

    def test_no_crash_empty_inputs(self, dialog):
        """Clicking search with all fields empty should not crash."""
        dialog.btn_search.click()


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _get_headers(table: QTableWidget):
    return [table.horizontalHeaderItem(c).text().lower()
            for c in range(table.columnCount())
            if table.horizontalHeaderItem(c)]
