"""
Configuration manager for DICOM Sync GUI.
Handles loading/saving of PACS configurations and application preferences.
"""

import json
import os
import platform
import socket
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG_FILE = "dicom_sync_config.json"

TRANSFER_SYNTAXES_NAMES = [
    "JPEG2000Lossless",
    "ExplicitVRLittleEndian",
    "ImplicitVRLittleEndian",
    "JPEGLossless",
    "JPEGLosslessSV1",
    "DeflatedExplicitVRLittleEndian",
]

RETRIEVE_METHODS = ["C-MOVE", "C-GET"]


def get_local_ip() -> str:
    """Get local IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class PacsNode:
    """Represents a PACS node (local or remote)."""

    def __init__(self, name: str = "", ae_title: str = "", ip_address: str = "",
                 port: int = 104, transfer_syntax: str = "JPEG2000Lossless",
                 retrieve_method: str = "C-MOVE"):
        self.name = name
        self.ae_title = ae_title
        self.ip_address = ip_address
        self.port = port
        self.transfer_syntax = transfer_syntax
        self.retrieve_method = retrieve_method  # "C-MOVE" or "C-GET"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ae_title": self.ae_title,
            "ip_address": self.ip_address,
            "port": self.port,
            "transfer_syntax": self.transfer_syntax,
            "retrieve_method": self.retrieve_method,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PacsNode":
        return cls(
            name=data.get("name", ""),
            ae_title=data.get("ae_title", ""),
            ip_address=data.get("ip_address", ""),
            port=data.get("port", 104),
            transfer_syntax=data.get("transfer_syntax", "JPEG2000Lossless"),
            retrieve_method=data.get("retrieve_method", "C-MOVE"),
        )


class AppConfig:
    """Application configuration."""

    def __init__(self, config_path: str = ""):
        self.config_path = config_path or self._default_config_path()
        self.local_node = PacsNode(
            name="Local PACS",
            ae_title="LOCAL_AE",
            ip_address=get_local_ip(),
            port=11112,
            transfer_syntax="JPEG2000Lossless",
        )
        self.remote_nodes: Dict[str, PacsNode] = {}

        # Fallback storage: download to folder if local PACS is not available
        self.fallback_storage_enabled: bool = False
        self.fallback_storage_path: str = os.path.expanduser("~/DICOM_Incoming")

        # Prior studies
        self.prior_studies_count: int = 0  # 0 = disabled
        self.prior_studies_same_modality: bool = False

        # Filter groups
        self.filter_group_names: List[str] = []  # ordered list of group names
        self.institution_assignments: Dict[str, str] = {}  # {institution: group_name}
        self.active_filter_groups: List[str] = []  # groups selected in dashboard
        self.filter_groups_enabled: bool = False  # master switch in dashboard

        # Download service defaults (can be overridden in dashboard)
        self.default_hours: int = 3
        self.max_images: int = 0  # 0 = no limit
        self.sync_interval: int = 60  # seconds to wait between query cycles

    @staticmethod
    def _default_config_path() -> str:
        """Platform-independent config location."""
        system = platform.system()
        if system == "Windows":
            base = os.environ.get("APPDATA", os.path.expanduser("~"))
        elif system == "Darwin":
            base = os.path.expanduser("~/Library/Application Support")
        else:
            base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        path = os.path.join(base, "DicomSyncGUI")
        os.makedirs(path, exist_ok=True)
        return os.path.join(path, DEFAULT_CONFIG_FILE)

    def load(self) -> bool:
        """Load configuration from file."""
        if not os.path.exists(self.config_path):
            return False
        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)

            if "local" in data:
                self.local_node = PacsNode.from_dict(data["local"])

            self.remote_nodes = {}
            for key, val in data.get("remotes", {}).items():
                self.remote_nodes[key] = PacsNode.from_dict(val)

            # Migrate old single-remote format
            if "remote" in data and "remotes" not in data:
                self.remote_nodes["default"] = PacsNode.from_dict(data["remote"])

            self.fallback_storage_enabled = data.get("fallback_storage_enabled", False)
            self.fallback_storage_path = data.get("fallback_storage_path",
                                                   os.path.expanduser("~/DICOM_Incoming"))
            self.prior_studies_count = data.get("prior_studies_count", 0)
            self.prior_studies_same_modality = data.get("prior_studies_same_modality", False)
            self.filter_group_names = data.get("filter_group_names", [])
            self.institution_assignments = data.get(
                "institution_assignments", {})
            self.active_filter_groups = data.get(
                "active_filter_groups", [])
            self.filter_groups_enabled = data.get(
                "filter_groups_enabled", False)
            self.default_hours = data.get("default_hours", 3)
            self.max_images = data.get("max_images", 0)
            self.sync_interval = data.get("sync_interval", 60)
            return True
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Config load error: {e}")
            return False

    def save(self):
        """Save configuration to file."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        data = {
            "local": self.local_node.to_dict(),
            "remotes": {k: v.to_dict() for k, v in self.remote_nodes.items()},
            "fallback_storage_enabled": self.fallback_storage_enabled,
            "fallback_storage_path": self.fallback_storage_path,
            "prior_studies_count": self.prior_studies_count,
            "prior_studies_same_modality": self.prior_studies_same_modality,
            "filter_group_names": self.filter_group_names,
            "institution_assignments": self.institution_assignments,
            "active_filter_groups": self.active_filter_groups,
            "filter_groups_enabled": self.filter_groups_enabled,
            "default_hours": self.default_hours,
            "max_images": self.max_images,
            "sync_interval": self.sync_interval,
        }
        with open(self.config_path, "w") as f:
            json.dump(data, f, indent=2)

    def get_remote_names(self) -> List[str]:
        return list(self.remote_nodes.keys())

    def get_local_dict(self) -> Dict[str, Any]:
        return self.local_node.to_dict()

    def get_remote_dict(self, name: str) -> Optional[Dict[str, Any]]:
        node = self.remote_nodes.get(name)
        return node.to_dict() if node else None

    def update_local_ip(self):
        new_ip = get_local_ip()
        if self.local_node.ip_address != new_ip:
            self.local_node.ip_address = new_ip
            self.save()
