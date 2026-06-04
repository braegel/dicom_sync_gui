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
from PySide6.QtWidgets import (
    QApplication, QTableWidget, QPushButton, QHeaderView,
)

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
    """Delay cell coloured red if > median + 2σ, green if < median − 2σ.

    1.0.12: thresholds unified with Download Duration (both ±2σ).  The
    previous Delay code used ±1σ which over-triggered red/green on
    naturally-spread datasets.
    """

    def _add_entries_with_delays(self, window, delays_seconds):
        """Add one completion per delay (seconds).

        ``study_time`` is fixed at ``"080000"`` so the rendered delay
        is determined purely by ``completed_time``.  Patient names
        differ per entry so insertion order is observable.
        """
        for i, d in enumerate(delays_seconds):
            comp_h = 8 + d // 3600
            comp_m = (d % 3600) // 60
            comp_s = d % 60
            window.add_completion(
                patient_name=f"P{i}",
                study_description="CT",
                study_time="080000",
                completed_time=f"{comp_h:02d}:{comp_m:02d}:{comp_s:02d}",
                institution_name="X",
            )

    def test_slow_delay_is_red(self, window):
        """Strong outlier > median + 2σ → red.

        Delays: 5 min × 8 + 30 min.  median=300s, σ≈471s,
        2σ≈943s → 1800s > 1243s → outlier is red.
        """
        delays = [300] * 8 + [1800]
        self._add_entries_with_delays(window, delays)
        delay_col = _find_delay_column(window)
        item = window.completions_table.item(0, delay_col)
        assert item is not None
        fg = item.foreground().color().name()
        bg = item.background().color().name()
        assert ("#e74c3c" in fg or "#e74c3c" in bg
                or "red" in fg or "red" in bg)

    def test_fast_delay_is_green(self, window):
        """Strong outlier < median − 2σ → green.

        Delays: 30 sec + 5 min × 8.  median=300s, σ≈85s,
        2σ≈170s → 30s < 130s → outlier is green.
        """
        delays = [30] + [300] * 8
        self._add_entries_with_delays(window, delays)
        last_row = window.completions_table.rowCount() - 1
        delay_col = _find_delay_column(window)
        item = window.completions_table.item(last_row, delay_col)
        assert item is not None
        fg = item.foreground().color().name()
        bg = item.background().color().name()
        assert ("#2ecc71" in fg or "#2ecc71" in bg
                or "green" in fg or "green" in bg)

    def test_normal_delay_no_color(self, window):
        """A delay near the median is not coloured."""
        delays = [300] * 8 + [1800]
        self._add_entries_with_delays(window, delays)
        delay_col = _find_delay_column(window)
        # Row 1 is a 5-min entry (one of the 8 baseline values).
        item = window.completions_table.item(1, delay_col)
        assert item is not None
        fg = item.foreground().color().name()
        assert "#e74c3c" not in fg
        assert "#2ecc71" not in fg

    def test_one_sigma_delay_does_not_trigger_red(self, window):
        """Discrimination: a delay that's > median + 1σ but
        < median + 2σ must stay neutral.  This catches the regression
        if the threshold ever slips back to ±1σ.

        Delays: [80, 90, 100×6, 110, 120, 135] seconds.
        median=100s, σ≈13.87s, 1σ→113.87s, 2σ→127.74s.
        The 120s entry sits between 1σ and 2σ and must NOT be red.
        """
        delays = [80, 90, 100, 100, 100, 100, 100, 100, 110, 120, 135]
        self._add_entries_with_delays(window, delays)
        delay_col = _find_delay_column(window)
        # 120 was added at index 9; with insertRow(0) the newest is
        # row 0, so index i lands at row (n-1)-i.
        row_of_120 = (len(delays) - 1) - 9
        item = window.completions_table.item(row_of_120, delay_col)
        assert item is not None
        fg = item.foreground().color().name()
        assert "#e74c3c" not in fg, (
            "120s delay is > median+1σ but < median+2σ — "
            "must NOT be coloured red under the ±2σ rule")

    def test_colors_update_when_new_entry_changes_stats(self, window):
        """Adding a new entry must re-colour all existing delay cells."""
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
        # Now add an extreme outlier.
        window.add_completion(
            patient_name="C", study_description="CT",
            study_time="100000", completed_time="10:20:00",
            institution_name="X",
        )
        # 20-min entry should now be red (row 0).  Spread:
        # [180, 180, 1200]: median=180, σ≈481, 2σ≈962 → 1200>1142 → red.
        item = window.completions_table.item(0, delay_col)
        fg = item.foreground().color().name()
        bg = item.background().color().name()
        assert "#e74c3c" in fg or "#e74c3c" in bg


