"""
Configuration manager for DICOM Sync GUI.
Handles loading/saving of PACS configurations and application preferences.

Architecture: each source PACS node carries its own *local destination*
settings (AE title, port, transfer syntax, fallback folder) so that
C-MOVE responses are directed correctly per source.
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
    """Represents a source PACS node with per-source service *and* local
    destination parameters.

    Remote-specific fields:
      - hours, max_images, sync_interval: service query parameters
      - local_ae_title, local_port, local_syntax: where C-MOVE should
        deliver images to (the local receiver for *this* source)
      - fallback_folder: directory to save images when no local PACS is
        reachable (a built-in SCP will be spawned automatically)
    """

    def __init__(self, name: str = "", ae_title: str = "", ip_address: str = "",
                 port: int = 104, transfer_syntax: str = "JPEG2000Lossless",
                 retrieve_method: str = "C-MOVE",
                 hours: int = 3, max_images: int = 0,
                 sync_interval: int = 60,
                 local_ae_title: str = "LOCAL_AE",
                 local_port: int = 11112,
                 local_syntax: str = "JPEG2000Lossless",
                 fallback_folder: str = ""):
        self.name = name
        self.ae_title = ae_title
        self.ip_address = ip_address
        self.port = port
        self.transfer_syntax = transfer_syntax
        self.retrieve_method = retrieve_method  # "C-MOVE" or "C-GET"
        # Per-source service parameters
        self.hours = hours
        self.max_images = max_images
        self.sync_interval = sync_interval
        # Per-source local destination (C-MOVE target)
        self.local_ae_title = local_ae_title
        self.local_port = local_port
        self.local_syntax = local_syntax
        self.fallback_folder = fallback_folder

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ae_title": self.ae_title,
            "ip_address": self.ip_address,
            "port": self.port,
            "transfer_syntax": self.transfer_syntax,
            "retrieve_method": self.retrieve_method,
            "hours": self.hours,
            "max_images": self.max_images,
            "sync_interval": self.sync_interval,
            "local_ae_title": self.local_ae_title,
            "local_port": self.local_port,
            "local_syntax": self.local_syntax,
            "fallback_folder": self.fallback_folder,
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
            hours=data.get("hours", 3),
            max_images=data.get("max_images", 0),
            sync_interval=data.get("sync_interval", 60),
            local_ae_title=data.get("local_ae_title", "LOCAL_AE"),
            local_port=data.get("local_port", 11112),
            local_syntax=data.get("local_syntax", "JPEG2000Lossless"),
            fallback_folder=data.get("fallback_folder", ""),
        )


class AppConfig:
    """Application configuration."""

    def __init__(self, config_path: str = ""):
        self.config_path = config_path or self._default_config_path()
        self.remote_nodes: Dict[str, PacsNode] = {}

        # Prior studies
        self.prior_studies_count: int = 0  # 0 = disabled
        self.prior_studies_same_modality: bool = False

        # Filter groups
        self.filter_group_names: List[str] = []  # ordered list of group names
        self.institution_assignments: Dict[str, str] = {}  # {institution: group_name}
        self.active_filter_groups: List[str] = []  # groups selected in dashboard
        self.filter_groups_enabled: bool = False  # master switch in dashboard
        self.filter_allow_small_series: bool = False  # download small series regardless of group
        self.filter_small_series_max: int = 20  # max images per series for the above

        # Legacy fields — kept for backward-compatible config loading.
        # New code reads per-source values from PacsNode directly.
        self.default_hours: int = 3
        self.max_images: int = 0
        self.sync_interval: int = 60
        # Legacy local node (used only during migration)
        self._legacy_local_node: Optional[Dict[str, Any]] = None
        self._legacy_fallback_enabled: bool = False
        self._legacy_fallback_path: str = ""

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

            self.remote_nodes = {}
            for key, val in data.get("remotes", {}).items():
                self.remote_nodes[key] = PacsNode.from_dict(val)

            # Migrate old single-remote format ("remote" dict → "remotes")
            if "remote" in data and "remotes" not in data:
                self.remote_nodes["default"] = PacsNode.from_dict(data["remote"])

            # Read legacy global values (needed for migration below)
            self.default_hours = data.get("default_hours", 3)
            self.max_images = data.get("max_images", 0)
            self.sync_interval = data.get("sync_interval", 60)

            # Legacy local node + fallback storage
            legacy_local = data.get("local", {})
            legacy_fallback_enabled = data.get("fallback_storage_enabled", False)
            legacy_fallback_path = data.get(
                "fallback_storage_path", os.path.expanduser("~/DICOM_Incoming"))
            self._legacy_local_node = legacy_local
            self._legacy_fallback_enabled = legacy_fallback_enabled
            self._legacy_fallback_path = legacy_fallback_path

            self.prior_studies_count = data.get("prior_studies_count", 0)
            self.prior_studies_same_modality = data.get("prior_studies_same_modality", False)
            self.filter_group_names = data.get("filter_group_names", [])
            self.institution_assignments = data.get(
                "institution_assignments", {})
            self.active_filter_groups = data.get(
                "active_filter_groups", [])
            self.filter_groups_enabled = data.get(
                "filter_groups_enabled", False)
            self.filter_allow_small_series = data.get(
                "filter_allow_small_series", False)
            self.filter_small_series_max = data.get(
                "filter_small_series_max", 20)

            # ── Migration: inject per-source fields from legacy globals ──
            remotes_raw = data.get("remotes", {})
            for key, node in self.remote_nodes.items():
                raw = remotes_raw.get(key, {})
                # Migrate service parameters
                if "hours" not in raw:
                    node.hours = self.default_hours
                    node.max_images = self.max_images
                    node.sync_interval = self.sync_interval
                # Migrate local destination from old global local_node
                if "local_ae_title" not in raw and legacy_local:
                    node.local_ae_title = legacy_local.get("ae_title", "LOCAL_AE")
                    node.local_port = legacy_local.get("port", 11112)
                    node.local_syntax = legacy_local.get(
                        "transfer_syntax", "JPEG2000Lossless")
                    if legacy_fallback_enabled:
                        node.fallback_folder = legacy_fallback_path

            print(f"Config loaded: {len(self.remote_nodes)} source(s) — "
                  f"{list(self.remote_nodes.keys())}")

            return True
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Config load error: {e}")
            return False

    def save(self):
        """Save configuration to file."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        data = {
            "remotes": {k: v.to_dict() for k, v in self.remote_nodes.items()},
            "prior_studies_count": self.prior_studies_count,
            "prior_studies_same_modality": self.prior_studies_same_modality,
            "filter_group_names": self.filter_group_names,
            "institution_assignments": self.institution_assignments,
            "active_filter_groups": self.active_filter_groups,
            "filter_groups_enabled": self.filter_groups_enabled,
            "filter_allow_small_series": self.filter_allow_small_series,
            "filter_small_series_max": self.filter_small_series_max,
            # Legacy globals (kept for downgrade compatibility)
            "default_hours": self.default_hours,
            "max_images": self.max_images,
            "sync_interval": self.sync_interval,
        }
        with open(self.config_path, "w") as f:
            json.dump(data, f, indent=2)

    def get_remote_names(self) -> List[str]:
        return list(self.remote_nodes.keys())

    def get_remote_dict(self, name: str) -> Optional[Dict[str, Any]]:
        node = self.remote_nodes.get(name)
        return node.to_dict() if node else None

    def get_local_dict_for(self, remote_key: str) -> Dict[str, Any]:
        """Return the local destination dict for a specific source PACS.

        This is used by DicomOperations as the 'local_config' and also as
        the C-MOVE destination AE title.
        """
        ip = getattr(self, '_local_ip', None) or get_local_ip()
        node = self.remote_nodes.get(remote_key)
        if not node:
            return {"ae_title": "LOCAL_AE", "ip_address": ip,
                    "port": 11112, "transfer_syntax": "JPEG2000Lossless"}
        return {
            "ae_title": node.local_ae_title,
            "ip_address": ip,
            "port": node.local_port,
            "transfer_syntax": node.local_syntax,
        }

    # ── Kept for backward compatibility with tests ────────────────────

    @property
    def local_node(self):
        """Legacy property — returns a PacsNode-like object from the first
        remote's local settings.  Only used in migration / tests."""
        ip = getattr(self, '_local_ip', None) or get_local_ip()
        if self.remote_nodes:
            first = next(iter(self.remote_nodes.values()))
            return PacsNode(
                name="Local PACS",
                ae_title=first.local_ae_title,
                ip_address=ip,
                port=first.local_port,
                transfer_syntax=first.local_syntax,
            )
        return PacsNode(
            name="Local PACS", ae_title="LOCAL_AE",
            ip_address=ip, port=11112,
        )

    @local_node.setter
    def local_node(self, value):
        """Legacy setter — ignored. Local config is now per-source."""
        pass

    def update_local_ip(self):
        """Refresh the cached local IP for all per-source local destinations.

        Called once at startup so that ``get_local_dict_for`` always returns
        the current LAN address.
        """
        self._local_ip = get_local_ip()

    def get_local_dict(self) -> Dict[str, Any]:
        """Legacy — returns the first source's local config."""
        if self.remote_nodes:
            first_key = next(iter(self.remote_nodes))
            return self.get_local_dict_for(first_key)
        ip = getattr(self, '_local_ip', None) or get_local_ip()
        return {"ae_title": "LOCAL_AE", "ip_address": ip,
                "port": 11112, "transfer_syntax": "JPEG2000Lossless"}

    # ── Filter groups export / import ────────────────────────────────────

    def export_filter_groups(self, path: str):
        """Export filter group names and institution assignments to a JSON file."""
        data = {
            "filter_group_names": list(self.filter_group_names),
            "institution_assignments": dict(self.institution_assignments),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def import_filter_groups(self, path: str, merge: bool = False) -> dict:
        """Import filter groups and institution assignments from a JSON file.

        Args:
            path: Path to the JSON file.
            merge: If *True*, merge with existing data (new groups are added,
                   existing institution assignments are overwritten by the
                   imported values).  If *False* (default), replace entirely.

        Returns:
            A summary dict with keys *groups_added*, *institutions_added*,
            and *institutions_updated*.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        imported_groups: List[str] = data.get("filter_group_names", [])
        imported_assignments: Dict[str, str] = data.get(
            "institution_assignments", {})

        summary = {
            "groups_added": 0,
            "institutions_added": 0,
            "institutions_updated": 0,
        }

        if merge:
            for g in imported_groups:
                if g not in self.filter_group_names:
                    self.filter_group_names.append(g)
                    summary["groups_added"] += 1
            for inst, grp in imported_assignments.items():
                if inst in self.institution_assignments:
                    if self.institution_assignments[inst] != grp:
                        self.institution_assignments[inst] = grp
                        summary["institutions_updated"] += 1
                else:
                    self.institution_assignments[inst] = grp
                    summary["institutions_added"] += 1
        else:
            summary["groups_added"] = len(imported_groups)
            summary["institutions_added"] = len(imported_assignments)
            self.filter_group_names = imported_groups
            self.institution_assignments = imported_assignments

        return summary
