"""
Tests for core.dicom_ops — DICOM network operations with mocked network.
"""

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from core.dicom_ops import (
    DicomOperations, parse_dicom_time, parse_dicom_date, TRANSFER_SYNTAXES,
)


# ═══════════════════════════════════════════════════════════════════════════
# parse_dicom_time
# ═══════════════════════════════════════════════════════════════════════════

class TestParseDicomTime:

    def test_normal_time(self):
        assert parse_dicom_time("143052") == "14:30"

    def test_time_with_fractional(self):
        assert parse_dicom_time("143052.123") == "14:30"

    def test_short_time(self):
        # "0900" → "09:00"
        result = parse_dicom_time("0900")
        assert result == "09:00"

    def test_empty_string(self):
        assert parse_dicom_time("") == ""

    def test_none_like_empty(self):
        # The function only expects str, but test with empty
        assert parse_dicom_time("") == ""

    def test_very_short_time(self):
        result = parse_dicom_time("12")
        assert result == "12:00"

    def test_midnight(self):
        assert parse_dicom_time("000000") == "00:00"


# ═══════════════════════════════════════════════════════════════════════════
# parse_dicom_date
# ═══════════════════════════════════════════════════════════════════════════

class TestParseDicomDate:

    def test_normal_date(self):
        assert parse_dicom_date("20260308") == "08.03.2026"

    def test_empty_string(self):
        assert parse_dicom_date("") == ""

    def test_none(self):
        assert parse_dicom_date(None) == ""

    def test_short_date_returns_unchanged(self):
        assert parse_dicom_date("2026") == "2026"

    def test_long_date_returns_unchanged(self):
        assert parse_dicom_date("202603080") == "202603080"

    def test_new_year(self):
        assert parse_dicom_date("20260101") == "01.01.2026"

    def test_end_of_year(self):
        assert parse_dicom_date("20261231") == "31.12.2026"


# ═══════════════════════════════════════════════════════════════════════════
# TRANSFER_SYNTAXES constant
# ═══════════════════════════════════════════════════════════════════════════

class TestTransferSyntaxes:

    def test_contains_expected_keys(self):
        expected = [
            "JPEG2000Lossless", "ExplicitVRLittleEndian",
            "ImplicitVRLittleEndian", "JPEGLossless",
            "JPEGLosslessSV1", "DeflatedExplicitVRLittleEndian",
        ]
        for key in expected:
            assert key in TRANSFER_SYNTAXES

    def test_values_are_uids(self):
        for key, val in TRANSFER_SYNTAXES.items():
            assert val is not None


# ═══════════════════════════════════════════════════════════════════════════
# DicomOperations — initialization
# ═══════════════════════════════════════════════════════════════════════════

class TestDicomOperationsInit:

    @pytest.fixture
    def local_config(self):
        return {
            "name": "Local", "ae_title": "LOCAL_AE",
            "ip_address": "127.0.0.1", "port": 11112,
            "transfer_syntax": "JPEG2000Lossless",
        }

    @pytest.fixture
    def remote_config(self):
        return {
            "name": "Remote CT", "ae_title": "CT_AE",
            "ip_address": "192.168.1.10", "port": 104,
            "transfer_syntax": "JPEG2000Lossless",
        }

    def test_creates_successfully(self, local_config, remote_config):
        ops = DicomOperations(local_config, remote_config, "ct")
        assert ops.remote_name == "ct"

    def test_default_transfer_syntax(self, local_config, remote_config):
        ops = DicomOperations(local_config, remote_config)
        # Default should be JPEG2000Lossless
        from pydicom.uid import JPEG2000Lossless
        assert ops.transfer_syntax == JPEG2000Lossless

    def test_custom_transfer_syntax(self, local_config, remote_config):
        remote_config["transfer_syntax"] = "ExplicitVRLittleEndian"
        ops = DicomOperations(local_config, remote_config)
        from pydicom.uid import ExplicitVRLittleEndian
        assert ops.transfer_syntax == ExplicitVRLittleEndian

    def test_unknown_syntax_fallback(self, local_config, remote_config):
        remote_config["transfer_syntax"] = "NonexistentSyntax"
        ops = DicomOperations(local_config, remote_config)
        from pydicom.uid import JPEG2000Lossless
        assert ops.transfer_syntax == JPEG2000Lossless

    def test_ae_title_set(self, local_config, remote_config):
        ops = DicomOperations(local_config, remote_config)
        assert ops.ae.ae_title == "LOCAL_AE"


