"""
Tests for gui.live_completions — Live download completions window.

Shows in-memory data from running transfer engines: PatientName,
StudyDescription, Time Acquired (StudyTime), Time Download Completed.
Data comes from the engine queue, NOT from the SQLite DB.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableWidget

from core.transfer_engine import TransferEngine, SeriesJob
from gui.live_completions import LiveCompletionsWindow
from gui.main_window import MainWindow
from core.config import AppConfig, PacsNode


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def window(qapp):
    win = LiveCompletionsWindow()
    yield win
    win.close()


# ═══════════════════════════════════════════════════════════════════════════
# Window basics
# ═══════════════════════════════════════════════════════════════════════════

class TestWindowBasics:

    def test_window_title(self, window):
        title = window.windowTitle().lower()
        assert "completion" in title or "download" in title

    def test_is_separate_window(self, window):
        assert window.windowFlags() & Qt.Window

    def test_has_table(self, window):
        assert window.completions_table is not None
        assert isinstance(window.completions_table, QTableWidget)

    def test_table_columns(self, window):
        headers = []
        for col in range(window.completions_table.columnCount()):
            item = window.completions_table.horizontalHeaderItem(col)
            if item:
                headers.append(item.text().lower())
        assert any("patient" in h for h in headers)
        assert any("study" in h or "description" in h for h in headers)
        assert any("acquired" in h or "study time" in h or "study_time" in h
                    for h in headers)
        assert any("completed" in h or "download" in h or "finished" in h
                    for h in headers)

    def test_starts_empty(self, window):
        assert window.completions_table.rowCount() == 0


# ═══════════════════════════════════════════════════════════════════════════
# Menu integration
# ═══════════════════════════════════════════════════════════════════════════

class TestMenuIntegration:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    def test_view_menu_has_completions(self):
        menubar = self.win.menuBar()
        for action in menubar.actions():
            if action.text() == "View":
                menu = action.menu()
                texts = [a.text().lower() for a in menu.actions()]
                assert any("completion" in t or "download" in t
                           for t in texts)
                return
        pytest.fail("View menu not found")


# ═══════════════════════════════════════════════════════════════════════════
# Adding completions
# ═══════════════════════════════════════════════════════════════════════════

class TestAddCompletion:

    def test_add_study_completion(self, window):
        window.add_completion(
            patient_name="Doe^John",
            study_description="CT Abdomen",
            study_time="143000",
            completed_time="15:30:45",
        )
        assert window.completions_table.rowCount() == 1

    def test_multiple_completions(self, window):
        for i in range(3):
            window.add_completion(
                patient_name=f"Patient {i}",
                study_description=f"Study {i}",
                study_time=f"{10 + i:02d}0000",
                completed_time=f"{11 + i:02d}:00:00",
            )
        assert window.completions_table.rowCount() == 3

    def test_shows_patient_name(self, window):
        window.add_completion(
            patient_name="Mueller^Hans",
            study_description="CT Head",
            study_time="080000",
            completed_time="08:15:30",
        )
        found = False
        for col in range(window.completions_table.columnCount()):
            item = window.completions_table.item(0, col)
            if item and "Mueller" in item.text():
                found = True
        assert found

    def test_shows_study_description(self, window):
        window.add_completion(
            patient_name="Test",
            study_description="MR Brain",
            study_time="120000",
            completed_time="12:30:00",
        )
        found = False
        for col in range(window.completions_table.columnCount()):
            item = window.completions_table.item(0, col)
            if item and "MR Brain" in item.text():
                found = True
        assert found

    def test_shows_study_time(self, window):
        window.add_completion(
            patient_name="Test",
            study_description="CT",
            study_time="143022",
            completed_time="15:00:00",
        )
        found = False
        for col in range(window.completions_table.columnCount()):
            item = window.completions_table.item(0, col)
            if item and "14:30" in item.text():
                found = True
        assert found

    def test_shows_completed_time(self, window):
        window.add_completion(
            patient_name="Test",
            study_description="CT",
            study_time="080000",
            completed_time="09:15:42",
        )
        found = False
        for col in range(window.completions_table.columnCount()):
            item = window.completions_table.item(0, col)
            if item and "09:15" in item.text():
                found = True
        assert found

    def test_newest_on_top(self, window):
        """Most recent completion should appear at the top."""
        window.add_completion(
            patient_name="First",
            study_description="CT 1",
            study_time="080000",
            completed_time="08:30:00",
        )
        window.add_completion(
            patient_name="Second",
            study_description="CT 2",
            study_time="090000",
            completed_time="09:30:00",
        )
        top_row_text = []
        for col in range(window.completions_table.columnCount()):
            item = window.completions_table.item(0, col)
            if item:
                top_row_text.append(item.text())
        assert any("Second" in t for t in top_row_text)


# ═══════════════════════════════════════════════════════════════════════════
# Clear
# ═══════════════════════════════════════════════════════════════════════════

class TestClear:

    def test_has_clear_button(self, window):
        assert window.btn_clear is not None

    def test_clear_empties_table(self, window):
        window.add_completion(
            patient_name="Test",
            study_description="CT",
            study_time="080000",
            completed_time="08:30:00",
        )
        assert window.completions_table.rowCount() == 1
        window.btn_clear.click()
        assert window.completions_table.rowCount() == 0


# ═══════════════════════════════════════════════════════════════════════════
# Integration with engine signals
# ═══════════════════════════════════════════════════════════════════════════

class TestEngineIntegration:
    """The MainWindow should wire study_completed to the live window."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    def test_main_window_has_completions_window(self):
        assert hasattr(self.win, 'completions_window')
        assert isinstance(self.win.completions_window, LiveCompletionsWindow)


