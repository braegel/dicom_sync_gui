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
from PySide6.QtWidgets import QApplication, QTableWidget, QPushButton

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
            if "delay" in txt or "diff" in txt:
                return c
    raise ValueError("Delay column not found")


def _get_delay_text(window, row: int) -> str:
    col = _find_delay_column(window)
    item = window.completions_table.item(row, col)
    return item.text() if item else ""


# ═══════════════════════════════════════════════════════════════════════════
# Download duration column (wall-clock for entire study)
# ═══════════════════════════════════════════════════════════════════════════

class TestDownloadDurationColumn:
    """Show how long the full study took to download (wall-clock,
    not per-series duration)."""

    def test_table_has_download_duration_column(self, window):
        headers = _get_headers(window)
        assert any("download" in h and ("duration" in h or "time" in h)
                    for h in headers) or any(
                        "wall" in h for h in headers)

    def test_download_duration_shown(self, window):
        window.add_completion(
            patient_name="Test",
            study_description="CT",
            study_time="080000",
            completed_time="08:05:30",
            institution_name="X",
            download_duration_seconds=125.0,
        )
        col = _find_download_duration_column(window)
        item = window.completions_table.item(0, col)
        assert item is not None
        # 125s = 2:05
        assert "2" in item.text()
        assert "05" in item.text()

    def test_download_duration_formatted_minutes_seconds(self, window):
        window.add_completion(
            patient_name="Test",
            study_description="CT",
            study_time="080000",
            completed_time="08:01:42",
            institution_name="X",
            download_duration_seconds=42.0,
        )
        col = _find_download_duration_column(window)
        item = window.completions_table.item(0, col)
        assert ":" in item.text()

    def test_download_duration_optional(self, window):
        """add_completion without duration param should not crash."""
        window.add_completion(
            patient_name="Test",
            study_description="CT",
            study_time="080000",
            completed_time="08:05:30",
            institution_name="X",
        )
        col = _find_download_duration_column(window)
        item = window.completions_table.item(0, col)
        assert item is not None  # cell exists, may show "—"

    def test_download_duration_long_study(self, window):
        """A 15-minute download should display as 15:00."""
        window.add_completion(
            patient_name="Test",
            study_description="CT",
            study_time="080000",
            completed_time="08:30:00",
            institution_name="X",
            download_duration_seconds=900.0,
        )
        col = _find_download_duration_column(window)
        item = window.completions_table.item(0, col)
        assert "15" in item.text()


def _find_download_duration_column(window):
    """Return the column index of the download duration column."""
    t = window.completions_table
    for c in range(t.columnCount()):
        h = t.horizontalHeaderItem(c)
        if h:
            txt = h.text().lower()
            if "download duration" in txt or "wall" in txt:
                return c
    raise ValueError("Download duration column not found")


# ═══════════════════════════════════════════════════════════════════════════
# Download duration color coding (2 stddev threshold)
# ═══════════════════════════════════════════════════════════════════════════

