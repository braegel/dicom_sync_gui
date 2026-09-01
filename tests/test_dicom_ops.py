"""
Tests for core.dicom_ops — DICOM network operations with mocked network.
"""

import logging
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from pydicom import Dataset

from core.dicom_ops import DicomOperations, TRANSFER_SYNTAXES


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

    def test_connection_timeout_set(self, local_config, remote_config):
        """A finite TCP connect timeout must be configured so an
        unreachable PACS fails fast instead of hanging the service
        loop / reachability check on the OS connect timeout."""
        from core.dicom_ops import CONNECTION_TIMEOUT_S
        ops = DicomOperations(local_config, remote_config)
        assert ops.ae.connection_timeout == CONNECTION_TIMEOUT_S
        assert 0 < CONNECTION_TIMEOUT_S <= 30

    def test_association_timeouts_set(self, local_config, remote_config):
        from core.dicom_ops import (
            ACSE_TIMEOUT_S, DIMSE_TIMEOUT_S, NETWORK_TIMEOUT_S)
        ops = DicomOperations(local_config, remote_config)
        assert ops.ae.acse_timeout == ACSE_TIMEOUT_S
        assert ops.ae.dimse_timeout == DIMSE_TIMEOUT_S
        assert ops.ae.network_timeout == NETWORK_TIMEOUT_S

    def test_associate_slows_dul_reactor(self, local_config, remote_config):
        """_associate must raise the DUL reactor's poll delay above
        pynetdicom's 1ms default so the reactor thread can't spin a core
        at ~100% and starve the Qt GUI thread of the GIL during a busy
        C-MOVE."""
        from core.dicom_ops import DUL_RUN_LOOP_DELAY_S
        ops = DicomOperations(local_config, remote_config)

        mock_dul = MagicMock()
        mock_dul._run_loop_delay = 0.001
        mock_assoc = MagicMock()
        mock_assoc.is_established = True
        mock_assoc.dul = mock_dul

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            result = ops._associate(remote_config)

        assert result is mock_assoc
        assert mock_dul._run_loop_delay == DUL_RUN_LOOP_DELAY_S
        assert DUL_RUN_LOOP_DELAY_S >= 0.01  # meaningfully above 1ms

    def test_move_dest_uses_local_config(self, local_config, remote_config):
        """C-MOVE destination should be the local_config (per-source)."""
        ops = DicomOperations(local_config, remote_config)
        assert ops.move_dest_config is local_config
        assert ops.move_dest_config.get('ae_title') == 'LOCAL_AE'

    def test_per_source_local_ae_used_for_move(self):
        """When local_config has a custom AE, that's what C-MOVE uses."""
        local = {"ae_title": "ARZT_4", "ip_address": "127.0.0.1", "port": 11113}
        remote = {"ae_title": "R_AE", "ip_address": "10.0.0.1", "port": 104,
                   "transfer_syntax": "JPEG2000Lossless"}
        ops = DicomOperations(local, remote)
        assert ops.ae.ae_title == "ARZT_4"
        assert ops.move_dest_config.get('ae_title') == 'ARZT_4'

    # ── requested presentation contexts: preferred transfer syntax ──────

    def _qr_contexts(self, ops):
        """The four Q/R contexts (everything except Verification)."""
        from pynetdicom.sop_class import Verification
        return [cx for cx in ops.ae.requested_contexts
                if cx.abstract_syntax != Verification]

    def test_qr_contexts_prefer_configured_syntax(self, local_config,
                                                  remote_config):
        """Configured syntax is offered first; Implicit VR LE (the DICOM
        baseline) must remain as fallback so interop cannot regress."""
        from pydicom.uid import (
            JPEG2000Lossless, ExplicitVRLittleEndian, ImplicitVRLittleEndian,
        )
        ops = DicomOperations(local_config, remote_config)
        qr = self._qr_contexts(ops)
        assert len(qr) == 4
        for cx in qr:
            assert cx.transfer_syntax[0] == JPEG2000Lossless
            assert ExplicitVRLittleEndian in cx.transfer_syntax
            assert ImplicitVRLittleEndian in cx.transfer_syntax

    def test_qr_contexts_no_duplicate_when_syntax_is_fallback(
            self, local_config, remote_config):
        """A configured syntax that already is a fallback (e.g. Explicit
        VR LE) must not appear twice in the offered list."""
        from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
        remote_config["transfer_syntax"] = "ExplicitVRLittleEndian"
        ops = DicomOperations(local_config, remote_config)
        for cx in self._qr_contexts(ops):
            assert list(cx.transfer_syntax) == [
                ExplicitVRLittleEndian, ImplicitVRLittleEndian]

    def test_qr_contexts_implicit_first_when_configured(
            self, local_config, remote_config):
        from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
        remote_config["transfer_syntax"] = "ImplicitVRLittleEndian"
        ops = DicomOperations(local_config, remote_config)
        for cx in self._qr_contexts(ops):
            assert list(cx.transfer_syntax) == [
                ImplicitVRLittleEndian, ExplicitVRLittleEndian]

    def test_verification_context_present(self, local_config, remote_config):
        """C-ECHO context still requested (with pynetdicom defaults)."""
        from pynetdicom.sop_class import Verification
        ops = DicomOperations(local_config, remote_config)
        assert any(cx.abstract_syntax == Verification
                   for cx in ops.ae.requested_contexts)


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

    def test_c_find_studies_connection_failure_raises(self, ops):
        """A failed association must raise PacsConnectionError (so the
        engine can tell "PACS down" from "no studies"), not silently
        return an empty list."""
        from core.dicom_ops import PacsConnectionError
        with patch.object(ops.ae, 'associate',
                          side_effect=Exception("Connection refused")):
            with pytest.raises(PacsConnectionError):
                ops.c_find_studies()

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

    def test_c_move_series_connection_failure_raises(self, ops):
        """A failed association during C-MOVE must raise
        PacsConnectionError so the engine aborts the cycle and pops the
        unreachable warning, rather than reporting a plain failure."""
        from core.dicom_ops import PacsConnectionError
        with patch.object(ops.ae, 'associate',
                          side_effect=Exception("Connection refused")):
            with pytest.raises(PacsConnectionError):
                ops.c_move_series("1.1.1", "1.1.1.1")

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


