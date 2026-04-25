"""
Tests for core.StorageSCP — signal emission and image counting.
"""
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QCoreApplication

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

    def test_images_received_not_incremented_on_failure(self, scp, tmp_path):
        event = MagicMock()
        event.dataset.SOPInstanceUID = "1.2.3"
        event.dataset.file_meta = MagicMock()
        event.dataset.save_as.side_effect = OSError("disk full")
        event.file_meta = event.dataset.file_meta

        scp.handle_store(event)
        assert scp.images_received == 0
