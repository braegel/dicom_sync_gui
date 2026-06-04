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
            "status", "institution_name", "accession_number",
            "images_per_minute", "study_date", "study_time",
        }
        assert set(d.keys()) == expected

    def test_study_date_time_defaults(self):
        job = SeriesJob()
        assert job.study_date == ""
        assert job.study_time == ""

    def test_study_date_time_in_to_dict(self):
        job = SeriesJob(study_date="20260401", study_time="143000")
        d = job.to_dict()
        assert d["study_date"] == "20260401"
        assert d["study_time"] == "143000"


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


# ═══════════════════════════════════════════════════════════════════════════
# TransferEngine — studies_queried signal
# ═══════════════════════════════════════════════════════════════════════════

class TestStudiesQueriedEmission:
    """studies_queried must carry institution_name resolved at series level,
    not the (often empty) study-level InstitutionName."""

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, qapp):
        self.config = populated_config
        self.config.filter_groups_enabled = False  # no filtering
        self.engine = TransferEngine(self.config, "ct")

    def _make_study_ds(self, study_uid, institution_name=""):
        """Fake DICOM study-level dataset (no InstitutionName)."""
        from datetime import datetime
        now = datetime.now()
        ds = MagicMock()
        ds.StudyInstanceUID = study_uid
        ds.StudyDate = now.strftime("%Y%m%d")
        ds.StudyTime = now.strftime("%H%M%S")
        ds.PatientName = "Doe^John"
        ds.PatientID = "P1"
        ds.StudyDescription = "CT Head"
        ds.InstitutionName = institution_name
        return ds

    def _make_series_ds(self, series_uid, institution_name="",
                        num_instances=10):
        """Fake DICOM series-level dataset."""
        ds = MagicMock()
        ds.SeriesInstanceUID = series_uid
        ds.SeriesDescription = "Axial"
        ds.Modality = "CT"
        ds.SeriesNumber = "1"
        ds.NumberOfSeriesRelatedInstances = num_instances
        ds.InstitutionName = institution_name
        return ds

    def test_institution_from_series_fallback(self):
        """When study-level InstitutionName is empty, the emitted dict
        must carry the institution resolved from the first series."""
        study_ds = self._make_study_ds("1.2.3", institution_name="")
        series_ds = self._make_series_ds("1.2.3.1",
                                         institution_name="Hospital Alpha")

        mock_ops = MagicMock()
        mock_ops.c_find_studies.return_value = [study_ds]
        mock_ops.c_find_series.return_value = [series_ds]
        mock_ops.c_find_local_series.return_value = []

        received = []
        self.engine.signals.studies_queried.connect(received.append)

        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(hours=1)
        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=mock_ops):
            self.engine._query_source(
                "20260401", cutoff, max_images=0, seen_series=set())

        assert len(received) == 1
        studies = received[0]
        assert len(studies) == 1
        assert studies[0]["institution_name"] == "Hospital Alpha"

    def test_institution_from_study_level_when_present(self):
        """When study-level InstitutionName IS present, it must be used."""
        study_ds = self._make_study_ds("1.2.3",
                                       institution_name="Clinic Beta")
        series_ds = self._make_series_ds("1.2.3.1",
                                         institution_name="Clinic Beta")

        mock_ops = MagicMock()
        mock_ops.c_find_studies.return_value = [study_ds]
        mock_ops.c_find_series.return_value = [series_ds]
        mock_ops.c_find_local_series.return_value = []

        received = []
        self.engine.signals.studies_queried.connect(received.append)

        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(hours=1)
        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=mock_ops):
            self.engine._query_source(
                "20260401", cutoff, max_images=0, seen_series=set())

        studies = received[0]
        assert studies[0]["institution_name"] == "Clinic Beta"

    def test_studies_queried_deduplicates_by_study_uid(self):
        """Multiple series from the same study must emit one entry."""
        study_ds = self._make_study_ds("1.2.3", institution_name="")
        series1 = self._make_series_ds("1.2.3.1",
                                       institution_name="Hospital Alpha")
        series2 = self._make_series_ds("1.2.3.2",
                                       institution_name="Hospital Alpha")

        mock_ops = MagicMock()
        mock_ops.c_find_studies.return_value = [study_ds]
        mock_ops.c_find_series.return_value = [series1, series2]
        mock_ops.c_find_local_series.return_value = []

        received = []
        self.engine.signals.studies_queried.connect(received.append)

        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(hours=1)
        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=mock_ops):
            self.engine._query_source(
                "20260401", cutoff, max_images=0, seen_series=set())

        studies = received[0]
        assert len(studies) == 1
        assert studies[0]["study_uid"] == "1.2.3"

    def test_filtered_out_studies_still_emitted(self):
        """Studies rejected by the institution filter must still appear
        in studies_queried — the signal counts all query results."""
        # Enable filtering: only Group A is active
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A"]
        self.config.filter_group_names = ["Group A", "Group B"]
        self.config.institution_assignments = {
            "Hospital Alpha": "Group A",
            "Clinic Beta": "Group B",  # Group B is NOT active → filtered
        }
        engine = TransferEngine(self.config, "ct")

        study_a = self._make_study_ds("1.1", institution_name="Hospital Alpha")
        study_b = self._make_study_ds("1.2", institution_name="Clinic Beta")
        series_a = self._make_series_ds("1.1.1",
                                        institution_name="Hospital Alpha")
        series_b = self._make_series_ds("1.2.1",
                                        institution_name="Clinic Beta")

        mock_ops = MagicMock()
        mock_ops.c_find_studies.return_value = [study_a, study_b]
        mock_ops.c_find_series.side_effect = [[series_a], [series_b]]
        mock_ops.c_find_local_series.return_value = []

        received = []
        engine.signals.studies_queried.connect(received.append)

        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(hours=1)
        with patch.object(engine, '_make_dicom_ops',
                          return_value=mock_ops):
            jobs = engine._query_source(
                "20260401", cutoff, max_images=0, seen_series=set())

        # Only Group A study should produce jobs
        assert all(j.institution_name == "Hospital Alpha" for j in jobs)
        # But BOTH studies must appear in the signal
        studies = received[0]
        uids = {s["study_uid"] for s in studies}
        assert "1.1" in uids
        assert "1.2" in uids
        institutions = {s["institution_name"] for s in studies}
        assert "Hospital Alpha" in institutions
        assert "Clinic Beta" in institutions

    def test_time_filtered_studies_still_emitted(self):
        """Studies outside the engine's time cutoff (but returned by the
        PACS query) must still appear in studies_queried — the dashboard
        applies its own 60-minute window."""
        from datetime import datetime, timedelta
        now = datetime.now()

        # recent study: inside cutoff
        recent = self._make_study_ds("1.1", institution_name="Hospital Alpha")
        # old study: outside cutoff (3 hours ago)
        old = MagicMock()
        old.StudyInstanceUID = "1.2"
        old.StudyDate = (now - timedelta(hours=3)).strftime("%Y%m%d")
        old.StudyTime = (now - timedelta(hours=3)).strftime("%H%M%S")
        old.PatientName = "Old^Patient"
        old.PatientID = "P2"
        old.StudyDescription = "Old CT"
        old.InstitutionName = "Clinic Beta"

        series_recent = self._make_series_ds(
            "1.1.1", institution_name="Hospital Alpha")

        mock_ops = MagicMock()
        # c_find_studies returns BOTH (PACS returns all for date range)
        mock_ops.c_find_studies.return_value = [recent, old]
        # c_find_series only called for the recent one (old filtered by time)
        mock_ops.c_find_series.return_value = [series_recent]
        mock_ops.c_find_local_series.return_value = []

        received = []
        self.engine.signals.studies_queried.connect(received.append)

        # cutoff = 1 hour ago → "old" study won't pass time filter
        cutoff = now - timedelta(hours=1)
        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=mock_ops):
            jobs = self.engine._query_source(
                "20260401", cutoff, max_images=0, seen_series=set())

        # Only the recent study should produce jobs
        assert len(jobs) == 1
        assert jobs[0].study_uid == "1.1"

        # But BOTH studies must appear in the signal
        studies = received[0]
        uids = {s["study_uid"] for s in studies}
        assert "1.1" in uids, "Recent study missing from signal"
        assert "1.2" in uids, "Time-filtered study missing from signal"


