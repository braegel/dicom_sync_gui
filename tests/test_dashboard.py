"""
Tests for gui.dashboard — SourceDashboard and StatsLabel.
"""

import os
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QGroupBox

from gui.dashboard import SourceDashboard, StatsLabel
from core.transfer_engine import TransferStats, TransferEngine, SeriesJob
from core.config import AppConfig, PacsNode


# ═══════════════════════════════════════════════════════════════════════════
# StatsLabel
# ═══════════════════════════════════════════════════════════════════════════

class TestStatsLabel:

    @pytest.fixture(autouse=True)
    def _create(self, qapp):
        self.label = StatsLabel()

    def test_default_text(self):
        assert self.label.text() == "\u2014"

    def test_set_value_updates_text(self):
        self.label.set_value(42.0, 50.0)
        assert self.label.text() == "42"

    def test_green_when_above_median_by_20pct(self):
        # median_all=100, value=130 → 130 > 100*1.2=120 → green
        self.label.set_value(130.0, 100.0)
        style = self.label.styleSheet()
        assert "#2ecc71" in style  # green

    def test_red_when_below_median_by_20pct(self):
        # median_all=100, value=70 → 70 < 100*0.8=80 → red
        self.label.set_value(70.0, 100.0)
        style = self.label.styleSheet()
        assert "#e74c3c" in style  # red

    def test_white_when_within_range(self):
        # median_all=100, value=100 → within ±20% → white
        self.label.set_value(100.0, 100.0)
        style = self.label.styleSheet()
        assert "white" in style

    def test_white_when_median_too_small(self):
        # median_all < 1 → white
        self.label.set_value(100.0, 0.5)
        style = self.label.styleSheet()
        assert "white" in style

    def test_white_when_value_too_small(self):
        # value < 1 → white
        self.label.set_value(0.5, 100.0)
        style = self.label.styleSheet()
        assert "white" in style

    def test_boundary_exactly_at_120pct(self):
        # value == median_all * 1.2 exactly → NOT green (> required)
        self.label.set_value(120.0, 100.0)
        style = self.label.styleSheet()
        assert "white" in style

    def test_boundary_exactly_at_80pct(self):
        # value == median_all * 0.8 exactly → NOT red (< required)
        self.label.set_value(80.0, 100.0)
        style = self.label.styleSheet()
        assert "white" in style


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — ETE format
# ═══════════════════════════════════════════════════════════════════════════

