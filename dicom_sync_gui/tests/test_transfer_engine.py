"""
Tests for core.transfer_engine — SeriesJob, TransferStats, TransferEngine.
"""

import time
import threading
from collections import deque
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from core.config import AppConfig, PacsNode
from core.transfer_engine import (
    SeriesJob, TransferStats, TransferEngine, TransferSignals,
    SeriesCompletionRecord,
)


# ═══════════════════════════════════════════════════════════════════════════
# SeriesJob
# ═══════════════════════════════════════════════════════════════════════════

class TestSeriesJob:

    def test_default_values(self):
        job = SeriesJob()
        assert job.patient_name == ""
        assert job.patient_id == ""
        assert job.study_description == ""
        assert job.series_description == ""
        assert job.modality == ""
        assert job.series_number == ""
        assert job.study_uid == ""
        assert job.series_uid == ""
        assert job.remote_count == 0
        assert job.local_count == 0
        assert job.status == "queued"
        assert job.institution_name == ""
        assert job.images_per_minute == 0.0

    def test_to_transfer_positive(self):
        job = SeriesJob(remote_count=100, local_count=30)
        assert job.to_transfer == 70

    def test_to_transfer_zero_when_complete(self):
        job = SeriesJob(remote_count=100, local_count=100)
        assert job.to_transfer == 0

    def test_to_transfer_zero_when_local_exceeds(self):
        job = SeriesJob(remote_count=50, local_count=60)
        assert job.to_transfer == 0

    def test_to_transfer_all_missing(self):
        job = SeriesJob(remote_count=200, local_count=0)
        assert job.to_transfer == 200

    def test_to_dict_contains_all_fields(self):
        job = SeriesJob(
            patient_name="Doe^John",
            patient_id="12345",
            study_description="CT Head",
            series_description="Axial",
            modality="CT",
            series_number="3",
            study_uid="1.2.3.4",
            series_uid="1.2.3.4.5",
            remote_count=100,
            local_count=10,
            status="transferring",
            institution_name="Hospital Alpha",
            images_per_minute=120.5,
        )
        d = job.to_dict()
        assert d["patient_name"] == "Doe^John"
        assert d["patient_id"] == "12345"
        assert d["study_description"] == "CT Head"
        assert d["series_description"] == "Axial"
        assert d["modality"] == "CT"
        assert d["series_number"] == "3"
        assert d["study_uid"] == "1.2.3.4"
        assert d["series_uid"] == "1.2.3.4.5"
        assert d["remote_count"] == 100
        assert d["local_count"] == 10
        assert d["status"] == "transferring"
        assert d["institution_name"] == "Hospital Alpha"
        assert d["images_per_minute"] == 120.5

    def test_to_dict_keys(self):
        job = SeriesJob()
        d = job.to_dict()
        expected = {
            "patient_name", "patient_id", "study_description",
            "series_description", "modality", "series_number",
            "study_uid", "series_uid", "remote_count", "local_count",
            "status", "institution_name", "images_per_minute",
        }
        assert set(d.keys()) == expected


# ═══════════════════════════════════════════════════════════════════════════
# SeriesCompletionRecord
# ═══════════════════════════════════════════════════════════════════════════

class TestSeriesCompletionRecord:

    def test_default_values(self):
        rec = SeriesCompletionRecord()
        assert rec.series_uid == ""
        assert rec.image_count == 0
        assert rec.duration_seconds == 0.0
        assert rec.images_per_minute == 0.0

    def test_custom_values(self):
        rec = SeriesCompletionRecord(
            series_uid="1.2.3", image_count=120,
            duration_seconds=60.0, images_per_minute=120.0)
        assert rec.series_uid == "1.2.3"
        assert rec.image_count == 120
        assert rec.duration_seconds == 60.0
        assert rec.images_per_minute == 120.0


# ═══════════════════════════════════════════════════════════════════════════
# TransferStats
# ═══════════════════════════════════════════════════════════════════════════