# ═══════════════════════════════════════════════════════════════════════════
# Extended columns: Institution, Delay, Median label, color coding
# ═══════════════════════════════════════════════════════════════════════════

class TestExtendedColumns:

    def test_table_has_institution_column(self, window):
        headers = _get_headers(window)
        assert any("institution" in h for h in headers)

    def test_table_has_delay_column(self, window):
        """Delay = time between acquisition and download completion."""
        headers = _get_headers(window)
        assert any("delay" in h or "diff" in h or "duration" in h
                    for h in headers)

    def test_institution_shown(self, window):
        window.add_completion(
            patient_name="Test",
            study_description="CT",
            study_time="080000",
            completed_time="08:30:00",
            institution_name="Klinik Nord",
        )
        found = False
        for col in range(window.completions_table.columnCount()):
            item = window.completions_table.item(0, col)
            if item and "Klinik Nord" in item.text():
                found = True
        assert found

    def test_delay_computed(self, window):
        """Delay between 08:00:00 acquired and 08:05:30 completed = 5:30."""
        window.add_completion(
            patient_name="Test",
            study_description="CT",
            study_time="080000",
            completed_time="08:05:30",
            institution_name="Test",
        )
        delay_text = _get_delay_text(window, 0)
        # Should contain 5 minutes and 30 seconds in some format
        assert "5" in delay_text
        assert "30" in delay_text

    def test_delay_shows_minutes_and_seconds(self, window):
        """Delay should be formatted as M:SS or MM:SS."""
        window.add_completion(
            patient_name="Test",
            study_description="CT",
            study_time="100000",
            completed_time="10:02:15",
            institution_name="Test",
        )
        delay_text = _get_delay_text(window, 0)
        assert ":" in delay_text  # formatted with colon


class TestMedianLabel:

    def test_has_median_label(self, window):
        assert window.lbl_median_delay is not None

    def test_median_updates_after_completions(self, window):
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:03:00",
            institution_name="X",
        )
        window.add_completion(
            patient_name="B", study_description="CT",
            study_time="090000", completed_time="09:05:00",
            institution_name="X",
        )
        window.add_completion(
            patient_name="C", study_description="CT",
            study_time="100000", completed_time="10:07:00",
            institution_name="X",
        )
        text = window.lbl_median_delay.text()
        # Median of 3min, 5min, 7min = 5min → should show "5"
        assert "5" in text

    def test_median_empty_when_no_data(self, window):
        text = window.lbl_median_delay.text()
        assert "—" in text or text.strip() == ""