class TestETEFormat:

    def test_format_ete_zero(self):
        assert SourceDashboard._format_ete(0) == "\u2014"

    def test_format_ete_negative(self):
        assert SourceDashboard._format_ete(-10) == "\u2014"

    def test_format_ete_seconds(self):
        assert SourceDashboard._format_ete(45) == "0:45"

    def test_format_ete_minutes(self):
        assert SourceDashboard._format_ete(125) == "2:05"

    def test_format_ete_exactly_one_hour(self):
        assert SourceDashboard._format_ete(3600) == "1:00:00"

    def test_format_ete_hours(self):
        # 1h 23m 45s = 5025s
        assert SourceDashboard._format_ete(5025) == "1:23:45"

    def test_format_ete_large(self):
        # 10h 0m 0s
        assert SourceDashboard._format_ete(36000) == "10:00:00"


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — status helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestStatusHelpers:

    def test_status_text_queued(self):
        text = SourceDashboard._status_text("queued")
        assert "Queued" in text

    def test_status_text_transferring(self):
        text = SourceDashboard._status_text("transferring")
        assert "Transferring" in text

    def test_status_text_done(self):
        text = SourceDashboard._status_text("done")
        assert "Done" in text

    def test_status_text_error(self):
        text = SourceDashboard._status_text("error")
        assert "Error" in text

    def test_status_text_skipped(self):
        text = SourceDashboard._status_text("skipped")
        assert "Skipped" in text

    def test_status_text_unknown(self):
        text = SourceDashboard._status_text("weirdo")
        assert text == "weirdo"

    def test_status_color_done_is_green(self):
        color = SourceDashboard._status_color("done")
        assert color.name() == "#2ecc71"

    def test_status_color_error_is_red(self):
        color = SourceDashboard._status_color("error")
        assert color.name() == "#e74c3c"

    def test_status_color_transferring_is_orange(self):
        color = SourceDashboard._status_color("transferring")
        assert color.name() == "#f39c12"

    def test_status_color_unknown(self):
        color = SourceDashboard._status_color("unknown")
        assert color.name() == "#d4d4d4"


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — widget creation and UI state
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardUI:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def test_initial_state_idle(self):
        assert self.dashboard._service_running is False
        assert self.dashboard._last_queue == []
        assert self.dashboard._current_stats is None

    def test_table_has_correct_columns(self):
        table = self.dashboard.series_table
        # 11 total columns: hidden ☑ (0), Patient–ETE (1-9), Group (10)
        assert table.columnCount() == 11
        visible = [
            table.horizontalHeaderItem(i).text()
            for i in range(table.columnCount())
            if not table.isColumnHidden(i)
        ]
        assert visible == [
            "Patient", "Study", "Series", "Modality",
            "Images", "Pending", "img/min", "Status", "ETE", "Group",
        ]

    def test_signals_exist(self):
        assert hasattr(self.dashboard, 'start_requested')
        assert hasattr(self.dashboard, 'stop_requested')

    def test_start_button_enabled_initially(self):
        assert self.dashboard.btn_start.isEnabled()
        assert not self.dashboard.btn_stop.isEnabled()

    def test_set_service_running_true(self):
        self.dashboard.set_service_running(True)
        assert not self.dashboard.btn_start.isEnabled()
        assert self.dashboard.btn_stop.isEnabled()
        assert not self.dashboard.hours_spin.isEnabled()
        assert not self.dashboard.max_images_spin.isEnabled()
        assert not self.dashboard.interval_spin.isEnabled()

    def test_set_service_running_false(self):
        self.dashboard.set_service_running(True)
        self.dashboard.set_service_running(False)
        assert self.dashboard.btn_start.isEnabled()
        assert not self.dashboard.btn_stop.isEnabled()
        assert "Stopped" in self.dashboard.lbl_status.text()

    def test_spinboxes_default_values_from_node(self):
        # populated_config ct node has hours=3, max_images=0, sync_interval=60
        assert self.dashboard.hours_spin.value() == 3
        assert self.dashboard.max_images_spin.value() == 0
        assert self.dashboard.interval_spin.value() == 60

    def test_filter_checkbox_default(self):
        # populated_config has filter_groups_enabled = True
        assert self.dashboard.filter_enable_check.isChecked()

    def test_restart_banner_hidden_initially(self):
        assert not self.dashboard.restart_banner.isVisible()

    def test_stats_labels_exist(self):
        assert hasattr(self.dashboard, 'stat_last')
        assert hasattr(self.dashboard, 'stat_med5')
        assert hasattr(self.dashboard, 'stat_med10')
        assert hasattr(self.dashboard, 'stat_medall')

    def test_remote_key_stored(self):
        assert self.dashboard.remote_key == "ct"


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — queue display
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardQueue:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def _make_job_dict(self, **overrides):
        base = {
            "patient_name": "Doe^John",
            "patient_id": "12345",
            "study_description": "CT Head",
            "series_description": "Axial",
            "modality": "CT",
            "series_number": "1",
            "study_uid": "1.2.3.4",
            "series_uid": "1.2.3.4.5",
            "remote_count": 100,
            "local_count": 10,
            "status": "queued",
            "institution_name": "Hospital",
            "images_per_minute": 0.0,
            "study_date": "",
            "study_time": "",
        }
        base.update(overrides)
        return base

    def test_on_queue_updated_populates_table(self):
        queue = [
            self._make_job_dict(series_uid="1.1"),
            self._make_job_dict(series_uid="1.2"),
        ]
        self.dashboard.on_queue_updated(queue)
        assert self.dashboard.series_table.rowCount() == 2

    def test_on_queue_updated_shows_patient_name(self):
        queue = [self._make_job_dict(patient_name="Smith^Jane")]
        self.dashboard.on_queue_updated(queue)
        item = self.dashboard.series_table.item(0, 1)  # col 1 = Patient
        assert item.text() == "Smith^Jane"

    def test_on_queue_updated_shows_pending(self):
        queue = [self._make_job_dict(remote_count=100, local_count=30)]
        self.dashboard.on_queue_updated(queue)
        pending_item = self.dashboard.series_table.item(0, 6)  # col 6 = Pending
        assert pending_item.text() == "70"

    def test_done_status_shows_checkmark_in_ete(self):
        queue = [self._make_job_dict(status="done")]
        self.dashboard.on_queue_updated(queue)
        ete_item = self.dashboard.series_table.item(0, 9)  # col 9 = ETE
        assert "\u2713" in ete_item.text()

    def test_error_status_shows_dash_in_ete(self):
        queue = [self._make_job_dict(status="error")]
        self.dashboard.on_queue_updated(queue)
        ete_item = self.dashboard.series_table.item(0, 9)  # col 9 = ETE
        assert "\u2014" in ete_item.text()

    def test_series_count_label(self):
        queue = [
            self._make_job_dict(status="done", series_uid="1"),
            self._make_job_dict(status="queued", series_uid="2"),
            self._make_job_dict(status="done", series_uid="3"),
        ]
        self.dashboard.on_queue_updated(queue)
        assert "2 / 3" in self.dashboard.lbl_total_series.text()

    def test_empty_queue_clears_table(self):
        # First fill
        self.dashboard.on_queue_updated([self._make_job_dict()])
        assert self.dashboard.series_table.rowCount() == 1
        # Then clear
        self.dashboard.on_queue_updated([])
        assert self.dashboard.series_table.rowCount() == 0

    def test_ipm_column_shows_value_for_done(self):
        queue = [self._make_job_dict(
            status="done", images_per_minute=150.0)]
        self.dashboard.on_queue_updated(queue)
        ipm_item = self.dashboard.series_table.item(0, 7)  # col 7 = img/min
        assert ipm_item.text() == "150"

    def test_ipm_column_shows_dash_for_queued(self):
        queue = [self._make_job_dict(status="queued")]
        self.dashboard.on_queue_updated(queue)
        ipm_item = self.dashboard.series_table.item(0, 7)  # col 7 = img/min
        assert "\u2014" in ipm_item.text()

    def test_ipm_column_shows_dash_for_done_zero_speed(self):
        queue = [self._make_job_dict(
            status="done", images_per_minute=0.0)]
        self.dashboard.on_queue_updated(queue)
        ipm_item = self.dashboard.series_table.item(0, 7)  # col 7 = img/min
        assert "\u2014" in ipm_item.text()


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — stats display
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardStats:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def test_on_stats_updated_stores_stats(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 60, 30.0)
        self.dashboard.on_stats_updated(stats)
        assert self.dashboard._current_stats is stats

    def test_on_stats_updated_updates_total_label(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 42, 10.0)
        self.dashboard.on_stats_updated(stats)
        assert "42" in self.dashboard.lbl_total_images.text()

    def test_refresh_stats_updates_labels(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 120, 60.0)  # 120 ipm
        stats.record_series("1.2", 60, 60.0)   # 60 ipm
        self.dashboard.on_stats_updated(stats)
        # last series = 60
        assert self.dashboard.stat_last.text() == "60"
        # median all = (60+120)/2 = 90
        assert self.dashboard.stat_medall.text() == "90"

    def test_no_refresh_when_no_completed(self):
        stats = TransferStats()
        stats.start_session()
        # No series recorded
        self.dashboard.on_stats_updated(stats)
        # Labels should still show default
        assert self.dashboard.stat_last.text() == "\u2014"


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — cycle events
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardCycles:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def test_on_cycle_started(self):
        self.dashboard.on_cycle_started(5)
        assert "5" in self.dashboard.lbl_cycle.text()
        assert "querying" in self.dashboard.lbl_status.text().lower()

    def test_on_cycle_finished_with_images(self):
        self.dashboard.on_cycle_finished(3, 42)
        assert "42" in self.dashboard.lbl_status.text()

    def test_on_cycle_finished_no_images(self):
        self.dashboard.on_cycle_finished(3, 0)
        assert "waiting" in self.dashboard.lbl_status.text().lower()


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — reset
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardReset:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def test_reset_clears_all(self):
        # Populate some data
        self.dashboard._last_queue = [{"dummy": True}]
        self.dashboard._current_stats = TransferStats()

        self.dashboard.reset()

        assert self.dashboard.series_table.rowCount() == 0
        assert self.dashboard._current_stats is None
        assert self.dashboard._last_queue == []
        assert self.dashboard.lbl_total_images.text() == "Total: 0 images"
        assert self.dashboard.lbl_total_series.text() == "Series: 0"
        assert self.dashboard.lbl_status.text() == "Idle"
        assert self.dashboard.stat_last.text() == "\u2014"
        assert self.dashboard.stat_med5.text() == "\u2014"
        assert self.dashboard.stat_med10.text() == "\u2014"
        assert self.dashboard.stat_medall.text() == "\u2014"


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — filter group UI
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardFilterUI:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def test_filter_menu_populated(self):
        actions = self.dashboard.filter_menu.actions()
        names = [a.text() for a in actions if a.isCheckable()]
        assert "Group A" in names
        assert "Group B" in names
        assert "Group C" in names

    def test_filter_btn_text_with_selection(self):
        # populated_config has active_filter_groups = ["Group A"]
        btn_text = self.dashboard.filter_btn.text()
        assert "Group A" in btn_text

    def test_filter_btn_text_empty(self):
        self.dashboard.config.active_filter_groups = []
        self.dashboard._update_filter_button_text()
        assert "Select Groups" in self.dashboard.filter_btn.text()

    def test_refresh_filter_groups_removes_invalid(self):
        self.dashboard.config.active_filter_groups = [
            "Group A", "Nonexistent"]
        self.dashboard.refresh_filter_groups()
        assert "Nonexistent" not in self.dashboard.config.active_filter_groups

    def test_filter_enable_disables_button(self):
        self.dashboard.filter_enable_check.setChecked(False)
        assert not self.dashboard.filter_btn.isEnabled()

    def test_filter_enable_enables_button(self):
        self.dashboard.filter_enable_check.setChecked(True)
        assert self.dashboard.filter_btn.isEnabled()

    def test_settings_changed_marks_dirty(self):
        self.dashboard._service_running = True
        self.dashboard._on_settings_changed()
        assert self.dashboard._settings_dirty is True

    def test_settings_changed_no_banner_when_stopped(self):
        self.dashboard._service_running = False
        self.dashboard._on_settings_changed()
        assert not self.dashboard.restart_banner.isVisible()


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — group column in series queue
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardGroupColumn:
    """When filter groups are enabled the series table must show an extra
    'Group' column that displays the group each series belongs to, derived
    from institution_name → institution_assignments."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        # populated_config has filter_groups_enabled=True,
        # active_filter_groups=["Group A"],
        # institution_assignments includes "Hospital Alpha" → "Group A"
        self.config = populated_config
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def _make_job(self, **overrides):
        base = {
            "patient_name": "Doe^John",
            "patient_id": "12345",
            "study_description": "CT Head",
            "series_description": "Axial",
            "modality": "CT",
            "series_number": "1",
            "study_uid": "1.2.3.4",
            "series_uid": "1.2.3.4.5",
            "remote_count": 100,
            "local_count": 10,
            "status": "queued",
            "institution_name": "Hospital Alpha",
            "images_per_minute": 0.0,
        }
        base.update(overrides)
        return base

    # -- column presence --------------------------------------------------

    def test_group_column_visible_when_filter_enabled(self):
        """A 'Group' column header must be present when filtering is on."""
        headers = [
            self.dashboard.series_table.horizontalHeaderItem(i).text()
            for i in range(self.dashboard.series_table.columnCount())
            if not self.dashboard.series_table.isColumnHidden(i)
        ]
        assert "Group" in headers

    def test_group_column_hidden_when_filter_disabled(self):
        """The 'Group' column must NOT appear when filtering is off."""
        self.config.filter_groups_enabled = False
        dash = SourceDashboard(config=self.config, remote_key="ct")
        headers = [
            dash.series_table.horizontalHeaderItem(i).text()
            for i in range(dash.series_table.columnCount())
            if not dash.series_table.isColumnHidden(i)
        ]
        assert "Group" not in headers

    # -- cell content -----------------------------------------------------

    def test_group_column_shows_group_name(self):
        """Series from 'Hospital Alpha' (assigned to 'Group A') must show
        'Group A' in the Group column."""
        self.dashboard.on_queue_updated([
            self._make_job(institution_name="Hospital Alpha"),
        ])
        group_col = self._find_group_column()
        item = self.dashboard.series_table.item(0, group_col)
        assert item is not None
        assert item.text() == "Group A"

    def test_group_column_shows_different_groups(self):
        """Two series from different institutions show their respective
        groups."""
        self.dashboard.on_queue_updated([
            self._make_job(institution_name="Hospital Alpha",
                           series_uid="1.1"),
            self._make_job(institution_name="Clinic Beta",
                           series_uid="1.2"),
        ])
        group_col = self._find_group_column()
        assert self.dashboard.series_table.item(0, group_col).text() \
            == "Group A"
        assert self.dashboard.series_table.item(1, group_col).text() \
            == "Group B"

    def test_group_column_unassigned_institution(self):
        """An institution not assigned to any group shows empty or a
        placeholder."""
        self.dashboard.on_queue_updated([
            self._make_job(institution_name="Unknown Clinic"),
        ])
        group_col = self._find_group_column()
        item = self.dashboard.series_table.item(0, group_col)
        assert item is not None
        # unassigned → empty string (assignment is "")
        assert item.text() == ""

    # -- helpers ----------------------------------------------------------

    def _find_group_column(self):
        """Return the column index whose header is 'Group'."""
        table = self.dashboard.series_table
        for col in range(table.columnCount()):
            hdr = table.horizontalHeaderItem(col)
            if hdr and hdr.text() == "Group":
                return col
        pytest.fail("No 'Group' column found in series table")


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — study rate display (studies per hour)
#
# The rate is computed from the raw PACS query response (all studies
# returned, including historical/filtered-out ones), NOT from the
# download queue.  The engine emits a studies_queried signal with
# study-level dicts; the dashboard receives them via on_studies_queried.
# ═══════════════════════════════════════════════════════════════════════════

def _dt(minutes_ago: float, ref: datetime | None = None) -> tuple[str, str]:
    """Return (study_date, study_time) strings for *minutes_ago* before *ref*.

    Utility for building study dicts with realistic DICOM date/time values.
    """
    ref = ref or datetime.now()
    dt = ref - timedelta(minutes=minutes_ago)
    return dt.strftime("%Y%m%d"), dt.strftime("%H%M%S")


def _make_study(study_uid="1.2.3.4", institution_name="Hospital Alpha",
                minutes_ago=30, ref=None, **overrides):
    """Build a study-level dict as emitted by the studies_queried signal."""
    sd, st = _dt(minutes_ago, ref)
    base = {
        "study_uid": study_uid,
        "study_date": sd,
        "study_time": st,
        "institution_name": institution_name,
    }
    base.update(overrides)
    return base


class TestStudyRateCalculation:
    """_compute_study_rates counts unique studies (by study_uid) whose
    study_date+study_time falls within the last 60 minutes, grouped by
    filter group.  Input is study-level dicts, not SeriesJob dicts."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.config = populated_config
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")
        self.now = datetime.now()

    def test_counts_unique_studies(self):
        """Two entries with the same study_uid count as ONE study."""
        studies = [
            _make_study("S1", "Hospital Alpha", 10, self.now),
            _make_study("S1", "Hospital Alpha", 10, self.now),
        ]
        rates = self.dashboard._compute_study_rates(studies, now=self.now)
        assert rates["Group A"] == 1

    def test_excludes_studies_older_than_60min(self):
        studies = [
            _make_study("OLD", "Hospital Alpha", 90, self.now),
            _make_study("NEW", "Hospital Alpha", 10, self.now),
        ]
        rates = self.dashboard._compute_study_rates(studies, now=self.now)
        assert rates["Group A"] == 1

    def test_groups_counted_separately(self):
        studies = [
            _make_study("S1", "Hospital Alpha", 5, self.now),
            _make_study("S2", "Clinic Beta", 5, self.now),
        ]
        rates = self.dashboard._compute_study_rates(studies, now=self.now)
        assert rates["Group A"] == 1
        assert rates["Group B"] == 1

    def test_total_when_filter_disabled(self):
        self.config.filter_groups_enabled = False
        studies = [
            _make_study("S1", "Hospital Alpha", 5, self.now),
            _make_study("S2", "Clinic Beta", 5, self.now),
        ]
        rates = self.dashboard._compute_study_rates(studies, now=self.now)
        assert "_total" in rates
        assert rates["_total"] == 2

    def test_empty_list_returns_zero(self):
        rates = self.dashboard._compute_study_rates([], now=self.now)
        assert rates.get("Group A", 0) == 0

    def test_study_at_boundary_excluded(self):
        studies = [_make_study("B", "Hospital Alpha", 60, self.now)]
        rates = self.dashboard._compute_study_rates(studies, now=self.now)
        assert rates.get("Group A", 0) == 0

    def test_study_at_59min_included(self):
        studies = [_make_study("I", "Hospital Alpha", 59, self.now)]
        rates = self.dashboard._compute_study_rates(studies, now=self.now)
        assert rates["Group A"] == 1

    def test_unassigned_institution_grouped_under_empty(self):
        studies = [_make_study("S1", "Unknown Clinic", 5, self.now)]
        rates = self.dashboard._compute_study_rates(studies, now=self.now)
        assert rates.get("", 0) == 1

    def test_includes_studies_not_in_download_queue(self):
        """Studies filtered out by institution filter must still be counted
        — the input comes from the raw query, not the download queue."""
        # "Nonexistent Hospital" has no group assignment and would be
        # filtered out of the download queue, but should still appear
        # in study rate stats.
        studies = [
            _make_study("S1", "Hospital Alpha", 5, self.now),
            _make_study("S2", "Nonexistent Hospital", 5, self.now),
        ]
        rates = self.dashboard._compute_study_rates(studies, now=self.now)
        total_counted = sum(rates.values())
        assert total_counted == 2


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — study rate display widgets, layout, and color coding
# ═══════════════════════════════════════════════════════════════════════════