class TestDownloadDurationColorCoding:
    """Download duration cell colored red if > median + 2 stddev,
    green if < median - 2 stddev. Uses 2σ (not 1σ like Delay) because
    download time is more variable per study size."""

    def _add_with_durations(self, window, durations):
        """Add a completion for each duration in seconds."""
        for i, dur in enumerate(durations):
            window.add_completion(
                patient_name=f"P{i}",
                study_description="CT",
                study_time=f"{8 + i:02d}0000",
                completed_time=f"{8 + i:02d}:01:00",
                institution_name="X",
                download_duration_seconds=float(dur),
            )

    def test_slow_download_is_red(self, window):
        """One large outlier (> median + 2σ) → red.
        Durations: 100×8 + 500. median=100, σ≈119.5, 2σ≈239 → 500>339."""
        durations = [100] * 8 + [500]
        self._add_with_durations(window, durations)
        # 500 was added last → row 0
        col = _find_download_duration_column(window)
        item = window.completions_table.item(0, col)
        assert item is not None
        fg = item.foreground().color().name()
        bg = item.background().color().name()
        assert ("#e74c3c" in fg or "#e74c3c" in bg
                or "red" in fg or "red" in bg)

    def test_fast_download_is_green(self, window):
        """One small outlier (< median - 2σ) → green.
        Durations: 10 + 100×8. median=100, σ≈28.3, 2σ≈56.6 → 10<43.4."""
        durations = [10] + [100] * 8
        self._add_with_durations(window, durations)
        # 10 was added first → bottom row
        last_row = window.completions_table.rowCount() - 1
        col = _find_download_duration_column(window)
        item = window.completions_table.item(last_row, col)
        assert item is not None
        fg = item.foreground().color().name()
        bg = item.background().color().name()
        assert ("#2ecc71" in fg or "#2ecc71" in bg
                or "green" in fg or "green" in bg)

    def test_normal_download_no_color(self, window):
        """A duration near the median is not colored red or green."""
        durations = [100] * 8 + [500]
        self._add_with_durations(window, durations)
        col = _find_download_duration_column(window)
        # Pick a row with a 100s value (not the row 0 outlier).
        item = window.completions_table.item(1, col)
        assert item is not None
        fg = item.foreground().color().name()
        assert "#e74c3c" not in fg
        assert "#2ecc71" not in fg

    def test_one_stddev_does_not_trigger(self, window):
        """A value > median + 1σ but < median + 2σ stays neutral
        (the Delay column uses 1σ, this column must use 2σ)."""
        # Durations: 100×8 + 250.
        # mean=116.67, σ≈47.1, 2σ≈94.3 → 250>194.3? Yes. Bad example.
        # Need value in (median+1σ, median+2σ).
        # Use [100]*10 + [180]: mean=107.27, σ≈22.6, 1σ→129.9, 2σ→152.5.
        # 180 > 152.5. Still triggers. Try [100]*20 + [150]:
        # mean=102.38, σ≈10.7, 1σ→113.1, 2σ→123.7. 150>123.7. Still red.
        # The outlier dominates σ. Use two close-ish outliers:
        # [100]*20 + [140, 140]: mean=103.64, var≈
        #   20*(3.64²)+2*(36.36²) = 264.99+2644.1 = 2909/22=132.2,
        #   σ≈11.5, median=100, 1σ→111.5, 2σ→123. 140>123. Still red.
        # Try [100]*50 + [130]: mean=100.59, σ≈4.2, 2σ→108.8. 130>108.8.
        # Conclusion: with many identical values, σ collapses → any
        # outlier is "extreme". So instead build a naturally-spread set.
        durations = [80, 90, 100, 100, 100, 100, 100, 100, 110, 120, 135]
        # n=11, sum=1135, mean≈103.18
        # variance: (23.18²+13.18²+3.18²×6+6.82²+16.82²+31.82²)/11
        #   ≈ (537+174+61+47+283+1013)/11 ≈ 2115/11 ≈ 192.3, σ≈13.87
        # median=100. 1σ→113.87, 2σ→127.74. 120 is between → neutral.
        self._add_with_durations(window, durations)
        col = _find_download_duration_column(window)
        # 120 was added 10th (index 9) → row 1 from top
        # (rows are inserted at 0, so newest=row 0, oldest=last row)
        # Index 9 in input → row = (n-1) - 9 = 1
        item = window.completions_table.item(1, col)
        assert item is not None
        fg = item.foreground().color().name()
        assert "#e74c3c" not in fg, (
            "120s is > median+1σ but < median+2σ — must NOT be red")

    def test_colors_update_when_new_entry_changes_stats(self, window):
        """Adding a new outlier should re-color earlier duration cells."""
        # First add 9 identical 100s entries → no coloring (σ=0).
        self._add_with_durations(window, [100] * 9)
        col = _find_download_duration_column(window)
        # Now add a big outlier.
        window.add_completion(
            patient_name="X",
            study_description="CT",
            study_time="200000",
            completed_time="20:01:00",
            institution_name="X",
            download_duration_seconds=600.0,
        )
        item = window.completions_table.item(0, col)
        fg = item.foreground().color().name()
        bg = item.background().color().name()
        assert ("#e74c3c" in fg or "#e74c3c" in bg
                or "red" in fg or "red" in bg)

    def test_missing_duration_does_not_crash_coloring(self, window):
        """Entries without download_duration_seconds must not break
        the color update for entries that do have it."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:01:00",
            institution_name="X",
        )  # no duration
        # Should not raise.
        self._add_with_durations(window, [100] * 8 + [500])
        col = _find_download_duration_column(window)
        # The 500s outlier (newest, row 0) should still be red.
        item = window.completions_table.item(0, col)
        assert item is not None
        fg = item.foreground().color().name()
        bg = item.background().color().name()
        assert ("#e74c3c" in fg or "#e74c3c" in bg
                or "red" in fg or "red" in bg)


# ═══════════════════════════════════════════════════════════════════════════
# Auto column width — every column wide enough to show its header label
# ═══════════════════════════════════════════════════════════════════════════

class TestAutoColumnWidth:
    """Each column must be wide enough to display its header text fully,
    so the user never sees truncated headers like 'Down…'."""

    def _assert_columns_fit_headers(self, window):
        t = window.completions_table
        fm = t.horizontalHeader().fontMetrics()
        for c in range(t.columnCount()):
            h = t.horizontalHeaderItem(c)
            if h is None:
                continue
            label = h.text()
            if not label:
                continue
            needed = fm.horizontalAdvance(label)
            actual = t.columnWidth(c)
            assert actual >= needed, (
                f"Column {c} ({label!r}) width {actual}px < "
                f"header text width {needed}px")

    def test_columns_fit_headers_when_empty(self, window):
        """Even with no rows, headers must be fully readable."""
        self._assert_columns_fit_headers(window)

    def test_columns_fit_headers_after_add(self, window):
        window.add_completion(
            patient_name="Doe^John",
            study_description="CT Abdomen",
            study_time="080000",
            completed_time="08:15:30",
            institution_name="St. Mary's Hospital",
            download_duration_seconds=120.0,
        )
        self._assert_columns_fit_headers(window)

    def test_columns_fit_headers_after_long_content(self, window):
        """Long cell content must not push the header into ellipsis."""
        window.add_completion(
            patient_name="VeryLongLastName^VeryLongFirstName",
            study_description="CT Thorax / Abdomen / Pelvis with contrast",
            study_time="080000",
            completed_time="09:45:11",
            institution_name="Some Very Long Institution Name e.V.",
            download_duration_seconds=345.0,
        )
        self._assert_columns_fit_headers(window)


# ═══════════════════════════════════════════════════════════════════════════
# Per-row "copy completion time to clipboard" button
# ═══════════════════════════════════════════════════════════════════════════

class TestCopyCompletedTimeButton:
    """Each row has a button that copies a string like
    'Image transfer completed: HH:MM:SS' to the system clipboard,
    so it can be pasted into a radiology report."""

    def test_each_row_has_a_copy_button(self, window):
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X",
        )
        btn = _find_copy_button(window, row=0)
        assert btn is not None, "row 0 has no copy button"
        assert isinstance(btn, QPushButton)

    def test_copy_button_puts_completed_time_on_clipboard(self, window, qapp):
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X",
        )
        clipboard = QApplication.clipboard()
        clipboard.clear()
        _find_copy_button(window, row=0).click()
        text = clipboard.text()
        assert "08:15:30" in text
        assert "transfer" in text.lower()
        assert "complet" in text.lower()  # completed / completet

    def test_copy_button_format(self, window, qapp):
        """The clipboard string follows the canonical template."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X",
        )
        QApplication.clipboard().clear()
        _find_copy_button(window, row=0).click()
        text = QApplication.clipboard().text().strip()
        assert text == "Image transfer completed: 08:15:30"

    def test_each_row_button_copies_its_own_time(self, window, qapp):
        """Per-row buttons must not all copy the same row's time."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X",
        )
        window.add_completion(
            patient_name="B", study_description="CT",
            study_time="090000", completed_time="09:22:11",
            institution_name="X",
        )
        # newest at row 0 → 09:22:11; older at row 1 → 08:15:30
        clipboard = QApplication.clipboard()

        clipboard.clear()
        _find_copy_button(window, row=0).click()
        assert "09:22:11" in clipboard.text()
        assert "08:15:30" not in clipboard.text()

        clipboard.clear()
        _find_copy_button(window, row=1).click()
        assert "08:15:30" in clipboard.text()
        assert "09:22:11" not in clipboard.text()

    def test_copy_button_survives_clear(self, window, qapp):
        """After Clear, new rows still get working buttons."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X",
        )
        window._clear()
        window.add_completion(
            patient_name="B", study_description="CT",
            study_time="090000", completed_time="09:22:11",
            institution_name="X",
        )
        QApplication.clipboard().clear()
        _find_copy_button(window, row=0).click()
        assert "09:22:11" in QApplication.clipboard().text()


