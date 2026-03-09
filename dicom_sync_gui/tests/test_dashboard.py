"""
Tests for gui.dashboard — SourceDashboard and StatsLabel.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui.dashboard import SourceDashboard, StatsLabel
from core.transfer_engine import TransferStats
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
        assert table.columnCount() == 9
        headers = [
            table.horizontalHeaderItem(i).text()
            for i in range(table.columnCount())
        ]
        assert headers == [
            "Patient", "Study", "Series", "Modality",
            "Images", "Pending", "img/min", "Status", "ETE",
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
        item = self.dashboard.series_table.item(0, 0)
        assert item.text() == "Smith^Jane"

    def test_on_queue_updated_shows_pending(self):
        queue = [self._make_job_dict(remote_count=100, local_count=30)]
        self.dashboard.on_queue_updated(queue)
        pending_item = self.dashboard.series_table.item(0, 5)
        assert pending_item.text() == "70"

    def test_done_status_shows_checkmark_in_ete(self):
        queue = [self._make_job_dict(status="done")]
        self.dashboard.on_queue_updated(queue)
        ete_item = self.dashboard.series_table.item(0, 8)  # col 8 = ETE
        assert "\u2713" in ete_item.text()

    def test_error_status_shows_dash_in_ete(self):
        queue = [self._make_job_dict(status="error")]
        self.dashboard.on_queue_updated(queue)
        ete_item = self.dashboard.series_table.item(0, 8)  # col 8 = ETE
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
        ipm_item = self.dashboard.series_table.item(0, 6)  # col 6 = img/min
        assert ipm_item.text() == "150"

    def test_ipm_column_shows_dash_for_queued(self):
        queue = [self._make_job_dict(status="queued")]
        self.dashboard.on_queue_updated(queue)
        ipm_item = self.dashboard.series_table.item(0, 6)
        assert "\u2014" in ipm_item.text()

    def test_ipm_column_shows_dash_for_done_zero_speed(self):
        queue = [self._make_job_dict(
            status="done", images_per_minute=0.0)]
        self.dashboard.on_queue_updated(queue)
        ipm_item = self.dashboard.series_table.item(0, 6)
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
