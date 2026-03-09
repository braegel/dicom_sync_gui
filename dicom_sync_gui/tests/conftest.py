"""
Shared fixtures for DICOM Sync GUI tests.
"""

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import AppConfig, PacsNode


@pytest.fixture(scope="session")
def qapp():
    """Create a single QApplication for the entire test session.
    
    PySide6 requires exactly one QApplication per process.
    """
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def tmp_config_path(tmp_path):
    """Return a temporary file path for config storage."""
    return str(tmp_path / "test_config.json")


@pytest.fixture
def default_config(tmp_config_path):
    """An AppConfig with a temp path and sensible defaults."""
    config = AppConfig(config_path=tmp_config_path)
    return config


@pytest.fixture
def populated_config(tmp_config_path):
    """An AppConfig with remote nodes and filter groups pre-configured."""
    config = AppConfig(config_path=tmp_config_path)
    config.local_node = PacsNode(
        name="Local", ae_title="LOCAL_AE",
        ip_address="127.0.0.1", port=11112,
        transfer_syntax="JPEG2000Lossless",
    )
    config.remote_nodes = {
        "ct": PacsNode(
            name="CT Scanner", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
            transfer_syntax="JPEG2000Lossless",
            retrieve_method="C-MOVE",
            hours=3, max_images=0, sync_interval=60,
        ),
        "mri": PacsNode(
            name="MRI Unit", ae_title="MRI_AE",
            ip_address="192.168.1.20", port=104,
            transfer_syntax="ExplicitVRLittleEndian",
            retrieve_method="C-GET",
            hours=24, max_images=1000, sync_interval=300,
        ),
    }
    config.fallback_storage_enabled = True
    config.fallback_storage_path = "/tmp/dicom_test"
    config.prior_studies_count = 2
    config.prior_studies_same_modality = True
    config.filter_group_names = ["Group A", "Group B", "Group C"]
    config.institution_assignments = {
        "Hospital Alpha": "Group A",
        "Clinic Beta": "Group B",
        "Hospital Gamma": "Group A",
        "Unknown Clinic": "",
    }
    config.active_filter_groups = ["Group A"]
    config.filter_groups_enabled = True
    config.default_hours = 6
    config.max_images = 500
    config.sync_interval = 120
    return config


@pytest.fixture
def sample_pacs_node():
    """A sample PacsNode."""
    return PacsNode(
        name="Test PACS", ae_title="TEST_AE",
        ip_address="10.0.0.1", port=4242,
        transfer_syntax="JPEGLossless",
        retrieve_method="C-GET",
    )


@pytest.fixture
def mock_dicom_dataset():
    """Create a mock DICOM dataset (simulates pydicom.Dataset)."""
    def _make(study_uid="1.2.3.4", patient_name="Doe^John",
              patient_id="12345", study_date="20260308",
              study_time="120000", study_description="CT Head",
              institution_name="Hospital Alpha",
              modalities_in_study="CT", accession="ACC001"):
        ds = MagicMock()
        ds.StudyInstanceUID = study_uid
        ds.PatientName = patient_name
        ds.PatientID = patient_id
        ds.StudyDate = study_date
        ds.StudyTime = study_time
        ds.StudyDescription = study_description
        ds.InstitutionName = institution_name
        ds.ModalitiesInStudy = modalities_in_study
        ds.AccessionNumber = accession
        return ds
    return _make


@pytest.fixture
def mock_series_dataset():
    """Create a mock series-level DICOM dataset."""
    def _make(series_uid="1.2.3.4.5", series_number="1",
              modality="CT", series_description="Axial",
              num_instances=100, institution_name="Hospital Alpha"):
        ds = MagicMock()
        ds.SeriesInstanceUID = series_uid
        ds.SeriesNumber = series_number
        ds.Modality = modality
        ds.SeriesDescription = series_description
        ds.NumberOfSeriesRelatedInstances = num_instances
        ds.InstitutionName = institution_name
        return ds
    return _make