class TestTransferStats:

    def test_initial_state(self):
        stats = TransferStats()
        assert stats.total_images == 0
        assert stats.start_time == 0.0
        assert stats.completed_count == 0

    def test_start_session_resets(self):
        stats = TransferStats()
        stats.total_images = 42
        stats._completed_series.append(
            SeriesCompletionRecord(image_count=42))
        stats.start_session()
        assert stats.total_images == 0
        assert stats.start_time > 0
        assert stats.completed_count == 0

    def test_record_series_increments_total(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 60, 30.0)
        assert stats.total_images == 60
        stats.record_series("1.2", 40, 20.0)
        assert stats.total_images == 100

    def test_record_series_increments_count(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 60, 30.0)
        assert stats.completed_count == 1
        stats.record_series("1.2", 40, 20.0)
        assert stats.completed_count == 2

    def test_record_series_computes_ipm(self):
        stats = TransferStats()
        stats.start_session()
        # 120 images in 60 seconds = 120 img/min
        stats.record_series("1.1", 120, 60.0)
        assert stats._completed_series[-1].images_per_minute == 120.0

    def test_record_series_zero_duration(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 100, 0.0)
        assert stats._completed_series[-1].images_per_minute == 0.0

    def test_last_series_ipm_empty(self):
        stats = TransferStats()
        assert stats.last_series_ipm() == 0.0

    def test_last_series_ipm(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 120, 60.0)  # 120 ipm
        stats.record_series("1.2", 60, 60.0)   # 60 ipm
        assert stats.last_series_ipm() == 60.0

    def test_median_n_ipm_empty(self):
        stats = TransferStats()
        assert stats.median_n_ipm(5) == 0.0

    def test_median_n_ipm_single(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 120, 60.0)  # 120 ipm
        assert stats.median_n_ipm(5) == 120.0

    def test_median_n_ipm_odd_count(self):
        stats = TransferStats()
        stats.start_session()
        # 3 series with ipm: 60, 120, 180
        stats.record_series("1.1", 60, 60.0)    # 60 ipm
        stats.record_series("1.2", 120, 60.0)   # 120 ipm
        stats.record_series("1.3", 180, 60.0)   # 180 ipm
        # median of [60, 120, 180] = 120
        assert stats.median_n_ipm(5) == 120.0

    def test_median_n_ipm_even_count(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 60, 60.0)    # 60 ipm
        stats.record_series("1.2", 120, 60.0)   # 120 ipm
        stats.record_series("1.3", 180, 60.0)   # 180 ipm
        stats.record_series("1.4", 240, 60.0)   # 240 ipm
        # median of [60, 120, 180, 240] = (120+180)/2 = 150
        assert stats.median_n_ipm(5) == 150.0

    def test_median_n_ipm_limits_to_last_n(self):
        stats = TransferStats()
        stats.start_session()
        # 6 series, request median of last 3
        stats.record_series("1.1", 10, 60.0)    # 10 ipm
        stats.record_series("1.2", 20, 60.0)    # 20 ipm
        stats.record_series("1.3", 30, 60.0)    # 30 ipm
        stats.record_series("1.4", 300, 60.0)   # 300 ipm
        stats.record_series("1.5", 600, 60.0)   # 600 ipm
        stats.record_series("1.6", 900, 60.0)   # 900 ipm
        # last 3: [300, 600, 900] → median = 600
        assert stats.median_n_ipm(3) == 600.0

    def test_median_all_ipm_empty(self):
        stats = TransferStats()
        assert stats.median_all_ipm() == 0.0

    def test_median_all_ipm(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 60, 60.0)    # 60 ipm
        stats.record_series("1.2", 120, 60.0)   # 120 ipm
        stats.record_series("1.3", 180, 60.0)   # 180 ipm
        assert stats.median_all_ipm() == 120.0

    def test_overall_images_per_minute_delegates(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 120, 60.0)   # 120 ipm
        stats.record_series("1.2", 240, 60.0)   # 240 ipm
        # median_all = (120+240)/2 = 180
        assert stats.overall_images_per_minute() == 180.0

    def test_overall_images_per_minute_no_data(self):
        stats = TransferStats()
        assert stats.overall_images_per_minute() == 0.0

    def test_median_static_method(self):
        assert TransferStats._median([]) == 0.0
        assert TransferStats._median([5.0]) == 5.0
        assert TransferStats._median([1.0, 3.0]) == 2.0
        assert TransferStats._median([1.0, 2.0, 3.0]) == 2.0
        assert TransferStats._median([3.0, 1.0, 2.0]) == 2.0  # unsorted input
        assert TransferStats._median([1.0, 2.0, 3.0, 4.0]) == 2.5

    def test_min_images_for_stats_default(self):
        stats = TransferStats()
        assert stats.MIN_IMAGES_FOR_STATS == 10


