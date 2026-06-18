"""
Tests for core.StorageSCP — signal emission and image counting.
"""
import inspect
import os
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QCoreApplication

from pydicom import dcmread
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

import core.storage_scp as storage_scp_module
from core.storage_scp import StorageSCP


@pytest.fixture
def scp(qapp, tmp_path):
    return StorageSCP("TEST_AE", 11112, str(tmp_path))


class TestStorageSCPSignal:
    """handle_store must emit image_received after a successful save."""

    def _make_event(self, tmp_path, sop_uid="1.2.3"):
        ds = MagicMock()
        ds.SOPInstanceUID = sop_uid
        ds.file_meta = MagicMock()

        def save_as(path, **_kw):
            open(path, "wb").close()

        ds.save_as = save_as

        event = MagicMock()
        event.dataset = ds
        event.file_meta = ds.file_meta
        return event

    def test_image_received_emitted_on_success(self, scp, tmp_path):
        received = []
        scp.image_received.connect(received.append)

        event = self._make_event(tmp_path)
        result = scp.handle_store(event)

        assert result == 0x0000
        assert len(received) == 1
        # Payload is now the running received-count (int), not the dataset.
        assert received == [1]

    def test_image_received_is_throttled(self, scp, tmp_path):
        """A per-image cross-thread emit floods the GUI thread; the SCP
        emits only the first image and then every Nth, while still
        storing (and counting) every image."""
        from core.storage_scp import _IMAGE_SIGNAL_EVERY
        received = []
        scp.image_received.connect(received.append)

        n = _IMAGE_SIGNAL_EVERY * 2
        for i in range(n):
            scp.handle_store(self._make_event(tmp_path, sop_uid=f"1.2.{i}"))

        # Every image was stored/counted...
        assert scp.images_received == n
        # ...but the signal fired only for #1 and each Nth.
        assert received == [1, _IMAGE_SIGNAL_EVERY, _IMAGE_SIGNAL_EVERY * 2]

    def test_image_received_not_emitted_on_save_failure(self, scp, tmp_path):
        received = []
        scp.image_received.connect(received.append)

        event = MagicMock()
        event.dataset.SOPInstanceUID = "1.2.3"
        event.dataset.file_meta = MagicMock()
        event.dataset.save_as.side_effect = OSError("disk full")
        event.file_meta = event.dataset.file_meta

        result = scp.handle_store(event)

        assert result == 0xC000
        assert received == []

    def test_images_received_counter_increments(self, scp, tmp_path):
        event = self._make_event(tmp_path, sop_uid="1.1")
        event2 = self._make_event(tmp_path, sop_uid="1.2")
        scp.handle_store(event)
        scp.handle_store(event2)
        assert scp.images_received == 2

    def test_handle_store_writes_conformant_part10_file(self, scp, tmp_path):
        """End-to-end through the REAL pydicom save path (no mocked
        save_as): the version-compatibility shim must hand the installed
        pydicom a keyword it accepts ("write_like_original" on 2.x,
        "enforce_file_format" on 3.x) and the result must be a
        standards-conformant Part 10 file -- preamble, "DICM" magic and
        File Meta Information -- readable by a strict dcmread()."""
        sop_uid = "1.2.3.4.5"
        ds = Dataset()
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7"  # Secondary Capture
        ds.SOPInstanceUID = sop_uid
        ds.PatientName = "Test^Patient"

        # pynetdicom delivers the file meta separately on the event;
        # handle_store reattaches it before saving.
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        file_meta.MediaStorageSOPInstanceUID = sop_uid
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

        event = MagicMock()
        event.dataset = ds
        event.file_meta = file_meta

        result = scp.handle_store(event)
        assert result == 0x0000

        filepath = tmp_path / f"{sop_uid}.dcm"
        assert filepath.exists()

        # Strict read (force=False) fails on files lacking the DICM
        # marker/file meta, so a successful read proves conformance.
        reread = dcmread(str(filepath))
        assert reread.SOPInstanceUID == sop_uid
        assert reread.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
        # 128-byte preamble must be present (written, not skipped).
        assert reread.preamble is not None

    def test_save_as_compat_kwargs_match_installed_pydicom(self):
        """The import-time shim must pick a keyword the installed
        Dataset.save_as actually accepts, with the conformance-enforcing
        value for whichever spelling was chosen."""
        params = inspect.signature(Dataset.save_as).parameters
        kwargs = storage_scp_module._SAVE_AS_KWARGS
        if "enforce_file_format" in params:  # pydicom 3.x
            assert kwargs == {"enforce_file_format": True}
        else:  # pydicom 2.x
            assert kwargs == {"write_like_original": False}

    def test_images_received_not_incremented_on_failure(self, scp, tmp_path):
        event = MagicMock()
        event.dataset.SOPInstanceUID = "1.2.3"
        event.dataset.file_meta = MagicMock()
        event.dataset.save_as.side_effect = OSError("disk full")
        event.file_meta = event.dataset.file_meta

        scp.handle_store(event)
        assert scp.images_received == 0


class TestStorageSCPStartFailure:
    """``start()`` must surface a bind failure instead of silently
    reporting success when ``start_server`` raises (port already in use,
    permission denied, etc.).  Otherwise the caller logs "started" and
    sends queries to a dead SCP."""

    def test_start_raises_runtime_error_when_start_server_raises(
            self, qapp, tmp_path):
        scp = StorageSCP("TEST_AE", 11112, str(tmp_path))

        def boom(*_a, **_kw):
            raise OSError("port already in use")

        # Patch the AE instance after construction so .start() builds it
        # via the normal code path, then replace start_server before the
        # worker thread runs.
        orig_start = scp.start

        def wrapped_start():
            # Stub the AE.start_server right after start() creates the AE
            # but before the worker thread fires.  Easiest: patch the
            # AE class so the freshly constructed instance picks it up.
            with patch("core.storage_scp.AE") as ae_cls:
                ae_instance = MagicMock()
                ae_instance.start_server.side_effect = boom
                ae_cls.return_value = ae_instance
                orig_start()

        with pytest.raises(RuntimeError, match="failed to bind"):
            wrapped_start()

        # After the failure, the flag must reflect "not running" so a
        # subsequent stop() is a clean no-op.
        assert scp.running is False

    def test_start_succeeds_when_start_server_blocks(
            self, qapp, tmp_path, monkeypatch):
        scp = StorageSCP("TEST_AE", 11112, str(tmp_path))
        gate = threading.Event()

        def block(*_a, **_kw):
            # Simulate a successful bind by blocking the reactor thread
            # the way pynetdicom's real start_server(block=True) does.
            gate.wait(timeout=2.0)

        with patch("core.storage_scp.AE") as ae_cls:
            ae_instance = MagicMock()
            ae_instance.start_server.side_effect = block
            ae_cls.return_value = ae_instance
            try:
                scp.start()  # must NOT raise
                assert scp.running is True
            finally:
                gate.set()
                # Let the daemon thread observe the gate flip and exit.
                if scp._thread is not None:
                    scp._thread.join(timeout=2.0)
