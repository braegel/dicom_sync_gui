"""
Tests for core.transfer_log — SQLite-based transfer performance logging.

The transfer log records per-series and per-study transfer metrics for
regulatory compliance documentation (StrlSchV / DIN 6868-159).
Patient-identifiable fields (PatientID, AccessionNumber) are stored as
SHA-256 hashes so that a specific examination can be traced back when
the original identifiers are known, without storing PII locally.
"""

import hashlib
import os
import sqlite3
import statistics
import threading
import time
from datetime import datetime

import pytest

from core.transfer_log import (
    TransferLog,
    MODALITY_BYTES_PER_IMAGE,
    estimate_bytes,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _sha256(value: str) -> str:
    """Reproduce the hash the production code should use."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "transfer_log.sqlite")


@pytest.fixture
def log(db_path):
    tl = TransferLog(db_path)
    yield tl
    tl.close()


@pytest.fixture
def sample_series_kwargs():
    """Minimal kwargs for recording a series transfer."""
    return dict(
        source_pacs="ct_scanner",
        study_uid="1.2.3.4",
        series_uid="1.2.3.4.5",
        patient_id="PAT001",
        accession_number="ACC001",
        study_date="20260405",
        study_time="143000",
        modality="CT",
        study_description="CT Abdomen",
        series_description="Axial 5mm",
        series_number="3",
        image_count=350,
        duration_seconds=45.0,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Database creation and schema
# ═══════════════════════════════════════════════════════════════════════════

class TestSchema:

    def test_creates_database_file(self, db_path):
        tl = TransferLog(db_path)
        tl.close()
        assert os.path.exists(db_path)

    def test_creates_series_table(self, log, db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='series_transfer'")
        assert cursor.fetchone() is not None
        conn.close()

    def test_creates_study_table(self, log, db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='study_transfer'")
        assert cursor.fetchone() is not None
        conn.close()

    def test_series_table_columns(self, log, db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(series_transfer)")
        columns = {row[1] for row in cursor.fetchall()}
        expected = {
            "id", "timestamp", "source_pacs",
            "study_uid_hash", "series_uid_hash",
            "patient_id_hash", "accession_number_hash",
            "study_date", "study_time",
            "modality", "study_description", "series_description",
            "series_number",
            "image_count", "duration_seconds", "images_per_minute",
            "estimated_bytes", "estimated_mbps",
        }
        conn.close()
        assert expected.issubset(columns)

    def test_study_table_columns(self, log, db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(study_transfer)")
        columns = {row[1] for row in cursor.fetchall()}
        expected = {
            "id", "timestamp", "source_pacs",
            "study_uid_hash", "patient_id_hash", "accession_number_hash",
            "study_date", "study_time",
            "modality", "study_description",
            "total_series", "total_images",
            "total_duration_seconds", "wall_clock_seconds",
            "total_estimated_bytes", "estimated_mbps",
        }
        conn.close()
        assert expected.issubset(columns)

    def test_reopen_existing_db(self, db_path):
        """Opening an existing DB does not fail or lose data."""
        tl1 = TransferLog(db_path)
        tl1.record_series(
            source_pacs="x", study_uid="1", series_uid="2",
            patient_id="P", accession_number="A",
            study_date="20260101", study_time="120000",
            modality="CT", study_description="Test",
            series_description="Axial", series_number="1",
            image_count=100, duration_seconds=10.0,
        )
        tl1.close()

        tl2 = TransferLog(db_path)
        rows = tl2.query_series()
        tl2.close()
        assert len(rows) == 1

    def test_creates_parent_directories(self, tmp_path):
        """TransferLog creates intermediate directories if needed."""
        nested = str(tmp_path / "sub" / "dir" / "transfer_log.sqlite")
        tl = TransferLog(nested)
        tl.close()
        assert os.path.exists(nested)

    def test_insert_rejects_bool_for_int_column(self, log):
        """``isinstance(True, int)`` is True in Python — without an
        explicit bool guard a caller bug like ``image_count=True``
        would silently land as ``1`` in the int column.  The schema
        check must reject it instead."""
        with pytest.raises(TypeError, match="image_count"):
            log.record_series(
                source_pacs="x", study_uid="1", series_uid="2",
                patient_id="P", accession_number="A",
                study_date="20260101", study_time="120000",
                modality="CT", study_description="Test",
                series_description="Axial", series_number="1",
                image_count=True,  # accidental bool — must raise
                duration_seconds=10.0,
            )


# ═══════════════════════════════════════════════════════════════════════════
# Byte estimation
# ═══════════════════════════════════════════════════════════════════════════

class TestByteEstimation:

    def test_ct_estimate(self):
        """CT: 512x512x2 = 524288 bytes per image."""
        result = estimate_bytes("CT", 100)
        assert result == 100 * MODALITY_BYTES_PER_IMAGE["CT"]

    def test_mr_estimate(self):
        result = estimate_bytes("MR", 200)
        assert result == 200 * MODALITY_BYTES_PER_IMAGE["MR"]

    def test_cr_estimate(self):
        result = estimate_bytes("CR", 2)
        assert result == 2 * MODALITY_BYTES_PER_IMAGE["CR"]

    def test_dx_estimate(self):
        result = estimate_bytes("DX", 1)
        assert result == MODALITY_BYTES_PER_IMAGE["DX"]

    def test_unknown_modality_uses_fallback(self):
        """Unknown modalities should use a sensible default."""
        result = estimate_bytes("XZ_UNKNOWN", 10)
        assert result > 0

    def test_zero_images(self):
        assert estimate_bytes("CT", 0) == 0

    def test_us_estimate(self):
        result = estimate_bytes("US", 50)
        assert result == 50 * MODALITY_BYTES_PER_IMAGE["US"]

    def test_pt_nm_modalities(self):
        """Nuclear medicine modalities should have estimates."""
        for mod in ("PT", "NM"):
            assert estimate_bytes(mod, 10) > 0


# ═══════════════════════════════════════════════════════════════════════════
# Recording series transfers
# ═══════════════════════════════════════════════════════════════════════════

class TestRecordSeries:

    def test_record_inserts_row(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        rows = log.query_series()
        assert len(rows) == 1

    def test_patient_id_is_hashed(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        rows = log.query_series()
        row = rows[0]
        assert row["patient_id_hash"] == _sha256("PAT001")
        assert "PAT001" not in str(row.values())

    def test_accession_number_is_hashed(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        rows = log.query_series()
        row = rows[0]
        assert row["accession_number_hash"] == _sha256("ACC001")

    def test_study_uid_is_hashed(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        rows = log.query_series()
        assert rows[0]["study_uid_hash"] == _sha256("1.2.3.4")

    def test_series_uid_is_hashed(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        rows = log.query_series()
        assert rows[0]["series_uid_hash"] == _sha256("1.2.3.4.5")

    def test_cleartext_fields_preserved(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        row = log.query_series()[0]
        assert row["source_pacs"] == "ct_scanner"
        assert row["study_date"] == "20260405"
        assert row["study_time"] == "143000"
        assert row["modality"] == "CT"
        assert row["study_description"] == "CT Abdomen"
        assert row["series_description"] == "Axial 5mm"
        assert row["series_number"] == "3"
        assert row["image_count"] == 350

    def test_duration_and_speed(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        row = log.query_series()[0]
        assert row["duration_seconds"] == pytest.approx(45.0)
        assert row["images_per_minute"] == pytest.approx((350 / 45.0) * 60)

    def test_estimated_bytes_computed(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        row = log.query_series()[0]
        expected_bytes = estimate_bytes("CT", 350)
        assert row["estimated_bytes"] == expected_bytes
        assert row["estimated_bytes"] > 0

    def test_estimated_mbps_computed(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        row = log.query_series()[0]
        expected_bytes = estimate_bytes("CT", 350)
        expected_mbps = (expected_bytes * 8) / (45.0 * 1_000_000)
        assert row["estimated_mbps"] == pytest.approx(expected_mbps, rel=0.01)

    def test_timestamp_is_iso_format(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        row = log.query_series()[0]
        # Should parse without error
        dt = datetime.fromisoformat(row["timestamp"])
        assert dt.year >= 2026

    def test_multiple_records(self, log, sample_series_kwargs):
        for i in range(5):
            kw = {**sample_series_kwargs, "series_uid": f"1.2.3.4.{i}"}
            log.record_series(**kw)
        assert len(log.query_series()) == 5

    def test_zero_duration_no_division_error(self, log, sample_series_kwargs):
        sample_series_kwargs["duration_seconds"] = 0.0
        log.record_series(**sample_series_kwargs)
        row = log.query_series()[0]
        assert row["images_per_minute"] == 0.0
        assert row["estimated_mbps"] == 0.0

    def test_empty_accession_hashed(self, log, sample_series_kwargs):
        """Even empty accession numbers are hashed (not stored as empty)."""
        sample_series_kwargs["accession_number"] = ""
        log.record_series(**sample_series_kwargs)
        row = log.query_series()[0]
        assert row["accession_number_hash"] == _sha256("")


# ═══════════════════════════════════════════════════════════════════════════
# Recording study transfers
# ═══════════════════════════════════════════════════════════════════════════

class TestRecordStudy:

    def test_record_study_inserts_row(self, log):
        log.record_study(
            source_pacs="ct",
            study_uid="1.2.3.4",
            patient_id="PAT001",
            accession_number="ACC001",
            study_date="20260405",
            study_time="143000",
            modality="CT",
            study_description="CT Abdomen",
            total_series=3,
            total_images=900,
            total_duration_seconds=120.0,
            wall_clock_seconds=130.0,
        )
        rows = log.query_studies()
        assert len(rows) == 1

    def test_study_patient_id_hashed(self, log):
        log.record_study(
            source_pacs="ct", study_uid="1.2.3",
            patient_id="PAT999", accession_number="ACC999",
            study_date="20260405", study_time="120000",
            modality="CT", study_description="Test",
            total_series=1, total_images=100,
            total_duration_seconds=10.0, wall_clock_seconds=12.0,
        )
        row = log.query_studies()[0]
        assert row["patient_id_hash"] == _sha256("PAT999")

    def test_study_estimated_bytes(self, log):
        log.record_study(
            source_pacs="ct", study_uid="1.2.3",
            patient_id="P", accession_number="A",
            study_date="20260405", study_time="120000",
            modality="CT", study_description="Test",
            total_series=2, total_images=500,
            total_duration_seconds=60.0, wall_clock_seconds=65.0,
        )
        row = log.query_studies()[0]
        assert row["total_estimated_bytes"] == estimate_bytes("CT", 500)

    def test_study_wall_clock_vs_total_duration(self, log):
        """wall_clock >= total_duration (includes gaps between series)."""
        log.record_study(
            source_pacs="ct", study_uid="1.2.3",
            patient_id="P", accession_number="A",
            study_date="20260405", study_time="120000",
            modality="CT", study_description="Test",
            total_series=3, total_images=600,
            total_duration_seconds=90.0, wall_clock_seconds=110.0,
        )
        row = log.query_studies()[0]
        assert row["wall_clock_seconds"] == pytest.approx(110.0)
        assert row["total_duration_seconds"] == pytest.approx(90.0)

    def test_study_mbps_uses_wall_clock(self, log):
        """Bandwidth estimate should be based on wall-clock time."""
        log.record_study(
            source_pacs="ct", study_uid="1.2.3",
            patient_id="P", accession_number="A",
            study_date="20260405", study_time="120000",
            modality="CT", study_description="Test",
            total_series=1, total_images=200,
            total_duration_seconds=30.0, wall_clock_seconds=35.0,
        )
        row = log.query_studies()[0]
        expected_bytes = estimate_bytes("CT", 200)
        expected_mbps = (expected_bytes * 8) / (35.0 * 1_000_000)
        assert row["estimated_mbps"] == pytest.approx(expected_mbps, rel=0.01)


# ═══════════════════════════════════════════════════════════════════════════
# Querying and filtering
# ═══════════════════════════════════════════════════════════════════════════

class TestQuerying:

    def _insert_series(self, log, study_date, modality="CT", source="ct"):
        log.record_series(
            source_pacs=source, study_uid="1.2.3", series_uid=f"1.2.3.{time.time()}",
            patient_id="P", accession_number="A",
            study_date=study_date, study_time="120000",
            modality=modality, study_description="Test",
            series_description="Axial", series_number="1",
            image_count=100, duration_seconds=10.0,
        )

    def test_query_series_by_date_range(self, log):
        self._insert_series(log, "20260401")
        self._insert_series(log, "20260405")
        self._insert_series(log, "20260410")
        rows = log.query_series(date_from="20260404", date_to="20260406")
        assert len(rows) == 1
        assert rows[0]["study_date"] == "20260405"

    def test_query_series_by_source(self, log):
        self._insert_series(log, "20260405", source="ct")
        self._insert_series(log, "20260405", source="mri")
        rows = log.query_series(source_pacs="ct")
        assert len(rows) == 1

    def test_query_series_by_modality(self, log):
        self._insert_series(log, "20260405", modality="CT")
        self._insert_series(log, "20260405", modality="MR")
        rows = log.query_series(modality="CT")
        assert len(rows) == 1

    def test_query_series_by_patient_hash(self, log):
        log.record_series(
            source_pacs="ct", study_uid="1", series_uid="1.1",
            patient_id="PAT_A", accession_number="A1",
            study_date="20260405", study_time="120000",
            modality="CT", study_description="Test",
            series_description="Axial", series_number="1",
            image_count=100, duration_seconds=10.0,
        )
        log.record_series(
            source_pacs="ct", study_uid="2", series_uid="2.1",
            patient_id="PAT_B", accession_number="A2",
            study_date="20260405", study_time="130000",
            modality="CT", study_description="Test",
            series_description="Axial", series_number="1",
            image_count=100, duration_seconds=10.0,
        )
        rows = log.query_series(patient_id="PAT_A")
        assert len(rows) == 1
        assert rows[0]["patient_id_hash"] == _sha256("PAT_A")

    def test_query_series_by_accession(self, log):
        log.record_series(
            source_pacs="ct", study_uid="1", series_uid="1.1",
            patient_id="P", accession_number="ACC_TARGET",
            study_date="20260405", study_time="120000",
            modality="CT", study_description="Test",
            series_description="Axial", series_number="1",
            image_count=100, duration_seconds=10.0,
        )
        log.record_series(
            source_pacs="ct", study_uid="2", series_uid="2.1",
            patient_id="P", accession_number="ACC_OTHER",
            study_date="20260405", study_time="130000",
            modality="CT", study_description="Test",
            series_description="Axial", series_number="1",
            image_count=100, duration_seconds=10.0,
        )
        rows = log.query_series(accession_number="ACC_TARGET")
        assert len(rows) == 1

    def test_query_returns_dicts(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        rows = log.query_series()
        assert isinstance(rows, list)
        assert isinstance(rows[0], dict)

    def test_query_studies_by_date(self, log):
        log.record_study(
            source_pacs="ct", study_uid="1", patient_id="P",
            accession_number="A", study_date="20260401",
            study_time="120000", modality="CT",
            study_description="Test", total_series=1,
            total_images=100, total_duration_seconds=10.0,
            wall_clock_seconds=12.0,
        )
        log.record_study(
            source_pacs="ct", study_uid="2", patient_id="P",
            accession_number="A", study_date="20260410",
            study_time="120000", modality="CT",
            study_description="Test", total_series=1,
            total_images=100, total_duration_seconds=10.0,
            wall_clock_seconds=12.0,
        )
        rows = log.query_studies(date_from="20260405")
        assert len(rows) == 1
        assert rows[0]["study_date"] == "20260410"

    def test_query_empty_db(self, log):
        assert log.query_series() == []
        assert log.query_studies() == []

    def test_query_combined_filters(self, log):
        """Multiple filters are AND-combined."""
        self._insert_series(log, "20260405", modality="CT", source="ct")
        self._insert_series(log, "20260405", modality="MR", source="ct")
        self._insert_series(log, "20260405", modality="CT", source="mri")
        rows = log.query_series(modality="CT", source_pacs="ct")
        assert len(rows) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Hash reproducibility (for tracing back examinations)
# ═══════════════════════════════════════════════════════════════════════════

class TestHashReproducibility:

    def test_same_input_same_hash(self, log, sample_series_kwargs):
        """Recording the same patient_id twice produces the same hash."""
        log.record_series(**sample_series_kwargs)
        kw2 = {**sample_series_kwargs, "series_uid": "1.2.3.4.99"}
        log.record_series(**kw2)
        rows = log.query_series()
        assert rows[0]["patient_id_hash"] == rows[1]["patient_id_hash"]

    def test_different_input_different_hash(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        kw2 = {**sample_series_kwargs,
               "patient_id": "PAT002", "series_uid": "1.2.3.4.99"}
        log.record_series(**kw2)
        rows = log.query_series()
        assert rows[0]["patient_id_hash"] != rows[1]["patient_id_hash"]

    def test_hash_is_sha256_hex(self, log, sample_series_kwargs):
        log.record_series(**sample_series_kwargs)
        row = log.query_series()[0]
        h = row["patient_id_hash"]
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_can_find_by_known_patient_id(self, log, sample_series_kwargs):
        """Given the original PatientID, the hash can be reproduced to query."""
        log.record_series(**sample_series_kwargs)
        target_hash = _sha256("PAT001")
        rows = log.query_series(patient_id="PAT001")
        assert len(rows) == 1
        assert rows[0]["patient_id_hash"] == target_hash


# ═══════════════════════════════════════════════════════════════════════════
# DB path resolution (platform-specific)
# ═══════════════════════════════════════════════════════════════════════════

class TestDefaultDbPath:

    def test_default_path_function_exists(self):
        from core.transfer_log import default_db_path
        path = default_db_path()
        assert path.endswith("transfer_log.sqlite")

    def test_default_path_is_absolute(self):
        from core.transfer_log import default_db_path
        assert os.path.isabs(default_db_path())

    def test_default_path_platform_appropriate(self):
        """On macOS should be in ~/Library/Logs/, on Linux in state dir."""
        import platform as plat
        from core.transfer_log import default_db_path
        path = default_db_path()
        system = plat.system()
        if system == "Darwin":
            assert "Library/Logs" in path
        elif system == "Linux":
            assert ".local/state" in path or "XDG_STATE_HOME" in os.environ


# ═══════════════════════════════════════════════════════════════════════════
# Thread safety
# ═══════════════════════════════════════════════════════════════════════════

class TestThreadSafety:

    def test_concurrent_writes(self, log, sample_series_kwargs):
        """Multiple threads recording simultaneously should not lose data."""
        errors = []

        def worker(idx):
            try:
                kw = {**sample_series_kwargs,
                      "series_uid": f"1.2.3.4.{idx}"}
                log.record_series(**kw)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(log.query_series()) == 20


# ═══════════════════════════════════════════════════════════════════════════
# Export-friendliness (the data should be usable in other systems)
# ═══════════════════════════════════════════════════════════════════════════

class TestExportFriendliness:

    def test_raw_sql_access(self, log, db_path, sample_series_kwargs):
        """DB can be opened with plain sqlite3 and queried."""
        log.record_series(**sample_series_kwargs)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM series_transfer").fetchall()
        conn.close()
        assert len(rows) == 1
        assert dict(rows[0])["modality"] == "CT"

    def test_study_aggregation_via_sql(self, log, db_path):
        """Series-level data can be aggregated to study-level via SQL."""
        for i in range(3):
            log.record_series(
                source_pacs="ct", study_uid="1.2.3",
                series_uid=f"1.2.3.{i}",
                patient_id="P", accession_number="A",
                study_date="20260405", study_time="120000",
                modality="CT", study_description="CT Abdomen",
                series_description=f"Series {i}", series_number=str(i),
                image_count=100, duration_seconds=10.0,
            )
        conn = sqlite3.connect(db_path)
        row = conn.execute("""
            SELECT study_uid_hash,
                   COUNT(*) as num_series,
                   SUM(image_count) as total_images,
                   SUM(duration_seconds) as total_duration,
                   SUM(estimated_bytes) as total_bytes
            FROM series_transfer
            GROUP BY study_uid_hash
        """).fetchone()
        conn.close()
        assert row[1] == 3   # num_series
        assert row[2] == 300  # total_images


# ═══════════════════════════════════════════════════════════════════════════
# mbps_stats — aggregate throughput statistics for the lookup dialog
# ═══════════════════════════════════════════════════════════════════════════

def _expected_mbps(image_count: int, duration_seconds: float,
                   modality: str = "CT") -> float:
    """Reproduce record_series' estimated_mbps formula."""
    return (estimate_bytes(modality, image_count) * 8
            / (duration_seconds * 1_000_000))


class TestMbpsStats:
    """``mbps_stats`` returns {count, mean, stddev, median} over all
    series rows with estimated_mbps > 0, or None below 2 rows.

    The median MUST follow the project-wide convention: for an even
    number of rows, AVERAGE the two middle values (review item #26 —
    the old implementation streamed only the upper middle row)."""

    def _record(self, log, sample_series_kwargs, *durations):
        """Record one series per duration (fixed 100-image CT series
        so estimated_mbps is fully determined by the duration)."""
        for i, dur in enumerate(durations):
            kw = dict(sample_series_kwargs)
            kw["series_uid"] = f"1.2.3.4.{i}"
            kw["image_count"] = 100
            kw["duration_seconds"] = dur
            log.record_series(**kw)

    def test_none_when_empty(self, log):
        assert log.mbps_stats() is None

    def test_none_with_single_row(self, log, sample_series_kwargs):
        self._record(log, sample_series_kwargs, 10.0)
        assert log.mbps_stats() is None

    def test_odd_row_count_takes_middle(self, log, sample_series_kwargs):
        # Durations 40 / 20 / 10 s → ascending mbps for 40 / 20 / 10.
        self._record(log, sample_series_kwargs, 40.0, 20.0, 10.0)
        stats = log.mbps_stats()
        assert stats is not None
        assert stats["count"] == 3
        assert stats["median"] == pytest.approx(
            _expected_mbps(100, 20.0))

    def test_even_row_count_averages_middle_pair(
            self, log, sample_series_kwargs):
        """Review item #26: even n must average the two middle rows,
        not return the upper one."""
        # Durations 40 / 20 / 10 / 5 s → 4 distinct ascending mbps
        # values; the median is the average of the 20 s and 10 s rows.
        self._record(log, sample_series_kwargs, 40.0, 20.0, 10.0, 5.0)
        stats = log.mbps_stats()
        assert stats is not None
        assert stats["count"] == 4
        lower_mid = _expected_mbps(100, 20.0)
        upper_mid = _expected_mbps(100, 10.0)
        expected = (lower_mid + upper_mid) / 2
        assert stats["median"] == pytest.approx(expected)
        # Guard against a regression to the old upper-middle pick.
        assert stats["median"] != pytest.approx(upper_mid)

    def test_zero_mbps_rows_excluded(self, log, sample_series_kwargs):
        """duration_seconds=0 rows get estimated_mbps=0 and must not
        enter the statistics."""
        self._record(log, sample_series_kwargs, 40.0, 20.0, 10.0)
        kw = dict(sample_series_kwargs)
        kw["series_uid"] = "1.2.3.4.zero"
        kw["duration_seconds"] = 0.0
        log.record_series(**kw)
        stats = log.mbps_stats()
        assert stats is not None
        assert stats["count"] == 3
        assert stats["median"] == pytest.approx(
            _expected_mbps(100, 20.0))

    def test_mean_and_stddev(self, log, sample_series_kwargs):
        self._record(log, sample_series_kwargs, 40.0, 20.0, 10.0, 5.0)
        stats = log.mbps_stats()
        vals = [_expected_mbps(100, d) for d in (40.0, 20.0, 10.0, 5.0)]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)  # population
        assert stats["mean"] == pytest.approx(mean)
        assert stats["stddev"] == pytest.approx(var ** 0.5)

    def test_stddev_precise_for_large_mean_small_spread(
            self, log, sample_series_kwargs):
        """stddev must stay accurate when the spread is tiny compared to
        the mean (a fast link: ~1000 Mbps ± 0.5).

        The old one-pass form E[X²] − E[X]² subtracts two numbers around
        1e6 to get a variance around 0.1 — most of the mantissa cancels
        away, leaving ~1e-10 relative error (and, on unluckier data, a
        negative variance that only a max(…, 0.0) clamp hid).  The
        two-pass form averages the squared deviations instead, which
        never subtracts anything large, so it lands within a few ulps
        of statistics.pstdev.  Hence the deliberately tight rel=1e-12:
        a regression to the old formula fails here.
        """
        # 100-image CT series → 419.4304 Mb; pick durations that put the
        # resulting estimated_mbps at 999.5 … 1000.5 in 0.1 steps.
        total_mbits = estimate_bytes("CT", 100) * 8 / 1_000_000
        durations = [total_mbits / (1000.0 + 0.1 * k)
                     for k in range(-5, 6)]
        self._record(log, sample_series_kwargs, *durations)

        # Compare against the values SQLite actually stored, so the test
        # measures the aggregation and not our restatement of the mbps
        # formula.
        vals = [r["estimated_mbps"] for r in log.query_series()]
        assert len(vals) == len(durations)
        assert min(vals) > 999.0 and max(vals) < 1001.0

        stats = log.mbps_stats()
        assert stats["mean"] == pytest.approx(
            statistics.fmean(vals), rel=1e-12)
        assert stats["stddev"] == pytest.approx(
            statistics.pstdev(vals), rel=1e-12)

    def test_stddev_zero_for_identical_values(
            self, log, sample_series_kwargs):
        """Identical rows have zero spread — the variance must come out
        exactly 0.0, never a negative float that the clamp turns into a
        silent 0 (or, unclamped, a math-domain error in the sqrt)."""
        self._record(log, sample_series_kwargs, 0.4, 0.4, 0.4, 0.4)
        stats = log.mbps_stats()
        assert stats["stddev"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Series failure tracking — "stop retrying after N attempts"
# ═══════════════════════════════════════════════════════════════════════════

class TestSeriesFailureTracking:
    """Persist per-series failure counts so the engine can blacklist
    series that repeatedly fail to download (e.g. tiny series with 1-5
    images that the source PACS refuses to send). After 2 failed
    attempts the series must be treated as permanently dead."""

    def test_unknown_series_has_zero_attempts(self, log):
        assert log.get_series_failure_count(
            source_pacs="ct_scanner",
            series_uid="1.2.3.4.5") == 0

    def test_record_failure_sets_count_to_one(self, log):
        log.record_series_failure(
            source_pacs="ct_scanner",
            series_uid="1.2.3.4.5")
        assert log.get_series_failure_count(
            source_pacs="ct_scanner",
            series_uid="1.2.3.4.5") == 1

    def test_record_failure_increments(self, log):
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3.4.5")
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3.4.5")
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3.4.5")
        assert log.get_series_failure_count(
            source_pacs="ct_scanner",
            series_uid="1.2.3.4.5") == 3

    def test_failures_scoped_per_source_pacs(self, log):
        """Same series_uid at two PACS is counted independently."""
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        log.record_series_failure(
            source_pacs="mr_scanner", series_uid="1.2.3")
        log.record_series_failure(
            source_pacs="mr_scanner", series_uid="1.2.3")
        assert log.get_series_failure_count(
            source_pacs="ct_scanner", series_uid="1.2.3") == 1
        assert log.get_series_failure_count(
            source_pacs="mr_scanner", series_uid="1.2.3") == 2

    def test_is_series_blacklisted_default_threshold_is_three(self, log):
        """Blacklist after the 3rd failed attempt."""
        assert log.is_series_blacklisted(
            source_pacs="ct_scanner", series_uid="1.2.3") is False
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        assert log.is_series_blacklisted(
            source_pacs="ct_scanner", series_uid="1.2.3") is False
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        assert log.is_series_blacklisted(
            source_pacs="ct_scanner", series_uid="1.2.3") is False
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        assert log.is_series_blacklisted(
            source_pacs="ct_scanner", series_uid="1.2.3") is True

    def test_is_series_blacklisted_custom_threshold(self, log):
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        assert log.is_series_blacklisted(
            source_pacs="ct_scanner", series_uid="1.2.3",
            max_attempts=5) is False
        assert log.is_series_blacklisted(
            source_pacs="ct_scanner", series_uid="1.2.3",
            max_attempts=3) is True

    def test_clear_series_failures_resets_count(self, log):
        """Called after a successful transfer so a later failure
        doesn't inherit stale attempts."""
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        log.clear_series_failures(
            source_pacs="ct_scanner", series_uid="1.2.3")
        assert log.get_series_failure_count(
            source_pacs="ct_scanner", series_uid="1.2.3") == 0
        assert log.is_series_blacklisted(
            source_pacs="ct_scanner", series_uid="1.2.3") is False

    def test_failures_persist_across_reopen(self, db_path):
        """A restart of the app must not reset the blacklist."""
        tl1 = TransferLog(db_path)
        tl1.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        tl1.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3")
        tl1.close()

        tl2 = TransferLog(db_path)
        try:
            assert tl2.get_series_failure_count(
                source_pacs="ct_scanner", series_uid="1.2.3") == 2
            # Two failures is below the default threshold of 3, but a
            # third (re-)failure after reopen must tip it over —
            # proving the count persisted.
            tl2.record_series_failure(
                source_pacs="ct_scanner", series_uid="1.2.3")
            assert tl2.is_series_blacklisted(
                source_pacs="ct_scanner", series_uid="1.2.3") is True
        finally:
            tl2.close()

    def test_series_uid_stored_hashed(self, log, db_path):
        """Series UID must be stored as SHA-256 — not plaintext —
        to match the rest of the schema's PII handling."""
        log.record_series_failure(
            source_pacs="ct_scanner", series_uid="1.2.3.4.5")
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT * FROM series_failures").fetchall()
        conn.close()
        assert len(rows) == 1
        row_text = " ".join(str(v) for v in rows[0])
        assert "1.2.3.4.5" not in row_text, (
            "raw series_uid leaked into the failures table")


# ═══════════════════════════════════════════════════════════════════════════
# No-progress detection — "the C-MOVE succeeded but nothing arrived"
# ═══════════════════════════════════════════════════════════════════════════

class TestLocalProgressTracking:
    """A C-MOVE the source answers with SUCCESS is not proof the images
    landed in the local PACS.  OsiriX, for one, does not store incoming
    ROI/Annotation SR objects as queryable series — so the source keeps
    reporting N instances, the local SERIES query keeps reporting 0, and
    the engine re-fetched such a series every cycle forever.

    Verification therefore straddles two cycles: a success ARMS the
    check with the pre-transfer local count, and the next cycle's local
    query DISARMS it with the count observed then.
    """

    ARM = dict(source_pacs="ct_scanner", series_uid="1.2.3")

    def test_unarmed_progress_note_is_a_noop(self, log):
        """A series sitting in the queue that was never transferred must
        not accumulate attempts it never made."""
        log.note_local_progress(**self.ARM, local_count=0)
        log.note_local_progress(**self.ARM, local_count=0)
        assert log.get_series_failure_count(**self.ARM) == 0

    def test_arming_alone_costs_nothing(self, log):
        log.arm_local_progress_check(**self.ARM, local_count=0)
        assert log.get_series_failure_count(**self.ARM) == 0

    def test_local_count_grew_clears_the_streak(self, log):
        log.record_series_failure(**self.ARM)
        log.record_series_failure(**self.ARM)
        log.arm_local_progress_check(**self.ARM, local_count=0)
        log.note_local_progress(**self.ARM, local_count=64)
        assert log.get_series_failure_count(**self.ARM) == 0

    def test_local_count_unchanged_counts_as_an_attempt(self, log):
        log.arm_local_progress_check(**self.ARM, local_count=0)
        log.note_local_progress(**self.ARM, local_count=0)
        assert log.get_series_failure_count(**self.ARM) == 1

    def test_partial_arrival_still_counts_as_progress(self, log):
        """7 of 300 images is miserable, but it is movement — the series
        is not the pathological "local PACS drops it" case this guards
        against, so it must keep its retries."""
        log.arm_local_progress_check(**self.ARM, local_count=0)
        log.note_local_progress(**self.ARM, local_count=7)
        assert log.get_series_failure_count(**self.ARM) == 0

    def test_repeated_fruitless_transfers_reach_the_blacklist(self, log):
        for _ in range(3):
            log.arm_local_progress_check(**self.ARM, local_count=0)
            log.note_local_progress(**self.ARM, local_count=0)
        assert log.is_series_blacklisted(**self.ARM) is True

    def test_verdict_is_idempotent_within_a_cycle(self, log):
        """Disarming after the verdict means a second local query in the
        same cycle cannot double-count one attempt."""
        log.arm_local_progress_check(**self.ARM, local_count=0)
        log.note_local_progress(**self.ARM, local_count=0)
        log.note_local_progress(**self.ARM, local_count=0)
        log.note_local_progress(**self.ARM, local_count=0)
        assert log.get_series_failure_count(**self.ARM) == 1

    def test_outright_failure_disarms_a_pending_check(self, log):
        """An error already settled the attempt; the armed check must
        not bill the same round trip a second time next cycle."""
        log.arm_local_progress_check(**self.ARM, local_count=0)
        log.record_series_failure(**self.ARM)
        log.note_local_progress(**self.ARM, local_count=0)
        assert log.get_series_failure_count(**self.ARM) == 1

    def test_scoped_per_source_pacs(self, log):
        log.arm_local_progress_check(
            source_pacs="a", series_uid="1.2.3", local_count=0)
        log.note_local_progress(
            source_pacs="a", series_uid="1.2.3", local_count=0)
        assert log.get_series_failure_count(
            source_pacs="b", series_uid="1.2.3") == 0

    def test_no_armed_attempt_has_no_age(self, log):
        assert log.seconds_since_armed_attempt(**self.ARM) is None

    def test_armed_attempt_reports_a_fresh_age(self, log):
        log.arm_local_progress_check(**self.ARM, local_count=0)
        age = log.seconds_since_armed_attempt(**self.ARM)
        assert age is not None and 0 <= age < 5

    def test_age_is_gone_once_the_verdict_is_in(self, log):
        """Disarming ends the grace period as well — a series already
        judged this cycle is not "still being imported"."""
        log.arm_local_progress_check(**self.ARM, local_count=0)
        log.note_local_progress(**self.ARM, local_count=0)
        assert log.seconds_since_armed_attempt(**self.ARM) is None

    def test_unparseable_timestamp_degrades_to_no_grace(self, log,
                                                        db_path):
        """A corrupt row must mean "no grace period", never "grace
        forever" — the latter would silently stop the series from ever
        being retried."""
        log.arm_local_progress_check(**self.ARM, local_count=0)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE series_failures SET last_attempt_at = 'not a date'")
        conn.commit()
        conn.close()
        assert log.seconds_since_armed_attempt(**self.ARM) is None

    def test_migrates_a_log_written_without_the_column(self, db_path):
        """An existing log from an older build must gain the column on
        the next open, keeping the failure counts it already had."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE series_failures ("
            "source_pacs TEXT NOT NULL, series_uid_hash TEXT NOT NULL, "
            "attempt_count INTEGER NOT NULL DEFAULT 0, "
            "last_attempt_at TEXT NOT NULL, "
            "PRIMARY KEY (source_pacs, series_uid_hash))")
        conn.execute(
            "INSERT INTO series_failures VALUES (?, ?, 2, ?)",
            ("ct_scanner", _sha256("1.2.3"), "2026-01-01T00:00:00"))
        conn.commit()
        conn.close()

        tl = TransferLog(db_path)
        try:
            assert tl.get_series_failure_count(**self.ARM) == 2
            # And the new mechanism works on the migrated row.
            tl.arm_local_progress_check(**self.ARM, local_count=0)
            tl.note_local_progress(**self.ARM, local_count=0)
            assert tl.is_series_blacklisted(**self.ARM) is True
        finally:
            tl.close()


class TestTransferLogIndexes:
    """The log is append-only and reaches tens of thousands of rows;
    the selective lookup columns need indexes or every Examination
    Lookup scans the whole history."""

    def test_selective_lookup_columns_are_indexed(self, tmp_path):
        log = TransferLog(str(tmp_path / "t.sqlite"))
        try:
            rows = log._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        finally:
            log.close()
        names = {r[0] for r in rows}
        for expected in ("idx_series_study_date", "idx_series_patient",
                         "idx_series_accession", "idx_series_mbps",
                         "idx_study_study_date", "idx_study_patient"):
            assert expected in names, f"missing index {expected}"

    def test_low_selectivity_columns_are_not_indexed(self, tmp_path):
        """source_pacs and modality have a handful of distinct values —
        an index there is measurably slower than the scan it replaces
        and costs write time on the engine's hot path."""
        log = TransferLog(str(tmp_path / "t.sqlite"))
        try:
            sql = " ".join(
                r[0] or "" for r in log._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index'"))
        finally:
            log.close()
        assert "source_pacs" not in sql
        assert "modality" not in sql

    def test_indexes_are_added_to_an_existing_log(self, tmp_path):
        """IF NOT EXISTS — an existing database picks them up on the
        next open, with no migration step."""
        path = str(tmp_path / "t.sqlite")
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE series_transfer (id INTEGER PRIMARY KEY, "
            "study_date TEXT, patient_id_hash TEXT, "
            "accession_number_hash TEXT, estimated_mbps REAL)")
        conn.commit()
        conn.close()

        log = TransferLog(path)
        try:
            names = {r[0] for r in log._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            log.close()
        assert "idx_series_patient" in names