# ═══════════════════════════════════════════════════════════════════════════
# TransferEngine — patient_studies_completed signal
# ═══════════════════════════════════════════════════════════════════════════

class TestPatientStudiesCompleted:
    """After the last series for a patient finishes (including priors),
    a patient_studies_completed signal must fire."""

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, qapp):
        self.config = populated_config
        self.config.filter_groups_enabled = False
        self.engine = TransferEngine(self.config, "ct")

    def test_signal_exists(self):
        assert hasattr(self.engine.signals, "patient_studies_completed")

    def test_fires_when_all_series_done(self):
        """Signal fires after the last series of a patient completes."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha"),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.2",
                      status="transferring", institution_name="Hospital Alpha"),
        ]
        received = []
        self.engine.signals.patient_studies_completed.connect(
            lambda pid, inst: received.append((pid, inst)))

        # Simulate the last series finishing
        self.engine._queue[1].status = "done"
        self.engine._check_patient_complete("P1")

        assert len(received) == 1
        assert received[0] == ("P1", "Hospital Alpha")

    def test_does_not_fire_while_series_pending(self):
        """Signal must NOT fire if any series is still queued/transferring."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha"),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.2",
                      status="queued", institution_name="Hospital Alpha"),
        ]
        received = []
        self.engine.signals.patient_studies_completed.connect(
            lambda pid, inst: received.append((pid, inst)))

        self.engine._check_patient_complete("P1")
        assert len(received) == 0

    def test_includes_prior_studies(self):
        """Prior studies (different study_uid, same patient) must all be
        done before the signal fires."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha"),
            SeriesJob(patient_id="P1", study_uid="S2", series_uid="2.1",
                      status="done",
                      study_description="[Prior] Old CT",
                      institution_name="Hospital Alpha"),
        ]
        received = []
        self.engine.signals.patient_studies_completed.connect(
            lambda pid, inst: received.append((pid, inst)))

        self.engine._check_patient_complete("P1")
        assert len(received) == 1

    def test_does_not_fire_twice_for_same_patient(self):
        """Signal must fire only once per patient per cycle."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha"),
        ]
        received = []
        self.engine.signals.patient_studies_completed.connect(
            lambda pid, inst: received.append((pid, inst)))

        self.engine._check_patient_complete("P1")
        self.engine._check_patient_complete("P1")
        assert len(received) == 1

    def test_error_series_does_not_block(self):
        """Errored series count as finished — don't block the signal."""
        self.engine._queue = [
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.1",
                      status="done", institution_name="Hospital Alpha"),
            SeriesJob(patient_id="P1", study_uid="S1", series_uid="1.2",
                      status="error", institution_name="Hospital Alpha"),
        ]
        received = []
        self.engine.signals.patient_studies_completed.connect(
            lambda pid, inst: received.append((pid, inst)))

        self.engine._check_patient_complete("P1")
        assert len(received) == 1


