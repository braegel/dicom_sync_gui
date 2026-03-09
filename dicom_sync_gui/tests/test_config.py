"""
Tests for core.config — PacsNode and AppConfig.
"""

import json
import os
import platform
from unittest.mock import patch

import pytest

from core.config import (
    AppConfig, PacsNode, TRANSFER_SYNTAXES_NAMES, RETRIEVE_METHODS,
    DEFAULT_CONFIG_FILE, get_local_ip,
)


# ═══════════════════════════════════════════════════════════════════════════
# PacsNode
# ═══════════════════════════════════════════════════════════════════════════

class TestPacsNode:

    def test_default_values(self):
        node = PacsNode()
        assert node.name == ""
        assert node.ae_title == ""
        assert node.ip_address == ""
        assert node.port == 104
        assert node.transfer_syntax == "JPEG2000Lossless"
        assert node.retrieve_method == "C-MOVE"
        assert node.hours == 3
        assert node.max_images == 0
        assert node.sync_interval == 60

    def test_custom_values(self, sample_pacs_node):
        node = sample_pacs_node
        assert node.name == "Test PACS"
        assert node.ae_title == "TEST_AE"
        assert node.ip_address == "10.0.0.1"
        assert node.port == 4242
        assert node.transfer_syntax == "JPEGLossless"
        assert node.retrieve_method == "C-GET"

    def test_custom_service_params(self):
        node = PacsNode(hours=12, max_images=500, sync_interval=120)
        assert node.hours == 12
        assert node.max_images == 500
        assert node.sync_interval == 120

    def test_to_dict(self, sample_pacs_node):
        d = sample_pacs_node.to_dict()
        assert isinstance(d, dict)
        assert d["name"] == "Test PACS"
        assert d["ae_title"] == "TEST_AE"
        assert d["ip_address"] == "10.0.0.1"
        assert d["port"] == 4242
        assert d["transfer_syntax"] == "JPEGLossless"
        assert d["retrieve_method"] == "C-GET"
        assert d["hours"] == 3  # default
        assert d["max_images"] == 0  # default
        assert d["sync_interval"] == 60  # default

    def test_to_dict_custom_service_params(self):
        node = PacsNode(name="X", hours=24, max_images=1000, sync_interval=300)
        d = node.to_dict()
        assert d["hours"] == 24
        assert d["max_images"] == 1000
        assert d["sync_interval"] == 300

    def test_from_dict_full(self):
        data = {
            "name": "Remote", "ae_title": "REM_AE",
            "ip_address": "192.168.0.1", "port": 11113,
            "transfer_syntax": "ExplicitVRLittleEndian",
            "retrieve_method": "C-GET",
            "hours": 12, "max_images": 200, "sync_interval": 90,
        }
        node = PacsNode.from_dict(data)
        assert node.name == "Remote"
        assert node.ae_title == "REM_AE"
        assert node.port == 11113
        assert node.retrieve_method == "C-GET"
        assert node.hours == 12
        assert node.max_images == 200
        assert node.sync_interval == 90

    def test_from_dict_defaults(self):
        node = PacsNode.from_dict({})
        assert node.name == ""
        assert node.port == 104
        assert node.transfer_syntax == "JPEG2000Lossless"
        assert node.retrieve_method == "C-MOVE"
        assert node.hours == 3
        assert node.max_images == 0
        assert node.sync_interval == 60

    def test_roundtrip(self, sample_pacs_node):
        d = sample_pacs_node.to_dict()
        restored = PacsNode.from_dict(d)
        assert restored.name == sample_pacs_node.name
        assert restored.port == sample_pacs_node.port
        assert restored.retrieve_method == sample_pacs_node.retrieve_method
        assert restored.hours == sample_pacs_node.hours
        assert restored.max_images == sample_pacs_node.max_images
        assert restored.sync_interval == sample_pacs_node.sync_interval

    def test_roundtrip_custom_service_params(self):
        node = PacsNode(
            name="Test", ae_title="T_AE",
            hours=48, max_images=9999, sync_interval=10,
        )
        restored = PacsNode.from_dict(node.to_dict())
        assert restored.hours == 48
        assert restored.max_images == 9999
        assert restored.sync_interval == 10


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig — basic properties
# ═══════════════════════════════════════════════════════════════════════════

