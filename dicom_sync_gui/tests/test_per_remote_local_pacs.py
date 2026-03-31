"""
Tests for per-remote local PACS configuration and automatic fallback SCP.

Feature 1: Each remote PACS can have its own local PACS destination.
           Different remote PACSes use different local PACSes.

Feature 2: If the configured local PACS is not reachable (C-ECHO fails),
           the engine should automatically spawn a local StorageSCP to
           receive the data.
"""

import json
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import AppConfig, PacsNode


# ============================================================================
# Feature 1: Per-remote local PACS configuration
# ============================================================================

class TestPerRemoteLocalPacsConfig:
    """AppConfig supports a separate local PACS for each remote PACS."""

    def test_local_nodes_dict_exists(self, tmp_config_path):
        """AppConfig should have a local_nodes dict mapping remote_key -> PacsNode."""
        config = AppConfig(config_path=tmp_config_path)
        assert hasattr(config, "local_nodes")
        assert isinstance(config.local_nodes, dict)

    def test_assign_local_node_per_remote(self, tmp_config_path):
        """Each remote key can have its own local PACS node."""
        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT Scanner", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.remote_nodes["mri"] = PacsNode(
            name="MRI Unit", ae_title="MRI_AE",
            ip_address="192.168.1.20", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.local_nodes["mri"] = PacsNode(
            name="Local MRI", ae_title="LOCAL_MRI",
            ip_address="127.0.0.1", port=11113,
        )

        assert config.local_nodes["ct"].ae_title == "LOCAL_CT"
        assert config.local_nodes["ct"].port == 11112
        assert config.local_nodes["mri"].ae_title == "LOCAL_MRI"
        assert config.local_nodes["mri"].port == 11113

    def test_get_local_dict_for_returns_per_remote_config(self, tmp_config_path):
        """get_local_dict_for(remote_key) returns the local config for that remote."""
        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT Scanner", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )

        local_dict = config.get_local_dict_for("ct")
        assert local_dict["ae_title"] == "LOCAL_CT"
        assert local_dict["port"] == 11112

    def test_get_local_dict_for_falls_back_to_default(self, tmp_config_path):
        """If no per-remote local is configured, fall back to the default local_node."""
        config = AppConfig(config_path=tmp_config_path)
        config.local_node = PacsNode(
            name="Default Local", ae_title="DEFAULT_AE",
            ip_address="127.0.0.1", port=11112,
        )
        config.remote_nodes["ct"] = PacsNode(
            name="CT Scanner", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        # No local_nodes["ct"] set

        local_dict = config.get_local_dict_for("ct")
        assert local_dict["ae_title"] == "DEFAULT_AE"
        assert local_dict["port"] == 11112

    def test_save_load_preserves_local_nodes(self, tmp_config_path):
        """Per-remote local nodes survive a save/load round trip."""
        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT Scanner", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.remote_nodes["mri"] = PacsNode(
            name="MRI Unit", ae_title="MRI_AE",
            ip_address="192.168.1.20", port=104,
        )
        config.local_nodes["mri"] = PacsNode(
            name="Local MRI", ae_title="LOCAL_MRI",
            ip_address="127.0.0.1", port=11113,
        )
        config.save()

        loaded = AppConfig(config_path=tmp_config_path)
        loaded.load()

        assert "ct" in loaded.local_nodes
        assert "mri" in loaded.local_nodes
        assert loaded.local_nodes["ct"].ae_title == "LOCAL_CT"
        assert loaded.local_nodes["ct"].port == 11112
        assert loaded.local_nodes["mri"].ae_title == "LOCAL_MRI"
        assert loaded.local_nodes["mri"].port == 11113

    def test_save_includes_local_nodes_in_json(self, tmp_config_path):
        """The saved JSON file should contain a 'local_nodes' section."""
        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT Scanner", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.save()

        with open(tmp_config_path) as f:
            data = json.load(f)

        assert "local_nodes" in data
        assert "ct" in data["local_nodes"]
        assert data["local_nodes"]["ct"]["ae_title"] == "LOCAL_CT"

    def test_migration_no_local_nodes_uses_default(self, tmp_config_path):
        """Loading an old config without local_nodes should work (empty dict)."""
        old_config = {
            "local": {"name": "Local", "ae_title": "OLD_AE",
                      "ip_address": "127.0.0.1", "port": 11112},
            "remotes": {
                "ct": {"name": "CT", "ae_title": "CT_AE",
                       "ip_address": "10.0.0.1", "port": 104},
            },
        }
        with open(tmp_config_path, "w") as f:
            json.dump(old_config, f)

        config = AppConfig(config_path=tmp_config_path)
        config.load()

        assert isinstance(config.local_nodes, dict)
        # No per-remote local nodes in old config, so dict should be empty
        assert len(config.local_nodes) == 0
        # Fallback should still work
        local_dict = config.get_local_dict_for("ct")
        assert local_dict["ae_title"] == "OLD_AE"