# ═══════════════════════════════════════════════════════════════════════════
# TransferEngine — priors must respect institution filter
# ═══════════════════════════════════════════════════════════════════════════

class TestPriorsInstitutionFilter:
    """Prior studies must be filtered by institution group, just like
    current studies.  If a prior belongs to an institution in an inactive
    group, it must be skipped."""

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, qapp):
        self.config = populated_config
        # Group A active, Group B inactive
        self.config.filter_groups_enabled = True
        self.config.active_filter_groups = ["Group A"]
        self.config.institution_assignments = {
            "Hospital Alpha": "Group A",
            "Clinic Beta": "Group B",
        }
        self.config.prior_studies_count = 2
        self.config.prior_studies_same_modality = False
        self.config.filter_allow_small_series = False
        self.engine = TransferEngine(self.config, "ct")

    def _make_study_ds(self, study_uid, patient_id="P1",
                       study_date="20260401", study_time="120000",
                       institution_name="", modalities="CT"):
        ds = MagicMock()
        ds.StudyInstanceUID = study_uid
        ds.StudyDate = study_date
        ds.StudyTime = study_time
        ds.PatientName = "Doe^John"
        ds.PatientID = patient_id
        ds.StudyDescription = "CT Head"
        ds.InstitutionName = institution_name
        ds.ModalitiesInStudy = modalities
        return ds

    def _make_series_ds(self, series_uid, institution_name="",
                        num_instances=50):
        ds = MagicMock()
        ds.SeriesInstanceUID = series_uid
        ds.SeriesDescription = "Axial"
        ds.Modality = "CT"
        ds.SeriesNumber = "1"
        ds.NumberOfSeriesRelatedInstances = num_instances
        ds.InstitutionName = institution_name
        return ds

    def test_priors_from_inactive_group_are_skipped(self):
        """A prior study whose institution belongs to an inactive group
        must NOT be downloaded."""
        # Current study is at Hospital Alpha (active Group A)
        current = [self._make_study_ds(
            "1.1", institution_name="Hospital Alpha")]

        # Prior study is at Clinic Beta (inactive Group B)
        prior_ds = self._make_study_ds(
            "2.1", study_date="20260301",
            institution_name="Clinic Beta")
        prior_series = self._make_series_ds(
            "2.1.1", institution_name="Clinic Beta")

        mock_ops = MagicMock()
        # c_find_studies(patient_id=...) returns current + prior
        mock_ops.c_find_studies.return_value = [
            current[0], prior_ds]
        mock_ops.c_find_series.return_value = [prior_series]
        mock_ops.c_find_local_series.return_value = []

        jobs = self.engine._resolve_priors(
            mock_ops, current, seen_series=set(), max_images=0)

        assert len(jobs) == 0, (
            "Prior from inactive group should be skipped")

    def test_priors_from_active_group_are_downloaded(self):
        """A prior study whose institution belongs to an active group
        must be downloaded."""
        current = [self._make_study_ds(
            "1.1", institution_name="Hospital Alpha")]

        prior_ds = self._make_study_ds(
            "2.1", study_date="20260301",
            institution_name="Hospital Alpha")
        prior_series = self._make_series_ds(
            "2.1.1", institution_name="Hospital Alpha")

        mock_ops = MagicMock()
        mock_ops.c_find_studies.return_value = [
            current[0], prior_ds]
        mock_ops.c_find_series.return_value = [prior_series]
        mock_ops.c_find_local_series.return_value = []

        jobs = self.engine._resolve_priors(
            mock_ops, current, seen_series=set(), max_images=0)

        assert len(jobs) == 1
        assert jobs[0].series_uid == "2.1.1"

    def test_priors_from_unknown_institution_are_downloaded(self):
        """A prior study from an unknown (unassigned) institution should
        still be downloaded — same rule as current studies."""
        current = [self._make_study_ds(
            "1.1", institution_name="Hospital Alpha")]

        prior_ds = self._make_study_ds(
            "2.1", study_date="20260301",
            institution_name="Brand New Hospital")
        prior_series = self._make_series_ds(
            "2.1.1", institution_name="Brand New Hospital")

        mock_ops = MagicMock()
        mock_ops.c_find_studies.return_value = [
            current[0], prior_ds]
        mock_ops.c_find_series.return_value = [prior_series]
        mock_ops.c_find_local_series.return_value = []

        jobs = self.engine._resolve_priors(
            mock_ops, current, seen_series=set(), max_images=0)

        assert len(jobs) == 1, (
            "Unknown institution should be downloaded (same as current)")

    def test_priors_unfiltered_when_filtering_disabled(self):
        """With filtering disabled, all priors should be downloaded."""
        self.config.filter_groups_enabled = False

        current = [self._make_study_ds(
            "1.1", institution_name="Hospital Alpha")]

        prior_ds = self._make_study_ds(
            "2.1", study_date="20260301",
            institution_name="Clinic Beta")
        prior_series = self._make_series_ds(
            "2.1.1", institution_name="Clinic Beta")

        mock_ops = MagicMock()
        mock_ops.c_find_studies.return_value = [
            current[0], prior_ds]
        mock_ops.c_find_series.return_value = [prior_series]
        mock_ops.c_find_local_series.return_value = []

        jobs = self.engine._resolve_priors(
            mock_ops, current, seen_series=set(), max_images=0)

        assert len(jobs) == 1