# ═══════════════════════════════════════════════════════════════════════════
# TransferStats — MIN_IMAGES_FOR_STATS threshold
# ═══════════════════════════════════════════════════════════════════════════

class TestTransferStatsThreshold:
    """Series with fewer than MIN_IMAGES_FOR_STATS images are excluded
    from speed statistics but still counted in total_images."""

    def test_small_series_excluded_from_last_ipm(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 120, 60.0)  # 120 ipm, qualifies
        stats.record_series("1.2", 5, 5.0)     # 60 ipm, too small
        # last qualifying is still series 1.1
        assert stats.last_series_ipm() == 120.0

    def test_small_series_excluded_from_median_all(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 60, 60.0)    # 60 ipm
        stats.record_series("1.2", 3, 1.0)      # 180 ipm, too small
        stats.record_series("1.3", 120, 60.0)   # 120 ipm
        # median of [60, 120] = 90  (the 3-image series is excluded)
        assert stats.median_all_ipm() == 90.0

    def test_small_series_excluded_from_median_n(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 60, 60.0)    # 60 ipm
        stats.record_series("1.2", 2, 1.0)      # excluded
        stats.record_series("1.3", 120, 60.0)   # 120 ipm
        stats.record_series("1.4", 4, 2.0)      # excluded
        stats.record_series("1.5", 180, 60.0)   # 180 ipm
        # qualifying: [60, 120, 180] → median last 2 = [120, 180] → 150
        assert stats.median_n_ipm(2) == 150.0

    def test_only_small_series_returns_zero(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 5, 5.0)
        stats.record_series("1.2", 3, 1.0)
        assert stats.last_series_ipm() == 0.0
        assert stats.median_all_ipm() == 0.0
        assert stats.median_n_ipm(5) == 0.0
        assert stats.overall_images_per_minute() == 0.0

    def test_small_series_still_counted_in_total(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 5, 5.0)
        assert stats.total_images == 5
        assert stats.completed_count == 1

    def test_exactly_threshold_qualifies(self):
        stats = TransferStats()
        stats.start_session()
        # Exactly 10 images → should qualify
        stats.record_series("1.1", 10, 60.0)  # 10 ipm
        assert stats.last_series_ipm() == 10.0
        assert stats.median_all_ipm() == 10.0

    def test_one_below_threshold_excluded(self):
        stats = TransferStats()
        stats.start_session()
        # 9 images → should be excluded
        stats.record_series("1.1", 9, 60.0)
        assert stats.last_series_ipm() == 0.0

    def test_mix_small_and_large_total_images(self):
        stats = TransferStats()
        stats.start_session()
        stats.record_series("1.1", 100, 60.0)  # qualifies
        stats.record_series("1.2", 3, 1.0)     # excluded from stats
        stats.record_series("1.3", 200, 60.0)  # qualifies
        assert stats.total_images == 303
        assert stats.completed_count == 3
        # Only 2 qualifying series for stats
        assert len(stats._stats_series) == 2


# ═══════════════════════════════════════════════════════════════════════════
# TransferSignals
# ═══════════════════════════════════════════════════════════════════════════

class TestTransferSignals:
    """Verify all expected signals exist on TransferSignals."""

    @pytest.fixture(autouse=True)
    def _create(self, qapp):
        self.signals = TransferSignals()

    def test_queue_updated_signal(self):
        assert hasattr(self.signals, "queue_updated")

    def test_series_started_signal(self):
        assert hasattr(self.signals, "series_started")

    def test_series_progress_signal(self):
        assert hasattr(self.signals, "series_progress")

    def test_series_completed_signal(self):
        assert hasattr(self.signals, "series_completed")

    def test_series_error_signal(self):
        assert hasattr(self.signals, "series_error")

    def test_stats_updated_signal(self):
        assert hasattr(self.signals, "stats_updated")

    def test_cycle_started_signal(self):
        assert hasattr(self.signals, "cycle_started")

    def test_cycle_finished_signal(self):
        assert hasattr(self.signals, "cycle_finished")

    def test_service_started_signal(self):
        assert hasattr(self.signals, "service_started")

    def test_service_stopped_signal(self):
        assert hasattr(self.signals, "service_stopped")

    def test_log_message_signal(self):
        assert hasattr(self.signals, "log_message")

    def test_unknown_institution_signal(self):
        assert hasattr(self.signals, "unknown_institution")


