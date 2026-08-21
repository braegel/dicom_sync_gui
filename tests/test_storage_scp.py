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
from pydicom.uid import (
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEG2000,
    JPEG2000Lossless,
    JPEGBaseline8Bit,
)

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

    def test_start_succeeds_when_bind_succeeds(self, qapp, tmp_path):
        """A successful bind returns the server object and leaves the
        SCP running — without the caller ever blocking."""
        scp = StorageSCP("TEST_AE", 11112, str(tmp_path))

        with patch("core.storage_scp.AE") as ae_cls:
            ae_instance = MagicMock()
            ae_instance.start_server.return_value = MagicMock()
            ae_cls.return_value = ae_instance

            scp.start()  # must NOT raise

            assert scp.running is True
            assert scp.ae is ae_instance

    def test_start_requests_a_non_blocking_server(self, qapp, tmp_path):
        """``block=False`` is what makes the bind happen (and fail)
        synchronously in the caller's thread — the caller is the GUI
        thread, so a blocking server plus a fixed sleep would freeze the
        UI on every service start."""
        scp = StorageSCP("TEST_AE", 11112, str(tmp_path))

        with patch("core.storage_scp.AE") as ae_cls:
            ae_instance = MagicMock()
            ae_cls.return_value = ae_instance
            scp.start()

        _args, kwargs = ae_instance.start_server.call_args
        assert kwargs["block"] is False

    def test_failed_bind_shuts_the_ae_down(self, qapp, tmp_path):
        """A half-constructed AE must not be left behind — its threads
        would outlive the failed start."""
        scp = StorageSCP("TEST_AE", 11112, str(tmp_path))

        with patch("core.storage_scp.AE") as ae_cls:
            ae_instance = MagicMock()
            ae_instance.start_server.side_effect = OSError("port in use")
            ae_cls.return_value = ae_instance

            with pytest.raises(RuntimeError, match="failed to bind"):
                scp.start()

        ae_instance.shutdown.assert_called_once()
        assert scp.ae is None


class TestStorageSCPTransferSyntaxPreference:
    """The SCP is the association ACCEPTOR, so ITS ordering decides which
    transfer syntax a sender ends up using.  Getting that order wrong is
    not cosmetic: a PACS was observed sending JPEG 2000 pixel data
    regardless of what was negotiated, so accepting an uncompressed
    syntax from it produced mislabelled objects that a DCMTK-based
    receiver downstream rejects.  See STORAGE_TRANSFER_SYNTAXES."""

    def test_lossless_compressed_ranks_above_uncompressed(self):
        order = storage_scp_module.STORAGE_TRANSFER_SYNTAXES
        assert order.index(JPEG2000Lossless) < order.index(ImplicitVRLittleEndian)
        assert order.index(JPEG2000Lossless) < order.index(ExplicitVRLittleEndian)

    def test_lossy_ranks_below_uncompressed(self):
        """A sender offering both lossy and uncompressed can serve
        either — picking lossy would discard image data for nothing."""
        order = storage_scp_module.STORAGE_TRANSFER_SYNTAXES
        for lossy in (JPEG2000, JPEGBaseline8Bit):
            assert order.index(ExplicitVRLittleEndian) < order.index(lossy)
            assert order.index(ImplicitVRLittleEndian) < order.index(lossy)

    def test_uncompressed_syntaxes_are_still_offered(self):
        """Preferring compressed must not mean refusing plain senders."""
        order = storage_scp_module.STORAGE_TRANSFER_SYNTAXES
        assert ImplicitVRLittleEndian in order
        assert ExplicitVRLittleEndian in order

    def test_storage_contexts_are_built_with_that_order(self, qapp, tmp_path):
        """The preference list is worthless unless the contexts actually
        carry it — pynetdicom's default storage contexts offer only the
        uncompressed syntaxes."""
        scp = StorageSCP("TEST_AE", 11112, str(tmp_path))
        with patch("core.storage_scp.AE") as ae_cls:
            ae_instance = MagicMock()
            ae_cls.return_value = ae_instance
            scp.start()

        offered = [
            call.args[1] for call in ae_instance.add_supported_context.call_args_list
        ]
        assert offered, "no supported contexts were registered"
        for syntaxes in offered:
            assert syntaxes is storage_scp_module.STORAGE_TRANSFER_SYNTAXES


