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

    def test_delay_wraps_across_midnight(self, window):
        """Acquired 23:55:00, completed 00:05:00 → the raw time-of-day
        diff is −85,800 s, but a completion can only come AFTER
        acquisition, so the delay must wrap forward by 24 h to the
        true 600 s (10:00).  Regression guard for the cross-midnight
        bug where abs() masked the sign and showed a bogus ~23 h
        delay while the negative raw value skewed the stats."""
        window.add_completion(
            patient_name="Night^Owl",
            study_description="CT",
            study_time="23:55:00",
            completed_time="00:05:00",
            institution_name="X",
        )
        delay_col = _find_delay_column(window)
        item = window.completions_table.item(0, delay_col)
        assert item is not None
        assert item.text() == "10:00"
        # The raw value stored for the median/σ statistics must be
        # the normalized, non-negative delay.
        assert item.data(Qt.UserRole) == 600


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
# Per-source statistics — colour bands and medians separated by source PACS
# ═══════════════════════════════════════════════════════════════════════════

class TestPerSourceStats:
    """Delay colour bands and the median readout must be computed per
    source PACS: a PACS that syncs hourly would otherwise paint every
    row of a real-time PACS green (and its own rows red)."""

    @staticmethod
    def _add(window, *, patient, delay_seconds, source):
        comp_h = 8 + delay_seconds // 3600
        comp_m = (delay_seconds % 3600) // 60
        comp_s = delay_seconds % 60
        window.add_completion(
            patient_name=patient, study_description="CT",
            study_time="080000",
            completed_time=f"{comp_h:02d}:{comp_m:02d}:{comp_s:02d}",
            institution_name="X", source=source,
        )

    @staticmethod
    def _delay_fg(window, patient):
        t = window.completions_table
        delay_col = _find_delay_column(window)
        for row in range(t.rowCount()):
            item = t.item(row, 1)  # Patient column
            if item is not None and item.text() == patient:
                return t.item(row, delay_col).foreground().color().name()
        raise ValueError(f"row for patient {patient!r} not found")

    def test_slow_source_not_red_against_fast_source(self, window):
        """A consistently slow PACS must not be flagged red just
        because another PACS is fast.  Old (global) stats: median=300s,
        the 1800s entries are way past +2σ → red.  Per-source stats:
        each group is internally uniform → no colouring at all."""
        for i in range(8):
            self._add(window, patient=f"F{i}", delay_seconds=300,
                      source="FastPACS")
        for i in range(2):
            self._add(window, patient=f"S{i}", delay_seconds=1800,
                      source="SlowPACS")
        fg = self._delay_fg(window, "S0")
        assert "#e74c3c" not in fg, (
            "SlowPACS rows must be judged against SlowPACS's own "
            "median, not the global one")

    def test_outlier_within_its_own_source_is_red(self, window):
        """Per-source banding still flags a genuine outlier inside one
        source: [300]×8 + [1800] → median=300, 2σ≈943 → 1800 is red."""
        for i in range(8):
            self._add(window, patient=f"P{i}", delay_seconds=300,
                      source="PACS-A")
        self._add(window, patient="OUT", delay_seconds=1800,
                  source="PACS-A")
        # A second source must not dilute PACS-A's statistics.
        self._add(window, patient="B0", delay_seconds=60, source="PACS-B")
        self._add(window, patient="B1", delay_seconds=60, source="PACS-B")
        assert "#e74c3c" in self._delay_fg(window, "OUT")

    def test_median_label_shows_one_value_per_source(self, window):
        """With two sources, the median readout lists each source with
        its own median instead of one mixed value."""
        for d in (180, 300, 420):       # median 5:00
            self._add(window, patient=f"A{d}", delay_seconds=d,
                      source="Alpha")
        for d in (660, 780, 900):       # median 13:00
            self._add(window, patient=f"B{d}", delay_seconds=d,
                      source="Beta")
        text = window.lbl_median_delay.text()
        assert "Alpha" in text and "Beta" in text
        assert "5:00" in text and "13:00" in text

    def test_median_label_plain_for_single_source(self, window):
        """One source only → keep the plain un-prefixed value."""
        for d in (180, 300, 420):
            self._add(window, patient=f"A{d}", delay_seconds=d,
                      source="Alpha")
        text = window.lbl_median_delay.text()
        assert text == "5:00"

    def test_same_study_from_second_source_gets_own_row(self, window):
        """The same StudyInstanceUID arriving via a second source PACS
        must NOT fold into the first source's row — otherwise the
        second source's numbers pollute the first's statistics."""
        for src in ("PACS1", "PACS2"):
            window.add_completion(
                study_uid="U1", patient_name="A", study_description="CT",
                study_time="080000", completed_time="08:05:00",
                institution_name="X", image_count=50,
                download_duration_seconds=30.0, source=src,
            )
        assert window.completions_table.rowCount() == 2

    def test_source_tag_survives_row_aggregation(self, window):
        """A repeat emit for the same study and source folds into the
        existing row — and the row keeps its source tag, so it stays
        in its source's statistics group."""
        from gui.live_completions import (
            _ROLE_SOURCE, _COL_DELAY, _COL_IMAGES)
        for img in (50, 20):
            window.add_completion(
                study_uid="U1", patient_name="A", study_description="CT",
                study_time="080000", completed_time="08:05:00",
                institution_name="X", image_count=img,
                download_duration_seconds=30.0, source="PACS1",
            )
        t = window.completions_table
        assert t.rowCount() == 1
        assert t.item(0, _COL_DELAY).data(_ROLE_SOURCE) == "PACS1"
        assert t.item(0, _COL_IMAGES).data(Qt.UserRole) == 70

    def test_duration_banding_separated_per_source(self, window):
        """The Download Duration column gets the same per-source
        treatment as Delay: a slow PACS's uniform durations must not
        be painted red against a fast PACS's."""
        from gui.live_completions import _COL_DURATION
        for i in range(8):
            window.add_completion(
                study_uid=f"F{i}", patient_name=f"F{i}",
                study_description="CT", study_time="080000",
                completed_time="08:05:00", institution_name="X",
                image_count=100, download_duration_seconds=300.0,
                source="FastPACS",
            )
        for i in range(2):
            window.add_completion(
                study_uid=f"S{i}", patient_name=f"S{i}",
                study_description="CT", study_time="080000",
                completed_time="08:05:00", institution_name="X",
                image_count=100, download_duration_seconds=1800.0,
                source="SlowPACS",
            )
        t = window.completions_table
        for row in range(t.rowCount()):
            if t.item(row, 1).text() == "S0":
                fg = t.item(row, _COL_DURATION) \
                    .foreground().color().name()
                assert "#e74c3c" not in fg, (
                    "SlowPACS durations must be judged against "
                    "SlowPACS's own median, not the global one")
                break
        else:
            pytest.fail("row S0 not found")


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