# ═══════════════════════════════════════════════════════════════════════════
# DicomOperations — C-ECHO with mocked network
# ═══════════════════════════════════════════════════════════════════════════

class TestCEcho:

    @pytest.fixture
    def ops(self):
        local = {"ae_title": "L_AE", "ip_address": "127.0.0.1", "port": 11112}
        remote = {"ae_title": "R_AE", "ip_address": "10.0.0.1", "port": 104,
                   "transfer_syntax": "JPEG2000Lossless"}
        return DicomOperations(local, remote)

    def test_c_echo_success(self, ops):
        mock_assoc = MagicMock()
        mock_assoc.is_established = True
        mock_status = MagicMock()
        mock_status.Status = 0x0000
        mock_assoc.send_c_echo.return_value = mock_status

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            result = ops.c_echo(target='remote')
            assert result is True
            mock_assoc.release.assert_called_once()

    def test_c_echo_failure_not_established(self, ops):
        mock_assoc = MagicMock()
        mock_assoc.is_established = False

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            result = ops.c_echo(target='remote')
            assert result is False

    def test_c_echo_exception(self, ops):
        with patch.object(ops.ae, 'associate',
                          side_effect=Exception("Network error")):
            result = ops.c_echo(target='remote')
            assert result is False

    def test_c_echo_local_target(self, ops):
        mock_assoc = MagicMock()
        mock_assoc.is_established = True
        mock_status = MagicMock()
        mock_status.Status = 0x0000
        mock_assoc.send_c_echo.return_value = mock_status

        with patch.object(ops.ae, 'associate', return_value=mock_assoc) as mock_associate:
            result = ops.c_echo(target='local')
            assert result is True
            # Should use local config IP
            mock_associate.assert_called_once_with(
                "127.0.0.1", 11112, ae_title="L_AE")


# ═══════════════════════════════════════════════════════════════════════════
# DicomOperations — C-FIND with mocked network
# ═══════════════════════════════════════════════════════════════════════════

class TestCFind:

    @pytest.fixture
    def ops(self):
        local = {"ae_title": "L_AE", "ip_address": "127.0.0.1", "port": 11112}
        remote = {"ae_title": "R_AE", "ip_address": "10.0.0.1", "port": 104,
                   "transfer_syntax": "JPEG2000Lossless"}
        return DicomOperations(local, remote)

    def _mock_find_results(self, ops, datasets):
        """Helper to mock c_find returning datasets."""
        mock_assoc = MagicMock()
        mock_assoc.is_established = True
        results = []
        for ds in datasets:
            status = MagicMock()
            status.Status = 0xFF00
            results.append((status, ds))
        # Add final pending status
        final = MagicMock()
        final.Status = 0x0000
        results.append((final, None))
        mock_assoc.send_c_find.return_value = results
        return mock_assoc

    def test_c_find_studies_returns_results(self, ops, mock_dicom_dataset):
        ds1 = mock_dicom_dataset(study_uid="1.1.1")
        ds2 = mock_dicom_dataset(study_uid="2.2.2")
        mock_assoc = self._mock_find_results(ops, [ds1, ds2])

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            results = ops.c_find_studies(study_date="20260308")
            assert len(results) == 2

    def test_c_find_studies_empty(self, ops):
        mock_assoc = MagicMock()
        mock_assoc.is_established = True
        final = MagicMock()
        final.Status = 0x0000
        mock_assoc.send_c_find.return_value = [(final, None)]

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            results = ops.c_find_studies()
            assert results == []

    def test_c_find_studies_connection_failure(self, ops):
        with patch.object(ops.ae, 'associate',
                          side_effect=Exception("Connection refused")):
            results = ops.c_find_studies()
            assert results == []

    def test_c_find_series_returns_results(self, ops, mock_series_dataset):
        s1 = mock_series_dataset(series_uid="1.1.1.1")
        mock_assoc = self._mock_find_results(ops, [s1])

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            results = ops.c_find_series("1.1.1")
            assert len(results) == 1

    def test_c_find_local_series(self, ops, mock_series_dataset):
        s1 = mock_series_dataset(series_uid="1.1.1.1")
        mock_assoc = self._mock_find_results(ops, [s1])

        with patch.object(ops.ae, 'associate', return_value=mock_assoc) as mock_associate:
            results = ops.c_find_local_series("1.1.1")
            assert len(results) == 1
            # Should use local config
            mock_associate.assert_called_once_with(
                "127.0.0.1", 11112, ae_title="L_AE")

    def test_c_find_images(self, ops):
        mock_ds = MagicMock()
        mock_ds.SOPInstanceUID = "1.2.3.4.5.6"
        mock_assoc = self._mock_find_results(ops, [mock_ds])

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            results = ops.c_find_images("1.1.1", "1.1.1.1")
            assert len(results) == 1