# ═══════════════════════════════════════════════════════════════════════════
# TransferEngine — Institution Filter
# ═══════════════════════════════════════════════════════════════════════════

class TestInstitutionFilter:
    """Test _passes_institution_filter logic."""

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, qapp):
        self.config = populated_config
        self.engine = TransferEngine(self.config, "ct")

    def test_filter_disabled_always_passes(self):
        self.config.filter_groups_enabled = False
        assert self.engine._passes_institution_filter("Anything") is True

    def test_filter_enabled_no_active_groups_passes(self):
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = []
        assert self.engine._passes_institution_filter("Hospital Alpha") is True

    def test_known_institution_in_active_group_passes(self):
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A"]
        # Hospital Alpha is assigned to Group A
        assert self.engine._passes_institution_filter("Hospital Alpha") is True

    def test_known_institution_in_inactive_group_fails(self):
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A"]
        # Clinic Beta is assigned to Group B (not active)
        assert self.engine._passes_institution_filter("Clinic Beta") is False

    def test_unknown_institution_passes_and_emits_signal(self):
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A"]
        emitted = []
        self.engine.signals.unknown_institution.connect(
            lambda name: emitted.append(name))
        # "New Clinic" is not in assignments
        assert self.engine._passes_institution_filter("New Clinic") is True
        assert emitted == ["New Clinic"]

    def test_unknown_institution_emits_only_once(self):
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A"]
        emitted = []
        self.engine.signals.unknown_institution.connect(
            lambda name: emitted.append(name))
        self.engine._passes_institution_filter("New Clinic")
        self.engine._passes_institution_filter("New Clinic")  # second time
        assert len(emitted) == 1

    def test_empty_institution_name_passes(self):
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A"]
        # Empty institution name → passes (unassigned)
        assert self.engine._passes_institution_filter("") is True

    def test_unassigned_institution_with_empty_group_passes(self):
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A"]
        # "Unknown Clinic" is in assignments but has group=""
        assert self.engine._passes_institution_filter("Unknown Clinic") is True

    def test_multiple_active_groups(self):
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A", "Group B"]
        assert self.engine._passes_institution_filter("Hospital Alpha") is True
        assert self.engine._passes_institution_filter("Clinic Beta") is True


# ═══════════════════════════════════════════════════════════════════════════
# TransferEngine — lifecycle
# ═══════════════════════════════════════════════════════════════════════════

class TestTransferEngineLifecycle:

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, qapp):
        self.config = populated_config
        self.engine = TransferEngine(self.config, "ct")

    def test_initial_state(self):
        assert self.engine.is_running is False
        assert self.engine._queue == []

    def test_remote_key_stored(self):
        assert self.engine.remote_key == "ct"

    def test_start_sets_running(self):
        with patch.object(self.engine, '_service_loop'):
            self.engine.start(hours=3, max_images=0, sync_interval=60)
            # Thread starts, running should be True
            assert self.engine._running is True
            self.engine.stop()

    def test_start_twice_does_nothing(self):
        with patch.object(self.engine, '_service_loop'):
            self.engine._running = True
            self.engine.start(hours=3, max_images=0, sync_interval=60)
            # _thread should still be None (start early-returned)
            assert self.engine._thread is None

    def test_stop_sets_cancel(self):
        self.engine._cancel.clear()
        self.engine.stop()
        assert self.engine._cancel.is_set()

    def test_make_dicom_ops(self):
        ops = self.engine._make_dicom_ops()
        assert ops is not None

    def test_notified_institutions_starts_empty(self):
        assert self.engine._notified_institutions == set()