# ═══════════════════════════════════════════════════════════════════════════
# DicomOperations — association release on exception
# ═══════════════════════════════════════════════════════════════════════════

class TestAssociationReleaseOnException:
    """If the C-FIND or C-MOVE iteration raises mid-stream, the
    established association must still be released so pynetdicom's TCP
    socket isn't left dangling until OS timeout."""

    @pytest.fixture
    def ops(self):
        local = {"ae_title": "L_AE", "ip_address": "127.0.0.1", "port": 11112}
        remote = {"ae_title": "R_AE", "ip_address": "10.0.0.1", "port": 104,
                   "transfer_syntax": "JPEG2000Lossless"}
        return DicomOperations(local, remote)

    def test_find_releases_assoc_when_iteration_raises(self, ops):
        mock_assoc = MagicMock()
        mock_assoc.is_established = True

        def boom(*_a, **_kw):
            raise RuntimeError("DIMSE timeout")

        mock_assoc.send_c_find.side_effect = boom

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            results = ops.c_find_studies()
            assert results == []
            mock_assoc.release.assert_called_once()

    def test_move_releases_assoc_when_iteration_raises(self, ops):
        mock_assoc = MagicMock()
        mock_assoc.is_established = True

        def boom(*_a, **_kw):
            raise RuntimeError("DIMSE timeout")

        mock_assoc.send_c_move.side_effect = boom

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            success, images = ops.c_move_series("1.1.1", "1.1.1.1")
            assert success is False
            assert images == 0
            mock_assoc.release.assert_called_once()

    def test_find_does_not_release_when_assoc_not_established(self, ops):
        from core.dicom_ops import PacsConnectionError
        mock_assoc = MagicMock()
        mock_assoc.is_established = False

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            with pytest.raises(PacsConnectionError):
                ops.c_find_studies()
            # Nothing to release on a non-established association.
            mock_assoc.release.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# DicomOperations — C-FIND completeness flag
# ═══════════════════════════════════════════════════════════════════════════