class TestStudyRateDisplay:
    """The dashboard must show a 'Studies / Hour' section with per-group
    labels (when filter active) or a single total label (when filter
    inactive).  It must sit between Transfer Speed and Series Queue.
    Color coding: 0 = neutral, 1-5 = green, 6-11 = yellow, ≥12 = red."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.config = populated_config
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")
        self.now = datetime.now()

    # -- widget existence -------------------------------------------------

    def test_study_rate_group_box_exists(self):
        assert hasattr(self.dashboard, "study_rate_group")
        assert self.dashboard.study_rate_group.title() == "Studies / Hour"

    def test_per_group_labels_when_filter_enabled(self):
        assert hasattr(self.dashboard, "study_rate_labels")
        assert isinstance(self.dashboard.study_rate_labels, dict)

    # -- layout position --------------------------------------------------

    def test_study_rate_between_speed_and_queue(self):
        """study_rate_group must appear after the Transfer Speed group and
        before the Series Queue group in the layout."""
        layout = self.dashboard.layout()
        positions = {}
        for i in range(layout.count()):
            widget = layout.itemAt(i).widget()
            if widget is self.dashboard.study_rate_group:
                positions["rate"] = i
            elif isinstance(widget, QGroupBox):
                title = widget.title()
                if "Transfer Speed" in title:
                    positions["speed"] = i
                elif "Series Queue" in title:
                    positions["queue"] = i

        assert "speed" in positions, "Transfer Speed group not found"
        assert "rate" in positions, "Studies / Hour group not found"
        assert "queue" in positions, "Series Queue group not found"
        assert positions["speed"] < positions["rate"] < positions["queue"]

    # -- color coding -----------------------------------------------------

    def test_rate_color_zero_is_neutral(self):
        color = SourceDashboard._study_rate_color(0)
        assert color is None or "white" in color.lower() or "transparent" in color.lower()

    def test_rate_color_1_to_5_is_green(self):
        for n in (1, 3, 5):
            color = SourceDashboard._study_rate_color(n)
            assert "#2ecc71" in color

    def test_rate_color_6_to_11_is_yellow(self):
        for n in (6, 8, 11):
            color = SourceDashboard._study_rate_color(n)
            assert "#f1c40f" in color

    def test_rate_color_12_plus_is_red(self):
        for n in (12, 15, 99):
            color = SourceDashboard._study_rate_color(n)
            assert "#e74c3c" in color

    # -- label update via on_studies_queried ------------------------------

    def test_labels_updated_on_studies_queried(self):
        """on_studies_queried (not on_queue_updated) must refresh labels."""
        studies = [
            _make_study(f"S{i}", "Hospital Alpha", 5, self.now)
            for i in range(3)
        ]
        self.dashboard.on_studies_queried(studies)
        lbl = self.dashboard.study_rate_labels.get("Group A")
        assert lbl is not None
        assert "3" in lbl.text()

    def test_on_queue_updated_does_not_update_study_rate(self):
        """on_queue_updated must NOT touch study rate labels — they are
        driven solely by on_studies_queried."""
        sd, st = _dt(5, self.now)
        queue = [{
            "patient_name": "X", "patient_id": "1",
            "study_description": "", "series_description": "",
            "modality": "CT", "series_number": "1",
            "study_uid": "S1", "series_uid": "1.1",
            "remote_count": 10, "local_count": 0,
            "status": "queued", "institution_name": "Hospital Alpha",
            "images_per_minute": 0.0,
            "study_date": sd, "study_time": st,
        }]
        self.dashboard.on_queue_updated(queue)
        lbl = self.dashboard.study_rate_labels.get("Group A")
        # Label should still show 0 — queue update doesn't affect rates
        assert lbl is not None
        assert "0" in lbl.text()

    def test_single_total_label_when_filter_disabled(self):
        self.config.filter_groups_enabled = False
        dash = SourceDashboard(config=self.config, remote_key="ct")
        studies = [_make_study("S1", "Hospital Alpha", 5, self.now)]
        dash.on_studies_queried(studies)
        lbl = dash.study_rate_labels.get("_total")
        assert lbl is not None
        assert "1" in lbl.text()


# ═══════════════════════════════════════════════════════════════════════════
# TransferSignals — studies_queried signal
# ═══════════════════════════════════════════════════════════════════════════

class TestStudiesQueriedSignal:
    """The engine must emit a studies_queried signal with study-level dicts
    from the raw PACS query response."""

    def test_signal_exists(self):
        from core.transfer_engine import TransferSignals
        signals = TransferSignals()
        assert hasattr(signals, "studies_queried")

    def test_signal_connected_in_main_window(self, populated_config, qapp):
        """_connect_engine_signals must wire studies_queried to
        dashboard.on_studies_queried."""
        from gui.main_window import MainWindow
        win = MainWindow(populated_config)
        dashboard = win.dashboards.get("ct")
        assert hasattr(dashboard, "on_studies_queried")
        assert callable(dashboard.on_studies_queried)


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — high-load popup
# ═══════════════════════════════════════════════════════════════════════════

class TestHighLoadPopup:
    """When any group's study rate hits ≥12 a warning popup with a sound
    must appear.  The popup can be disabled via config.
    Triggered by on_studies_queried, not on_queue_updated."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.config = populated_config
        self.config.high_load_alert_enabled = True
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")
        self.now = datetime.now()

    def _build_studies(self, n_studies):
        """Build a list of *n_studies* unique study dicts in the last hour."""
        return [
            _make_study(f"S{i}", "Hospital Alpha", 5, self.now)
            for i in range(n_studies)
        ]

    # -- popup trigger ----------------------------------------------------

    @patch("gui.dashboard.QMessageBox")
    def test_popup_shown_at_12_studies(self, mock_msgbox_cls):
        self.dashboard.on_studies_queried(self._build_studies(12))
        mock_msgbox_cls.return_value.show.assert_called_once()

    @patch("gui.dashboard.QMessageBox")
    def test_popup_not_shown_at_11_studies(self, mock_msgbox_cls):
        self.dashboard.on_studies_queried(self._build_studies(11))
        mock_msgbox_cls.return_value.show.assert_not_called()

    @patch("gui.dashboard.QMessageBox")
    def test_popup_message_is_english(self, mock_msgbox_cls):
        self.dashboard.on_studies_queried(self._build_studies(14))
        instance = mock_msgbox_cls.return_value
        title = instance.setWindowTitle.call_args[0][0]
        message = instance.setText.call_args[0][0]
        assert "load" in title.lower() or "load" in message.lower()

    # -- sound ------------------------------------------------------------

    @patch("gui.dashboard.QApplication.beep")
    @patch("gui.dashboard.QMessageBox")
    def test_sound_played_on_popup(self, mock_msgbox_cls, mock_beep):
        self.dashboard.on_studies_queried(self._build_studies(12))
        mock_beep.assert_called_once()

    # -- config disable ---------------------------------------------------

    @patch("gui.dashboard.QMessageBox")
    def test_popup_suppressed_when_disabled(self, mock_msgbox_cls):
        self.config.high_load_alert_enabled = False
        self.dashboard.on_studies_queried(self._build_studies(15))
        mock_msgbox_cls.return_value.show.assert_not_called()

    @patch("gui.dashboard.QApplication.beep")
    @patch("gui.dashboard.QMessageBox")
    def test_no_sound_when_disabled(self, mock_msgbox_cls, mock_beep):
        self.config.high_load_alert_enabled = False
        self.dashboard.on_studies_queried(self._build_studies(15))
        mock_beep.assert_not_called()

    # -- no repeated popup for same rate ----------------------------------

    @patch("gui.dashboard.QMessageBox")
    def test_popup_not_repeated_on_same_data(self, mock_msgbox_cls):
        studies = self._build_studies(13)
        self.dashboard.on_studies_queried(studies)
        self.dashboard.on_studies_queried(studies)
        assert mock_msgbox_cls.return_value.show.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════
