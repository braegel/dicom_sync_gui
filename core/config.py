"""
Configuration manager for DICOM Sync GUI.
Handles loading/saving of PACS configurations and application preferences.

Architecture: each source PACS node carries its own *local destination*
settings (AE title, port, transfer syntax, fallback folder) so that
C-MOVE responses are directed correctly per source.
"""

import json
import logging
import os
import platform
import socket
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("dicom_sync")

DEFAULT_CONFIG_FILE = "dicom_sync_config.json"

# Repeated literal defaults for the local C-MOVE destination, hoisted so
# the dataclass defaults, from_dict, and the fallback dicts cannot drift.
DEFAULT_LOCAL_PORT = 11112
DEFAULT_REMOTE_PORT = 104
DEFAULT_LOCAL_AE_TITLE = "LOCAL_AE"
DEFAULT_TRANSFER_SYNTAX = "JPEG2000Lossless"

TRANSFER_SYNTAXES_NAMES = [
    "JPEG2000Lossless",
    "ExplicitVRLittleEndian",
    "ImplicitVRLittleEndian",
    "JPEGLossless",
    "JPEGLosslessSV1",
    "DeflatedExplicitVRLittleEndian",
]

# Retrieve methods offered in the UI.  "C-GET" was removed from the
# list: it was never implemented (the engine always issues C-MOVE),
# so offering it silently misled the user.  The ``retrieve_method``
# field on PacsNode is kept so existing config files round-trip; a
# real C-GET implementation can re-add the option here.
RETRIEVE_METHODS = ["C-MOVE"]


def default_priority_terms() -> List[Dict[str, Any]]:
    """Default priority series terms seeded on every new PacsNode.

    These bias the engine's per-cycle download queue toward studies
    whose series descriptions match — typical neuro/vascular series
    that radiologists routinely want first.  List index is priority
    (0 = highest).  All entries default to plain substring matching;
    the user can toggle the per-row ``is_regex`` flag in the dialog.

    A fresh list is returned on every call so callers can mutate
    their copy without leaking into other nodes.
    """
    return [{"term": t, "is_regex": False} for t in (
        "cct", "cta", "ct-a", "angio", "nevas",
        "perf", "perfusion", "ctp", "ct-p",
    )]