class TestAppConfigDefaults:

    def test_default_local_node(self, default_config):
        assert default_config.local_node.name == "Local PACS"
        assert default_config.local_node.ae_title == "LOCAL_AE"
        assert default_config.local_node.port == 11112

    def test_default_remote_nodes_empty(self, default_config):
        assert default_config.remote_nodes == {}

    def test_default_fallback(self, default_config):
        assert default_config.fallback_storage_enabled is False
        assert "DICOM_Incoming" in default_config.fallback_storage_path

    def test_default_prior_studies(self, default_config):
        assert default_config.prior_studies_count == 0
        assert default_config.prior_studies_same_modality is False

    def test_default_filter_groups(self, default_config):
        assert default_config.filter_group_names == []
        assert default_config.institution_assignments == {}
        assert default_config.active_filter_groups == []
        assert default_config.filter_groups_enabled is False

    def test_default_service_params(self, default_config):
        assert default_config.default_hours == 3
        assert default_config.max_images == 0
        assert default_config.sync_interval == 60


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig — save and load
# ═══════════════════════════════════════════════════════════════════════════

class TestAppConfigPersistence:

    def test_save_creates_file(self, populated_config):
        populated_config.save()
        assert os.path.exists(populated_config.config_path)

    def test_save_produces_valid_json(self, populated_config):
        populated_config.save()
        with open(populated_config.config_path) as f:
            data = json.load(f)
        assert "local" in data
        assert "remotes" in data
        assert "filter_group_names" in data

    def test_load_nonexistent_returns_false(self, tmp_config_path):
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is False

    def test_save_then_load_roundtrip(self, populated_config):
        populated_config.save()

        loaded = AppConfig(config_path=populated_config.config_path)
        assert loaded.load() is True

        # Local node
        assert loaded.local_node.ae_title == "LOCAL_AE"
        assert loaded.local_node.port == 11112

        # Remote nodes
        assert "ct" in loaded.remote_nodes
        assert "mri" in loaded.remote_nodes
        assert loaded.remote_nodes["ct"].name == "CT Scanner"
        assert loaded.remote_nodes["mri"].retrieve_method == "C-GET"

        # Per-source service parameters on remote nodes
        assert loaded.remote_nodes["ct"].hours == 3
        assert loaded.remote_nodes["ct"].max_images == 0
        assert loaded.remote_nodes["ct"].sync_interval == 60
        assert loaded.remote_nodes["mri"].hours == 24
        assert loaded.remote_nodes["mri"].max_images == 1000
        assert loaded.remote_nodes["mri"].sync_interval == 300

        # Fallback
        assert loaded.fallback_storage_enabled is True
        assert loaded.fallback_storage_path == "/tmp/dicom_test"

        # Prior studies
        assert loaded.prior_studies_count == 2
        assert loaded.prior_studies_same_modality is True

        # Filter groups
        assert loaded.filter_group_names == ["Group A", "Group B", "Group C"]
        assert loaded.institution_assignments["Hospital Alpha"] == "Group A"
        assert loaded.active_filter_groups == ["Group A"]
        assert loaded.filter_groups_enabled is True

        # Legacy global service defaults
        assert loaded.default_hours == 6
        assert loaded.max_images == 500
        assert loaded.sync_interval == 120

    def test_load_corrupt_json(self, tmp_config_path):
        with open(tmp_config_path, "w") as f:
            f.write("{invalid json!!}")
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is False

    def test_load_empty_json(self, tmp_config_path):
        with open(tmp_config_path, "w") as f:
            json.dump({}, f)
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is True
        assert config.remote_nodes == {}

    def test_migrate_old_single_remote_format(self, tmp_config_path):
        """Old configs had 'remote' key instead of 'remotes'."""
        old_data = {
            "local": PacsNode(name="L", ae_title="L_AE").to_dict(),
            "remote": PacsNode(name="R", ae_title="R_AE", port=105).to_dict(),
        }
        with open(tmp_config_path, "w") as f:
            json.dump(old_data, f)
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is True
        assert "default" in config.remote_nodes
        assert config.remote_nodes["default"].port == 105

    def test_migrate_old_remotes_without_per_source_params(self, tmp_config_path):
        """Old remotes without hours/max_images/sync_interval should inherit globals."""
        old_data = {
            "local": PacsNode(name="L", ae_title="L_AE").to_dict(),
            "remotes": {
                "ct": {
                    "name": "CT", "ae_title": "CT_AE",
                    "ip_address": "10.0.0.1", "port": 104,
                    "transfer_syntax": "JPEG2000Lossless",
                    "retrieve_method": "C-MOVE",
                    # No hours/max_images/sync_interval!
                },
            },
            "default_hours": 12,
            "max_images": 999,
            "sync_interval": 180,
        }
        with open(tmp_config_path, "w") as f:
            json.dump(old_data, f)
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is True
        # Should inherit the global values
        assert config.remote_nodes["ct"].hours == 12
        assert config.remote_nodes["ct"].max_images == 999
        assert config.remote_nodes["ct"].sync_interval == 180


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig — helper methods
# ═══════════════════════════════════════════════════════════════════════════