class TestCFindCompleteness:
    """A C-FIND that breaks off mid-iteration returns partial results.
    Callers that treat the list as authoritative (the engine's study
    completion decision) must be able to tell that apart from a short
    but complete answer."""

    @pytest.fixture
    def ops(self):
        local = {"ae_title": "L_AE", "ip_address": "127.0.0.1", "port": 11112}
        remote = {"ae_title": "R_AE", "ip_address": "10.0.0.1", "port": 104,
                   "transfer_syntax": "JPEG2000Lossless"}
        return DicomOperations(local, remote)

    def _assoc_yielding(self, datasets, then_raise=None):
        """Association whose send_c_find yields *datasets* and then
        optionally raises mid-iteration."""
        mock_assoc = MagicMock()
        mock_assoc.is_established = True

        def _gen(*_a, **_kw):
            for ds in datasets:
                status = MagicMock()
                status.Status = 0xFF00
                yield status, ds
            if then_raise is not None:
                raise then_raise
            final = MagicMock()
            final.Status = 0x0000
            yield final, None

        mock_assoc.send_c_find.side_effect = _gen
        return mock_assoc

    def test_complete_query_flagged_complete(self, ops, mock_series_dataset):
        s1 = mock_series_dataset(series_uid="1.1.1.1")
        mock_assoc = self._assoc_yielding([s1])

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            complete, results = ops.c_find_series_checked("1.1.1")
        assert complete is True
        assert len(results) == 1

    def test_empty_result_is_still_complete(self, ops):
        """Zero results is a legitimate answer, not a failure."""
        mock_assoc = self._assoc_yielding([])

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            complete, results = ops.c_find_series_checked("1.1.1")
        assert complete is True
        assert results == []

    def test_mid_iteration_error_flagged_incomplete(
            self, ops, mock_series_dataset):
        """The partial results still come back — but flagged."""
        s1 = mock_series_dataset(series_uid="1.1.1.1")
        s2 = mock_series_dataset(series_uid="1.1.1.2")
        mock_assoc = self._assoc_yielding(
            [s1, s2], then_raise=RuntimeError("DIMSE timeout"))

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            complete, results = ops.c_find_series_checked("1.1.1")
        assert complete is False
        assert len(results) == 2
        mock_assoc.release.assert_called_once()

    def test_c_find_series_stays_list_only(self, ops, mock_series_dataset):
        """The public method keeps its plain-list signature for the GUI
        call sites, even when the query was truncated."""
        s1 = mock_series_dataset(series_uid="1.1.1.1")
        mock_assoc = self._assoc_yielding(
            [s1], then_raise=RuntimeError("DIMSE timeout"))

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            results = ops.c_find_series("1.1.1")
        assert isinstance(results, list)
        assert len(results) == 1

    def test_connection_failure_still_raises(self, ops):
        """PacsConnectionError must keep propagating — the completeness
        flag is for established-but-broken associations only."""
        from core.dicom_ops import PacsConnectionError
        with patch.object(ops.ae, 'associate',
                          side_effect=Exception("Connection refused")):
            with pytest.raises(PacsConnectionError):
                ops.c_find_series_checked("1.1.1")

    def test_execute_find_drops_the_flag(self, ops, mock_series_dataset):
        """_execute_find is the list-only convenience wrapper used by
        callers that only aggregate what they got."""
        s1 = mock_series_dataset(series_uid="1.1.1.1")
        mock_assoc = self._assoc_yielding([s1])
        from pydicom import Dataset
        query = Dataset()
        query.QueryRetrieveLevel = 'SERIES'

        with patch.object(ops.ae, 'associate', return_value=mock_assoc):
            results = ops._execute_find(query)
        assert [r for r in results] == [s1]


# ═══════════════════════════════════════════════════════════════════════════
# Oversized-UID suppression (OsiriX comma-joined SeriesInstanceUID)
# ═══════════════════════════════════════════════════════════════════════════