# ═══════════════════════════════════════════════════════════════════════════
# TransferEngine — retry blacklist after 2 failed attempts
# ═══════════════════════════════════════════════════════════════════════════

class TestRetryBlacklist:
    """Small series (1-5 images) sometimes can't be retrieved from the
    source PACS. The engine currently re-queues them every sync cycle
    forever. After 2 failed C-MOVE attempts a series must be
    blacklisted and never re-queried for download."""

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, qapp, tmp_path):
        from core.transfer_log import TransferLog
        self.config = populated_config
        self.config.filter_groups_enabled = False
        self.engine = TransferEngine(self.config, "ct")
        # Swap the engine's transfer log for one pointing at a tmp DB
        # so tests never touch the user's real log.
        self.engine._transfer_log.close()
        self.engine._transfer_log = TransferLog(
            str(tmp_path / "transfer_log.sqlite"))

    def _make_study_ds(self, study_uid="1.2.3"):
        from datetime import datetime
        now = datetime.now()
        ds = MagicMock()
        ds.StudyInstanceUID = study_uid
        ds.StudyDate = now.strftime("%Y%m%d")
        ds.StudyTime = now.strftime("%H%M%S")
        ds.PatientName = "Doe^John"
        ds.PatientID = "P1"
        ds.StudyDescription = "CT Head"
        ds.InstitutionName = "Hospital Alpha"
        ds.AccessionNumber = "ACC1"
        return ds

    def _make_series_ds(self, series_uid="1.2.3.1", num_instances=3):
        ds = MagicMock()
        ds.SeriesInstanceUID = series_uid
        ds.SeriesDescription = "Tiny localizer"
        ds.Modality = "CT"
        ds.SeriesNumber = "1"
        ds.NumberOfSeriesRelatedInstances = num_instances
        ds.InstitutionName = "Hospital Alpha"
        return ds

    def _mock_ops(self, study_ds, series_ds, c_move_success=False):
        ops = MagicMock()
        ops.c_find_studies.return_value = [study_ds]
        ops.c_find_series.return_value = [series_ds]
        ops.c_find_local_series.return_value = []
        ops.c_move_series.return_value = (
            c_move_success,
            0 if not c_move_success
            else series_ds.NumberOfSeriesRelatedInstances)
        return ops

    def test_failing_transfer_records_failure_in_log(self):
        """A failed C-MOVE must call record_series_failure so the
        attempt count is persisted for the next cycle."""
        study = self._make_study_ds("1.2.3")
        series = self._make_series_ds("1.2.3.1")
        ops = self._mock_ops(study, series, c_move_success=False)

        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=ops):
            self.engine._run_one_cycle(hours=24, max_images=0)

        assert self.engine._transfer_log.get_series_failure_count(
            source_pacs="ct", series_uid="1.2.3.1") == 1

    def test_two_cycles_of_failure_increment_to_two(self):
        study = self._make_study_ds("1.2.3")
        series = self._make_series_ds("1.2.3.1")
        ops = self._mock_ops(study, series, c_move_success=False)

        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=ops):
            self.engine._run_one_cycle(hours=24, max_images=0)
            self.engine._run_one_cycle(hours=24, max_images=0)

        assert self.engine._transfer_log.get_series_failure_count(
            source_pacs="ct", series_uid="1.2.3.1") == 2

    def test_blacklisted_series_is_not_queued_on_third_cycle(self):
        """After 2 failures, a 3rd cycle must NOT build a SeriesJob
        for the same series — it should be filtered out during
        _build_study_jobs / _query_source."""
        study = self._make_study_ds("1.2.3")
        series = self._make_series_ds("1.2.3.1")
        ops = self._mock_ops(study, series, c_move_success=False)

        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=ops):
            self.engine._run_one_cycle(hours=24, max_images=0)
            self.engine._run_one_cycle(hours=24, max_images=0)
            ops.c_move_series.reset_mock()
            self.engine._run_one_cycle(hours=24, max_images=0)

        assert ops.c_move_series.call_count == 0, (
            "blacklisted series must not be retried")
        assert all(j.series_uid != "1.2.3.1"
                   for j in self.engine._queue)

    def test_blacklist_does_not_affect_other_series(self):
        """A blacklisted series must not prevent healthy siblings in
        the same study from being queued."""
        study = self._make_study_ds("1.2.3")
        bad = self._make_series_ds("1.2.3.bad", num_instances=3)
        good = self._make_series_ds("1.2.3.good", num_instances=300)

        ops = MagicMock()
        ops.c_find_studies.return_value = [study]
        ops.c_find_series.return_value = [bad, good]
        ops.c_find_local_series.return_value = []

        def c_move_side_effect(study_uid, series_uid):
            if series_uid == "1.2.3.bad":
                return (False, 0)
            return (True, 300)
        ops.c_move_series.side_effect = c_move_side_effect

        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=ops):
            self.engine._run_one_cycle(hours=24, max_images=0)
            self.engine._run_one_cycle(hours=24, max_images=0)
            ops.c_move_series.reset_mock()
            self.engine._run_one_cycle(hours=24, max_images=0)

        attempted = [call.args[1]
                     for call in ops.c_move_series.call_args_list]
        assert "1.2.3.bad" not in attempted
        assert all(j.series_uid != "1.2.3.bad"
                   for j in self.engine._queue)

    def test_successful_transfer_clears_prior_failures(self):
        """If a series previously failed once and then succeeds, the
        failure counter resets so a later failure isn't inherited."""
        self.engine._transfer_log.record_series_failure(
            source_pacs="ct", series_uid="1.2.3.1")
        assert self.engine._transfer_log.get_series_failure_count(
            source_pacs="ct", series_uid="1.2.3.1") == 1

        study = self._make_study_ds("1.2.3")
        series = self._make_series_ds("1.2.3.1")
        ops = self._mock_ops(study, series, c_move_success=True)

        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=ops):
            self.engine._run_one_cycle(hours=24, max_images=0)

        assert self.engine._transfer_log.get_series_failure_count(
            source_pacs="ct", series_uid="1.2.3.1") == 0

    def test_preseeded_blacklist_skips_first_cycle(self):
        """If the DB already says this series has 2 failures from a
        previous app session, the very first cycle after restart must
        not re-query it for transfer."""
        self.engine._transfer_log.record_series_failure(
            source_pacs="ct", series_uid="1.2.3.1")
        self.engine._transfer_log.record_series_failure(
            source_pacs="ct", series_uid="1.2.3.1")

        study = self._make_study_ds("1.2.3")
        series = self._make_series_ds("1.2.3.1")
        ops = self._mock_ops(study, series, c_move_success=False)

        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=ops):
            self.engine._run_one_cycle(hours=24, max_images=0)

        assert ops.c_move_series.call_count == 0
        assert all(j.series_uid != "1.2.3.1"
                   for j in self.engine._queue)

    def test_study_with_blacklisted_series_still_reaches_fully_complete(self):
        """A study with one blacklisted and one healthy series must
        still emit study_completed(fully_complete=True). The
        blacklisted series is excluded from 'total remote series' —
        otherwise the live completions window would silently drop
        every study that ever contained a tiny-and-stuck series."""
        study = self._make_study_ds("1.2.3")
        bad = self._make_series_ds("1.2.3.bad", num_instances=3)
        good = self._make_series_ds("1.2.3.good", num_instances=300)

        # Pre-blacklist "bad" so we can test the positive path in a
        # single cycle rather than orchestrating 3 cycles.
        self.engine._transfer_log.record_series_failure(
            source_pacs="ct", series_uid="1.2.3.bad")
        self.engine._transfer_log.record_series_failure(
            source_pacs="ct", series_uid="1.2.3.bad")

        ops = MagicMock()
        ops.c_find_studies.return_value = [study]
        ops.c_find_series.return_value = [bad, good]
        ops.c_find_local_series.return_value = []
        ops.c_move_series.return_value = (True, 300)

        received = []
        self.engine.signals.study_completed.connect(
            lambda uid, inst, full: received.append((uid, full)))

        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=ops):
            self.engine._run_one_cycle(hours=24, max_images=0)

        assert ("1.2.3", True) in received, (
            "study with a blacklisted series must still fire "
            "study_completed with fully_complete=True")