def _find_copy_button(window, row: int):
    """Find the per-row copy button for the given row.

    The button is a QPushButton placed as a cellWidget in some column
    of the completions table. Returns None if not found.
    """
    t = window.completions_table
    for c in range(t.columnCount()):
        w = t.cellWidget(row, c)
        if w is None:
            continue
        if isinstance(w, QPushButton):
            return w
        # Maybe wrapped in a container widget
        children = w.findChildren(QPushButton)
        if children:
            return children[0]
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Copy button localization — clipboard text in the configured language
# ═══════════════════════════════════════════════════════════════════════════

class TestCopyButtonLocalization:
    """LiveCompletionsWindow takes a language parameter; the copy
    button's clipboard text is produced in that language.  Required
    languages: en, de, fr, es."""

    def _make_window(self, qapp, language):
        win = LiveCompletionsWindow(language=language)
        return win

    def _add_row_and_click(self, win):
        win.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X",
        )
        QApplication.clipboard().clear()
        _find_copy_button(win, row=0).click()
        return QApplication.clipboard().text().strip()

    def test_default_language_is_english(self, qapp):
        """No explicit language → English."""
        win = LiveCompletionsWindow()
        try:
            win.add_completion(
                patient_name="A", study_description="CT",
                study_time="080000", completed_time="08:15:30",
                institution_name="X",
            )
            QApplication.clipboard().clear()
            _find_copy_button(win, row=0).click()
            text = QApplication.clipboard().text().strip()
            assert text == "Image transfer completed: 08:15:30"
        finally:
            win.close()

    def test_english_explicit(self, qapp):
        win = self._make_window(qapp, "en")
        try:
            assert self._add_row_and_click(win) == \
                "Image transfer completed: 08:15:30"
        finally:
            win.close()

    def test_german(self, qapp):
        """Clipboard format in German: 'Abschluss Bildübertragung: HH:MM:SS'."""
        win = self._make_window(qapp, "de")
        try:
            assert self._add_row_and_click(win) == \
                "Abschluss Bildübertragung: 08:15:30"
        finally:
            win.close()

    def test_french(self, qapp):
        win = self._make_window(qapp, "fr")
        try:
            assert self._add_row_and_click(win) == \
                "Transfert d'images terminé: 08:15:30"
        finally:
            win.close()

    def test_spanish(self, qapp):
        win = self._make_window(qapp, "es")
        try:
            assert self._add_row_and_click(win) == \
                "Transferencia de imágenes completada: 08:15:30"
        finally:
            win.close()

    def test_unknown_language_falls_back_to_english(self, qapp):
        """A bad language value must not break the UI."""
        win = self._make_window(qapp, "xx")
        try:
            assert self._add_row_and_click(win) == \
                "Image transfer completed: 08:15:30"
        finally:
            win.close()