class TestDelayColorCoding:
    """Delay cell colored red if > median + 1 stddev,
    green if < median - 1 stddev."""

    def _add_entries(self, window):
        """Add entries with delays: 3, 5, 5, 5, 7 min.
        Median=5, stddev≈1.26. So 3min → green, 7min → red, 5min → neutral."""
        delays = [
            ("080000", "08:03:00"),  # 3 min
            ("090000", "09:05:00"),  # 5 min
            ("100000", "10:05:00"),  # 5 min
            ("110000", "11:05:00"),  # 5 min
            ("120000", "12:07:00"),  # 7 min
        ]
        for i, (acq, comp) in enumerate(delays):
            window.add_completion(
                patient_name=f"P{i}",
                study_description="CT",
                study_time=acq,
                completed_time=comp,
                institution_name="X",
            )

    def test_slow_delay_is_red(self, window):
        """7 min delay (> median + 1σ) → red text or red background."""
        self._add_entries(window)
        # 7-min entry was added last → row 0 (newest on top)
        delay_col = _find_delay_column(window)
        item = window.completions_table.item(0, delay_col)
        assert item is not None
        fg = item.foreground().color().name()
        bg = item.background().color().name()
        # Should have red coloring (foreground or background)
        assert "#e74c3c" in fg or "#e74c3c" in bg or "red" in fg or "red" in bg

    def test_fast_delay_is_green(self, window):
        """3 min delay (< median - 1σ) → green text or green background."""
        self._add_entries(window)
        # 3-min entry was added first → bottom row (row 4)
        last_row = window.completions_table.rowCount() - 1
        delay_col = _find_delay_column(window)
        item = window.completions_table.item(last_row, delay_col)
        assert item is not None
        fg = item.foreground().color().name()
        bg = item.background().color().name()
        assert "#2ecc71" in fg or "#2ecc71" in bg or "green" in fg or "green" in bg

    def test_normal_delay_no_color(self, window):
        """5 min delay (≈ median) → no special coloring."""
        self._add_entries(window)
        # 5-min entries are rows 1, 2, 3
        delay_col = _find_delay_column(window)
        item = window.completions_table.item(1, delay_col)
        assert item is not None
        fg = item.foreground().color().name()
        # Should not be red or green
        assert "#e74c3c" not in fg
        assert "#2ecc71" not in fg

    def test_colors_update_when_new_entry_changes_stats(self, window):
        """Adding a new entry should re-color all existing delay cells."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:03:00",
            institution_name="X",
        )
        window.add_completion(
            patient_name="B", study_description="CT",
            study_time="090000", completed_time="09:03:00",
            institution_name="X",
        )
        # Both are 3 min, identical → no coloring (stddev=0)
        delay_col = _find_delay_column(window)
        # Now add an outlier
        window.add_completion(
            patient_name="C", study_description="CT",
            study_time="100000", completed_time="10:20:00",
            institution_name="X",
        )
        # 20 min entry should now be red (row 0)
        item = window.completions_table.item(0, delay_col)
        fg = item.foreground().color().name()
        bg = item.background().color().name()
        assert "#e74c3c" in fg or "#e74c3c" in bg


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _get_headers(window):
    t = window.completions_table
    return [t.horizontalHeaderItem(c).text().lower()
            for c in range(t.columnCount())
            if t.horizontalHeaderItem(c)]


def _find_delay_column(window):
    """Return the column index of the delay/diff column."""
    t = window.completions_table
    for c in range(t.columnCount()):
        h = t.horizontalHeaderItem(c)
        if h:
            txt = h.text().lower()
            if "delay" in txt or "diff" in txt or "duration" in txt:
                return c
    raise ValueError("Delay column not found")


def _get_delay_text(window, row: int) -> str:
    col = _find_delay_column(window)
    item = window.completions_table.item(row, col)
    return item.text() if item else ""