class TestCumulativeAggregation:
    """With ``cumulative=True`` a repeat ``study_uid`` emit carries the
    study's running totals, so they REPLACE the row's values instead of
    being summed.  This matches the transfer engine, which re-emits the
    full study (cumulative image count) on every new image arrival."""

    def test_cumulative_image_count_replaces_not_sums(self, window):
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=599,
            download_duration_seconds=120.0, cumulative=True,
        )
        # Re-emit: the engine now reports the cumulative total 605
        # (6 more images landed), NOT an increment of 6.
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=605,
            download_duration_seconds=130.0, cumulative=True,
        )
        images_col = _column_index_for_header(window, "images")
        assert window.completions_table.item(0, images_col).text() == "605"

    def test_cumulative_completed_time_advances(self, window):
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=599, cumulative=True,
        )
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=605, cumulative=True,
        )
        comp_col = _column_index_for_header(window, "completed")
        assert "08:20:00" in window.completions_table.item(0, comp_col).text()

    def test_cumulative_copy_button_uses_latest_time(self, window, qapp):
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=599, cumulative=True,
        )
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=605, cumulative=True,
        )
        QApplication.clipboard().clear()
        _find_copy_button(window, row=0).click()
        text = QApplication.clipboard().text()
        assert "08:20:00" in text
        assert "08:15:30" not in text

    def test_cumulative_does_not_shrink_on_smaller_reemit(self, window):
        """A re-emit with a smaller/absent count (e.g. a no-op re-query)
        must not shrink the row."""
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=605,
            download_duration_seconds=130.0, cumulative=True,
        )
        window.add_completion(
            study_uid="S1", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=3,
            download_duration_seconds=5.0, cumulative=True,
        )
        images_col = _column_index_for_header(window, "images")
        assert window.completions_table.item(0, images_col).text() == "605"


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