class TestStorageSCPRecreatesVanishedStorageDir:
    """The storage folder is watched by a local PACS that imports what
    lands in it and then deletes the emptied directory.  Without a retry
    every image after that first import fails with ENOENT until the
    service is restarted."""

    def _event(self, sop_uid, written):
        ds = MagicMock()
        ds.SOPInstanceUID = sop_uid
        ds.file_meta = MagicMock()

        def save_as(path, **_kw):
            if not os.path.isdir(os.path.dirname(path)):
                raise FileNotFoundError(2, "No such file or directory", path)
            open(path, "wb").close()
            written.append(path)

        ds.save_as = save_as
        event = MagicMock()
        event.dataset = ds
        event.file_meta = MagicMock()
        return event

    def test_store_succeeds_after_directory_is_removed(self, qapp, tmp_path):
        storage = tmp_path / "incoming"
        scp = StorageSCP("TEST_AE", 11112, str(storage))
        written = []

        assert scp.handle_store(self._event("1.2.3", written)) == 0x0000
        # Local PACS imports the file and takes the directory with it.
        os.remove(written[0])
        os.rmdir(storage)

        assert scp.handle_store(self._event("1.2.4", written)) == 0x0000
        assert scp.images_received == 2
        assert storage.is_dir()


class TestStorageSCPPreferredSyntax:
    """The node's "Preferred Syntax" is the RECEIVER's preference, so it
    only ever had a meaning for the built-in SCP — when the local PACS
    receives, that negotiation happens on an association this app is not
    part of.  It used to be stored and never read."""

    def test_no_preference_keeps_the_default_order(self, qapp, tmp_path):
        scp = StorageSCP("AE", 11112, str(tmp_path))
        assert scp.transfer_syntaxes is storage_scp_module.STORAGE_TRANSFER_SYNTAXES

    def test_preference_moves_to_the_front(self, qapp, tmp_path):
        scp = StorageSCP("AE", 11112, str(tmp_path),
                         preferred_syntax=ImplicitVRLittleEndian)
        assert scp.transfer_syntaxes[0] == ImplicitVRLittleEndian

    def test_other_syntaxes_are_kept_as_fallbacks(self, qapp, tmp_path):
        """Choosing a syntax a given sender does not offer must degrade
        to the default order, not fail the association."""
        scp = StorageSCP("AE", 11112, str(tmp_path),
                         preferred_syntax=ImplicitVRLittleEndian)
        assert set(storage_scp_module.STORAGE_TRANSFER_SYNTAXES) <= set(
            scp.transfer_syntaxes)
        assert scp.transfer_syntaxes.count(ImplicitVRLittleEndian) == 1

    def test_syntax_outside_the_default_list_is_honoured(self, qapp, tmp_path):
        """The UI offers a few syntaxes the default order does not
        enumerate — picking one must still take effect."""
        jpeg_lossless = "1.2.840.10008.1.2.4.57"
        scp = StorageSCP("AE", 11112, str(tmp_path),
                         preferred_syntax=jpeg_lossless)
        assert scp.transfer_syntaxes[0] == jpeg_lossless

    def test_contexts_are_registered_with_the_preference(self, qapp, tmp_path):
        scp = StorageSCP("AE", 11112, str(tmp_path),
                         preferred_syntax=ImplicitVRLittleEndian)
        with patch("core.storage_scp.AE") as ae_cls:
            ae_instance = MagicMock()
            ae_cls.return_value = ae_instance
            scp.start()
        for call in ae_instance.add_supported_context.call_args_list:
            assert call.args[1][0] == ImplicitVRLittleEndian