# ═══════════════════════════════════════════════════════════════════════════
# TransferEngine — _fetch_local_series_counts must not silently swallow errors
# ═══════════════════════════════════════════════════════════════════════════

class TestFetchLocalSeriesCounts:
    """A failing local PACS query (timeout, AE rejection, network blip)
    must be logged. Silently returning an empty dict makes the engine
    think nothing is locally stored and re-download the whole study
    every cycle — wasting bandwidth without any user-visible signal."""

    def test_returns_empty_dict_on_exception(self, caplog):
        ops = MagicMock()
        ops.c_find_local_series.side_effect = RuntimeError("timeout")
        result = TransferEngine._fetch_local_series_counts(
            ops, study_uid="1.2.3")
        assert result == {}

    def test_logs_warning_on_exception(self, caplog):
        import logging
        ops = MagicMock()
        ops.c_find_local_series.side_effect = RuntimeError(
            "AE rejected association")
        with caplog.at_level(logging.WARNING, logger="dicom_sync"):
            TransferEngine._fetch_local_series_counts(
                ops, study_uid="1.2.3.4")
        assert any("Local PACS query failed" in rec.message
                   for rec in caplog.records), (
            "_fetch_local_series_counts must log a WARNING when the "
            "local query fails — otherwise re-downloads happen silently")
        # Study UID should appear in the log so the user can pinpoint
        # which study failed.
        assert any("1.2.3.4" in rec.message for rec in caplog.records)

    def test_returns_counts_on_success(self):
        ops = MagicMock()
        s1 = MagicMock()
        s1.SeriesInstanceUID = "1.2.3.1"
        s1.NumberOfSeriesRelatedInstances = 50
        s2 = MagicMock()
        s2.SeriesInstanceUID = "1.2.3.2"
        s2.NumberOfSeriesRelatedInstances = 100
        ops.c_find_local_series.return_value = [s1, s2]
        result = TransferEngine._fetch_local_series_counts(
            ops, study_uid="1.2.3")
        assert result == {"1.2.3.1": 50, "1.2.3.2": 100}