class TestVrViolationSuppression:
    """pydicom reports a non-conformant value on two channels.  These
    tests pin the log channel, which used to leak the message straight
    into the application log despite the warnings filter."""

    OVERSIZED = ",".join([
        "1.2.276.0.7230010.3.1.3.0.856.1788247304.741472",
        "1.2.276.0.7230010.3.1.3.0.856.1788247304.741474",
        "1.2.276.0.7230010.3.1.3.0.856.1788247304.741476",
    ])
    # Valid length, but a component with a leading zero -- the other
    # violation seen in the field.
    LEADING_ZERO = "1.2.276.0.18.200.714.2.0.18168.20260825.082142.761"

    @pytest.fixture(autouse=True)
    def _fresh_filter_state(self):
        """Reinstall the filter per test and take it off again, so the
        module-level 'installed once' flag cannot leak between tests."""
        import core.dicom_ops as ops_mod
        pydicom_logger = logging.getLogger("pydicom")
        saved = list(pydicom_logger.filters)
        saved_flag = ops_mod._vr_filter_installed
        # Start from an empty filter list, not just a reset flag: any
        # earlier test that built a DicomOperations left a filter
        # instance behind whose "already seen" set would swallow this
        # test's first record.
        pydicom_logger.filters = []
        ops_mod._vr_filter_installed = False
        yield
        pydicom_logger.filters = saved
        ops_mod._vr_filter_installed = saved_flag

    @staticmethod
    def _install():
        from core.dicom_ops import silence_repeated_vr_violations
        silence_repeated_vr_violations()

    def test_first_report_is_kept(self, caplog):
        self._install()
        with caplog.at_level(logging.WARNING, logger="pydicom"):
            Dataset().SeriesInstanceUID = self.OVERSIZED
        assert len(caplog.records) == 1

    def test_repeats_of_the_same_violation_are_dropped(self, caplog):
        self._install()
        with caplog.at_level(logging.WARNING, logger="pydicom"):
            for _ in range(20):
                Dataset().SeriesInstanceUID = self.OVERSIZED
        assert len(caplog.records) == 1

    def test_distinct_values_still_collapse_onto_one_line(self, caplog):
        """The whole point of stripping the value out of the signature:
        every malformed UID differs, so keying on the raw message would
        keep the log flooded and the 'seen' set unbounded."""
        self._install()
        with caplog.at_level(logging.WARNING, logger="pydicom"):
            for n in range(20):
                Dataset().SeriesInstanceUID = self.OVERSIZED.replace(
                    "741472", f"7414{n:02d}")
        assert len(caplog.records) == 1

    def test_a_different_violation_gets_its_own_line(self, caplog):
        self._install()
        with caplog.at_level(logging.WARNING, logger="pydicom"):
            Dataset().SeriesInstanceUID = self.OVERSIZED
            Dataset().SeriesInstanceUID = self.LEADING_ZERO
            Dataset().SeriesInstanceUID = self.OVERSIZED
            Dataset().SeriesInstanceUID = self.LEADING_ZERO
        assert len(caplog.records) == 2

    def test_other_pydicom_records_always_pass(self, caplog):
        self._install()
        with caplog.at_level(logging.WARNING, logger="pydicom"):
            for _ in range(3):
                logging.getLogger("pydicom").warning("something else entirely")
        assert len(caplog.records) == 3

    def test_install_is_idempotent(self):
        pydicom_logger = logging.getLogger("pydicom")
        before = len(pydicom_logger.filters)
        for _ in range(5):
            self._install()
        assert len(pydicom_logger.filters) == before + 1

    def test_constructing_dicom_operations_installs_it(self):
        import core.dicom_ops as ops_mod
        cfg = {"ae_title": "LOCAL", "ip_address": "127.0.0.1", "port": 11112,
               "transfer_syntax": "ExplicitVRLittleEndian"}
        DicomOperations(cfg, dict(cfg, ae_title="REMOTE"))
        assert ops_mod._vr_filter_installed is True


# ═══════════════════════════════════════════════════════════════════════════
# find_session — one association per endpoint instead of one per query
# ═══════════════════════════════════════════════════════════════════════════