# ═══════════════════════════════════════════════════════════════════════════
# Column resizing — user can drag column borders (Excel / LibreOffice style)
# ═══════════════════════════════════════════════════════════════════════════

class TestColumnResizing:
    """Every column in the completions table must be user-resizable
    by dragging the column border."""

    def test_all_columns_use_interactive_resize_mode(self, window):
        header = window.completions_table.horizontalHeader()
        for col in range(window.completions_table.columnCount()):
            mode = header.sectionResizeMode(col)
            assert mode == QHeaderView.Interactive, (
                f"column {col} resize mode is {mode}; "
                f"expected QHeaderView.Interactive so the user can "
                f"drag its border like in Excel / LibreOffice")

    def test_user_set_column_width_persists(self, window):
        """When the user drags a column border to a new width, the
        width must stick.  ResizeToContents would snap back; only
        Interactive mode keeps the manual width."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:01:00",
            institution_name="X",
        )
        target = 200
        # Patient column index — looked up by header label so the
        # test stays robust against future column reorderings.
        patient_col = _column_index_for_header(window, "patient")
        assert patient_col >= 0
        window.completions_table.setColumnWidth(patient_col, target)
        assert window.completions_table.columnWidth(patient_col) == target


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


def _column_index_for_header(window, needle: str) -> int:
    """Return the column index whose header text contains *needle*
    (case-insensitive).  -1 if no column matches."""
    t = window.completions_table
    needle = needle.lower()
    for c in range(t.columnCount()):
        item = t.horizontalHeaderItem(c)
        if item and needle in item.text().lower():
            return c
    return -1


# ═══════════════════════════════════════════════════════════════════════════
# Copy button is in column 0  +  table is sortable by column header
# ═══════════════════════════════════════════════════════════════════════════

class TestCopyButtonInFirstColumn:
    """The per-row Copy button must live in the FIRST column so the
    user can click it without horizontally scrolling on narrow windows."""

    def test_copy_button_lives_in_column_zero(self, window):
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X",
        )
        t = window.completions_table
        cell0 = t.cellWidget(0, 0)
        # Either the cell widget itself is the button, or it wraps one.
        if isinstance(cell0, QPushButton):
            assert cell0.text().strip().lower().startswith("copy")
            return
        assert cell0 is not None, (
            "column 0 must host the per-row Copy button widget")
        buttons = cell0.findChildren(QPushButton)
        assert buttons, (
            "column 0 must contain a QPushButton (the Copy button)")
        assert buttons[0].text().strip().lower().startswith("copy")

    def test_copy_button_is_not_in_last_column(self, window):
        """Regression guard: after the move, no Copy button should
        remain in the previous last-column slot."""
        window.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X",
        )
        t = window.completions_table
        last = t.columnCount() - 1
        w = t.cellWidget(0, last)
        if w is None:
            return  # nothing in last column = fine
        if isinstance(w, QPushButton):
            assert not w.text().strip().lower().startswith("copy"), (
                "Copy button must have moved out of the last column")
        else:
            buttons = w.findChildren(QPushButton)
            assert not any(b.text().strip().lower().startswith("copy")
                           for b in buttons), (
                "Copy button must have moved out of the last column")


class TestAggregateByStudyUid:
    """When the engine emits study_completed multiple times for the
    same study_uid (because more series arrived for that study in a
    later query cycle), the Download Completions window must UPDATE
    the existing row instead of appending a duplicate.  Numeric
    fields (image_count, download_duration_seconds) are summed; the
    completed_time advances to the latest.
    """

    def test_repeated_study_uid_keeps_single_row(self, window):
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=100,
            download_duration_seconds=20.0,
        )
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=50,
            download_duration_seconds=10.0,
        )
        assert window.completions_table.rowCount() == 1, (
            "two add_completion calls with the same study_uid must "
            "leave exactly one row in the table")

    def test_image_count_is_summed(self, window):
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=100,
            download_duration_seconds=20.0,
        )
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=50,
            download_duration_seconds=10.0,
        )
        images_col = _column_index_for_header(window, "images")
        assert images_col >= 0
        assert window.completions_table.item(0, images_col).text() == "150"

    def test_completed_time_advances_to_latest(self, window):
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=100,
            download_duration_seconds=20.0,
        )
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=50,
            download_duration_seconds=10.0,
        )
        comp_col = _column_index_for_header(window, "completed")
        assert comp_col >= 0
        assert "08:20:00" in window.completions_table.item(0, comp_col).text()

    def test_download_duration_is_summed(self, window):
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=100,
            download_duration_seconds=20.0,
        )
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:30",
            institution_name="X", image_count=50,
            download_duration_seconds=10.0,
        )
        dur_col = _column_index_for_header(window, "duration")
        assert dur_col >= 0
        # 20 + 10 = 30 seconds total → "0:30"
        assert window.completions_table.item(0, dur_col).text() == "0:30"

    def test_different_study_uids_remain_separate_rows(self, window):
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=100,
        )
        window.add_completion(
            study_uid="S2", patient_name="B", study_description="MR",
            study_time="090000", completed_time="09:15:30",
            institution_name="Y", image_count=50,
        )
        assert window.completions_table.rowCount() == 2

    def test_aggregated_row_stays_at_original_position(self, window):
        """Aggregating must NOT move the row — the existing entry
        stays where it is, only its contents update."""
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=100,
        )
        window.add_completion(
            study_uid="S2", patient_name="B", study_description="MR",
            study_time="090000", completed_time="09:15:30",
            institution_name="Y", image_count=50,
        )
        # After two inserts at row 0: S2 at row 0, S1 at row 1.
        # An aggregation hit on S1 must keep S1 at row 1.
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=25,
        )

        assert window.completions_table.rowCount() == 2
        patient_col = _column_index_for_header(window, "patient")
        assert window.completions_table.item(0, patient_col).text() == "B"
        assert window.completions_table.item(1, patient_col).text() == "A"

    def test_copy_button_on_aggregated_row_uses_latest_time(
            self, window, qapp):
        """After aggregation, the per-row Copy button must copy the
        LATEST completed_time, not the original one."""
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=100,
            download_duration_seconds=20.0,
        )
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=50,
            download_duration_seconds=10.0,
        )
        QApplication.clipboard().clear()
        _find_copy_button(window, row=0).click()
        text = QApplication.clipboard().text()
        assert "08:20:00" in text
        assert "08:15:30" not in text


class TestMinImagesThreshold:
    """``add_completion`` honours an optional ``min_images_threshold``
    that suppresses tiny entries.  The threshold is applied against
    the CUMULATIVE image count for the row so a late second-wave
    emit can promote a previously-suppressed study into the table."""

    def test_first_emit_below_threshold_creates_no_row(self, window):
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=5,
            min_images_threshold=10,
        )
        assert window.completions_table.rowCount() == 0

    def test_second_wave_crosses_threshold_creates_row(self, window):
        """Wave 1 has 5 images (below threshold) → no row.  Wave 2
        adds 50 more → since the cumulative new+existing of 50 alone
        is over the threshold, the row is now created."""
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=5,
            min_images_threshold=10,
        )
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:25:30",
            institution_name="X", image_count=50,
            min_images_threshold=10,
        )
        assert window.completions_table.rowCount() == 1

    def test_threshold_aggregates_against_existing_row(self, window):
        """Once a row exists, threshold checks fold the new wave into
        the existing count — never blocks an update that takes the
        total even higher."""
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=50,
            min_images_threshold=10,
        )
        # Add a 3-image wave — sum is 53, comfortably above threshold.
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=3,
            min_images_threshold=10,
        )
        images_col = _column_index_for_header(window, "images")
        assert window.completions_table.item(0, images_col).text() == "53"


class TestSortableColumns:
    """Clicking a column header sorts the table by that column,
    toggling ascending → descending → ascending."""

    def test_sorting_is_enabled(self, window):
        assert window.completions_table.isSortingEnabled(), (
            "completions table must enable QTableWidget sorting so "
            "column headers are clickable")

    def test_header_sort_indicator_shown(self, window):
        header = window.completions_table.horizontalHeader()
        assert header.isSortIndicatorShown(), (
            "header must visibly show a sort-direction arrow")

    def test_sort_by_patient_ascending_reorders_rows(self, window):
        """Insert rows out of order, ask for ascending sort by Patient,
        rows must reorder lexicographically."""
        window.add_completion(
            patient_name="Charlie", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X",
        )
        window.add_completion(
            patient_name="Alpha", study_description="CT",
            study_time="080100", completed_time="08:16:30",
            institution_name="X",
        )
        window.add_completion(
            patient_name="Bravo", study_description="CT",
            study_time="080200", completed_time="08:17:30",
            institution_name="X",
        )
        patient_col = _column_index_for_header(window, "patient")
        assert patient_col >= 0, "no Patient column found"

        window.completions_table.sortByColumn(patient_col, Qt.AscendingOrder)

        order = [window.completions_table.item(r, patient_col).text()
                 for r in range(window.completions_table.rowCount())]
        assert order == ["Alpha", "Bravo", "Charlie"], (
            f"ascending sort expected [Alpha, Bravo, Charlie], "
            f"got {order}")

    def test_sort_toggles_descending_on_second_click(self, window):
        """Re-sorting the same column in descending order reverses
        the rows."""
        for name in ("Charlie", "Alpha", "Bravo"):
            window.add_completion(
                patient_name=name, study_description="CT",
                study_time="080000", completed_time="08:15:30",
                institution_name="X",
            )
        patient_col = _column_index_for_header(window, "patient")
        assert patient_col >= 0

        window.completions_table.sortByColumn(patient_col, Qt.AscendingOrder)
        window.completions_table.sortByColumn(patient_col, Qt.DescendingOrder)

        order = [window.completions_table.item(r, patient_col).text()
                 for r in range(window.completions_table.rowCount())]
        assert order == ["Charlie", "Bravo", "Alpha"], (
            f"descending sort expected [Charlie, Bravo, Alpha], "
            f"got {order}")


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


# ═══════════════════════════════════════════════════════════════════════════
# Live ETE countdown — remaining time + expected completion clock
# ═══════════════════════════════════════════════════════════════════════════

class TestTransferProgressCountdown:
    """The Download Completions window shows a live countdown:
    estimated remaining time for the current download queue and the
    expected wall-clock time when the download will finish.

    Data is pushed in via ``update_transfer_progress(pending_images,
    images_per_minute)``; a 1-second QTimer ticks the remaining time
    down between updates."""

    # ── UI element existence ──────────────────────────────────────────

    def test_has_remaining_time_label(self, window):
        assert hasattr(window, "lbl_remaining_time"), (
            "LiveCompletionsWindow needs a lbl_remaining_time QLabel")

    def test_has_expected_completion_label(self, window):
        assert hasattr(window, "lbl_expected_completion"), (
            "LiveCompletionsWindow needs a lbl_expected_completion QLabel")

    def test_initial_state_shows_dash(self, window):
        assert "—" in window.lbl_remaining_time.text()
        assert "—" in window.lbl_expected_completion.text()

    # ── Remaining time computation ────────────────────────────────────

    def test_remaining_time_five_minutes(self, window):
        """600 pending images at 120 img/min → 5:00 remaining."""
        window.update_transfer_progress(
            pending_images=600, images_per_minute=120.0)
        text = window.lbl_remaining_time.text()
        assert "5:00" in text

    def test_remaining_time_two_and_half_minutes(self, window):
        """300 pending images at 120 img/min → 2:30 remaining."""
        window.update_transfer_progress(
            pending_images=300, images_per_minute=120.0)
        assert "2:30" in window.lbl_remaining_time.text()

    def test_remaining_time_over_one_hour(self, window):
        """3600 images at 60 img/min → 60 min → 1:00:00."""
        window.update_transfer_progress(
            pending_images=3600, images_per_minute=60.0)
        assert "1:00:00" in window.lbl_remaining_time.text()

    # ── Expected completion time ──────────────────────────────────────

    def test_expected_completion_shows_clock_time(self, window):
        """Must show a HH:MM:SS wall-clock time, not just a duration."""
        from datetime import datetime, timedelta
        before = datetime.now()
        window.update_transfer_progress(
            pending_images=600, images_per_minute=120.0)
        # Expected ≈ now + 5 min
        expected = before + timedelta(minutes=5)
        text = window.lbl_expected_completion.text()
        assert f"{expected.hour:02d}:{expected.minute:02d}" in text

    # ── Edge cases ────────────────────────────────────────────────────

    def test_zero_rate_shows_dash(self, window):
        """Can't estimate without a transfer rate."""
        window.update_transfer_progress(
            pending_images=600, images_per_minute=0.0)
        assert "—" in window.lbl_remaining_time.text()
        assert "—" in window.lbl_expected_completion.text()

    def test_zero_pending_shows_dash(self, window):
        """No pending work → nothing to count down."""
        window.update_transfer_progress(
            pending_images=0, images_per_minute=120.0)
        assert "—" in window.lbl_remaining_time.text() or \
            "0:00" in window.lbl_remaining_time.text()

    def test_negative_pending_treated_as_zero(self, window):
        window.update_transfer_progress(
            pending_images=-10, images_per_minute=120.0)
        text = window.lbl_remaining_time.text()
        assert "—" in text or "0:00" in text

    # ── Update replaces previous estimate ─────────────────────────────

    def test_update_replaces_previous_estimate(self, window):
        window.update_transfer_progress(
            pending_images=600, images_per_minute=120.0)
        window.update_transfer_progress(
            pending_images=300, images_per_minute=120.0)
        text = window.lbl_remaining_time.text()
        assert "2:30" in text
        assert "5:00" not in text

    # ── Reset on clear ────────────────────────────────────────────────

    def test_clear_resets_countdown(self, window):
        """Pressing Clear must also reset the ETE display."""
        window.update_transfer_progress(
            pending_images=600, images_per_minute=120.0)
        window._clear()
        assert "—" in window.lbl_remaining_time.text()
        assert "—" in window.lbl_expected_completion.text()