def get_local_ip() -> str:
    """Get local IP address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


@dataclass(eq=False)
class PacsNode:
    """Represents a source PACS node with per-source service *and* local
    destination parameters.

    Remote-specific fields:
      - hours, max_images, sync_interval: service query parameters
      - local_ae_title, local_port, local_syntax: where C-MOVE should
        deliver images to (the local receiver for *this* source)
      - fallback_folder: directory to save images when no local PACS is
        reachable (a built-in SCP will be spawned automatically)

    ``eq=False`` keeps the pre-dataclass identity semantics: nodes are
    mutable config objects compared by object identity (``in`` checks,
    dict membership), not field-by-field.
    """

    name: str = ""
    ae_title: str = ""
    ip_address: str = ""
    port: int = DEFAULT_REMOTE_PORT
    transfer_syntax: str = DEFAULT_TRANSFER_SYNTAX
    # Only "C-MOVE" is honored; "C-GET" may still appear in old config
    # files but the engine has no C-GET path (see RETRIEVE_METHODS).
    retrieve_method: str = "C-MOVE"
    # Per-source service parameters
    hours: int = 3
    max_images: int = 0
    sync_interval: int = 60
    # Per-source local destination (C-MOVE target)
    local_ae_title: str = DEFAULT_LOCAL_AE_TITLE
    local_port: int = DEFAULT_LOCAL_PORT
    local_syntax: str = DEFAULT_TRANSFER_SYNTAX
    fallback_folder: str = ""
    notification_sound_enabled: bool = True
    notification_sound_path: str = ""
    # Per-source ordered list of {"term": str, "is_regex": bool}.
    # ``None`` (the default) seeds the bundled defaults in
    # ``__post_init__`` so every fresh node gets the standard list.
    # An empty list passed explicitly is preserved (user-cleared).
    priority_series_terms: Optional[List[Dict[str, Any]]] = None

    def __post_init__(self):
        # An explicit non-empty list is deep-copied so the caller can
        # keep using its own list without leaking mutations into the
        # node, symmetric to the defensive copy in ``from_dict``.
        if self.priority_series_terms is None:
            self.priority_series_terms = default_priority_terms()
        else:
            self.priority_series_terms = [
                dict(e) for e in self.priority_series_terms]

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
            "notification_sound_enabled": self.notification_sound_enabled,
            "notification_sound_path": self.notification_sound_path,
            # Deep-copy entry dicts so callers can mutate the result
            # without leaking into the live PacsNode state.
            "priority_series_terms": [
                dict(e) for e in self.priority_series_terms],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PacsNode":
        return cls(
            name=data.get("name", ""),
            ae_title=data.get("ae_title", ""),
            ip_address=data.get("ip_address", ""),
            port=data.get("port", DEFAULT_REMOTE_PORT),
            transfer_syntax=data.get("transfer_syntax", DEFAULT_TRANSFER_SYNTAX),
            retrieve_method=data.get("retrieve_method", "C-MOVE"),
            hours=data.get("hours", 3),
            max_images=data.get("max_images", 0),
            sync_interval=data.get("sync_interval", 60),
            local_ae_title=data.get("local_ae_title", DEFAULT_LOCAL_AE_TITLE),
            local_port=data.get("local_port", DEFAULT_LOCAL_PORT),
            local_syntax=data.get("local_syntax", DEFAULT_TRANSFER_SYNTAX),
            fallback_folder=data.get("fallback_folder", ""),
            notification_sound_enabled=data.get("notification_sound_enabled", True),
            notification_sound_path=data.get("notification_sound_path", ""),
            # Missing key → bundled defaults (legacy migration).
            # Explicit ``[]`` (user-cleared) is preserved.  Deep-copy
            # the entry dicts so the caller's ``data`` mapping cannot
            # mutate the live PacsNode list (and vice versa).
            priority_series_terms=(
                [dict(e) for e in data["priority_series_terms"]]
                if "priority_series_terms" in data
                else default_priority_terms()
            ),
        )


# ── Filter groups: shared export / import helpers ────────────────────────
# Used both by AppConfig (persisted state) and by FilterGroupsDialog
# (which operates on unsaved working copies), so the merge semantics
# and the on-disk JSON shape cannot drift apart between the two.

def write_filter_groups_json(path: str, group_names: List[str],
                             assignments: Dict[str, str]):
    """Write filter group names and institution assignments to a JSON file.

    The file shape is the export format consumed by
    ``merge_filter_group_data`` / ``AppConfig.import_filter_groups``:
    ``{"filter_group_names": [...], "institution_assignments": {...}}``.
    """
    data = {
        "filter_group_names": list(group_names),
        "institution_assignments": dict(assignments),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def merge_filter_group_data(
    group_names: List[str],
    assignments: Dict[str, str],
    imported_groups: List[str],
    imported_assignments: Dict[str, str],
    merge: bool,
) -> Tuple[List[str], Dict[str, str], Dict[str, int]]:
    """Merge or replace filter group data with imported data (pure, no I/O).

    Args:
        group_names: Current ordered list of group names.
        assignments: Current {institution: group_name} mapping.
        imported_groups: Group names read from an export file.
        imported_assignments: Assignments read from an export file.
        merge: If *True*, merge with existing data (new groups are
               appended, existing institution assignments are
               overwritten by the imported values).  If *False*,
               replace entirely.

    Returns:
        ``(new_group_names, new_assignments, summary)`` where *summary*
        is a dict with keys *groups_added*, *institutions_added* and
        *institutions_updated*.  The inputs are never mutated; fresh
        list/dict copies are returned in both modes.
    """
    summary = {
        "groups_added": 0,
        "institutions_added": 0,
        "institutions_updated": 0,
    }

    if merge:
        new_group_names = list(group_names)
        new_assignments = dict(assignments)
        for g in imported_groups:
            if g not in new_group_names:
                new_group_names.append(g)
                summary["groups_added"] += 1
        for inst, grp in imported_assignments.items():
            if inst not in new_assignments:
                new_assignments[inst] = grp
                summary["institutions_added"] += 1
                continue
            # Already known: only an actual change counts as an update,
            # so re-importing the same file reports zero changes.
            if new_assignments[inst] != grp:
                new_assignments[inst] = grp
                summary["institutions_updated"] += 1
    else:
        # Replace mode: everything imported counts as "added".
        summary["groups_added"] = len(imported_groups)
        summary["institutions_added"] = len(imported_assignments)
        new_group_names = list(imported_groups)
        new_assignments = dict(imported_assignments)

    return new_group_names, new_assignments, summary


class AppConfig:
    """Application configuration."""

    def __init__(self, config_path: str = ""):
        self.config_path = config_path or self._default_config_path()
        self.remote_nodes: Dict[str, PacsNode] = {}

        # Cached local LAN IP; populated by update_local_ip() at startup,
        # falls back to a fresh lookup via _resolve_local_ip() until then.
        self._local_ip: Optional[str] = None

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
        self.high_load_alert_enabled: bool = True  # popup when ≥12 studies/hour

        # UI language (en, de, fr, es)
        self.language: str = "en"

        # DEPRECATED state — the old *global* service parameters and the
        # old global C-MOVE destination, kept only so pre-per-source
        # config files still load.  New code reads the per-source values
        # off PacsNode instead.  See the "DEPRECATED COMPATIBILITY
        # SURFACE" section further down for the full rationale and for
        # the migration helpers that consume these.
        self.default_hours: int = 3
        self.max_images: int = 0
        self.sync_interval: int = 60
        self._legacy_local_node: Optional[Dict[str, Any]] = None
        self._legacy_fallback_enabled: bool = False
        self._legacy_fallback_path: str = ""
        # Raw on-disk dict, captured at load() time.  Unknown keys are
        # round-tripped on save() so a newer-version field written by a
        # future build is not silently lost when this version saves.
        self._raw_data: Dict[str, Any] = {}

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
            # Explicit UTF-8: the platform default encoding is cp1252 on
            # a German Windows box, which throws UnicodeDecodeError on a
            # config that was hand-edited (or written by any other tool)
            # as UTF-8.  load() would then report failure, the app would
            # re-run the first-run wizard, and the next save() would
            # start from an empty _raw_data — silently dropping the
            # unknown-key round-tripping save() promises.
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._raw_data = dict(data)

            self._load_remote_nodes(data)
            self._capture_legacy(data)
            self._read_filter_settings(data)
            self._migrate_legacy_into_nodes(data)

            logger.info(f"Config loaded: {len(self.remote_nodes)} source(s) — "
                       f"{list(self.remote_nodes.keys())}")

            return True
        except (json.JSONDecodeError, KeyError, OSError,
                UnicodeDecodeError) as e:
            # OSError: unreadable file (permissions, stale network
            # mount) — must not crash the app at startup.
            # UnicodeDecodeError: corrupt / binary file content.
            # Same contract as the parse-error path: log and report
            # failure so the caller falls back to defaults.
            logger.error(f"Config load error: {e}")
            return False

    def _load_remote_nodes(self, data: Dict[str, Any]) -> None:
        """Build ``remote_nodes`` from the on-disk dict, including the
        old single-remote ("remote") format migration."""
        self.remote_nodes = {}
        for key, val in data.get("remotes", {}).items():
            self.remote_nodes[key] = PacsNode.from_dict(val)

        # Migrate old single-remote format ("remote" dict → "remotes")
        if "remote" in data and "remotes" not in data:
            self.remote_nodes["default"] = PacsNode.from_dict(data["remote"])

    def _read_filter_settings(self, data: Dict[str, Any]) -> None:
        """Read prior-studies, filter-group, alert and language settings."""
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
        self.high_load_alert_enabled = data.get(
            "high_load_alert_enabled", True)
        self.language = data.get("language", "en")

    def save(self) -> bool:
        """Save configuration to file atomically.

        Writes to a sibling ``.tmp`` file and ``os.replace``s it onto the
        real path so a crash mid-write cannot leave the user with a
        truncated config (which would otherwise re-trigger the
        initial-setup wizard on next launch).

        Returns *True* on success, *False* if the file could not be
        written (``OSError``: permissions, full disk, stale mount …).
        A failure is logged rather than raised — one caller is a
        debounced-save QTimer slot, where an exception would propagate
        into the Qt event loop instead of anything that could handle it.
        """
        # Start from the raw on-disk dict so unknown keys (e.g. settings
        # added by a future build) round-trip instead of being silently
        # dropped on save.
        data = dict(self._raw_data)
        data.update({
            "remotes": {k: v.to_dict() for k, v in self.remote_nodes.items()},
            "prior_studies_count": self.prior_studies_count,
            "prior_studies_same_modality": self.prior_studies_same_modality,
            "filter_group_names": self.filter_group_names,
            "institution_assignments": self.institution_assignments,
            "active_filter_groups": self.active_filter_groups,
            "filter_groups_enabled": self.filter_groups_enabled,
            "filter_allow_small_series": self.filter_allow_small_series,
            "filter_small_series_max": self.filter_small_series_max,
            "high_load_alert_enabled": self.high_load_alert_enabled,
            "language": self.language,
            # Legacy globals (kept for downgrade compatibility — see the
            # DEPRECATED COMPATIBILITY SURFACE section)
            "default_hours": self.default_hours,
            "max_images": self.max_images,
            "sync_interval": self.sync_interval,
        })
        tmp_path = self.config_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            # UTF-8 to match load().  json.dump escapes non-ASCII by
            # default so today's writes are pure ASCII either way, but
            # pinning the encoding keeps the two ends in lockstep should
            # that default ever change.
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.config_path)
            return True
        except OSError as e:
            logger.error(f"Config save error ({self.config_path}): {e}")
            return False

    def get_remote_names(self) -> List[str]:
        return list(self.remote_nodes.keys())

    def get_remote_dict(self, name: str) -> Optional[Dict[str, Any]]:
        node = self.remote_nodes.get(name)
        return node.to_dict() if node else None

    def _resolve_local_ip(self) -> str:
        """Return the cached local LAN IP, falling back to a fresh lookup
        until ``update_local_ip()`` has populated the cache."""
        return self._local_ip or get_local_ip()

    def update_local_ip(self):
        """Refresh the cached local IP for all per-source local destinations.

        Called once at startup so that ``get_local_dict_for`` always returns
        the current LAN address.
        """
        self._local_ip = get_local_ip()

    def get_local_dict_for(self, remote_key: str) -> Dict[str, Any]:
        """Return the local destination dict for a specific source PACS.

        This is used by DicomOperations as the 'local_config' and also as
        the C-MOVE destination AE title.
        """
        ip = self._resolve_local_ip()
        node = self.remote_nodes.get(remote_key)
        if not node:
            return {"ae_title": DEFAULT_LOCAL_AE_TITLE, "ip_address": ip,
                    "port": DEFAULT_LOCAL_PORT,
                    "transfer_syntax": DEFAULT_TRANSFER_SYNTAX}
        return {
            "ae_title": node.local_ae_title,
            "ip_address": ip,
            "port": node.local_port,
            "transfer_syntax": node.local_syntax,
        }

    # ══ DEPRECATED COMPATIBILITY SURFACE ═════════════════════════════════
    #
    # Everything below this banner and above the "Filter groups" section
    # is *deprecated*.  It exists for exactly two reasons:
    #
    #   1. Old config files must keep loading.  Before the per-source
    #      rewrite, hours / max_images / sync_interval and the C-MOVE
    #      destination ("local" node + fallback storage) were single
    #      GLOBAL settings.  A config written by such a build still has
    #      to come up with every source configured correctly, so those
    #      globals are read into the ``_legacy_*`` / ``default_hours`` /
    #      ``max_images`` / ``sync_interval`` attributes declared in
    #      ``__init__`` and then folded into the individual PacsNodes.
    #      save() also keeps writing the globals back so downgrading to
    #      an older build doesn't lose them.
    #   2. ``local_node`` and ``get_local_dict()`` predate the per-source
    #      destination and are still referenced by tests.
    #
    # NEW CODE MUST NOT USE ANY OF THIS.  The current API is:
    #   - per-source service params  → ``node.hours`` / ``node.max_images``
    #                                  / ``node.sync_interval`` on the
    #                                  PacsNode from ``remote_nodes``
    #   - per-source C-MOVE target   → ``get_local_dict_for(remote_key)``
    #
    # The migration helpers are called from ``load()`` (``_capture_legacy``
    # before ``_migrate_legacy_into_nodes``, which depends on it) — they
    # live down here rather than next to load() so the live load path
    # reads as four named steps without the legacy bulk in between.

    def _capture_legacy(self, data: Dict[str, Any]) -> None:
        """Read legacy global values and the legacy local node / fallback
        storage, stashing them for use by ``_migrate_legacy_into_nodes``."""
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

    def _migrate_legacy_into_nodes(self, data: Dict[str, Any]) -> None:
        """Inject per-source fields from the legacy globals captured by
        ``_capture_legacy`` into any node that predates them."""
        remotes_raw = data.get("remotes", {})
        legacy_local = self._legacy_local_node or {}
        for key, node in self.remote_nodes.items():
            raw = remotes_raw.get(key, {})
            # Migrate service parameters
            if "hours" not in raw:
                node.hours = self.default_hours
                node.max_images = self.max_images
                node.sync_interval = self.sync_interval
            # Migrate local destination from old global local_node
            if "local_ae_title" not in raw and legacy_local:
                node.local_ae_title = legacy_local.get("ae_title", DEFAULT_LOCAL_AE_TITLE)
                node.local_port = legacy_local.get("port", DEFAULT_LOCAL_PORT)
                node.local_syntax = legacy_local.get(
                    "transfer_syntax", DEFAULT_TRANSFER_SYNTAX)
                if self._legacy_fallback_enabled:
                    node.fallback_folder = self._legacy_fallback_path

    @property
    def local_node(self):
        """Legacy property — returns a PacsNode-like object from the first
        remote's local settings.  Only used in migration / tests."""
        ip = self._resolve_local_ip()
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
            name="Local PACS", ae_title=DEFAULT_LOCAL_AE_TITLE,
            ip_address=ip, port=DEFAULT_LOCAL_PORT,
        )

    @local_node.setter
    def local_node(self, value: Any) -> None:
        """Rejects the assignment.

        There is no global local node any more — the C-MOVE destination
        is per source (``PacsNode.local_ae_title`` / ``local_port`` /
        ``local_syntax``).  This used to accept the assignment and
        silently discard it, so code written against the old API kept
        "working" while configuring nothing at all.  Failing loudly is
        the whole point.
        """
        raise AttributeError(
            "AppConfig.local_node is read-only: the C-MOVE destination "
            "is configured per source PACS. Set local_ae_title / "
            "local_port / local_syntax on the PacsNode instead.")

    def get_local_dict(self) -> Dict[str, Any]:
        """Legacy — returns the first source's local config."""
        if self.remote_nodes:
            first_key = next(iter(self.remote_nodes))
            return self.get_local_dict_for(first_key)
        ip = self._resolve_local_ip()
        return {"ae_title": DEFAULT_LOCAL_AE_TITLE, "ip_address": ip,
                "port": DEFAULT_LOCAL_PORT,
                "transfer_syntax": DEFAULT_TRANSFER_SYNTAX}

    # ══ END DEPRECATED COMPATIBILITY SURFACE ═════════════════════════════

    # ── Filter groups export / import ────────────────────────────────────

    def export_filter_groups(self, path: str):
        """Export filter group names and institution assignments to a JSON file."""
        write_filter_groups_json(
            path, self.filter_group_names, self.institution_assignments)

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

        # Shared pure helper — the same logic the FilterGroupsDialog
        # applies to its unsaved working copies.
        (self.filter_group_names,
         self.institution_assignments,
         summary) = merge_filter_group_data(
            self.filter_group_names, self.institution_assignments,
            imported_groups, imported_assignments, merge)

        return summary
