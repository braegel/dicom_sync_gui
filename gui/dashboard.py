"""
Source Dashboard — one widget per configured source PACS.
Shows per-source service controls (Start/Stop, hours, max images, interval),
the series queue with ETE (estimated time to completion),
and real-time throughput statistics with color-coded indicators.
"""

import itertools
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QAction
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QGroupBox, QGridLayout, QHeaderView,
    QPushButton, QSpinBox, QFormLayout, QCheckBox, QComboBox,
    QListWidget, QListWidgetItem, QMenu, QToolButton, QFrame,
    QMessageBox, QApplication,
)

from core.transfer_engine import TERMINAL_STATUSES, TransferStats
# WAV synthesis lives in gui.notification_sound (which owns the
# module-level path cache); ``_generate_default_sound`` and the two
# frequency constants are re-exported here for backwards compatibility
# — tests and older call sites import them from gui.dashboard.
from gui.notification_sound import (  # noqa: F401  (re-exports)
    _NORMAL_FREQ_2, _SAD_FREQ_2, _generate_default_sound,
)
from gui.study_rate import (  # noqa: F401  (constants re-exported)
    COLOR_GREEN, COLOR_RED, COLOR_YELLOW,
    STUDY_RATE_GOOD_MAX, STUDY_RATE_HIGH_LOAD, STUDY_RATE_WARN_MAX,
    compute_study_rates, study_rate_color,
)
from gui.styles import (
    BTN_AMBER, BTN_BLUE, BTN_DOWNLOAD_SELECTED, BTN_START, BTN_STOP,
)

logger = logging.getLogger("dicom_sync")


@dataclass
class ServiceParams:
    """Per-source download-service parameters captured at Start / Restart
    click time.  Kept as a dataclass so the four fields can be passed
    around as a single typed bundle (in particular the Restart-pending
    snapshot in ``SourceDashboard``); converted to a plain dict at the
    Qt-signal boundary because ``start_requested`` is defined as
    ``Signal(str, dict)`` for cross-module compatibility.

    ``__post_init__`` performs sanity checks so accidental sentinel
    values (e.g. a Restart-snapshot built from an uninitialised
    Spinbox) never reach the engine.
    """
    hours: int
    max_images: int
    sync_interval: int
    selection_mode: bool

    def __post_init__(self) -> None:
        if self.hours <= 0:
            raise ValueError(
                f"ServiceParams.hours must be > 0, got {self.hours!r}")
        if self.max_images < 0:
            raise ValueError(
                f"ServiceParams.max_images must be >= 0, "
                f"got {self.max_images!r}")
        if self.sync_interval < 10:
            raise ValueError(
                f"ServiceParams.sync_interval must be >= 10s, "
                f"got {self.sync_interval!r}")

    def to_dict(self) -> dict:
        return asdict(self)


# ── UI thresholds and colors ─────────────────────────────────────────────
# A series rate is shaded green/red when it deviates by more than this
# ratio from the median-of-all baseline.
SPEED_BAND_RATIO = 0.2

# Studies-per-hour thresholds (STUDY_RATE_GOOD_MAX / WARN_MAX /
# HIGH_LOAD) moved to gui.study_rate alongside the pure rate logic;
# re-imported above so existing ``gui.dashboard`` imports keep working.

# Stats refresh tick (ms)
STATS_REFRESH_MS = 2000
# Debounce window for coalescing config writes from UI events (ms)
CONFIG_SAVE_DEBOUNCE_MS = 500
# How long a single image may stall before the slow-transfer watchdog
# fires a sad-sound + popup + auto-restart (ms).  The watchdog re-arms
# on every per-image progress tick (series_progress), so this is a
# per-image stall timeout, NOT a per-series budget: it only fires when
# no single image arrives for this long, which means the transfer is
# genuinely wedged rather than merely slow on a large series.
_SLOW_TRANSFER_TIMEOUT_MS = 10_000

# How long the (non-blocking) slow-transfer auto-restart notice stays on
# screen before it dismisses itself (ms).  It's informational only — the
# service restarts regardless of whether the user ever sees it.
_SLOW_TRANSFER_NOTICE_MS = 5_000

# Statuses a series can no longer leave: it contributes no pending
# images and gets no ETE.  "unavailable" = retry budget exhausted.
# Canonical definition now lives in core.transfer_engine; aliased here
# for minimal churn so existing _TERMINAL_STATUSES call sites keep working.
_TERMINAL_STATUSES = TERMINAL_STATUSES

# Palette.  Green/yellow/red are defined in gui.study_rate (single
# source of truth, shared with the studies-per-hour colour bands) and
# re-imported above; the remaining accents are dashboard-only.
COLOR_ORANGE = "#f39c12"
COLOR_BLUE_ACCENT = "#3498db"
COLOR_MUTED = "#969696"

# Notification sound suppression threshold: studies whose total
# downloaded image count is below this are treated as "too small to
# be a real exam" and play no completion sound — typically isolated
# small series, stray priors, or partial fetches that would otherwise
# spam the user.
MIN_IMAGES_FOR_NOTIFICATION_SOUND = 21