class TestAppConfigHelpers:

    def test_get_remote_names(self, populated_config):
        names = populated_config.get_remote_names()
        assert set(names) == {"ct", "mri"}

    def test_get_local_dict(self, populated_config):
        d = populated_config.get_local_dict()
        assert d["ae_title"] == "LOCAL_AE"

    def test_get_remote_dict_existing(self, populated_config):
        d = populated_config.get_remote_dict("ct")
        assert d is not None
        assert d["name"] == "CT Scanner"

    def test_get_remote_dict_missing(self, populated_config):
        assert populated_config.get_remote_dict("nonexistent") is None

    def test_update_local_ip(self, populated_config):
        """update_local_ip should save if IP changed."""
        populated_config.local_node.ip_address = "0.0.0.0"
        populated_config.save()
        populated_config.update_local_ip()
        # After update, ip should be different from 0.0.0.0
        # (either real IP or 127.0.0.1)
        assert populated_config.local_node.ip_address != "0.0.0.0"


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig — default config path
# ═══════════════════════════════════════════════════════════════════════════

class TestDefaultConfigPath:

    @patch("core.config.platform.system", return_value="Darwin")
    def test_macos_path(self, _mock):
        path = AppConfig._default_config_path()
        assert "Library/Application Support/DicomSyncGUI" in path
        assert path.endswith(DEFAULT_CONFIG_FILE)

    @patch("core.config.platform.system", return_value="Linux")
    def test_linux_path(self, _mock):
        path = AppConfig._default_config_path()
        assert "DicomSyncGUI" in path
        assert path.endswith(DEFAULT_CONFIG_FILE)

    @patch("core.config.platform.system", return_value="Windows")
    def test_windows_path(self, _mock):
        path = AppConfig._default_config_path()
        assert "DicomSyncGUI" in path


# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

class TestConstants:

    def test_transfer_syntaxes(self):
        assert "JPEG2000Lossless" in TRANSFER_SYNTAXES_NAMES
        assert "ExplicitVRLittleEndian" in TRANSFER_SYNTAXES_NAMES
        assert len(TRANSFER_SYNTAXES_NAMES) >= 5

    def test_retrieve_methods(self):
        assert RETRIEVE_METHODS == ["C-MOVE", "C-GET"]


class TestGetLocalIP:

    def test_returns_string(self):
        ip = get_local_ip()
        assert isinstance(ip, str)

    def test_returns_valid_ip_format(self):
        ip = get_local_ip()
        parts = ip.split(".")
        assert len(parts) == 4
        for p in parts:
            assert p.isdigit()

    @patch("socket.socket")
    def test_fallback_on_error(self, mock_sock):
        mock_sock.side_effect = OSError("Network unreachable")
        ip = get_local_ip()
        assert ip == "127.0.0.1"
