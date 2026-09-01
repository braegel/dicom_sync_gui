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
import threading
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
# Where the local inventory C-FIND goes when the built-in Storage SCP
# is receiving and no explicit query host was configured.  Loopback is
# the one address the built-in SCP provably never occupies: it binds
# the interface that reaches the source PACS (see ``local_ip_for``),
# never 127.0.0.1.  A local PACS listening on the wildcard therefore
# always answers here, whichever interface the built-in SCP took.
LOCAL_QUERY_LOOPBACK = "127.0.0.1"

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


def local_ip_for(host: str) -> str:
    """Return the local interface address the kernel would use to reach
    *host* -- which is NOT necessarily the address ``get_local_ip()``
    reports.

    ``get_local_ip()`` asks the route to 8.8.8.8, i.e. the default
    route, so on this machine it answers with the LAN address.  A source
    PACS reached through a VPN tunnel is a different route entirely and
    arrives on the tunnel address.  ``receive_with_builtin_scp`` needs
    that tunnel address: a C-STORE coming back from such a PACS is
    addressed to it, and only a socket bound to exactly that address
    takes precedence over another process holding the wildcard bind on
    the same port.

    Uses a connectionless UDP socket, so nothing is actually sent and
    the PACS never sees it.  Falls back to ``get_local_ip()`` when the
    route cannot be resolved (VPN down, bad address).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))  # discard port; no packet leaves
            return s.getsockname()[0]
    except Exception:
        return get_local_ip()


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
    # Receive this source's C-MOVE images with the BUILT-IN Storage SCP
    # instead of letting them go to the local PACS -- even when that
    # PACS is perfectly reachable (which is what separates this from the
    # ``fallback_folder`` path).
    #
    # Needed when the local PACS cannot be trusted as a C-STORE target
    # for this particular source.  Seen in the field: a PACS that sends
    # JPEG 2000 pixel data whatever transfer syntax was negotiated, and
    # a DCMTK-based local PACS that picks the uncompressed syntax out of
    # what that source offers -- every image then arrives mislabelled
    # and is rejected, so every C-MOVE reports 0 completed / N failed.
    # The built-in SCP negotiates the compressed syntax (see
    # STORAGE_TRANSFER_SYNTAXES) and writes conformant files into
    # ``fallback_folder``, where the local PACS picks them up.
    #
    # The SCP binds ONLY the interface address that reaches this source
    # (see ``local_ip_for``), so the local PACS keeps serving its own
    # address and port for everything else, including the engine's
    # local C-FIND.
    receive_with_builtin_scp: bool = False
    # Where to ask "what has already arrived?" -- the local inventory
    # C-FIND, which is what stops the engine re-downloading series it
    # already has.  Empty / 0 mean "work it out" -- the C-MOVE
    # destination normally, but LOOPBACK when
    # ``receive_with_builtin_scp`` is on, because then the two can
    # collide.  See ``AppConfig.get_local_query_dict_for``.
    #
    # They must be separable because ``receive_with_builtin_scp`` can
    # make the two endpoints genuinely different.  The built-in SCP
    # binds the interface address that reaches this source and answers
    # STORAGE only -- it has no Find presentation context at all.  When
    # that address is also the one ``get_local_dict_for`` derives (i.e.
    # the source is reached over the default route, not a VPN), the
    # built-in SCP's more-specific bind steals the inventory query from
    # the local PACS, every series then looks absent, and the engine
    # re-downloads the entire time window every cycle.
    #
    # Pointing the query at the local PACS explicitly -- ``127.0.0.1``
    # when it runs on this machine -- keeps the two apart.
    local_query_host: str = ""
    local_query_port: int = 0
    notification_sound_enabled: bool = True
    notification_sound_path: str = ""
    # Per-source ordered list of {"term": str, "is_regex": bool}.
    # ``None`` (the default) seeds the bundled defaults in
    # ``__post_init__`` so every fresh node gets the standard list.
    # An empty list passed explicitly is preserved (user-cleared).
    priority_series_terms: Optional[List[Dict[str, Any]]] = None

    def __post_init__(self) -> None:
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
            "receive_with_builtin_scp": self.receive_with_builtin_scp,
            "local_query_host": self.local_query_host,
            "local_query_port": self.local_query_port,
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
            receive_with_builtin_scp=bool(
                data.get("receive_with_builtin_scp", False)),
            local_query_host=data.get("local_query_host", ""),
            local_query_port=int(data.get("local_query_port", 0) or 0),
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
                             assignments: Dict[str, str]) -> None:
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


class FilterGroupImportError(ValueError):
    """The contents of a filter-group import file are not the expected
    shape.

    A ``ValueError`` subclass so existing ``except ValueError`` handlers
    keep working, but nameable on its own so the GUI can tell "this file
    is not a filter-group export" apart from any other ValueError.
    """


def parse_filter_groups_payload(
    data: Any,
) -> Tuple[List[str], Dict[str, str]]:
    """Validate a decoded filter-group export and return
    ``(group_names, assignments)``.

    The file is chosen by the user, so its shape is untrusted input and
    JSON alone guarantees nothing about it.  Without this check the two
    fields go straight into :py:func:`merge_filter_group_data`, where a
    wrong type does not fail loudly but produces nonsense:

    * ``{"filter_group_names": "CT"}`` iterates the STRING and silently
      creates two groups, ``"C"`` and ``"T"``;
    * ``{"institution_assignments": []}`` raises a bare
      ``AttributeError`` (merge) or ``ValueError`` (replace) from deep
      inside the merge, which in the dialog escapes an unguarded Qt
      slot.

    Types are rejected rather than coerced: ``"CT"`` is not a
    one-element list, and guessing that it might be is how the first
    case above became silent data corruption.  Both keys are OPTIONAL
    (a file may legitimately carry only groups or only assignments);
    only a present-but-wrong type is an error.

    Raises:
        FilterGroupImportError: with a message naming the offending
            field, suitable for showing to the user verbatim.
    """
    if not isinstance(data, dict):
        raise FilterGroupImportError(
            "The file must contain a JSON object, but it contains "
            f"{type(data).__name__}.")

    groups = data.get("filter_group_names", [])
    if not isinstance(groups, list):
        raise FilterGroupImportError(
            '"filter_group_names" must be a list of group names, but '
            f"it is {type(groups).__name__}.")
    bad_group = next(
        (g for g in groups if not isinstance(g, str)), None)
    if bad_group is not None:
        raise FilterGroupImportError(
            '"filter_group_names" must contain only text entries, but '
            f"it contains {type(bad_group).__name__} ({bad_group!r}).")

    assignments = data.get("institution_assignments", {})
    if not isinstance(assignments, dict):
        raise FilterGroupImportError(
            '"institution_assignments" must be an object mapping each '
            f"institution to a group, but it is "
            f"{type(assignments).__name__}.")
    for inst, grp in assignments.items():
        if not isinstance(inst, str) or not isinstance(grp, str):
            raise FilterGroupImportError(
                '"institution_assignments" must map text to text, but '
                f"it contains {inst!r}: {grp!r}.")

    return list(groups), dict(assignments)


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

    def __init__(self, config_path: str = "") -> None:
        self.config_path = config_path or self._default_config_path()
        self._init_sources()
        self._init_download_settings()
        self._init_legacy_state()
        self._init_writer()

    def _init_sources(self) -> None:
        """The configured source PACS and the cached local address."""
        self.remote_nodes: Dict[str, PacsNode] = {}
        # Cached local LAN IP; populated by update_local_ip() at
        # startup, falls back to a fresh lookup via _resolve_local_ip()
        # until then.
        self._local_ip: Optional[str] = None

    def _init_download_settings(self) -> None:
        """Everything the user sets in the dashboard and Settings that
        is NOT per source: prior studies, institution filtering, the
        high-load alert and the UI language."""
        # Prior studies
        self.prior_studies_count: int = 0  # 0 = disabled
        self.prior_studies_same_modality: bool = False

        # Filter groups
        self.filter_group_names: List[str] = []  # ordered group names
        self.institution_assignments: Dict[str, str] = {}  # inst -> group
        self.active_filter_groups: List[str] = []  # selected in dashboard
        self.filter_groups_enabled: bool = False  # master switch
        # Download small series even outside an active group, capped at
        # filter_small_series_max images.
        self.filter_allow_small_series: bool = False
        self.filter_small_series_max: int = 20
        self.high_load_alert_enabled: bool = True  # popup at >=12/hour

        # UI language (en, de, fr, es)
        self.language: str = "en"

    def _init_legacy_state(self) -> None:
        """DEPRECATED state — the old *global* service parameters and
        the old global C-MOVE destination, kept only so pre-per-source
        config files still load.  New code reads the per-source values
        off PacsNode instead.  See the "DEPRECATED COMPATIBILITY
        SURFACE" section further down for the full rationale and for
        the migration helpers that consume these."""
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

    def _init_writer(self) -> None:
        """Background-writer state (see save_async / flush).

        ``_io_lock`` serializes the actual file write so a background
        write and a synchronous ``save()`` can never interleave and
        produce a half-merged config.  ``_pending_payload`` is a
        one-slot mailbox: bursts coalesce to the newest snapshot
        instead of queueing one write per UI event."""
        self._io_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_payload: Optional[Dict[str, Any]] = None
        self._writer: Optional[threading.Thread] = None

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
        """Save configuration to file atomically, synchronously.

        Writes to a sibling ``.tmp`` file and ``os.replace``s it onto the
        real path so a crash mid-write cannot leave the user with a
        truncated config (which would otherwise re-trigger the
        initial-setup wizard on next launch).

        Returns *True* on success, *False* if the file could not be
        written (``OSError``: permissions, full disk, stale mount …).
        A failure is logged rather than raised — callers include Qt
        slots, where an exception would propagate into the event loop
        instead of anything that could handle it.

        This call fsyncs, so it is the RIGHT choice for a one-shot
        user action ("I pressed OK, it must be on disk before the
        dialog reports success") and the WRONG one for a recurring
        autosave on the GUI thread — use :py:meth:`save_async` there.

        Drains the background writer first.  A queued snapshot is by
        definition older than what we are about to write, so letting it
        land afterwards would silently roll the change back — making
        that ordering the caller's job is a trap, so it lives here.
        """
        self.flush()
        return self._write_payload(self._build_payload())

    def _build_payload(self) -> Dict[str, Any]:
        """Snapshot the current settings as the dict that goes to disk.

        Cheap and side-effect free, so it can run on the GUI thread even
        when the write itself is handed to the background writer — the
        snapshot is what makes that write consistent.
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
        return data

    def _write_payload(self, data: Dict[str, Any]) -> bool:
        """Atomically write *data* to ``config_path``.

        Holds ``_io_lock`` for the whole write so a synchronous
        ``save()`` and the background writer cannot interleave their
        tmp-file/rename dance on the same path.
        """
        tmp_path = self.config_path + ".tmp"
        with self._io_lock:
            return self._write_payload_locked(data, tmp_path)

    def _write_payload_locked(self, data: Dict[str, Any],
                              tmp_path: str) -> bool:
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

    def save_async(self) -> None:
        """Snapshot the settings now, write them off the GUI thread.

        For the recurring autosave path (a hammered spinbox, rapid
        toggles): ``save()`` fsyncs, and doing that in a Qt slot stalls
        the event loop for the duration of the disk write — invisible on
        a local SSD, seconds on a network home directory.

        Bursts coalesce: the payload is a one-slot mailbox, so ten
        changes in a row produce one write of the final state rather
        than ten writes.  At most one writer thread exists at a time.
        Call :py:meth:`flush` before a synchronous ``save()`` (and
        before shutdown) so a queued older snapshot cannot land after
        newer state was written.
        """
        payload = self._build_payload()
        with self._pending_lock:
            self._pending_payload = payload
            if self._writer is not None and self._writer.is_alive():
                # The running writer re-checks the mailbox before it
                # exits, so it will pick this snapshot up.
                return
            self._writer = threading.Thread(
                target=self._drain_pending, daemon=True,
                name="config-save")
            self._writer.start()

    def _drain_pending(self) -> None:
        """Background writer loop: write the newest pending snapshot
        until the mailbox is empty, then retire.

        Clearing ``_writer`` happens under ``_pending_lock`` in the same
        critical section that finds the mailbox empty, so a concurrent
        ``save_async`` either sees a live writer (and just queues) or
        starts a fresh one — a snapshot can never be dropped between the
        two.
        """
        while True:
            with self._pending_lock:
                payload = self._pending_payload
                self._pending_payload = None
                if payload is None:
                    self._writer = None
                    return
            self._write_payload(payload)

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for the background writer to drain, at most *timeout*.

        Needed before a synchronous ``save()`` and before shutdown: a
        queued snapshot is by definition older than the current state,
        so letting it land afterwards would silently roll settings back.
        A writer that overruns the timeout is left running — it is a
        daemon thread writing atomically, so the worst case is the older
        snapshot winning, never a corrupt file.
        """
        with self._pending_lock:
            writer = self._writer
        if writer is not None and writer.is_alive():
            writer.join(timeout)

    def get_remote_names(self) -> List[str]:
        return list(self.remote_nodes.keys())

    def get_remote_dict(self, name: str) -> Optional[Dict[str, Any]]:
        node = self.remote_nodes.get(name)
        return node.to_dict() if node else None

    def _resolve_local_ip(self) -> str:
        """Return the cached local LAN IP, falling back to a fresh lookup
        until ``update_local_ip()`` has populated the cache."""
        return self._local_ip or get_local_ip()

    def update_local_ip(self) -> None:
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

    def get_local_query_dict_for(self, remote_key: str) -> Dict[str, Any]:
        """Return the endpoint to ask "what has already arrived?" for
        *remote_key* — the local inventory C-FIND target.

        Three cases, in precedence order:

        * ``local_query_host`` / ``local_query_port`` set -- the
          explicit override wins, always.
        * ``receive_with_builtin_scp`` on and no override -- the host
          falls back to ``LOCAL_QUERY_LOOPBACK`` rather than to the
          C-MOVE destination.  Those two addresses coincide whenever
          the source is reached over the DEFAULT ROUTE instead of a
          tunnel, and the built-in SCP's interface-specific bind then
          takes the inventory query away from the local PACS.  The SCP
          answers Storage only, so the query comes back empty-but-
          established, the engine cannot verify what has arrived, and
          that source quietly stops downloading.  Defaulting to
          loopback removes the coincidence: the built-in SCP never
          binds 127.0.0.1.
        * neither -- the C-MOVE destination, the historical behaviour,
          correct whenever the local PACS both receives and answers.

        The port is NOT given the same treatment: the local PACS
        listens on one port for both roles in every deployment seen so
        far, and guessing a different one would break the common case
        to fix nothing.
        """
        target = self.get_local_dict_for(remote_key)
        node = self.remote_nodes.get(remote_key)
        if node is None:
            return target
        if node.local_query_host:
            target["ip_address"] = node.local_query_host
        elif node.receive_with_builtin_scp:
            target["ip_address"] = LOCAL_QUERY_LOOPBACK
        if node.local_query_port:
            target["port"] = node.local_query_port
        return target

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
    def local_node(self) -> "PacsNode":
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

    def export_filter_groups(self, path: str) -> None:
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

        # Same validation the dialog applies — a malformed file must
        # fail with a named error here too, not silently create groups
        # out of the characters of a string.
        imported_groups, imported_assignments = (
            parse_filter_groups_payload(data))

        # Shared pure helper — the same logic the FilterGroupsDialog
        # applies to its unsaved working copies.
        (self.filter_group_names,
         self.institution_assignments,
         summary) = merge_filter_group_data(
            self.filter_group_names, self.institution_assignments,
            imported_groups, imported_assignments, merge)

        return summary