# ═══════════════════════════════════════════════════════════════════════════
# TransferEngine — one DicomOperations (AE) per cycle, not per series
# ═══════════════════════════════════════════════════════════════════════════

class TestSingleAEPerCycle:
    """The engine must reuse a single DicomOperations (= one pynetdicom AE)
    across all cycles.  Creating fresh AEs causes a segfault: the AE's
    reactor threads outlive the GC'd object and crash in mark_stacks."""

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, qapp, tmp_path):
        from core.transfer_log import TransferLog
        self.config = populated_config
        self.config.filter_groups_enabled = False
        self.engine = TransferEngine(self.config, "ct")
        self.engine._transfer_log.close()
        self.engine._transfer_log = TransferLog(
            str(tmp_path / "transfer_log.sqlite"))

    def _make_study_ds(self):
        from datetime import datetime
        now = datetime.now()
        ds = MagicMock()
        ds.StudyInstanceUID = "1.2.3"
        ds.StudyDate = now.strftime("%Y%m%d")
        ds.StudyTime = now.strftime("%H%M%S")
        ds.PatientName = "Doe^John"
        ds.PatientID = "P1"
        ds.StudyDescription = "CT Head"
        ds.InstitutionName = "Hospital"
        ds.AccessionNumber = "ACC1"
        return ds

    def _make_series_ds(self, series_uid, num=100):
        ds = MagicMock()
        ds.SeriesInstanceUID = series_uid
        ds.SeriesDescription = "Axial"
        ds.Modality = "CT"
        ds.SeriesNumber = "1"
        ds.NumberOfSeriesRelatedInstances = num
        ds.InstitutionName = "Hospital"
        return ds

    def test_make_dicom_ops_called_once_across_cycles(self):
        """_make_dicom_ops must be called exactly once even across
        multiple cycles — the AE is reused for the engine's lifetime."""
        study = self._make_study_ds()
        s1 = self._make_series_ds("1.2.3.1")
        s2 = self._make_series_ds("1.2.3.2")
        s3 = self._make_series_ds("1.2.3.3")

        ops = MagicMock()
        ops.c_find_studies.return_value = [study]
        ops.c_find_series.return_value = [s1, s2, s3]
        ops.c_find_local_series.return_value = []
        ops.c_move_series.return_value = (True, 100)

        with patch.object(self.engine, '_make_dicom_ops',
                          return_value=ops) as mock_make:
            self.engine._run_one_cycle(hours=24, max_images=0)
            self.engine._run_one_cycle(hours=24, max_images=0)

        assert mock_make.call_count == 1, (
            f"_make_dicom_ops called {mock_make.call_count} times "
            f"but must be called exactly once — reactor threads from "
            f"GC'd AEs cause a segfault in mark_stacks")
        # All 3 series should have been transferred in each cycle
        assert ops.c_move_series.call_count == 6