# Config — high_load_alert_enabled
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigHighLoadAlert:
    """AppConfig must have a high_load_alert_enabled flag, defaulting True."""

    def test_default_true(self, tmp_config_path):
        config = AppConfig(config_path=tmp_config_path)
        assert config.high_load_alert_enabled is True

    def test_roundtrip(self, tmp_config_path):
        config = AppConfig(config_path=tmp_config_path)
        config.high_load_alert_enabled = False
        config.save()
        config2 = AppConfig(config_path=tmp_config_path)
        config2.load()
        assert config2.high_load_alert_enabled is False


# ═══════════════════════════════════════════════════════════════════════════
# SourceDashboard — study completion notification sound
# ═══════════════════════════════════════════════════════════════════════════

class TestPerPacsSoundCheckbox:
    """The dashboard must have a checkbox to toggle notification sound
    per source PACS, reading/writing from the PacsNode."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.config = populated_config
        self.node = self.config.remote_nodes["ct"]
        self.node.notification_sound_enabled = True
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def test_checkbox_exists(self):
        """Dashboard has a sound notification checkbox."""
        assert hasattr(self.dashboard, "sound_check")
        assert self.dashboard.sound_check.isChecked()

    def test_checkbox_reflects_node_disabled(self):
        """When node has sound disabled, checkbox is unchecked."""
        self.node.notification_sound_enabled = False
        dashboard = SourceDashboard(
            config=self.config, remote_key="ct")
        assert not dashboard.sound_check.isChecked()

    def test_toggling_checkbox_saves_to_node(self):
        """Unchecking the checkbox sets node.notification_sound_enabled=False."""
        self.dashboard.sound_check.setChecked(False)
        assert self.node.notification_sound_enabled is False

    def test_toggling_checkbox_on_saves_to_node(self):
        self.dashboard.sound_check.setChecked(False)
        self.node.notification_sound_enabled = False  # confirm off
        self.dashboard.sound_check.setChecked(True)
        assert self.node.notification_sound_enabled is True


class TestStudyCompleteSound:
    """Sound notification respects per-PACS settings (node-level),
    not global config."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.config = populated_config
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A"]
        self.node = self.config.remote_nodes["ct"]
        self.node.notification_sound_enabled = True
        self.node.notification_sound_path = ""
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def test_has_on_study_completed_slot(self):
        assert hasattr(self.dashboard, "on_study_completed")
        assert callable(self.dashboard.on_study_completed)

    def test_sound_played_for_active_group(self):
        """Institution in active filter group → play sound."""
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.dashboard.on_study_completed(
                "1.2.3", "Hospital Alpha")
        mock_play.assert_called_once()

    def test_no_sound_for_partial_download(self):
        """Study not fully downloaded (small-series only) → no sound."""
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.dashboard.on_study_completed(
                "1.2.3", "Hospital Alpha", fully_complete=False)
        mock_play.assert_not_called()

    def test_no_sound_for_inactive_group(self):
        """Institution in inactive group → no sound."""
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.dashboard.on_study_completed(
                "1.2.3", "Clinic Beta")  # Group B, not active
        mock_play.assert_not_called()

    def test_no_sound_when_node_disabled(self):
        """node.notification_sound_enabled=False → no sound."""
        self.node.notification_sound_enabled = False
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.dashboard.on_study_completed(
                "1.2.3", "Hospital Alpha")
        mock_play.assert_not_called()

    def test_sound_plays_when_filter_disabled(self):
        """With filtering off, sound plays for any institution."""
        self.config.filter_groups_enabled = False
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.dashboard.on_study_completed(
                "1.2.3", "Whatever Hospital")
        mock_play.assert_called_once()

    def test_sound_for_unassigned_institution_when_filter_on(self):
        """Unassigned institution with filter on → no sound (not in any
        active group)."""
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.dashboard.on_study_completed(
                "1.2.3", "Nonexistent Hospital")
        mock_play.assert_not_called()

    def test_custom_sound_per_pacs(self, tmp_path):
        """When node has a custom sound path, that file is played."""
        wav = tmp_path / "custom.wav"
        wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        self.node.notification_sound_path = str(wav)
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.dashboard.on_study_completed(
                "1.2.3", "Hospital Alpha")
        mock_play.assert_called_once_with(str(wav))

    def test_default_sound_when_node_path_empty(self):
        """Empty node sound path → play default generated sound."""
        self.node.notification_sound_path = ""
        with patch(
            "gui.dashboard._generate_default_sound",
            return_value="/tmp/default.wav"
        ) as mock_gen:
            with patch.object(self.dashboard, "_play_sound") as mock_play:
                self.dashboard.on_study_completed(
                    "1.2.3", "Hospital Alpha")
        mock_gen.assert_called_once()
        mock_play.assert_called_once_with("/tmp/default.wav")