class TestTransferEngineUsesPerRemoteLocal:
    """TransferEngine should use the per-remote local PACS when available."""

    def test_make_dicom_ops_uses_per_remote_local(self, tmp_config_path):
        """_make_dicom_ops should pass the per-remote local config."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT Scanner", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )

        engine = TransferEngine(config, "ct")

        with patch("core.transfer_engine.DicomOperations") as MockOps:
            engine._make_dicom_ops()
            call_args = MockOps.call_args
            local_dict_used = call_args[0][0]  # first positional arg
            assert local_dict_used["ae_title"] == "LOCAL_CT"
            assert local_dict_used["port"] == 11112

    def test_make_dicom_ops_falls_back_to_default_local(self, tmp_config_path):
        """Without a per-remote local, _make_dicom_ops uses the default local_node."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.local_node = PacsNode(
            name="Default", ae_title="DEFAULT_AE",
            ip_address="127.0.0.1", port=11112,
        )
        config.remote_nodes["ct"] = PacsNode(
            name="CT Scanner", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        # No local_nodes["ct"] — should use get_local_dict_for which falls back

        engine = TransferEngine(config, "ct")

        # Engine must call get_local_dict_for (not get_local_dict)
        with patch.object(config, "get_local_dict_for",
                          return_value=config.local_node.to_dict()) as mock_method, \
             patch("core.transfer_engine.DicomOperations") as MockOps:
            engine._make_dicom_ops()
            mock_method.assert_called_once_with("ct")
            local_dict_used = MockOps.call_args[0][0]
            assert local_dict_used["ae_title"] == "DEFAULT_AE"

    def test_different_remotes_get_different_locals(self, tmp_config_path):
        """Two engines for different remotes should use different local configs."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.remote_nodes["mri"] = PacsNode(
            name="MRI", ae_title="MRI_AE",
            ip_address="192.168.1.20", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.local_nodes["mri"] = PacsNode(
            name="Local MRI", ae_title="LOCAL_MRI",
            ip_address="127.0.0.1", port=11113,
        )

        engine_ct = TransferEngine(config, "ct")
        engine_mri = TransferEngine(config, "mri")

        with patch("core.transfer_engine.DicomOperations") as MockOps:
            engine_ct._make_dicom_ops()
            ct_local = MockOps.call_args[0][0]

            engine_mri._make_dicom_ops()
            mri_local = MockOps.call_args[0][0]

        assert ct_local["ae_title"] == "LOCAL_CT"
        assert ct_local["port"] == 11112
        assert mri_local["ae_title"] == "LOCAL_MRI"
        assert mri_local["port"] == 11113


# ============================================================================
# Feature 2: Auto-spawn local DICOM server when local PACS is unreachable
# ============================================================================

class TestAutoSpawnFallbackSCP:
    """When the local PACS is not reachable, the engine should start a StorageSCP."""

    def test_engine_checks_local_pacs_reachability(self, tmp_config_path):
        """At the start of service, engine should C-ECHO the local PACS."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.fallback_storage_enabled = True
        config.fallback_storage_path = "/tmp/dicom_fallback"

        engine = TransferEngine(config, "ct")

        with patch("core.transfer_engine.DicomOperations") as MockOps:
            mock_ops_instance = MagicMock()
            MockOps.return_value = mock_ops_instance
            mock_ops_instance.c_echo.return_value = True
            mock_ops_instance.c_find_studies.return_value = []

            engine._run_one_cycle(hours=3, max_images=0)

            mock_ops_instance.c_echo.assert_called_with(target="local")

    def test_engine_starts_scp_when_local_unreachable(self, tmp_config_path):
        """If local PACS C-ECHO fails, engine should spawn a StorageSCP."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.fallback_storage_enabled = True
        config.fallback_storage_path = "/tmp/dicom_fallback"

        engine = TransferEngine(config, "ct")

        with patch("core.transfer_engine.DicomOperations") as MockOps, \
             patch("core.transfer_engine.StorageSCP") as MockSCP:
            mock_ops_instance = MagicMock()
            MockOps.return_value = mock_ops_instance
            mock_ops_instance.c_echo.return_value = False  # local unreachable
            mock_ops_instance.c_find_studies.return_value = []

            mock_scp_instance = MagicMock()
            MockSCP.return_value = mock_scp_instance

            engine._run_one_cycle(hours=3, max_images=0)

            MockSCP.assert_called_once()
            mock_scp_instance.start.assert_called_once()

    def test_scp_uses_correct_config(self, tmp_config_path):
        """The fallback SCP should use the local PACS ae_title, port, and storage path."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.fallback_storage_enabled = True
        config.fallback_storage_path = "/tmp/dicom_fallback"

        engine = TransferEngine(config, "ct")

        with patch("core.transfer_engine.DicomOperations") as MockOps, \
             patch("core.transfer_engine.StorageSCP") as MockSCP:
            mock_ops_instance = MagicMock()
            MockOps.return_value = mock_ops_instance
            mock_ops_instance.c_echo.return_value = False
            mock_ops_instance.c_find_studies.return_value = []

            mock_scp_instance = MagicMock()
            MockSCP.return_value = mock_scp_instance

            engine._run_one_cycle(hours=3, max_images=0)

            args, kwargs = MockSCP.call_args
            # StorageSCP(ae_title, port, storage_path)
            assert args[0] == "LOCAL_CT"
            assert args[1] == 11112

    def test_no_scp_when_local_reachable(self, tmp_config_path):
        """If local PACS C-ECHO succeeds, no SCP should be spawned."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.fallback_storage_enabled = True
        config.fallback_storage_path = "/tmp/dicom_fallback"

        engine = TransferEngine(config, "ct")

        with patch("core.transfer_engine.DicomOperations") as MockOps, \
             patch("core.transfer_engine.StorageSCP") as MockSCP:
            mock_ops_instance = MagicMock()
            MockOps.return_value = mock_ops_instance
            mock_ops_instance.c_echo.return_value = True  # local reachable
            mock_ops_instance.c_find_studies.return_value = []

            engine._run_one_cycle(hours=3, max_images=0)

            MockSCP.assert_not_called()

    def test_no_scp_when_fallback_disabled(self, tmp_config_path):
        """If fallback storage is disabled, no SCP even if local is unreachable."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.fallback_storage_enabled = False

        engine = TransferEngine(config, "ct")

        with patch("core.transfer_engine.DicomOperations") as MockOps, \
             patch("core.transfer_engine.StorageSCP") as MockSCP:
            mock_ops_instance = MagicMock()
            MockOps.return_value = mock_ops_instance
            mock_ops_instance.c_echo.return_value = False
            mock_ops_instance.c_find_studies.return_value = []

            engine._run_one_cycle(hours=3, max_images=0)

            MockSCP.assert_not_called()

    def test_scp_stopped_when_engine_stops(self, tmp_config_path):
        """When the engine stops, any running fallback SCP should be stopped."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.fallback_storage_enabled = True
        config.fallback_storage_path = "/tmp/dicom_fallback"

        engine = TransferEngine(config, "ct")

        mock_scp = MagicMock()
        mock_scp.running = True
        engine._fallback_scp = mock_scp

        engine.stop()
        # After stopping, the SCP should be shut down
        # Give the engine a moment to clean up
        assert hasattr(engine, "_fallback_scp")
        mock_scp.stop.assert_called_once()

    def test_scp_not_restarted_if_already_running(self, tmp_config_path):
        """If a fallback SCP is already running, don't start a second one."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.fallback_storage_enabled = True
        config.fallback_storage_path = "/tmp/dicom_fallback"

        engine = TransferEngine(config, "ct")

        existing_scp = MagicMock()
        existing_scp.running = True
        engine._fallback_scp = existing_scp

        with patch("core.transfer_engine.DicomOperations") as MockOps, \
             patch("core.transfer_engine.StorageSCP") as MockSCP:
            mock_ops_instance = MagicMock()
            MockOps.return_value = mock_ops_instance
            mock_ops_instance.c_echo.return_value = False
            mock_ops_instance.c_find_studies.return_value = []

            engine._run_one_cycle(hours=3, max_images=0)

            # Should NOT create a new SCP since one is already running
            MockSCP.assert_not_called()

    def test_engine_has_fallback_scp_attribute(self, tmp_config_path):
        """TransferEngine should have a _fallback_scp attribute (initially None)."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )

        engine = TransferEngine(config, "ct")
        assert hasattr(engine, "_fallback_scp")
        assert engine._fallback_scp is None

    def test_fallback_scp_uses_per_remote_storage_path(self, tmp_config_path):
        """Each remote's fallback SCP should store to a remote-specific subdirectory."""
        from core.transfer_engine import TransferEngine

        config = AppConfig(config_path=tmp_config_path)
        config.remote_nodes["ct"] = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="192.168.1.10", port=104,
        )
        config.local_nodes["ct"] = PacsNode(
            name="Local CT", ae_title="LOCAL_CT",
            ip_address="127.0.0.1", port=11112,
        )
        config.fallback_storage_enabled = True
        config.fallback_storage_path = "/tmp/dicom_fallback"

        engine = TransferEngine(config, "ct")

        with patch("core.transfer_engine.DicomOperations") as MockOps, \
             patch("core.transfer_engine.StorageSCP") as MockSCP:
            mock_ops_instance = MagicMock()
            MockOps.return_value = mock_ops_instance
            mock_ops_instance.c_echo.return_value = False
            mock_ops_instance.c_find_studies.return_value = []

            mock_scp_instance = MagicMock()
            MockSCP.return_value = mock_scp_instance

            engine._run_one_cycle(hours=3, max_images=0)

            call_args = MockSCP.call_args
            # The storage path should include the remote key for isolation
            storage_path_used = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("storage_path")
            assert "ct" in storage_path_used