class StatsLabel(QLabel):
    """A label that is color-coded relative to the median-all baseline."""

    def __init__(self, text: str = "\u2014",
                 parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setFamilies(["Menlo", "Consolas", "Courier New"])
        font.setPointSize(16)
        font.setBold(True)
        self.setFont(font)
        self.setMinimumWidth(120)
        self.setMinimumHeight(50)
        self.setStyleSheet(self._style("white"))

    def set_value(self, value: float, median_all: float) -> None:
        """Update text and colour.

        *median_all* is the overall-median baseline.  A value more than
        ``SPEED_BAND_RATIO`` above baseline is green; the same below is
        red; everything else stays white.  If baseline is < 1 we don't
        colour-code (not enough data).
        """
        self.setText(f"{value:.0f}")
        if median_all < 1 or value < 1:
            self.setStyleSheet(self._style("white"))
            return
        if value > median_all * (1 + SPEED_BAND_RATIO):
            self.setStyleSheet(self._style(COLOR_GREEN))
        elif value < median_all * (1 - SPEED_BAND_RATIO):
            self.setStyleSheet(self._style(COLOR_RED))
        else:
            self.setStyleSheet(self._style("white"))

    @staticmethod
    def _style(color: str) -> str:
        bg = {
            COLOR_GREEN: "#1a3a1a",
            COLOR_RED: "#3a1a1a",
        }.get(color, "#2c2c2c")
        return (
            f"QLabel {{ color: {color}; background: {bg}; "
            f"border: 1px solid #555; border-radius: 6px; padding: 6px; }}"
        )


class SourceDashboard(QWidget):
    """Dashboard widget for a single source PACS — controls + live progress."""

    # Signals to main window
    start_requested = Signal(str, dict)   # remote_key, {hours, max_images, sync_interval, selection_mode}
    stop_requested = Signal(str)          # remote_key
    selection_confirmed = Signal(str, list)  # remote_key, [series_uid, ...]

    def __init__(self, config, remote_key: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.remote_key = remote_key
        self._current_stats: Optional[TransferStats] = None
        self._last_queue: list = []
        # series_uid sequence of the queue currently rendered in
        # ``series_table`` by ``on_queue_updated``.  Lets the next
        # queue emit decide between a cheap in-place cell update (uid
        # sequence unchanged — the common per-series progress case)
        # and a full table rebuild.  ``None`` means "no valid normal-
        # mode render" and forces a rebuild; it is reset whenever the
        # table content comes from somewhere else (selection mode,
        # ``reset()``).
        self._rendered_uids: Optional[list[str]] = None
        self._service_running = False
        self._settings_dirty = False
        # True between a Restart-click and the subsequent
        # set_service_running(False) callback; tells that callback to
        # re-emit start_requested instead of just leaving the engine
        # idle.  ``_restart_params`` snapshots the Spinbox values at
        # click time so a user fiddling with them while the engine
        # winds down doesn't accidentally change the restart's
        # effective settings.
        self._restart_pending = False
        self._restart_params: Optional[ServiceParams] = None
        # True when the pending restart was triggered by the slow-transfer
        # watchdog (not the manual Restart button).  The auto-start path
        # forwards this so MainWindow keeps retrying-with-siren on an
        # unreachable PACS instead of aborting the start.
        self._restart_is_auto = False
        self._last_high_load_groups: set = set()
        # Set True while _setup_ui runs so any toggled signal fired by
        # setChecked() during construction is ignored — those would
        # otherwise call config.save() before the rest of the UI exists.
        self._initializing = True
        # Lazy single long-lived QSoundEffect.  Creating one per
        # play_sound call has historically raced PySide6 dealloc when
        # notifications arrive in quick succession (study_completed
        # comes from the pynetdicom reactor thread); creating it in
        # __init__ instead allocates an audio backend during widget
        # construction which is undesirable in headless test runs.
        # Deferred init solves both: one instance, but only when the
        # first sound actually plays.
        self._sound_effect: Optional[QSoundEffect] = None
        # The single, reusable, non-blocking slow-transfer notice (and
        # its auto-close timer).  Kept on the instance so repeated
        # timeouts refresh it instead of stacking dialogs.
        self._slow_transfer_notice: Optional[QMessageBox] = None
        # series_uid of the currently active transfer (set by
        # on_series_started, cleared by on_series_completed /
        # on_series_error).  The slow-transfer watchdog uses this to
        # include the series UID in its log/popup.
        self._active_series_uid: Optional[str] = None
        # Images the active transfer has received so far (from the latest
        # series_progress emit).  Lets the Pending / ETE columns count
        # down live between queue/stats emits instead of jumping only
        # when a whole series completes.
        self._active_series_done: int = 0
        # True while the engine has reported a PACS connection outage
        # (connection_lost) and not yet recovered (connection_restored).
        # The slow-transfer watchdog skips the auto-restart in this state
        # because the engine recovers automatically without help.
        self._pacs_connection_lost: bool = False
        # Per-row cache of the last-rendered Status (col 8) and img/min
        # text (col 7), keyed by series_uid.  The in-place queue update
        # only rewrites those two (brush-carrying) cells when the cached
        # value actually changed, avoiding a setItem storm on every
        # completed series.  Cleared on full rebuild and reset.
        self._queue_cell_cache: dict = {}
        # Monotonic timestamp of the last real transfer progress (set in
        # on_series_started / on_series_progress).  The slow-transfer
        # watchdog double-checks the actual elapsed wall time against this
        # before restarting, so a timer that fires late merely because the
        # GUI thread was busy does NOT trigger a spurious auto-restart.
        self._last_progress_ts: float = 0.0
        self._setup_ui()

        # Refresh stats display every 2 seconds
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_stats_display)
        self._timer.start(STATS_REFRESH_MS)

        # Debounce config writes: a hammered spinbox / rapid toggles
        # otherwise call config.save() once per UI event, hitting disk
        # synchronously on the main thread.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(CONFIG_SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self.config.save)

        # Safety timer for ``_on_restart_clicked`` — see that method's
        # docstring.  Single-shot, started on Restart click, stopped
        # when the engine actually goes idle (auto-start branch).
        self._restart_safety_timer = QTimer(self)
        self._restart_safety_timer.setSingleShot(True)
        self._restart_safety_timer.timeout.connect(
            self._on_restart_safety_timeout)

        # Slow-transfer watchdog: fires when a single image download
        # takes longer than _SLOW_TRANSFER_TIMEOUT_MS without completing.
        # Plays the sad sound, shows a non-modal popup, and auto-restarts
        # the service so a wedged C-MOVE doesn't block the queue forever.
        self._slow_transfer_timer = QTimer(self)
        self._slow_transfer_timer.setSingleShot(True)
        self._slow_transfer_timer.timeout.connect(
            self._on_slow_transfer_detected)

        # Auto-dismiss the non-blocking slow-transfer notice after
        # _SLOW_TRANSFER_NOTICE_MS so it never lingers in front of the
        # running service.
        self._slow_transfer_notice_timer = QTimer(self)
        self._slow_transfer_notice_timer.setSingleShot(True)
        self._slow_transfer_notice_timer.timeout.connect(
            self._on_slow_transfer_notice_closed)

        # 1 Hz live refresh of the Pending / ETE columns so they count
        # down every second during an active transfer instead of only
        # jumping when a queue/stats signal arrives.  Runs only while a
        # transfer is active (started/stopped in on_series_started /
        # on_series_completed / on_series_error).
        self._live_refresh_timer = QTimer(self)
        self._live_refresh_timer.setInterval(1000)
        self._live_refresh_timer.timeout.connect(
            self._refresh_pending_and_ete)

    def _save_config_debounced(self) -> None:
        """Schedule a config save 500ms in the future; coalesces bursts."""
        self._save_timer.start()

    def flush_pending_save(self) -> None:
        """Synchronously persist any pending debounced save."""
        if self._save_timer.isActive():
            self._save_timer.stop()
        self.config.save()

    @property
    def _remote_node(self):
        return self.config.remote_nodes.get(self.remote_key)

    def _setup_ui(self) -> None:
        """Build the dashboard top-to-bottom.  Each section lives in its
        own ``_build_*`` helper; they run in layout order and attach
        their widgets to *layout* themselves.  ``_initializing`` is True
        for the whole build (set in ``__init__``) so setChecked() /
        setValue() calls inside the builders can't trigger config
        saves; it flips to False only after the last section exists."""
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self._build_service_controls(layout)
        self._build_filter_section(layout)
        self._build_restart_banner(layout)
        self._build_stats_section(layout)
        self._build_study_rate_section(layout)
        self._build_queue_table(layout)
        self._build_summary_bar(layout)

        # Construction complete — accept user-driven toggle events now.
        self._initializing = False

    def _build_service_controls(self, layout: QVBoxLayout) -> None:
        """Service Controls group: parameter spinboxes + Start/Stop/
        Restart and the (initially hidden) selection-mode buttons."""
        node = self._remote_node

        ctrl_group = QGroupBox("Download Service")
        ctrl_layout = QHBoxLayout()

        form = QFormLayout()
        self.hours_spin = QSpinBox()
        self.hours_spin.setRange(1, 168)
        self.hours_spin.setValue(node.hours if node else 3)
        self.hours_spin.setSuffix(" hours")
        self.hours_spin.valueChanged.connect(self._on_settings_changed)
        form.addRow("Download last:", self.hours_spin)

        self.max_images_spin = QSpinBox()
        self.max_images_spin.setRange(0, 99999)
        self.max_images_spin.setSpecialValueText("No limit")
        self.max_images_spin.setSuffix(" images")
        self.max_images_spin.setValue(node.max_images if node else 0)
        self.max_images_spin.valueChanged.connect(self._on_settings_changed)
        form.addRow("Max images / series:", self.max_images_spin)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(10, 600)
        self.interval_spin.setValue(node.sync_interval if node else 60)
        self.interval_spin.setSuffix(" sec")
        self.interval_spin.setToolTip(
            "Seconds to wait between query cycles when no images are found")
        self.interval_spin.valueChanged.connect(self._on_settings_changed)
        form.addRow("Query interval:", self.interval_spin)

        self.manual_selection_check = QCheckBox("Manual series selection")
        self.manual_selection_check.setToolTip(
            "After each query, pause and let you choose which series to download.")
        form.addRow("", self.manual_selection_check)

        self.sound_check = QCheckBox("Sound notification on download")
        self.sound_check.setToolTip(
            "Play a notification sound when a study completes downloading.")
        self.sound_check.setChecked(
            node.notification_sound_enabled if node else True)
        self.sound_check.toggled.connect(self._on_sound_toggled)
        form.addRow("", self.sound_check)

        ctrl_layout.addLayout(form)
        ctrl_layout.addStretch()

        # Start / Stop buttons
        btn_layout = QVBoxLayout()
        self.btn_start = QPushButton("  Start Service  ")
        self.btn_start.setFont(QFont("", 11, QFont.Bold))
        self.btn_start.setStyleSheet(BTN_START)
        self.btn_start.clicked.connect(self._on_start_clicked)

        self.btn_stop = QPushButton("  Stop Service  ")
        self.btn_stop.setFont(QFont("", 11, QFont.Bold))
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(BTN_STOP)
        self.btn_stop.clicked.connect(self._on_stop_clicked)

        self.btn_restart = QPushButton("  Restart Service  ")
        self.btn_restart.setFont(QFont("", 11, QFont.Bold))
        self.btn_restart.setEnabled(False)
        self.btn_restart.setStyleSheet(BTN_AMBER)
        self.btn_restart.clicked.connect(self._on_restart_clicked)

        self.btn_download_selected = QPushButton("  Download Selected  ")
        self.btn_download_selected.setFont(QFont("", 11, QFont.Bold))
        self.btn_download_selected.setStyleSheet(BTN_DOWNLOAD_SELECTED)
        self.btn_download_selected.setVisible(False)
        self.btn_download_selected.clicked.connect(
            self._on_download_selected_clicked)

        self.btn_select_all = QPushButton("  Select All  ")
        self.btn_select_all.setStyleSheet(BTN_BLUE)
        self.btn_select_all.setVisible(False)
        self.btn_select_all.clicked.connect(
            lambda: self._set_all_series_checked(True))

        self.btn_deselect_all = QPushButton("  Deselect All  ")
        self.btn_deselect_all.setStyleSheet(BTN_BLUE)
        self.btn_deselect_all.setVisible(False)
        self.btn_deselect_all.clicked.connect(
            lambda: self._set_all_series_checked(False))

        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_stop)
        btn_layout.addWidget(self.btn_restart)
        btn_layout.addWidget(self.btn_select_all)
        btn_layout.addWidget(self.btn_deselect_all)
        btn_layout.addWidget(self.btn_download_selected)
        ctrl_layout.addLayout(btn_layout)

        ctrl_group.setLayout(ctrl_layout)
        layout.addWidget(ctrl_group)

    def _build_filter_section(self, layout: QVBoxLayout) -> None:
        """Institution Filter group: master toggle, multi-select group
        dropdown, and the small-series exception row."""
        filter_group = QGroupBox("Institution Filter")
        filter_vbox = QVBoxLayout()

        fl = QHBoxLayout()

        self.filter_enable_check = QCheckBox("Enable group filtering")
        self.filter_enable_check.setChecked(
            self.config.filter_groups_enabled)
        self.filter_enable_check.toggled.connect(
            self._on_filter_toggled)
        fl.addWidget(self.filter_enable_check)

        fl.addWidget(QLabel("Active groups:"))

        # Multi-select dropdown button
        self.filter_btn = QToolButton()
        self.filter_btn.setText("Select Groups...")
        self.filter_btn.setPopupMode(QToolButton.InstantPopup)
        self.filter_btn.setStyleSheet(
            "QToolButton { padding: 5px 12px; border: 1px solid #555; "
            "border-radius: 4px; background: #2c2c2c; min-width: 200px; "
            "text-align: left; }"
            "QToolButton::menu-indicator { subcontrol-position: right center; }")

        self.filter_menu = QMenu(self.filter_btn)
        self.filter_btn.setMenu(self.filter_menu)

        fl.addWidget(self.filter_btn)

        self.lbl_filter_info = QLabel("")
        self.lbl_filter_info.setStyleSheet(
            f"QLabel {{ color: {COLOR_ORANGE}; font-style: italic; }}")
        fl.addWidget(self.lbl_filter_info)

        fl.addStretch()
        filter_vbox.addLayout(fl)

        # Small-series exception row
        fl2 = QHBoxLayout()

        self.small_series_check = QCheckBox(
            "Allow small series (other groups)")
        self.small_series_check.setToolTip(
            "Download series with few images even if the institution "
            "is not in an active filter group.")
        self.small_series_check.setChecked(
            self.config.filter_allow_small_series)
        self.small_series_check.toggled.connect(
            self._on_small_series_toggled)
        fl2.addWidget(self.small_series_check)

        self.lbl_small_max = QLabel("Max images/series:")
        fl2.addWidget(self.lbl_small_max)

        self.small_series_spin = QSpinBox()
        self.small_series_spin.setRange(1, 999)
        self.small_series_spin.setValue(
            self.config.filter_small_series_max)
        self.small_series_spin.valueChanged.connect(
            self._on_small_series_max_changed)
        fl2.addWidget(self.small_series_spin)

        fl2.addStretch()
        filter_vbox.addLayout(fl2)

        filter_group.setLayout(filter_vbox)
        layout.addWidget(filter_group)

        # Initial menu/label state.  Runs BEFORE the queue table exists;
        # ``_update_filter_enabled_state`` guards its series_table access
        # with hasattr for exactly this construction window.
        self._populate_filter_menu()
        self._update_filter_button_text()
        self._update_filter_enabled_state()

    def _build_restart_banner(self, layout: QVBoxLayout) -> None:
        """Hidden warning banner shown once settings change while the
        service is running."""
        self.restart_banner = QLabel(
            "\u26a0  Settings changed. Restart the service for "
            "changes to take effect.")
        self.restart_banner.setStyleSheet(
            "QLabel { background: #7f6000; color: #fff; padding: 8px; "
            "border-radius: 4px; font-weight: bold; }")
        self.restart_banner.setAlignment(Qt.AlignCenter)
        self.restart_banner.setVisible(False)
        layout.addWidget(self.restart_banner)

    def _build_stats_section(self, layout: QVBoxLayout) -> None:
        """Throughput Statistics group: the four colour-coded
        images-per-minute StatsLabels."""
        stats_group = QGroupBox("Transfer Speed (images / minute)")
        sl = QGridLayout()

        for col, label in enumerate(
                ["Last Series", "Median 5", "Median 10", "Median All"], 1):
            lbl = QLabel(label)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFont(QFont("", 10, QFont.Bold))
            sl.addWidget(lbl, 0, col)

        sl.addWidget(QLabel("Rate:"), 1, 0)

        self.stat_last = StatsLabel()
        self.stat_med5 = StatsLabel()
        self.stat_med10 = StatsLabel()
        self.stat_medall = StatsLabel()

        sl.addWidget(self.stat_last, 1, 1)
        sl.addWidget(self.stat_med5, 1, 2)
        sl.addWidget(self.stat_med10, 1, 3)
        sl.addWidget(self.stat_medall, 1, 4)

        stats_group.setLayout(sl)
        layout.addWidget(stats_group)

    def _build_study_rate_section(self, layout: QVBoxLayout) -> None:
        """Studies / Hour group: per-group (or total) rate labels."""
        self.study_rate_group = QGroupBox("Studies / Hour")
        self.study_rate_layout = QHBoxLayout()
        self.study_rate_labels: dict[str, QLabel] = {}
        self._rebuild_study_rate_labels()
        self.study_rate_group.setLayout(self.study_rate_layout)
        layout.addWidget(self.study_rate_group)

    def _build_queue_table(self, layout: QVBoxLayout) -> None:
        """Series Queue group: the 11-column queue table (checkbox
        column 0 hidden outside selection mode, Group column 10 hidden
        while filtering is off)."""
        table_group = QGroupBox("Series Queue")
        tl = QVBoxLayout()

        self.series_table = QTableWidget()
        # Column 11 ("Series Created") is appended last so the existing
        # hardcoded column indices (Pending=6, Status=8, ETE=9, Group=10)
        # stay valid.
        self.series_table.setColumnCount(12)
        self.series_table.setHorizontalHeaderLabels([
            "☑", "Patient", "Study", "Series", "Modality",
            "Images", "Pending", "img/min", "Status", "ETE", "Group",
            "Series Created"
        ])
        header = self.series_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(9, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(10, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(11, QHeaderView.ResizeToContents)
        self.series_table.setAlternatingRowColors(True)
        self.series_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.series_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.series_table.setColumnHidden(0, True)
        self.series_table.setColumnHidden(10,
                                          not self.config.filter_groups_enabled)

        tl.addWidget(self.series_table)
        table_group.setLayout(tl)
        layout.addWidget(table_group, 1)  # stretch factor 1 = takes all space

    def _build_summary_bar(self, layout: QVBoxLayout) -> None:
        """Bottom summary bar: totals, cycle counter, status label."""
        summary = QHBoxLayout()
        self.lbl_total_images = QLabel("Total: 0 images")
        self.lbl_total_images.setFont(QFont("", 11, QFont.Bold))
        self.lbl_total_series = QLabel("Series: 0")
        self.lbl_cycle = QLabel("Cycle: \u2014")
        self.lbl_status = QLabel("Idle")
        self.lbl_status.setFont(QFont("", 11))
        summary.addWidget(self.lbl_total_images)
        summary.addWidget(self.lbl_total_series)
        summary.addWidget(self.lbl_cycle)
        summary.addStretch()
        summary.addWidget(self.lbl_status)
        layout.addLayout(summary)

    # ── Filter group handling ─────────────────────────────────────────

    def _populate_filter_menu(self) -> None:
        """Build the checkable menu items for each filter group."""
        self.filter_menu.clear()
        active = set(self.config.active_filter_groups)

        for name in self.config.filter_group_names:
            action = QAction(name, self.filter_menu)
            action.setCheckable(True)
            action.setChecked(name in active)
            action.toggled.connect(self._on_filter_group_toggled)
            self.filter_menu.addAction(action)

        if not self.config.filter_group_names:
            empty_action = QAction(
                "(no groups configured)", self.filter_menu)
            empty_action.setEnabled(False)
            self.filter_menu.addAction(empty_action)

    def _on_filter_group_toggled(self, checked: bool) -> None:
        """Update active groups when a menu item is toggled."""
        if self._initializing:
            return
        active = []
        for action in self.filter_menu.actions():
            if action.isCheckable() and action.isChecked():
                active.append(action.text())
        self.config.active_filter_groups = active
        self.config.filter_groups_enabled = (
            self.filter_enable_check.isChecked())
        self._save_config_debounced()
        self._update_filter_button_text()

    def _on_filter_toggled(self, enabled: bool) -> None:
        """Master switch for filtering."""
        if self._initializing:
            return
        self.config.filter_groups_enabled = enabled
        self._save_config_debounced()
        self._update_filter_enabled_state()
        self._update_filter_button_text()
        self._rebuild_study_rate_labels()
        self._on_settings_changed()

    def _on_small_series_toggled(self, checked: bool) -> None:
        if self._initializing:
            return
        self.config.filter_allow_small_series = checked
        self._save_config_debounced()
        self._update_filter_enabled_state()
        self._on_settings_changed()

    def _on_small_series_max_changed(self, value: int) -> None:
        if self._initializing:
            return
        self.config.filter_small_series_max = value
        self._save_config_debounced()
        self._on_settings_changed()

    def _update_filter_enabled_state(self) -> None:
        enabled = self.filter_enable_check.isChecked()
        self.filter_btn.setEnabled(enabled)
        if hasattr(self, "series_table"):
            self.series_table.setColumnHidden(10, not enabled)
        # Small-series controls: visible only when filtering is enabled
        small_visible = enabled and self.small_series_check.isChecked()
        self.small_series_check.setVisible(enabled)
        self.lbl_small_max.setVisible(small_visible)
        self.small_series_spin.setVisible(small_visible)
        if enabled:
            self.lbl_filter_info.setText("")
        else:
            self.lbl_filter_info.setText(
                "Filtering disabled \u2014 all studies will be downloaded.")

    def _update_filter_button_text(self) -> None:
        active = self.config.active_filter_groups
        if not active:
            self.filter_btn.setText("Select Groups...")
        elif len(active) <= 3:
            self.filter_btn.setText(", ".join(active))
        else:
            self.filter_btn.setText(
                f"{len(active)} groups selected")

        if self.filter_enable_check.isChecked() and active:
            count = sum(
                1 for inst, grp
                in self.config.institution_assignments.items()
                if grp in set(active))
            self.lbl_filter_info.setText(
                f"Filtering active: {len(active)} group(s), "
                f"{count} institution(s)")

    def refresh_filter_groups(self) -> None:
        """Called when filter groups are edited in the dialog."""
        # Remove active selections that no longer exist
        valid_names = set(self.config.filter_group_names)
        self.config.active_filter_groups = [
            g for g in self.config.active_filter_groups
            if g in valid_names]
        self._save_config_debounced()
        self._populate_filter_menu()
        self._update_filter_button_text()
        self._update_filter_enabled_state()
        self._rebuild_study_rate_labels()

    def _on_sound_toggled(self, checked: bool) -> None:
        node = self._remote_node
        if node:
            node.notification_sound_enabled = checked
            self._save_config_debounced()

    # ── Service control handlers ──────────────────────────────────────────

    def _on_settings_changed(self) -> None:
        if self._service_running:
            self._settings_dirty = True
            self.restart_banner.setVisible(True)

    def _on_start_clicked(self, auto_restart: bool = False) -> None:
        self._settings_dirty = False
        self.restart_banner.setVisible(False)
        # Save per-source params to config (immediate — user-intent
        # action; we want this on disk before the engine starts).
        node = self._remote_node
        if node:
            node.hours = self.hours_spin.value()
            node.max_images = self.max_images_spin.value()
            node.sync_interval = self.interval_spin.value()
            self.flush_pending_save()
        params = self._current_service_params().to_dict()
        # When the start comes from the slow-transfer watchdog's
        # auto-restart, MainWindow must keep retrying (with the siren
        # alarm) instead of aborting if the PACS reachability probe
        # fails — the PACS is often just slow, which is what triggered
        # the restart in the first place.
        params["auto_restart"] = auto_restart
        # Give the user immediate feedback during the startup window;
        # MainWindow may take a moment to bring the engine up.
        self.lbl_status.setText("Starting service…")
        self.start_requested.emit(self.remote_key, params)

    def _on_stop_clicked(self) -> None:
        self._settings_dirty = False
        self.restart_banner.setVisible(False)
        self.stop_requested.emit(self.remote_key)

    def _on_restart_clicked(self, auto_restart: bool = False) -> None:
        """Stop the service and start it again as soon as it has
        actually stopped.  The two-phase flow piggybacks on the
        existing stop/start signals — no new wiring needed in
        MainWindow.

        *auto_restart* marks a watchdog-triggered restart (vs. the
        manual Restart button).  It is forwarded to the auto-start path
        so MainWindow keeps retrying-with-siren on an unreachable PACS
        instead of aborting.

        Idempotent: a second click while a restart is already pending
        is a no-op; we never re-emit stop_requested because the
        engine is already shutting down and a duplicate stop could
        race the freshly-started replacement engine.

        Spinbox values are snapshotted at click time so the engine
        comes back up with the parameters the user actually saw when
        they pressed Restart — not whatever they happened to fiddle
        the controls to during the C-MOVE wind-down window.

        Safety timeout: if ``set_service_running(False)`` never
        arrives (e.g. the engine wedges and ``_join_engines_responsive``
        gives up at 30 s without emitting ``service_stopped``), the
        ``_restart_pending`` flag would otherwise leave the status
        label stuck on "Restarting…" forever.  A QTimer clears it
        after twice the join timeout.
        """
        if self._restart_pending:
            return
        self._restart_pending = True
        self._restart_is_auto = auto_restart
        self._restart_params = self._current_service_params()
        self.lbl_status.setText("Restarting…")
        # 60 s = 2 × the closeEvent join timeout in MainWindow; if
        # the engine hasn't stopped by then something is wedged.
        self._restart_safety_timer.start(60_000)
        self.stop_requested.emit(self.remote_key)

    def _on_restart_safety_timeout(self) -> None:
        """Fired if a Restart click never gets its corresponding
        ``set_service_running(False)`` callback within 60 s — clears
        the pending flag so the status label and cycle handlers
        recover."""
        if not self._restart_pending:
            return
        self._restart_pending = False
        self._restart_params = None
        self.lbl_status.setText(
            "Restart timed out — engine did not stop within 60 s.")
        logger.warning(
            f"[{self.remote_key}] restart safety timeout fired; "
            f"engine did not emit service_stopped after the Restart "
            f"click — pending flag cleared")

    def _current_service_params(self) -> ServiceParams:
        return ServiceParams(
            hours=self.hours_spin.value(),
            max_images=self.max_images_spin.value(),
            sync_interval=self.interval_spin.value(),
            selection_mode=self.manual_selection_check.isChecked(),
        )

    def show_awaiting_pacs(self, message: str) -> None:
        """Put the dashboard into the auto-restart waiting state: the
        engine is stopped while MainWindow keeps probing an unreachable
        PACS, but the user must still be able to Stop the loop, so keep
        the Stop button live and show *message*."""
        # NOTE(i18n): *message* arrives pre-rendered from the caller; any
        # German wording should be produced via core.i18n.tr(key, language)
        # at the call site once the corresponding keys exist in core/i18n.py.
        self._service_running = True
        self._apply_control_enabled(True)
        self.lbl_status.setText(message)

    def set_service_running(self, running: bool) -> None:
        self._service_running = running
        self._apply_control_enabled(running)
        if running:
            # First Cycle-Started signal will overwrite this once it
            # lands, but until then the user gets a clear "yes, it's
            # coming up" hint instead of a stale "Stopped" / "Restarting…".
            self.lbl_status.setText("Service running…")
            return
        # Stopped path.
        self._settings_dirty = False
        self.restart_banner.setVisible(False)
        self.lbl_status.setText("Stopped")
        self._apply_selection_ui_visible(False)
        self.series_table.setColumnHidden(0, True)
        # No transfer can be in flight once stopped — halt the live
        # Pending / ETE countdown (and the stall watchdog).
        self._disarm_active_transfer()
        if self._restart_pending:
            # Engine reached the idle state we were waiting for after
            # a Restart click; bring it back up with the params
            # snapshotted at click time.  Clear the flags BEFORE
            # ``_on_start_clicked`` so any synchronous follow-up
            # callbacks see the clean state.  Cancel the safety timer
            # since the expected callback arrived in time.
            self._restart_safety_timer.stop()
            params = self._restart_params
            auto = self._restart_is_auto
            self._restart_pending = False
            self._restart_params = None
            self._restart_is_auto = False
            # If the source was removed from the config during the
            # restart wait window, MainWindow would silently drop the
            # start_requested signal and leave the user staring at a
            # half-finished restart.  Bail out cleanly instead.
            if self.remote_key not in self.config.remote_nodes:
                self.lbl_status.setText(
                    "Restart cancelled — source removed.")
                return
            if params is not None:
                # Re-apply the snapshotted spinbox values so the
                # user-visible state matches the start request.
                self.hours_spin.setValue(params.hours)
                self.max_images_spin.setValue(params.max_images)
                self.interval_spin.setValue(params.sync_interval)
                self.manual_selection_check.setChecked(
                    params.selection_mode)
            self._on_start_clicked(auto_restart=auto)

    def _apply_control_enabled(self, running: bool) -> None:
        """Enable / disable the service-control buttons and spinboxes
        for the running / idle state."""
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_restart.setEnabled(running)
        self.hours_spin.setEnabled(not running)
        self.max_images_spin.setEnabled(not running)
        self.interval_spin.setEnabled(not running)

    def _apply_selection_ui_visible(self, visible: bool) -> None:
        """Toggle the manual-selection helpers (used when the engine
        is in selection-mode), all in one place."""
        self.btn_download_selected.setVisible(visible)
        self.btn_select_all.setVisible(visible)
        self.btn_deselect_all.setVisible(visible)

    # ── ETE calculation ───────────────────────────────────────────────────

    def _get_rate(self) -> float:
        """Current transfer rate in images per second. 0 if unknown."""
        if not self._current_stats or self._current_stats.total_images == 0:
            return 0.0
        ipm = self._current_stats.overall_images_per_minute()
        return ipm / 60.0 if ipm > 0 else 0.0

    def _pending_for(self, job: dict) -> int:
        """Pending images for one job, discounting images the active
        transfer has already received this run so the count drops live."""
        if job["status"] in _TERMINAL_STATUSES:
            return 0
        pending = max(job["remote_count"] - job["local_count"], 0)
        if (job["series_uid"] == self._active_series_uid
                and self._active_series_done > 0):
            pending = max(pending - self._active_series_done, 0)
        return pending

    def _compute_cumulative_pending(self, queue: list) -> list:
        """Return a list of cumulative pending-image counts per queue row."""
        return list(itertools.accumulate(
            self._pending_for(j) for j in queue))

    @staticmethod
    def _format_ete(seconds: float) -> str:
        """Format seconds into mm:ss or hh:mm:ss."""
        if seconds <= 0:
            return "\u2014"
        seconds = int(seconds)
        if seconds >= 3600:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            return f"{h}:{m:02d}:{s:02d}"
        m = seconds // 60
        s = seconds % 60
        return f"{m}:{s:02d}"

    # ── Queue-table cell builders ─────────────────────────────────────────
    # Single source of truth per mutable column — shared by the
    # full-rebuild and in-place paths of ``on_queue_updated`` (and the
    # stats-driven ``_update_ete_column``) so text and colour can never
    # drift between them.

    def _make_pending_item(self, job: dict) -> QTableWidgetItem:
        """Pending column (6): images still to fetch, discounting any the
        active transfer has already received so the count drops live."""
        return QTableWidgetItem(str(self._pending_for(job)))

    @staticmethod
    def _make_ipm_item(job: dict) -> QTableWidgetItem:
        """img/min column (7): rate in blue once the series is done."""
        ipm = job.get("images_per_minute", 0.0)
        if job["status"] == "done" and ipm > 0:
            ipm_item = QTableWidgetItem(f"{ipm:.0f}")
            ipm_item.setForeground(QColor(COLOR_BLUE_ACCENT))
        else:
            ipm_item = QTableWidgetItem("—")
            ipm_item.setForeground(QColor(COLOR_MUTED))
        ipm_item.setTextAlignment(Qt.AlignCenter)
        return ipm_item

    @classmethod
    def _make_status_item(cls, status: str) -> QTableWidgetItem:
        """Status column (8): human-readable text + status colour."""
        status_item = QTableWidgetItem(cls._status_text(status))
        status_item.setForeground(cls._status_color(status))
        return status_item

    @staticmethod
    def _format_series_created(date_digits: str, time_digits: str) -> str:
        """Format a DICOM SeriesDate (``YYYYMMDD``) and SeriesTime
        (``HHMMSS``) into ``DD.MM.YYYY HH:MM`` for display.  Falls back
        gracefully: missing time shows the date only; an unparseable
        value is shown verbatim; both missing → dash."""
        d = (date_digits or "").strip()
        t = (time_digits or "").strip()
        date_part = ""
        if len(d) == 8 and d.isdigit():
            date_part = f"{d[6:8]}.{d[4:6]}.{d[0:4]}"
        elif d:
            date_part = d
        time_part = ""
        if len(t) >= 4 and t[:4].isdigit():
            time_part = f"{t[0:2]}:{t[2:4]}"
        if date_part and time_part:
            return f"{date_part} {time_part}"
        return date_part or time_part or "—"

    @classmethod
    def _make_series_created_item(cls, job: dict) -> QTableWidgetItem:
        """Series Created column (11): when the series was acquired on the
        modality (SeriesDate/SeriesTime, with study date/time fallback)."""
        item = QTableWidgetItem(cls._format_series_created(
            job.get("series_date", ""), job.get("series_time", "")))
        item.setForeground(QColor(COLOR_MUTED))
        return item

    @classmethod
    def _make_ete_item(cls, status: str, cumulative_pending: int,
                       rate: float) -> QTableWidgetItem:
        """ETE column (9): check-mark when done, dash when dead or
        unknown, otherwise the cumulative pending image count for this
        and all preceding rows divided by the current rate."""
        if status in _TERMINAL_STATUSES:
            ete_text = "✓" if status == "done" else "—"
            ete_item = QTableWidgetItem(ete_text)
            if status == "done":
                ete_item.setForeground(QColor(COLOR_GREEN))
            else:
                ete_item.setForeground(QColor(COLOR_MUTED))
        elif rate > 0:
            ete_seconds = cumulative_pending / rate
            ete_item = QTableWidgetItem(cls._format_ete(ete_seconds))
            ete_item.setForeground(QColor(COLOR_ORANGE))
        else:
            ete_item = QTableWidgetItem("—")
            ete_item.setForeground(QColor(COLOR_MUTED))
        ete_item.setTextAlignment(Qt.AlignCenter)
        return ete_item

    def _update_series_summary(self, queue: list) -> None:
        """Refresh the 'Series: done / total' summary label."""
        done_count = sum(1 for job in queue if job["status"] == "done")
        self.lbl_total_series.setText(
            f"Series: {done_count} / {len(queue)}")

    # ── Slots called by engine signals ────────────────────────────────────

    def on_queue_updated(self, queue: list) -> None:
        """Render the series table from the full queue list.

        The engine emits the full queue after EVERY completed series,
        so unconditionally tearing the table down and rebuilding it is
        O(n²) widget churn per cycle, flickers visibly, and destroys
        the user's row selection on each emit.  Instead we compare the
        incoming series_uid sequence with the one currently rendered:

        * sequence differs (new cycle, selection filtering, first
          render) → full rebuild as before;
        * sequence identical (the common per-series progress emit) →
          update only the mutable cells in place — Pending (6),
          img/min (7), Status (8), ETE (9) and the done-count summary.
          Row selection intentionally survives these updates.
        """
        self._last_queue = queue
        # Hide selection UI — engine is now downloading or idle
        self._apply_selection_ui_visible(False)
        self.series_table.setColumnHidden(0, True)

        uids = [job["series_uid"] for job in queue]
        if uids == self._rendered_uids:
            self._update_queue_cells_in_place(queue)
        else:
            self._rebuild_queue_table(queue)
            self._rendered_uids = uids

    def _rebuild_queue_table(self, queue: list) -> None:
        """Full rebuild of the series table (uid sequence changed).

        Wrapped in ``setUpdatesEnabled(False)`` so the whole rebuild
        repaints once.  Resets ``_queue_cell_cache`` since every row is
        re-rendered fresh here."""
        rate = self._get_rate()
        cumulative = self._compute_cumulative_pending(queue)
        self._queue_cell_cache.clear()

        self.series_table.setUpdatesEnabled(False)
        try:
            self.series_table.setRowCount(0)
            for i, job in enumerate(queue):
                row = self.series_table.rowCount()
                self.series_table.insertRow(row)

                self.series_table.setItem(
                    row, 1, QTableWidgetItem(job["patient_name"]))
                self.series_table.setItem(
                    row, 2, QTableWidgetItem(job["study_description"]))
                self.series_table.setItem(
                    row, 3, QTableWidgetItem(job["series_description"]))
                self.series_table.setItem(
                    row, 4, QTableWidgetItem(job["modality"]))
                self.series_table.setItem(
                    row, 5, QTableWidgetItem(str(job["remote_count"])))

                self.series_table.setItem(
                    row, 6, self._make_pending_item(job))
                self.series_table.setItem(row, 7, self._make_ipm_item(job))
                self.series_table.setItem(
                    row, 8, self._make_status_item(job["status"]))
                self.series_table.setItem(
                    row, 9, self._make_ete_item(
                        job["status"], cumulative[i], rate))

                # Group column
                group = self.config.institution_assignments.get(
                    job.get("institution_name", ""), "")
                self.series_table.setItem(
                    row, 10, QTableWidgetItem(group))

                # Series Created column (static per series).
                self.series_table.setItem(
                    row, 11, self._make_series_created_item(job))

                self._queue_cell_cache[job["series_uid"]] = (
                    job["status"], self._ipm_text(job))
        finally:
            self.series_table.setUpdatesEnabled(True)

        self._update_series_summary(queue)

    def _update_queue_cells_in_place(self, queue: list) -> None:
        """Same uid sequence as currently rendered — refresh only the
        cells that can change between per-series progress emits.  The
        static columns (patient, study, series, modality, images,
        group) are keyed by series_uid and cannot have changed.

        This runs after EVERY completed series, so it must be cheap.
        Pending (6) and ETE (9) are updated in place via ``setText``;
        the brush-carrying img/min (7) and Status (8) cells are only
        rebuilt when their rendered value actually changed (tracked in
        ``_queue_cell_cache``).  The whole pass is wrapped in
        ``setUpdatesEnabled(False)`` so Qt coalesces one repaint instead
        of emitting a ``dataChanged``→``visualRect`` storm per cell —
        that storm was pinning the GUI thread at 100% on large queues."""
        rate = self._get_rate()
        cumulative = self._compute_cumulative_pending(queue)

        self.series_table.setUpdatesEnabled(False)
        try:
            for i, job in enumerate(queue):
                uid = job["series_uid"]
                cached = self._queue_cell_cache.get(uid, (None, None))

                # Pending (6): update existing item text in place.
                pending_item = self.series_table.item(i, 6)
                if pending_item is not None:
                    pending_item.setText(str(self._pending_for(job)))
                else:
                    self.series_table.setItem(
                        i, 6, self._make_pending_item(job))

                # img/min (7) and Status (8) carry a colour brush, so a
                # plain setText would not recolour them — rebuild only on
                # an actual change.
                status = job["status"]
                ipm_text = self._ipm_text(job)
                if cached != (status, ipm_text):
                    self.series_table.setItem(
                        i, 7, self._make_ipm_item(job))
                    self.series_table.setItem(
                        i, 8, self._make_status_item(status))
                    self._queue_cell_cache[uid] = (status, ipm_text)

                # ETE (9): in-place text via the shared helper.
                self._set_ete_text(i, status, cumulative[i], rate)
        finally:
            self.series_table.setUpdatesEnabled(True)

        self._update_series_summary(queue)

    @staticmethod
    def _ipm_text(job: dict) -> str:
        """The img/min cell's display text for *job* — used both to
        render the cell and as the change-detection key for the in-place
        update cache."""
        ipm = job.get("images_per_minute", 0.0)
        return f"{ipm:.0f}" if job["status"] == "done" and ipm > 0 else "—"

    def on_queue_ready_for_selection(self, queue: list) -> None:
        """Engine paused after query — show checkboxes for manual selection."""
        self._last_queue = queue
        # The table now holds selection-mode rows (checkbox column,
        # "Waiting" statuses) — force the next on_queue_updated to do a
        # full rebuild even if the uid sequence happens to match.
        self._rendered_uids = None
        self.series_table.setRowCount(0)
        self.series_table.setColumnHidden(0, False)

        for job in queue:
            row = self.series_table.rowCount()
            self.series_table.insertRow(row)

            cb_item = QTableWidgetItem()
            cb_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            cb_item.setCheckState(Qt.Checked)
            cb_item.setData(Qt.UserRole, job["series_uid"])
            self.series_table.setItem(row, 0, cb_item)

            self.series_table.setItem(
                row, 1, QTableWidgetItem(job["patient_name"]))
            self.series_table.setItem(
                row, 2, QTableWidgetItem(job["study_description"]))
            self.series_table.setItem(
                row, 3, QTableWidgetItem(job["series_description"]))
            self.series_table.setItem(
                row, 4, QTableWidgetItem(job["modality"]))
            self.series_table.setItem(
                row, 5, QTableWidgetItem(str(job["remote_count"])))
            pending = job["remote_count"] - job["local_count"]
            self.series_table.setItem(
                row, 6, QTableWidgetItem(str(max(pending, 0))))
            self.series_table.setItem(row, 7, QTableWidgetItem("\u2014"))
            status_item = QTableWidgetItem("\u23f3 Waiting")
            status_item.setForeground(QColor(COLOR_ORANGE))
            self.series_table.setItem(row, 8, status_item)
            self.series_table.setItem(row, 9, QTableWidgetItem("\u2014"))

            # Group column
            group = self.config.institution_assignments.get(
                job.get("institution_name", ""), "")
            self.series_table.setItem(
                row, 10, QTableWidgetItem(group))

            # Series Created column.
            self.series_table.setItem(
                row, 11, self._make_series_created_item(job))

        total = sum(max(j["remote_count"] - j["local_count"], 0) for j in queue)
        self.lbl_total_series.setText(f"Series: 0 / {len(queue)}")
        self.lbl_total_images.setText(f"Pending: {total} images")
        self.lbl_status.setText(
            f"Awaiting selection — {len(queue)} series found")
        self.btn_download_selected.setVisible(True)
        self.btn_select_all.setVisible(True)
        self.btn_deselect_all.setVisible(True)
        # Allow checking/unchecking in the table
        self.series_table.setEditTriggers(QTableWidget.NoEditTriggers)

    def _set_all_series_checked(self, checked: bool) -> None:
        """Check or uncheck every row of the selection table."""
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.series_table.rowCount()):
            cb_item = self.series_table.item(row, 0)
            if cb_item is not None:
                cb_item.setCheckState(state)

    def _on_download_selected_clicked(self) -> None:
        """Collect checked series UIDs and confirm selection to the engine."""
        selected_uids = []
        for row in range(self.series_table.rowCount()):
            cb_item = self.series_table.item(row, 0)
            if cb_item and cb_item.checkState() == Qt.Checked:
                uid = cb_item.data(Qt.UserRole)
                if uid:
                    selected_uids.append(uid)
        self.selection_confirmed.emit(self.remote_key, selected_uids)

    def on_series_started(self, info: dict) -> None:
        if self._restart_pending:
            return  # keep the "Restarting\u2026" label stable
        self.lbl_status.setText(
            f"Transferring: {info['patient_name']} \u2014 "
            f"[{info.get('modality', '')}] {info['series_description']}")
        self._active_series_uid = info.get("series_uid")
        self._active_series_done = 0
        self._last_progress_ts = time.monotonic()
        self._slow_transfer_timer.start(_SLOW_TRANSFER_TIMEOUT_MS)
        # Drive the per-second Pending / ETE countdown for as long as a
        # transfer is in flight.
        if not self._live_refresh_timer.isActive():
            self._live_refresh_timer.start()

    def on_series_progress(self, series_uid: str, completed: int,
                           total: int) -> None:
        """Per-image progress tick during a C-MOVE \u2014 re-arm the stall
        watchdog so a slow-but-alive transfer is not mistaken for a
        wedged one, and record how many images have arrived so the
        Pending / ETE columns can count down live.  Only acts while a
        transfer is actually active."""
        if self._active_series_uid is None or self._restart_pending:
            return
        self._active_series_done = max(completed, 0)
        self._last_progress_ts = time.monotonic()
        self._slow_transfer_timer.start(_SLOW_TRANSFER_TIMEOUT_MS)

    def _disarm_active_transfer(self) -> None:
        """Tear down all per-transfer activity: stop the stall watchdog,
        forget the active series, and halt the per-second Pending / ETE
        countdown.  Shared by on_series_completed / on_series_error (a
        series finished) and by the stopped path of set_service_running /
        reset (no transfer can be in flight).  Each call is a no-op when
        the timers are already stopped, so it is safe at every site."""
        self._slow_transfer_timer.stop()
        self._active_series_uid = None
        self._active_series_done = 0
        self._last_progress_ts = 0.0
        self._live_refresh_timer.stop()

    def on_series_completed(self, series_uid: str, images: int) -> None:
        """Called when a series finishes successfully \u2014 disarms the watchdog."""
        self._disarm_active_transfer()

    def on_series_error(self, series_uid: str, error_msg: str) -> None:
        """Called when a series transfer fails \u2014 disarms the watchdog."""
        self._disarm_active_transfer()

    def on_connection_lost(self, remote_key: str, detail: str) -> None:
        """PACS became unreachable \u2014 suppress the slow-transfer watchdog restart.
        The engine retries automatically; no restart needed from our side."""
        self._pacs_connection_lost = True
        self._slow_transfer_timer.stop()

    def on_connection_restored(self, remote_key: str) -> None:
        """PACS connection recovered \u2014 re-arm the watchdog for future transfers."""
        self._pacs_connection_lost = False

    def _on_slow_transfer_detected(self) -> None:
        """Watchdog fired: series download exceeded _SLOW_TRANSFER_TIMEOUT_MS.

        Skipped when the engine already reported a PACS connection outage
        (it recovers on its own). Otherwise plays the sad sound, shows a
        non-modal warning, and auto-restarts to unblock a wedged C-MOVE."""
        if self._pacs_connection_lost:
            return
        # Guard against a SPURIOUS firing: the watchdog is a GUI-thread
        # QTimer, so if the GUI thread was busy (e.g. rendering a big
        # queue) the timer can fire late even though images kept arriving
        # — the queued ``on_series_progress`` re-arms just hadn't been
        # delivered yet.  Re-check the real elapsed time since the last
        # progress; if it's under the timeout the transfer is alive, so
        # re-arm for the remainder instead of restarting.
        if self._active_series_uid is not None and self._last_progress_ts:
            elapsed_ms = (time.monotonic() - self._last_progress_ts) * 1000
            if elapsed_ms < _SLOW_TRANSFER_TIMEOUT_MS:
                remaining = int(_SLOW_TRANSFER_TIMEOUT_MS - elapsed_ms)
                self._slow_transfer_timer.start(max(remaining, 100))
                return
        uid = self._active_series_uid or "?"
        logger.warning(
            f"[{self.remote_key}] Slow/stalled transfer detected "
            f"(>{_SLOW_TRANSFER_TIMEOUT_MS // 1000}s) \u2014 series {uid} \u2014 "
            f"triggering auto-restart")
        self._play_sound(_generate_default_sound(sad=True))
        self._show_slow_transfer_notice()
        if self._service_running and not self._restart_pending:
            self._on_restart_clicked(auto_restart=True)

    def _show_slow_transfer_notice(self) -> None:
        """Show a purely informational, non-blocking notice that the
        service is auto-restarting after a slow download.

        Reuses a single self-dismissing popup: repeated timeouts update
        the existing notice instead of stacking new dialogs the user
        would have to click away, and it auto-closes after a few seconds
        so it never sits in front of the running service."""
        # TODO(i18n): these user-facing German strings (and the window
        # title below) should move to core.i18n.tr(key, language) once the
        # corresponding translation keys exist in core/i18n.py.
        text = (
            f"Ein Bild-Download bei <b>{self.remote_key}</b> hat l\u00e4nger als "
            f"{_SLOW_TRANSFER_TIMEOUT_MS // 1000} Sekunden gedauert.\n\n"
            f"Der Dienst wird automatisch neu gestartet.")
        existing = self._slow_transfer_notice
        if existing is not None:
            # Already on screen \u2014 just refresh its text and re-arm the
            # auto-close timer instead of stacking another dialog.
            existing.setText(text)
            self._slow_transfer_notice_timer.start(
                _SLOW_TRANSFER_NOTICE_MS)
            return
        msg = QMessageBox(self)
        msg.setWindowTitle("Langsamer Download erkannt")
        msg.setText(text)
        msg.setIcon(QMessageBox.Warning)
        msg.setStandardButtons(QMessageBox.Ok)
        # Explicitly non-modal so it can never block the event loop or
        # the running service; the user may dismiss it or ignore it.
        msg.setWindowModality(Qt.NonModal)
        msg.setModal(False)
        msg.finished.connect(self._on_slow_transfer_notice_closed)
        self._slow_transfer_notice = msg
        msg.show()
        self._slow_transfer_notice_timer.start(_SLOW_TRANSFER_NOTICE_MS)

    def _on_slow_transfer_notice_closed(self, _result: int = 0) -> None:
        """Drop the reference once the notice is dismissed (by the user
        or the auto-close timer) so the next timeout creates a fresh one.

        Called both from the dialog's ``finished`` signal (user clicked
        OK / closed it) and from the auto-close timer.  In the timer
        case the dialog is still visible, so close it — but disconnect
        ``finished`` first so the close doesn't re-enter this handler."""
        self._slow_transfer_notice_timer.stop()
        notice = self._slow_transfer_notice
        self._slow_transfer_notice = None
        if notice is not None:
            try:
                notice.finished.disconnect(
                    self._on_slow_transfer_notice_closed)
            except (RuntimeError, TypeError):
                pass
            notice.close()
            notice.deleteLater()

    def on_stats_updated(self, stats: TransferStats) -> None:
        self._current_stats = stats
        self.lbl_total_images.setText(f"Total: {stats.total_images} images")
        self._refresh_stats_display()
        # Re-render ETE values with updated rate
        if self._last_queue:
            self._update_ete_column()

    def on_cycle_started(self, cycle: int) -> None:
        self.lbl_cycle.setText(f"Cycle: {cycle}")
        if self._restart_pending:
            return
        self.lbl_status.setText(f"Cycle {cycle} \u2014 querying...")

    def on_cycle_finished(self, cycle: int, images: int) -> None:
        if self._restart_pending:
            return
        if images > 0:
            self.lbl_status.setText(
                f"Cycle {cycle} done \u2014 {images} images")
        else:
            self.lbl_status.setText(
                f"Cycle {cycle} \u2014 waiting...")

    # ── Stats display ─────────────────────────────────────────────────────

    def _refresh_stats_display(self) -> None:
        stats = self._current_stats
        if not stats or stats.completed_count == 0:
            return

        median_all = stats.median_all_ipm()
        last = stats.last_series_ipm()
        med5 = stats.median_n_ipm(5)
        med10 = stats.median_n_ipm(10)

        self.stat_last.set_value(last, median_all)
        self.stat_med5.set_value(med5, median_all)
        self.stat_med10.set_value(med10, median_all)
        self.stat_medall.set_value(median_all, median_all)

        self.lbl_total_images.setText(f"Total: {stats.total_images} images")

    def _update_ete_column(self) -> None:
        """Update only the ETE column without rebuilding the whole table.

        Updates existing cells' text in place (see ``_set_ete_text``)
        rather than reallocating an item per row on every stats tick."""
        rate = self._get_rate()
        queue = self._last_queue
        cumulative = self._compute_cumulative_pending(queue)
        rows = self.series_table.rowCount()
        for i, job in enumerate(queue):
            if i >= rows:
                break
            self._set_ete_text(i, job["status"], cumulative[i], rate)

    def _refresh_pending_and_ete(self) -> None:
        """Per-second tick: refresh the Pending (6) and ETE (9) columns
        from the active transfer's live progress so both count down
        smoothly between queue/stats signals.  Driven by
        ``_live_refresh_timer`` only while a transfer is in flight.

        Updates the TEXT of existing cells in place (``setText``) rather
        than allocating fresh ``QTableWidgetItem``s every tick — at 1 Hz
        across a large queue (and several source tabs) the allocation +
        relayout churn of recreating every cell was enough to make the UI
        unresponsive during a download.  When the window is hidden there
        is nothing to repaint, so we skip the work entirely."""
        queue = self._last_queue
        if not queue or not self.isVisible():
            return
        rate = self._get_rate()
        cumulative = self._compute_cumulative_pending(queue)
        rows = self.series_table.rowCount()
        for i, job in enumerate(queue):
            if i >= rows:
                break
            pending_item = self.series_table.item(i, 6)
            if pending_item is not None:
                pending_item.setText(str(self._pending_for(job)))
            else:
                self.series_table.setItem(i, 6, self._make_pending_item(job))
            self._set_ete_text(i, job["status"], cumulative[i], rate)

    def _set_ete_text(self, row: int, status: str,
                      cumulative_pending: int, rate: float) -> None:
        """Update only the ETE cell's TEXT in place when an item already
        exists, falling back to building a fresh item otherwise.  Avoids
        re-creating the item (and re-setting its brush/alignment) on
        every 1 Hz tick."""
        existing = self.series_table.item(row, 9)
        if existing is None:
            self.series_table.setItem(
                row, 9, self._make_ete_item(status, cumulative_pending, rate))
            return
        if status in _TERMINAL_STATUSES or rate <= 0:
            # Terminal / unknown-rate cells don't count down; leave the
            # item the queue path already rendered untouched.
            return
        existing.setText(self._format_ete(cumulative_pending / rate))

    # ── Study rate ────────────────────────────────────────────────────────

    def _rebuild_study_rate_labels(self) -> None:
        """Create or recreate per-group (or total) labels."""
        # Clear existing.  ``deleteLater()`` schedules the QLabel for
        # destruction at the end of the current event-loop slice;
        # ``setParent(None)`` would just detach them and leave the C++
        # objects alive until the next Python GC, which on long-lived
        # MainWindows leaks one QLabel per filter-group reconfiguration.
        for lbl in self.study_rate_labels.values():
            lbl.deleteLater()
        self.study_rate_labels.clear()

        if self.config.filter_groups_enabled:
            for name in self.config.filter_group_names:
                lbl = QLabel(f"{name}: 0")
                self.study_rate_layout.addWidget(lbl)
                self.study_rate_labels[name] = lbl
        else:
            lbl = QLabel("Total: 0")
            self.study_rate_layout.addWidget(lbl)
            self.study_rate_labels["_total"] = lbl

    def _compute_study_rates(self, queue: list,
                             now: datetime | None = None) -> dict[str, int]:
        """Count unique studies within the last 60 minutes, grouped.

        Thin wrapper that feeds the relevant config values into the
        pure ``gui.study_rate.compute_study_rates`` — kept as an
        instance method for backwards compatibility (tests and older
        call sites invoke it on the dashboard)."""
        return compute_study_rates(
            queue,
            filter_groups_enabled=self.config.filter_groups_enabled,
            institution_assignments=self.config.institution_assignments,
            now=now)

    @staticmethod
    def _study_rate_color(n: int) -> str | None:
        """Return CSS color string for a study rate value.  Thin
        wrapper around the pure ``gui.study_rate.study_rate_color``."""
        return study_rate_color(n)

    def on_study_completed(self, study_uid: str,
                           institution_name: str,
                           fully_complete: bool = True,
                           image_count: Optional[int] = None) -> None:
        """Slot for study_completed — play notification sound
        only when the study was fully downloaded *and* enough images
        actually arrived.

        ``image_count`` is the total number of images downloaded for
        this study (sum of ``transferred_images`` over done series).
        When the count is below ``MIN_IMAGES_FOR_NOTIFICATION_SOUND``
        the study is treated as too small to chime — typically a
        stray prior or a partial fetch.  ``None`` (the default used
        by direct unit-test calls) skips the size check.
        """
        if not fully_complete:
            return
        if (image_count is not None
                and image_count < MIN_IMAGES_FOR_NOTIFICATION_SOUND):
            return
        self._play_notification_if_allowed(institution_name)

    def _play_notification_if_allowed(self, institution_name: str) -> None:
        """Play notification sound if enabled and institution passes filter."""
        node = self._remote_node
        if not node or not node.notification_sound_enabled:
            return
        if self.config.filter_groups_enabled:
            group = self.config.institution_assignments.get(
                institution_name, "")
            if group not in set(self.config.active_filter_groups):
                return
        path = node.notification_sound_path
        if path and os.path.isfile(path):
            self._play_sound(path)
        else:
            self._play_sound(_generate_default_sound(sad=self._is_speed_red()))

    def _is_speed_red(self) -> bool:
        """Return True if median-5 speed is below the red threshold."""
        stats = self._current_stats
        if not stats or stats.completed_count < 1:
            return False
        median_all = stats.median_all_ipm()
        if median_all < 1:
            return False
        return stats.median_n_ipm(5) < median_all * (1 - SPEED_BAND_RATIO)

    def _play_sound(self, path: str) -> None:
        """Play a WAV file via the long-lived QSoundEffect (lazy)."""
        if self._sound_effect is None:
            self._sound_effect = QSoundEffect(self)
        self._sound_effect.stop()
        self._sound_effect.setSource(QUrl.fromLocalFile(path))
        self._sound_effect.play()

    def on_studies_queried(self, studies: list) -> None:
        """Slot for the studies_queried signal — receives raw query results."""
        self._update_study_rate_display(studies)

    def _update_study_rate_display(self, studies: list) -> None:
        """Refresh the study rate labels and trigger high-load popup."""
        rates = self._compute_study_rates(studies)

        # Create labels on the fly for groups not yet tracked
        for key in rates:
            if key not in self.study_rate_labels:
                display = "Unassigned" if key == "" else key
                lbl = QLabel(f"{display}: 0")
                self.study_rate_layout.addWidget(lbl)
                self.study_rate_labels[key] = lbl

        for key, lbl in self.study_rate_labels.items():
            count = rates.get(key, 0)
            if key == "_total":
                lbl.setText(f"Total: {count}")
            elif key == "":
                lbl.setText(f"Unassigned: {count}")
            else:
                lbl.setText(f"{key}: {count}")
            color = self._study_rate_color(count)
            if color:
                lbl.setStyleSheet(f"color: {color};")
            else:
                lbl.setStyleSheet("")

        # High-load popup (non-modal so it doesn't block signal processing)
        high_groups = {g for g, c in rates.items()
                       if c >= STUDY_RATE_HIGH_LOAD}
        new_high = high_groups - self._last_high_load_groups
        if new_high and self.config.high_load_alert_enabled:
            QApplication.beep()
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("High Study Load")
            msg.setText(
                # Threshold interpolated from the constant so the
                # message can never drift from the actual trigger.
                f"The incoming study rate has exceeded the threshold "
                f"(≥{STUDY_RATE_HIGH_LOAD} studies/hour). "
                f"Please check system capacity.",
            )
            msg.setModal(False)
            msg.setAttribute(Qt.WA_DeleteOnClose)
            msg.show()
        self._last_high_load_groups = high_groups

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _status_text(status: str) -> str:
        return {
            "queued": "\u23f3 Queued",
            "transferring": "\u25b6 Transferring...",
            "done": "\u2713 Done",
            "error": "\u2717 Error",
            "skipped": "\u2014 Skipped",
            "unavailable": "\u2298 Not available",
        }.get(status, status)

    @staticmethod
    def _status_color(status: str) -> QColor:
        return {
            "queued": QColor(COLOR_MUTED),
            "transferring": QColor(COLOR_ORANGE),
            "done": QColor(COLOR_GREEN),
            "error": QColor(COLOR_RED),
            "skipped": QColor(COLOR_MUTED),
            "unavailable": QColor(COLOR_RED),
        }.get(status, QColor("#d4d4d4"))

    def reset(self) -> None:
        self.series_table.setRowCount(0)
        self._current_stats = None
        self._last_queue = []
        # Stop the live Pending / ETE countdown (and stall watchdog) —
        # no transfer is active.
        self._disarm_active_transfer()
        # Table was just cleared — drop the per-row render cache and
        # force the next on_queue_updated to rebuild.
        self._queue_cell_cache.clear()
        self._last_progress_ts = 0.0
        self._rendered_uids = None
        self._last_high_load_groups = set()
        self.stat_last.setText("\u2014")
        self.stat_med5.setText("\u2014")
        self.stat_med10.setText("\u2014")
        self.stat_medall.setText("\u2014")
        self.lbl_total_images.setText("Total: 0 images")
        self.lbl_total_series.setText("Series: 0")
        self.lbl_cycle.setText("Cycle: \u2014")
        self.lbl_status.setText("Idle")
        self._rebuild_study_rate_labels()

    def sync_from_config(self) -> None:
        """Update spinboxes from the current config node values."""
        node = self._remote_node
        if node:
            self.hours_spin.setValue(node.hours)
            self.max_images_spin.setValue(node.max_images)
            self.interval_spin.setValue(node.sync_interval)