class TestTimeColumnsSortByDate:
    """The Time Acquired / Download Completed columns display only the
    time of day but must sort by the full date+time, so studies from
    different days end up in chronological order — not interleaved by
    time-of-day."""

    def test_acquired_sorts_chronologically_across_days(self, window):
        """Earlier day must sort before a later day even when its
        time-of-day is *later* (a naive text sort would invert them)."""
        # Yesterday 23:00 — earlier instant, but later clock time.
        window.add_completion(
            patient_name="Yesterday", study_description="CT",
            study_date="20260610", study_time="230000",
            completed_date="20260610", completed_time="23:10:00",
            institution_name="X",
        )
        # Today 08:00 — later instant, but earlier clock time.
        window.add_completion(
            patient_name="Today", study_description="CT",
            study_date="20260611", study_time="080000",
            completed_date="20260611", completed_time="08:10:00",
            institution_name="X",
        )
        acq_col = _column_index_for_header(window, "acquired")
        patient_col = _column_index_for_header(window, "patient")
        assert acq_col >= 0

        window.completions_table.sortByColumn(acq_col, Qt.AscendingOrder)

        order = [window.completions_table.item(r, patient_col).text()
                 for r in range(window.completions_table.rowCount())]
        assert order == ["Yesterday", "Today"], (
            "ascending acquired-time sort must order by date+time, "
            f"not by clock time alone; got {order}")

    def test_completed_sorts_chronologically_across_days(self, window):
        window.add_completion(
            patient_name="Yesterday", study_description="CT",
            study_date="20260610", study_time="100000",
            completed_date="20260610", completed_time="23:00:00",
            institution_name="X",
        )
        window.add_completion(
            patient_name="Today", study_description="CT",
            study_date="20260611", study_time="100000",
            completed_date="20260611", completed_time="07:00:00",
            institution_name="X",
        )
        comp_col = _column_index_for_header(window, "completed")
        patient_col = _column_index_for_header(window, "patient")
        assert comp_col >= 0

        window.completions_table.sortByColumn(comp_col, Qt.AscendingOrder)

        order = [window.completions_table.item(r, patient_col).text()
                 for r in range(window.completions_table.rowCount())]
        assert order == ["Yesterday", "Today"]

    def test_same_day_still_sorts_by_time(self, window):
        """Within one day the time of day still decides the order."""
        for name, t in (("Late", "14:00:00"), ("Early", "08:00:00")):
            window.add_completion(
                patient_name=name, study_description="CT",
                study_date="20260611", study_time=t.replace(":", ""),
                completed_date="20260611", completed_time=t,
                institution_name="X",
            )
        acq_col = _column_index_for_header(window, "acquired")
        patient_col = _column_index_for_header(window, "patient")

        window.completions_table.sortByColumn(acq_col, Qt.AscendingOrder)

        order = [window.completions_table.item(r, patient_col).text()
                 for r in range(window.completions_table.rowCount())]
        assert order == ["Early", "Late"]

    def test_aggregation_updates_completed_sort_key(self, window):
        """A repeat emit that advances the completion into a new day
        must move the row chronologically, not keep the stale key."""
        # Row created "yesterday".
        window.add_completion(
            study_uid="U1", patient_name="Rolling", study_description="CT",
            study_date="20260610", study_time="100000",
            completed_date="20260610", completed_time="23:50:00",
            institution_name="X", image_count=50,
            download_duration_seconds=10.0, source="P1",
        )
        # Second row, genuinely today.
        window.add_completion(
            study_uid="U2", patient_name="Fresh", study_description="CT",
            study_date="20260611", study_time="100000",
            completed_date="20260611", completed_time="08:00:00",
            institution_name="X", image_count=50,
            download_duration_seconds=10.0, source="P1",
        )
        # U1 gets a second wave that completes after midnight (today,
        # later than Fresh).
        window.add_completion(
            study_uid="U1", patient_name="Rolling", study_description="CT",
            study_date="20260610", study_time="100000",
            completed_date="20260611", completed_time="09:00:00",
            institution_name="X", image_count=50,
            download_duration_seconds=10.0, source="P1",
        )
        comp_col = _column_index_for_header(window, "completed")
        patient_col = _column_index_for_header(window, "patient")

        window.completions_table.sortByColumn(comp_col, Qt.AscendingOrder)

        order = [window.completions_table.item(r, patient_col).text()
                 for r in range(window.completions_table.rowCount())]
        assert order == ["Fresh", "Rolling"], (
            "Rolling completed at 09:00 today, after Fresh at 08:00; "
            "its sort key must follow the advanced completion time")


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
        """Clipboard format in German: 'Abschluss Bildeingang: HH:MM:SS'."""
        win = self._make_window(qapp, "de")
        try:
            assert self._add_row_and_click(win) == \
                "Abschluss Bildeingang: 08:15:30"
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

    def test_set_language_changes_existing_row(self, qapp):
        """A language switch after a row was added must change what its
        Copy button copies — the prefix resolves at click time."""
        win = self._make_window(qapp, "en")
        try:
            win.add_completion(
                patient_name="A", study_description="CT",
                study_time="080000", completed_time="08:15:30",
                institution_name="X",
            )
            win.set_language("de")
            QApplication.clipboard().clear()
            _find_copy_button(win, row=0).click()
            assert QApplication.clipboard().text().strip() == \
                "Abschluss Bildeingang: 08:15:30"
        finally:
            win.close()