# ═══════════════════════════════════════════════════════════════════════════
# TransferEngine — priority series ordering
# ═══════════════════════════════════════════════════════════════════════════

class TestPrioritySeriesOrdering:
    """``_apply_priority_ordering`` reorders the per-cycle job list so
    studies whose series descriptions match a configured term float to
    the top, in the order the terms appear in
    ``PacsNode.priority_series_terms``."""

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, qapp):
        self.config = populated_config
        self.engine = TransferEngine(self.config, "ct")
        # Tests configure priority_series_terms directly per case so
        # they are independent of the default-population logic.
        self.config.remote_nodes["ct"].priority_series_terms = []

    def _make_job(self, study_uid, series_uid, series_description,
                  remote_count=100):
        return SeriesJob(
            study_uid=study_uid,
            series_uid=series_uid,
            series_description=series_description,
            patient_name="P",
            patient_id="PID",
            modality="CT",
            remote_count=remote_count,
        )

    def test_empty_priority_list_keeps_original_order(self):
        """Regression sentinel: with no terms configured, ordering is
        a no-op (pre-1.0.12 behaviour)."""
        self.config.remote_nodes["ct"].priority_series_terms = []
        jobs = [
            self._make_job("S1", "S1.1", "Axial"),
            self._make_job("S2", "S2.1", "CCT brain"),
            self._make_job("S3", "S3.1", "Sagittal"),
        ]
        out = self.engine._apply_priority_ordering(list(jobs))
        assert [j.series_uid for j in out] == ["S1.1", "S2.1", "S3.1"]

    def test_study_with_matching_series_floats_above_unmatched(self):
        self.config.remote_nodes["ct"].priority_series_terms = [
            {"term": "cct", "is_regex": False},
        ]
        jobs = [
            self._make_job("S1", "S1.1", "Axial"),
            self._make_job("S2", "S2.1", "CCT brain"),
            self._make_job("S3", "S3.1", "Sagittal"),
        ]
        out = self.engine._apply_priority_ordering(list(jobs))
        # Matched study S2 comes first; the rest preserve order.
        assert [j.study_uid for j in out] == ["S2", "S1", "S3"]

    def test_priority_order_within_matched_studies(self):
        """``cct`` (index 0) outranks ``perfusion`` (index 5)."""
        self.config.remote_nodes["ct"].priority_series_terms = [
            {"term": "cct", "is_regex": False},
            {"term": "cta", "is_regex": False},
            {"term": "ct-a", "is_regex": False},
            {"term": "angio", "is_regex": False},
            {"term": "nevas", "is_regex": False},
            {"term": "perfusion", "is_regex": False},
        ]
        jobs = [
            self._make_job("S1", "S1.1", "Perfusion map"),
            self._make_job("S2", "S2.1", "Axial"),
            self._make_job("S3", "S3.1", "CCT brain"),
        ]
        out = self.engine._apply_priority_ordering(list(jobs))
        assert [j.study_uid for j in out] == ["S3", "S1", "S2"]

    def test_unmatched_studies_preserve_relative_order_at_end(self):
        self.config.remote_nodes["ct"].priority_series_terms = [
            {"term": "cct", "is_regex": False},
        ]
        jobs = [
            self._make_job("S1", "S1.1", "Axial"),
            self._make_job("S2", "S2.1", "Sagittal"),
            self._make_job("S3", "S3.1", "Coronal"),
        ]
        out = self.engine._apply_priority_ordering(list(jobs))
        # No matches → list returns unchanged.
        assert [j.study_uid for j in out] == ["S1", "S2", "S3"]

    def test_regex_entry_matches_when_is_regex_true(self):
        self.config.remote_nodes["ct"].priority_series_terms = [
            {"term": r"^CT[AP]\b", "is_regex": True},
        ]
        jobs = [
            self._make_job("S1", "S1.1", "Axial"),
            self._make_job("S2", "S2.1", "CTA Carotis"),   # match
            self._make_job("S3", "S3.1", "Some CTAxial"),  # no match (no boundary)
        ]
        out = self.engine._apply_priority_ordering(list(jobs))
        assert out[0].study_uid == "S2"

    def test_substring_match_is_case_insensitive(self):
        self.config.remote_nodes["ct"].priority_series_terms = [
            {"term": "ANGIO", "is_regex": False},
        ]
        jobs = [
            self._make_job("S1", "S1.1", "Axial"),
            self._make_job("S2", "S2.1", "carotid angio max"),
        ]
        out = self.engine._apply_priority_ordering(list(jobs))
        assert out[0].study_uid == "S2"

    def test_all_series_of_priority_study_stay_grouped_together(self):
        """One series of study X matches; the other two don't → all
        three still ride together at the top, contiguous."""
        self.config.remote_nodes["ct"].priority_series_terms = [
            {"term": "cct", "is_regex": False},
        ]
        jobs = [
            self._make_job("S0", "S0.1", "Axial"),       # unmatched
            self._make_job("SX", "SX.1", "Localizer"),   # study X, no match
            self._make_job("SX", "SX.2", "CCT brain"),   # study X, MATCH
            self._make_job("SX", "SX.3", "Bone window"), # study X, no match
            self._make_job("SY", "SY.1", "Coronal"),     # unmatched
        ]
        out = self.engine._apply_priority_ordering(list(jobs))
        # All three SX series come first; SX stays internally ordered.
        assert [j.series_uid for j in out[:3]] == [
            "SX.1", "SX.2", "SX.3"]
        # Unmatched studies follow in original order.
        assert [j.study_uid for j in out[3:]] == ["S0", "SY"]

    def test_engine_reads_from_its_own_remote_node(self):
        """The ``mri`` engine must NOT honour the ``ct`` engine's
        priority list — each engine reads its own PacsNode."""
        self.config.remote_nodes["ct"].priority_series_terms = [
            {"term": "cct", "is_regex": False},
        ]
        self.config.remote_nodes["mri"].priority_series_terms = []
        mri_engine = TransferEngine(self.config, "mri")
        jobs = [
            self._make_job("S1", "S1.1", "Axial"),
            self._make_job("S2", "S2.1", "CCT brain"),
        ]
        out = mri_engine._apply_priority_ordering(list(jobs))
        # No reordering on mri engine.
        assert [j.study_uid for j in out] == ["S1", "S2"]