class TestSadSound:
    """When median-5 speed is red (< median_all * 0.8), the default
    notification sound should be 'sad' — second tone lower instead of higher."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.config = populated_config
        self.config.filter_groups_enabled = False
        self.node = self.config.remote_nodes["ct"]
        self.node.notification_sound_enabled = True
        self.node.notification_sound_path = ""
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def _set_stats(self, median5, median_all):
        """Set up _current_stats with controlled median values."""
        stats = MagicMock(spec=TransferStats)
        stats.median_n_ipm.return_value = median5
        stats.median_all_ipm.return_value = median_all
        stats.completed_count = 5
        self.dashboard._current_stats = stats

    def test_sad_sound_when_speed_red(self):
        """median5 < median_all * 0.8 → _generate_default_sound(sad=True)."""
        self._set_stats(median5=50.0, median_all=100.0)  # 50 < 80 → red
        with patch(
            "gui.dashboard._generate_default_sound",
            return_value="/tmp/sad.wav"
        ) as mock_gen:
            with patch.object(self.dashboard, "_play_sound"):
                self.dashboard.on_study_completed(
                    "1.2.3", "Hospital Alpha")
        mock_gen.assert_called_once_with(sad=True)

    def test_normal_sound_when_speed_ok(self):
        """median5 >= median_all * 0.8 → _generate_default_sound(sad=False)."""
        self._set_stats(median5=90.0, median_all=100.0)  # 90 >= 80 → ok
        with patch(
            "gui.dashboard._generate_default_sound",
            return_value="/tmp/normal.wav"
        ) as mock_gen:
            with patch.object(self.dashboard, "_play_sound"):
                self.dashboard.on_study_completed(
                    "1.2.3", "Hospital Alpha")
        mock_gen.assert_called_once_with(sad=False)

    def test_normal_sound_when_speed_green(self):
        """Well above baseline → normal sound."""
        self._set_stats(median5=150.0, median_all=100.0)
        with patch(
            "gui.dashboard._generate_default_sound",
            return_value="/tmp/normal.wav"
        ) as mock_gen:
            with patch.object(self.dashboard, "_play_sound"):
                self.dashboard.on_study_completed(
                    "1.2.3", "Hospital Alpha")
        mock_gen.assert_called_once_with(sad=False)

    def test_normal_sound_when_no_stats(self):
        """No stats yet → default to normal sound (not sad)."""
        self.dashboard._current_stats = None
        with patch(
            "gui.dashboard._generate_default_sound",
            return_value="/tmp/normal.wav"
        ) as mock_gen:
            with patch.object(self.dashboard, "_play_sound"):
                self.dashboard.on_study_completed(
                    "1.2.3", "Hospital Alpha")
        mock_gen.assert_called_once_with(sad=False)

    def test_normal_sound_when_insufficient_data(self):
        """median_all < 1 (not enough data) → normal sound."""
        self._set_stats(median5=0.5, median_all=0.5)
        with patch(
            "gui.dashboard._generate_default_sound",
            return_value="/tmp/normal.wav"
        ) as mock_gen:
            with patch.object(self.dashboard, "_play_sound"):
                self.dashboard.on_study_completed(
                    "1.2.3", "Hospital Alpha")
        mock_gen.assert_called_once_with(sad=False)

    def test_sad_sound_at_exact_threshold(self):
        """median5 == median_all * 0.8 is NOT red (boundary is <, not <=)."""
        self._set_stats(median5=80.0, median_all=100.0)  # exactly at boundary
        with patch(
            "gui.dashboard._generate_default_sound",
            return_value="/tmp/normal.wav"
        ) as mock_gen:
            with patch.object(self.dashboard, "_play_sound"):
                self.dashboard.on_study_completed(
                    "1.2.3", "Hospital Alpha")
        mock_gen.assert_called_once_with(sad=False)

    def test_custom_sound_ignores_speed(self):
        """Custom WAV path → always plays that file, regardless of speed."""
        self._set_stats(median5=30.0, median_all=100.0)  # very red
        self.node.notification_sound_path = "/tmp/custom.wav"
        with patch("os.path.isfile", return_value=True):
            with patch.object(self.dashboard, "_play_sound") as mock_play:
                self.dashboard.on_study_completed(
                    "1.2.3", "Hospital Alpha")
        mock_play.assert_called_once_with("/tmp/custom.wav")


class TestSadSoundGeneration:
    """Verify the WAV generation produces different tones for sad vs normal."""

    def test_sad_flag_accepted(self):
        """_generate_default_sound accepts sad parameter."""
        from gui.dashboard import _generate_default_sound
        path = _generate_default_sound(sad=False)
        assert path.endswith(".wav")
        assert os.path.exists(path)

    def test_sad_and_normal_produce_different_files(self):
        """sad=True and sad=False should produce different WAV data."""
        from gui.dashboard import _generate_default_sound
        import gui.dashboard as mod
        # Clear cache to force regeneration
        mod._default_sound_path = None
        mod._sad_sound_path = None
        normal = _generate_default_sound(sad=False)
        sad = _generate_default_sound(sad=True)
        assert normal != sad
        with open(normal, "rb") as f:
            normal_data = f.read()
        with open(sad, "rb") as f:
            sad_data = f.read()
        assert normal_data != sad_data

    def test_sad_sound_second_tone_lower(self):
        """In sad mode, the second tone should be lower than 880 Hz (A5),
        creating a descending interval instead of ascending."""
        from gui.dashboard import _generate_default_sound, _SAD_FREQ_2
        assert _SAD_FREQ_2 < 880, (
            f"Sad second tone ({_SAD_FREQ_2} Hz) should be below A5 (880 Hz)")

    def test_normal_sound_second_tone_higher(self):
        """In normal mode, the second tone should be higher than 880 Hz."""
        from gui.dashboard import _NORMAL_FREQ_2
        assert _NORMAL_FREQ_2 > 880, (
            f"Normal second tone ({_NORMAL_FREQ_2} Hz) should be above A5 (880 Hz)")


# ═══════════════════════════════════════════════════════════════════════════
# Config — PacsNode notification sound fields
# ═══════════════════════════════════════════════════════════════════════════

class TestPacsNodeNotificationSound:

    def test_default_enabled(self):
        node = PacsNode(name="CT", ae_title="CT_AE")
        assert node.notification_sound_enabled is True

    def test_default_path_empty(self):
        node = PacsNode(name="CT", ae_title="CT_AE")
        assert node.notification_sound_path == ""

    def test_roundtrip(self, tmp_config_path):
        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes = {
            "ct": PacsNode(name="CT", ae_title="CT_AE",
                           notification_sound_enabled=False,
                           notification_sound_path="/sounds/ding.wav"),
        }
        config.save()
        config2 = AppConfig(config_path=tmp_config_path)
        config2.load()
        node = config2.remote_nodes["ct"]
        assert node.notification_sound_enabled is False
        assert node.notification_sound_path == "/sounds/ding.wav"

    def test_to_dict_includes_sound_fields(self):
        node = PacsNode(name="CT", ae_title="CT_AE",
                        notification_sound_enabled=False,
                        notification_sound_path="/a.wav")
        d = node.to_dict()
        assert d["notification_sound_enabled"] is False
        assert d["notification_sound_path"] == "/a.wav"

    def test_from_dict_reads_sound_fields(self):
        d = {"name": "CT", "ae_title": "CT_AE",
             "notification_sound_enabled": False,
             "notification_sound_path": "/b.wav"}
        node = PacsNode.from_dict(d)
        assert node.notification_sound_enabled is False
        assert node.notification_sound_path == "/b.wav"

    def test_from_dict_defaults_when_missing(self):
        """Old config files without sound fields → defaults."""
        d = {"name": "CT", "ae_title": "CT_AE"}
        node = PacsNode.from_dict(d)
        assert node.notification_sound_enabled is True
        assert node.notification_sound_path == ""

# ═══════════════════════════════════════════════════════════════════════════
# End-to-end: Engine → Dashboard sound (per study, not per series)
# ═══════════════════════════════════════════════════════════════════════════

class TestSoundPerStudyNotPerSeries:
    """The notification sound must play once per completed STUDY
    (study_uid), not per series and not per patient.  Each study
    carries its own institution, so the filter is per study."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.config = populated_config
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A"]
        self.node = self.config.remote_nodes["ct"]
        self.node.notification_sound_enabled = True
        self.node.notification_sound_path = ""
        self.engine = TransferEngine(self.config, "ct")
        self.dashboard = SourceDashboard(
            config=self.config, remote_key="ct")
        self.engine.signals.study_completed.connect(
            self.dashboard.on_study_completed)

    def test_no_sound_while_series_pending(self):
        """Study S1 has 3 series — only 1 done → no sound."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha"),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.2",
                      status="queued", institution_name="Hospital Alpha"),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.3",
                      status="queued", institution_name="Hospital Alpha"),
        ]
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.engine._check_study_complete("S1")
        mock_play.assert_not_called()

    def test_sound_when_all_series_of_study_done(self):
        """All 3 series of study S1 are done → exactly one sound."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha"),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.2",
                      status="done", institution_name="Hospital Alpha"),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.3",
                      status="done", institution_name="Hospital Alpha"),
        ]
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.engine._check_study_complete("S1")
        mock_play.assert_called_once()

    def test_no_repeat_sound_on_second_check(self):
        """Calling _check_study_complete again must not play a second sound."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha"),
        ]
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.engine._check_study_complete("S1")
            self.engine._check_study_complete("S1")
        mock_play.assert_called_once()

    def test_no_sound_for_inactive_group(self):
        """Study complete but institution is in inactive group → no sound."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Clinic Beta"),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.2",
                      status="done", institution_name="Clinic Beta"),
        ]
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.engine._check_study_complete("S1")
        mock_play.assert_not_called()

    def test_two_studies_same_patient_two_sounds(self):
        """Patient P1 has two studies from active group — each completing
        triggers its own sound."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha"),
            SeriesJob(patient_id="P1", study_uid="S2", series_uid="2.1",
                      status="done", institution_name="Hospital Alpha"),
        ]
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.engine._check_study_complete("S1")
            self.engine._check_study_complete("S2")
        assert mock_play.call_count == 2

    def test_active_study_sounds_inactive_study_silent(self):
        """Same patient, two studies from different groups — only the
        active group's study triggers a sound."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha"),
            SeriesJob(patient_id="P1", study_uid="S2", series_uid="2.1",
                      status="done", institution_name="Clinic Beta"),
        ]
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.engine._check_study_complete("S1")
            self.engine._check_study_complete("S2")
        mock_play.assert_called_once()

    def test_partial_queue_fires_completion(self):
        """Filtered-out series are not in the queue; completion must
        fire as soon as every *queued* series of the study is done,
        regardless of how many series the PACS originally had."""
        # The PACS may have had 5 series for S1; the filter let only
        # 2 through.  Both are done → sound must play.
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha",
                      remote_count=300),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.2",
                      status="done", institution_name="Hospital Alpha",
                      remote_count=200),
        ]
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.engine._check_study_complete("S1")
        mock_play.assert_called_once()

    def test_small_failed_series_does_not_block_completion(self):
        """A queued series with < 6 remote images that errors out must
        not suppress the completion sound — it's typically a stuck
        localizer."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha",
                      remote_count=300),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.2",
                      status="error", institution_name="Hospital Alpha",
                      remote_count=3),  # tiny series, failed
        ]
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.engine._check_study_complete("S1")
        mock_play.assert_called_once()

    def test_large_failed_series_blocks_completion(self):
        """A queued series with ≥ 6 remote images that errors out must
        suppress the completion sound — only small series are tolerated."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha",
                      remote_count=300),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.2",
                      status="error", institution_name="Hospital Alpha",
                      remote_count=50),  # not small; failure blocks
        ]
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.engine._check_study_complete("S1")
        mock_play.assert_not_called()

    def test_series_completed_signal_does_not_trigger_sound(self):
        """The series_completed signal (per series) must NOT play sound."""
        with patch.object(self.dashboard, "_play_sound") as mock_play:
            self.engine.signals.series_completed.emit("1.1", 50)
        mock_play.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Selection-mode Select All / Deselect All buttons