# ═══════════════════════════════════════════════════════════════════════════
# Button LABELS are localized too — not just the copied clipboard text
# ═══════════════════════════════════════════════════════════════════════════

class TestButtonLabelLocalization:
    """The per-row "Copy" caption and the window's "Clear" caption go
    through core.i18n.tr, so a German user sees "Kopieren" / "Leeren"
    instead of English buttons next to German cell content."""

    def _add_row(self, win):
        win.add_completion(
            patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X",
        )

    def test_english_labels(self, window):
        self._add_row(window)
        assert _find_copy_button(window, row=0).text() == "Copy"
        assert window.btn_clear.text() == "Clear"

    def test_german_labels(self, qapp):
        win = LiveCompletionsWindow(language="de")
        try:
            self._add_row(win)
            assert _find_copy_button(win, row=0).text() == "Kopieren"
            assert win.btn_clear.text() == "Leeren"
        finally:
            win.close()

    def test_french_and_spanish_labels(self, qapp):
        for lang, copy_label, clear_label in (
                ("fr", "Copier", "Effacer"),
                ("es", "Copiar", "Borrar")):
            win = LiveCompletionsWindow(language=lang)
            try:
                self._add_row(win)
                assert _find_copy_button(win, row=0).text() == copy_label
                assert win.btn_clear.text() == clear_label
            finally:
                win.close()

    def test_unknown_language_falls_back_to_english_labels(self, qapp):
        win = LiveCompletionsWindow(language="xx")
        try:
            self._add_row(win)
            assert _find_copy_button(win, row=0).text() == "Copy"
            assert win.btn_clear.text() == "Clear"
        finally:
            win.close()

    def test_set_language_relabels_clear_button(self, window):
        window.set_language("de")
        assert window.btn_clear.text() == "Leeren"

    def test_set_language_relabels_existing_copy_buttons(self, window):
        """Labels are set at build time, so a language switch must
        re-label the rows that already exist — otherwise the window
        would sit half-translated until new completions arrive."""
        self._add_row(window)
        self._add_row(window)
        window.set_language("de")
        assert _find_copy_button(window, row=0).text() == "Kopieren"
        assert _find_copy_button(window, row=1).text() == "Kopieren"

    def test_rows_added_after_set_language_use_new_label(self, window):
        window.set_language("de")
        self._add_row(window)
        assert _find_copy_button(window, row=0).text() == "Kopieren"

    def test_set_language_on_empty_table_does_not_raise(self, window):
        window.set_language("es")
        assert window.btn_clear.text() == "Borrar"

    def test_aggregation_reinstalled_button_keeps_language(self, window):
        """_update_existing_row rebuilds the Copy button; it must come
        back in the current language, not the build-time one."""
        window.add_completion(
            study_uid="1.2.3", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:15:30",
            institution_name="X", image_count=10,
        )
        window.set_language("de")
        window.add_completion(
            study_uid="1.2.3", patient_name="A", study_description="CT",
            study_time="080000", completed_time="08:20:00",
            institution_name="X", image_count=5,
        )
        assert window.completions_table.rowCount() == 1
        assert _find_copy_button(window, row=0).text() == "Kopieren"