# ═══════════════════════════════════════════════════════════════════════════
# DicomOperations — C-MOVE with mocked network
# ═══════════════════════════════════════════════════════════════════════════

class TestCMove:

    @pytest.fixture
    def ops(self):
        local = {"ae_title": "L_AE", "ip_address": "127.0.0.1", "port": 11112}
        remote = {"ae_title": "R_AE", "ip_address": "10.0.0.1", "port": 104,
                   "transfer_syntax": "JPEG2000Lossless"}
        return DicomOperations(local, remote)

    def test_c_move_series_success(self, ops):
        mock_assoc = MagicMock()
        mock_assoc.is_established = True

        pending = MagicMock()
        pending.Status = 0xFF00
        pending.NumberOfCompletedSuboperations = 5

        final = MagicMock()
        final.Status = 0x0000
        final.NumberOfCompletedSuboperations = 50

        mock_assoc.send_c_move.return_value = [
            (pending, None), (final, None)
        ]

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            success, images = ops.c_move_series("1.1.1", "1.1.1.1")
            assert success is True
            assert images == 50

    def test_c_move_series_failure(self, ops):
        with patch.object(ops.ae, 'associate',
                          side_effect=Exception("Connection refused")):
            success, images = ops.c_move_series("1.1.1", "1.1.1.1")
            assert success is False
            assert images == 0

    def test_c_move_image(self, ops):
        mock_assoc = MagicMock()
        mock_assoc.is_established = True
        status = MagicMock()
        status.Status = 0x0000
        status.NumberOfCompletedSuboperations = 1
        mock_assoc.send_c_move.return_value = [(status, None)]

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            success, images = ops.c_move_image("1.1", "1.1.1", "1.1.1.1")
            assert success is True


# ═══════════════════════════════════════════════════════════════════════════
# DicomOperations — c_find_institution_names
# ═══════════════════════════════════════════════════════════════════════════

class TestCFindInstitutionNames:

    @pytest.fixture
    def ops(self):
        local = {"ae_title": "L_AE", "ip_address": "127.0.0.1", "port": 11112}
        remote = {"ae_title": "R_AE", "ip_address": "10.0.0.1", "port": 104,
                   "transfer_syntax": "JPEG2000Lossless"}
        return DicomOperations(local, remote)

    def test_study_level_institution(self, ops, mock_dicom_dataset):
        """If InstitutionName is available at study level, no series query needed."""
        ds = mock_dicom_dataset(
            study_uid="1.1", institution_name="Hospital Alpha")

        with patch.object(ops, 'c_find_studies', return_value=[ds]):
            with patch.object(ops, '_execute_find') as mock_find:
                names = ops.c_find_institution_names(study_date="20260308")
                assert "Hospital Alpha" in names
                # _execute_find should not be called for series-level fallback
                mock_find.assert_not_called()

    def test_series_level_fallback(self, ops, mock_dicom_dataset,
                                    mock_series_dataset):
        """When study-level has no InstitutionName, fall back to series."""
        study_ds = mock_dicom_dataset(
            study_uid="1.1", institution_name="")

        series_ds = mock_series_dataset(
            series_uid="1.1.1", institution_name="Clinic Beta")

        with patch.object(ops, 'c_find_studies', return_value=[study_ds]):
            with patch.object(ops, '_execute_find',
                              return_value=[series_ds]):
                names = ops.c_find_institution_names()
                assert "Clinic Beta" in names

    def test_returns_sorted_unique(self, ops, mock_dicom_dataset):
        ds1 = mock_dicom_dataset(study_uid="1.1", institution_name="Beta")
        ds2 = mock_dicom_dataset(study_uid="1.2", institution_name="Alpha")
        ds3 = mock_dicom_dataset(study_uid="1.3", institution_name="Beta")

        with patch.object(ops, 'c_find_studies',
                          return_value=[ds1, ds2, ds3]):
            names = ops.c_find_institution_names()
            assert names == ["Alpha", "Beta"]

    def test_empty_results(self, ops):
        with patch.object(ops, 'c_find_studies', return_value=[]):
            names = ops.c_find_institution_names()
            assert names == []