class TestFindSession:
    """A query cycle runs 1 + 2n C-FINDs.  Outside a session each opens
    and releases its own association; inside one they share three."""

    @pytest.fixture
    def ops(self):
        local = {"ae_title": "L_AE", "ip_address": "127.0.0.1", "port": 11112}
        remote = {"ae_title": "R_AE", "ip_address": "10.0.0.1", "port": 104,
                  "transfer_syntax": "JPEG2000Lossless"}
        return DicomOperations(local, remote)

    @staticmethod
    def _assoc(established=True, raises=None):
        a = MagicMock()
        a.is_established = established

        def _gen(*_a, **_kw):
            if raises is not None:
                raise raises
            final = MagicMock()
            final.Status = 0x0000
            yield final, None

        a.send_c_find.side_effect = _gen
        return a

    def test_without_a_session_every_query_associates(self, ops):
        """The historical behaviour has to survive untouched."""
        a = self._assoc()
        with patch.object(ops.ae, 'associate', return_value=a) as associate:
            for _ in range(4):
                ops.c_find_series_checked("1.1.1")
        assert associate.call_count == 4
        assert a.release.call_count == 4

    def test_session_shares_one_association_per_endpoint(self, ops):
        a = self._assoc()
        with patch.object(ops.ae, 'associate', return_value=a) as associate:
            with ops.find_session():
                for _ in range(10):
                    ops.c_find_series_checked("1.1.1")
                assert a.release.call_count == 0, "released mid-session"
        assert associate.call_count == 1
        assert a.release.call_count == 1

    def test_remote_and_local_get_separate_associations(self, ops):
        a = self._assoc()
        with patch.object(ops.ae, 'associate', return_value=a) as associate:
            with ops.find_session():
                for _ in range(5):
                    ops.c_find_series_checked("1.1.1")
                    ops.c_find_local_series_checked("1.1.1")
        assert associate.call_count == 2
        assert a.release.call_count == 2

    def test_pool_is_released_even_when_the_block_raises(self, ops):
        a = self._assoc()
        with patch.object(ops.ae, 'associate', return_value=a):
            with pytest.raises(ValueError):
                with ops.find_session():
                    ops.c_find_series_checked("1.1.1")
                    raise ValueError("boom")
        assert a.release.call_count == 1

    def test_a_stale_pooled_association_is_replaced(self, ops):
        """A peer that aborts leaves an association that is no longer
        established; handing it to the next query would fail every
        remaining query of the cycle."""
        pooled, live = self._assoc(), self._assoc()
        with patch.object(ops.ae, 'associate',
                          side_effect=[pooled, live]) as associate:
            with ops.find_session():
                ops.c_find_series_checked("1.1.1")
                assert associate.call_count == 1
                # The peer aborts between two queries.
                pooled.is_established = False
                ops.c_find_series_checked("1.1.2")
        assert associate.call_count == 2
        # Only the live one is still in the pool at exit; the stale
        # entry was dropped without a release, which is correct -- there
        # is no association left to release.
        assert live.release.call_count == 1

    def test_a_broken_query_evicts_and_reconnects(self, ops):
        """A mid-stream DIMSE failure must not poison the rest of the
        cycle: the association is dropped and the next query gets a
        fresh one."""
        broken = self._assoc(raises=RuntimeError("DIMSE timeout"))
        good = self._assoc()
        with patch.object(ops.ae, 'associate',
                          side_effect=[broken, good]) as associate:
            with ops.find_session():
                complete, _ = ops.c_find_series_checked("1.1.1")
                assert complete is False
                assert broken.release.call_count == 1
                complete, _ = ops.c_find_series_checked("1.1.2")
                assert complete is True
        assert associate.call_count == 2
        # The replacement is released once, by the session.
        assert good.release.call_count == 1

    def test_nested_sessions_release_once(self, ops):
        a = self._assoc()
        with patch.object(ops.ae, 'associate', return_value=a) as associate:
            with ops.find_session():
                with ops.find_session():
                    ops.c_find_series_checked("1.1.1")
                assert a.release.call_count == 0, "inner block released"
                ops.c_find_series_checked("1.1.2")
        assert associate.call_count == 1
        assert a.release.call_count == 1

    def test_pool_is_cleared_after_the_session(self, ops):
        """A query after the block must associate again, not reuse a
        released association."""
        first, second = self._assoc(), self._assoc()
        with patch.object(ops.ae, 'associate',
                          side_effect=[first, second]) as associate:
            with ops.find_session():
                ops.c_find_series_checked("1.1.1")
            ops.c_find_series_checked("1.1.2")
        assert associate.call_count == 2
        assert second.release.call_count == 1

    def test_c_move_is_unaffected_by_a_session(self, ops):
        """C-MOVE opens its own association and must keep releasing it,
        or a transfer would leak one per series."""
        find_assoc, move_assoc = self._assoc(), MagicMock()
        move_assoc.is_established = True
        final = MagicMock(); final.Status = 0x0000
        final.NumberOfCompletedSuboperations = 3
        move_assoc.send_c_move.side_effect = lambda *a, **k: iter(
            [(final, None)])
        with patch.object(ops.ae, 'associate',
                          side_effect=[find_assoc, move_assoc]):
            with ops.find_session():
                ops.c_find_series_checked("1.1.1")
                success, images = ops.c_move_series("1.1.1", "1.1.1.1")
        assert (success, images) == (True, 3)
        assert move_assoc.release.call_count == 1