# ═══════════════════════════════════════════════════════════════════════════
# Images and img/min columns — per-study throughput at a glance
# ═══════════════════════════════════════════════════════════════════════════

class TestImagesAndImagesPerMinuteColumns:
    """Each completed study shows the total number of transferred
    images and the images/minute throughput, computed from the
    wall-clock download duration."""

    def test_images_column_header_exists(self, window):
        headers = _get_headers(window)
        assert any(h == "images" for h in headers), (
            f"expected an 'Images' column, got {headers}")

    def test_images_per_minute_column_header_exists(self, window):
        headers = _get_headers(window)
        assert any("img/min" in h or "images/min" in h or "ipm" in h
                    for h in headers), (
            f"expected an img/min column, got {headers}")

    def test_image_count_displayed(self, window):
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:01:00",
            institution_name="X",
            download_duration_seconds=60.0,
            image_count=300,
        )
        col = _find_images_column(window)
        item = window.completions_table.item(0, col)
        assert item is not None
        assert item.text() == "300"

    def test_images_per_minute_computed(self, window):
        """300 images in 60 s = 300 img/min."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:01:00",
            institution_name="X",
            download_duration_seconds=60.0,
            image_count=300,
        )
        col = _find_ipm_column(window)
        item = window.completions_table.item(0, col)
        assert item is not None
        assert item.text() == "300"

    def test_images_per_minute_rounded_to_integer(self, window):
        """200 images in 90 s -> 133.33 img/min -> displayed as '133'."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:01:30",
            institution_name="X",
            download_duration_seconds=90.0,
            image_count=200,
        )
        col = _find_ipm_column(window)
        item = window.completions_table.item(0, col)
        assert item.text() == "133"

    def test_missing_image_count_shows_dash(self, window):
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:01:00",
            institution_name="X",
            download_duration_seconds=60.0,
            # no image_count
        )
        img_col = _find_images_column(window)
        ipm_col = _find_ipm_column(window)
        assert window.completions_table.item(0, img_col).text() == "—"
        assert window.completions_table.item(0, ipm_col).text() == "—"

    def test_missing_duration_keeps_image_count_but_dashes_ipm(self, window):
        """Without a duration, img/min cannot be computed — but the
        image count itself should still be visible."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:01:00",
            institution_name="X",
            image_count=300,
            # no download_duration_seconds
        )
        img_col = _find_images_column(window)
        ipm_col = _find_ipm_column(window)
        assert window.completions_table.item(0, img_col).text() == "300"
        assert window.completions_table.item(0, ipm_col).text() == "—"

    def test_zero_duration_does_not_divide_by_zero(self, window):
        """A zero duration must not crash; img/min degrades to '—'."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:01:00",
            institution_name="X",
            download_duration_seconds=0.0,
            image_count=300,
        )
        img_col = _find_images_column(window)
        ipm_col = _find_ipm_column(window)
        assert window.completions_table.item(0, img_col).text() == "300"
        assert window.completions_table.item(0, ipm_col).text() == "—"


def _find_images_column(window):
    t = window.completions_table
    for c in range(t.columnCount()):
        h = t.horizontalHeaderItem(c)
        if h and h.text().strip().lower() == "images":
            return c
    raise ValueError("Images column not found")


def _find_ipm_column(window):
    t = window.completions_table
    for c in range(t.columnCount()):
        h = t.horizontalHeaderItem(c)
        if h:
            txt = h.text().lower()
            if "img/min" in txt or "images/min" in txt or "ipm" in txt:
                return c
    raise ValueError("img/min column not found")