# ═══════════════════════════════════════════════════════════════════════════
# Time / delay computation extracted from add_completion
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeRowTiming:
    """add_completion delegates time parsing, sort-key building and the
    midnight-normalized delay to _compute_row_timing.  These lock the
    contract of that helper so the extraction can't silently drift."""

    def test_formats_and_sort_keys(self):
        from gui.live_completions import _compute_row_timing
        t = _compute_row_timing("080000", "081530", "20250101", "20250101")
        assert t.formatted_acq == "08:00:00"
        assert t.formatted_comp == "08:15:30"
        assert t.acq_sort == "20250101080000"
        assert t.comp_sort == "20250101081530"
        assert t.delay_seconds == 930

    def test_midnight_crossing_wraps_forward(self):
        from gui.live_completions import _compute_row_timing
        t = _compute_row_timing("235500", "000500", "", "")
        assert t.delay_seconds == 600

    def test_unparseable_times_pass_through_with_no_delay(self):
        from gui.live_completions import _compute_row_timing
        t = _compute_row_timing("n/a", "", "", "")
        assert t.formatted_acq == "n/a"
        assert t.formatted_comp == ""
        assert t.delay_seconds is None


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


# ═══════════════════════════════════════════════════════════════════════════
# Row cap — the window must not grow without bound
# ═══════════════════════════════════════════════════════════════════════════

class TestRowCap:
    """The completions window stays open for a whole shift on a 24/7
    service.  Without a cap the table grew forever AND every completion
    re-scanned all of it, so both memory and per-completion cost were
    unbounded."""

    @staticmethod
    def _add(window, n: int, *, start_hour: int = 0) -> None:
        for i in range(n):
            h, rest = divmod(i, 3600)
            m, s = divmod(rest, 60)
            window.add_completion(
                study_uid=f"S{i}",
                patient_name=f"P{i}",
                study_description="Study",
                study_time="080000",
                study_date="20260101",
                completed_time=f"{start_hour + h:02d}:{m:02d}:{s:02d}",
                completed_date="20260101",
                image_count=100,
                download_duration_seconds=10.0,
            )

    def test_row_count_is_capped(self, window):
        from gui.live_completions import MAX_COMPLETION_ROWS
        self._add(window, MAX_COMPLETION_ROWS + 25)
        assert window.completions_table.rowCount() == MAX_COMPLETION_ROWS

    def test_oldest_completions_are_dropped_first(self, window):
        from gui.live_completions import MAX_COMPLETION_ROWS, _COL_PATIENT
        self._add(window, MAX_COMPLETION_ROWS + 10)
        t = window.completions_table
        kept = {t.item(r, _COL_PATIENT).text()
                for r in range(t.rowCount())}
        # P0..P9 completed earliest (00:00:00 … 00:00:09) → evicted.
        assert not (kept & {f"P{i}" for i in range(10)})
        assert f"P{MAX_COMPLETION_ROWS + 9}" in kept

    def test_under_the_cap_nothing_is_dropped(self, window):
        self._add(window, 20)
        assert window.completions_table.rowCount() == 20