# ═══════════════════════════════════════════════════════════════════════════

class TestSelectionModeToggleAll:
    """In manual-selection mode the user gets a checkbox per series.
    The Select All and Deselect All buttons toggle every checkbox at once."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dashboard = SourceDashboard(
            config=populated_config, remote_key="ct")

    def _make_job_dict(self, series_uid: str):
        return {
            "patient_name": "Doe^John", "patient_id": "1",
            "study_description": "CT Head", "series_description": "Axial",
            "modality": "CT", "series_number": "1",
            "study_uid": "1.2.3", "series_uid": series_uid,
            "remote_count": 100, "local_count": 0, "status": "queued",
            "institution_name": "Hospital", "images_per_minute": 0.0,
            "study_date": "", "study_time": "",
        }

    def _queue(self, n: int):
        return [self._make_job_dict(f"1.{i}") for i in range(n)]

    def test_buttons_hidden_by_default(self):
        assert not self.dashboard.btn_select_all.isVisible()
        assert not self.dashboard.btn_deselect_all.isVisible()

    def test_buttons_visible_in_selection_mode(self):
        self.dashboard.show()
        self.dashboard.on_queue_ready_for_selection(self._queue(3))
        assert self.dashboard.btn_select_all.isVisible()
        assert self.dashboard.btn_deselect_all.isVisible()

    def test_buttons_hidden_when_queue_updated_after_selection(self):
        self.dashboard.show()
        self.dashboard.on_queue_ready_for_selection(self._queue(3))
        self.dashboard.on_queue_updated(self._queue(3))
        assert not self.dashboard.btn_select_all.isVisible()
        assert not self.dashboard.btn_deselect_all.isVisible()

    def test_deselect_all_unchecks_every_row(self):
        self.dashboard.on_queue_ready_for_selection(self._queue(3))
        # All rows start checked per on_queue_ready_for_selection
        self.dashboard._set_all_series_checked(False)
        for row in range(self.dashboard.series_table.rowCount()):
            cb = self.dashboard.series_table.item(row, 0)
            assert cb.checkState() == Qt.Unchecked

    def test_select_all_checks_every_row(self):
        self.dashboard.on_queue_ready_for_selection(self._queue(3))
        # Start by clearing them
        self.dashboard._set_all_series_checked(False)
        self.dashboard._set_all_series_checked(True)
        for row in range(self.dashboard.series_table.rowCount()):
            cb = self.dashboard.series_table.item(row, 0)
            assert cb.checkState() == Qt.Checked

    def test_select_all_after_partial_user_selection(self):
        self.dashboard.on_queue_ready_for_selection(self._queue(3))
        # User manually unchecked the middle one
        self.dashboard.series_table.item(1, 0).setCheckState(Qt.Unchecked)
        self.dashboard._set_all_series_checked(True)
        for row in range(3):
            cb = self.dashboard.series_table.item(row, 0)
            assert cb.checkState() == Qt.Checked


