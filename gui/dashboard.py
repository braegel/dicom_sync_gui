"""
Source Dashboard — one widget per configured source PACS.
Shows per-source service controls (Start/Stop, hours, max images, interval),
the series queue with ETE (estimated time to completion),
and real-time throughput statistics with color-coded indicators.
"""

import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QFont, QAction
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox, QGridLayout,
    QPushButton, QSpinBox, QFormLayout, QCheckBox, QMenu, QToolButton,
    QMessageBox, QApplication,
)

from core.i18n import tr
from core.transfer_engine import TERMINAL_STATUSES, TransferStats
from gui.notification_sound import _generate_default_sound
from gui import queue_table
from gui.queue_table import QueueTableView
from gui.service_watchdog import (
    WATCHDOG_POLL_MS,
    ActiveTransfer, PendingRestart, pending_images_of,
    resolve_pending_restart, series_deadline_s,
    CANCEL_SOURCE_GONE, RESUME,
)
from gui.study_rate import (
    COLOR_GREEN, COLOR_RED, STUDY_RATE_HIGH_LOAD,
    compute_study_rates, study_rate_color,
)
from gui.styles import (
    BTN_AMBER, BTN_BLUE, BTN_DOWNLOAD_SELECTED, BTN_START, BTN_STOP,
    COLOR_ORANGE, LBL_RESTART_BANNER, STAT_BG_BAD,
    STAT_BG_GOOD, SURFACE_BG, TOOLBTN_FILTER,
    stat_label_style,
)

if TYPE_CHECKING:  # annotation only
    from core.config import PacsNode

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
# Minimum gap between two high-study-load warnings (s).  The rate is
# recomputed on every query cycle, so a load hovering at the threshold
# crosses it repeatedly; without a cooldown the dialog re-popped every
# few seconds and the user had to keep clicking it away.
_HIGH_LOAD_COOLDOWN_S = 15 * 60

# How long the (non-blocking) slow-transfer auto-restart notice stays on
# screen before it dismisses itself (ms).  It's informational only — the
# service restarts regardless of whether the user ever sees it.
_SLOW_TRANSFER_NOTICE_MS = 5_000

# How long to wait for the engine to actually stop after a Restart click
# before forcing the issue.  A wedged C-MOVE only unblocks once its DIMSE
# timeout (300 s in core.dicom_ops) elapses, so this MUST exceed that —
# otherwise the safety timer fires while the engine is still legitimately
# winding down a stuck transfer and the restart is abandoned, leaving the
# service "Stopped".  360 s = 300 s DIMSE + margin.
_RESTART_SAFETY_TIMEOUT_MS = 360_000

# Statuses a series can no longer leave: it contributes no pending
# images and gets no ETE.  "unavailable" = retry budget exhausted.
# Canonical definition now lives in core.transfer_engine; aliased here
# for minimal churn so existing _TERMINAL_STATUSES call sites keep working.
_TERMINAL_STATUSES = TERMINAL_STATUSES


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
            COLOR_GREEN: STAT_BG_GOOD,
            COLOR_RED: STAT_BG_BAD,
        }.get(color, SURFACE_BG)
        return stat_label_style(color, bg)


class SourceDashboard(QWidget):
    """Dashboard widget for a single source PACS — controls + live progress."""

    # Signals to main window
    start_requested = Signal(str, dict)   # remote_key, {hours, max_images, sync_interval, selection_mode}
    stop_requested = Signal(str)          # remote_key
    selection_confirmed = Signal(str, list)  # remote_key, [series_uid, ...]

    def __init__(self, config: Any, remote_key: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._init_state(config, remote_key)
        self._setup_ui()
        self._init_timers()

    def _init_state(self, config: Any, remote_key: str) -> None:
        """Declare every piece of per-dashboard state.

        Split out of ``__init__`` so the constructor reads as the
        three-step sequence it actually is (state → widgets →
        timers); the timers in particular must come AFTER ``_setup_ui``
        because several of them connect to slots that touch widgets.
        """
        self.config = config
        self.remote_key = remote_key
        self._current_stats: Optional[TransferStats] = None
        self._last_queue: list[dict] = []
        self._service_running = False
        self._settings_dirty = False
        # Set between a Restart-click and the subsequent
        # set_service_running(False) callback; tells that callback to
        # re-emit start_requested instead of just leaving the engine
        # idle.  ``None`` means no restart is pending — see
        # gui.service_watchdog.PendingRestart for what it carries.
        self._pending_restart: Optional[PendingRestart] = None
        self._last_high_load_groups: set[str] = set()
        # Monotonic timestamp of the last high-load warning; see
        # _HIGH_LOAD_COOLDOWN_S.
        self._last_high_load_alert_ts: float = 0.0
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
        # The transfer currently in flight: which series, how many
        # images have landed, when progress last arrived, and the
        # watchdog deadline.  One object rather than four attributes so
        # no disarm path can reset three of them and forget the fourth.
        self._active = ActiveTransfer()
        # True while the engine has reported a PACS connection outage
        # (connection_lost) and not yet recovered (connection_restored).
        # The slow-transfer watchdog skips the auto-restart in this state
        # because the engine recovers automatically without help.
        self._pacs_connection_lost: bool = False

    def _init_timers(self) -> None:
        """Create the five QTimers that drive the dashboard.  Must run
        after ``_setup_ui`` — every timeout slot here touches widgets
        the builders create."""
        # Refresh stats display every 2 seconds
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_stats_display)
        self._timer.start(STATS_REFRESH_MS)

        # Debounce config writes: a hammered spinbox / rapid toggles
        # otherwise call save() once per UI event.  The debounce alone
        # was not enough — the write fsyncs, so it stalled the event
        # loop for the duration of the disk write.  ``save_async``
        # snapshots on this thread and writes on a background one; the
        # snapshot is what keeps that write consistent.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(CONFIG_SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self.config.save_async)

        # Safety timer for ``_on_restart_clicked`` — see that method's
        # docstring.  Single-shot, started on Restart click, stopped
        # when the engine actually goes idle (auto-start branch).
        self._restart_safety_timer = QTimer(self)
        self._restart_safety_timer.setSingleShot(True)
        self._restart_safety_timer.timeout.connect(
            self._on_restart_safety_timeout)

        # Series-level stall watchdog: a recurring poll that asks
        # ``_active`` whether the transfer is genuinely wedged (see
        # gui.service_watchdog for the two-condition rule), then
        # auto-restarts the service.
        self._slow_transfer_timer = QTimer(self)
        self._slow_transfer_timer.setInterval(WATCHDOG_POLL_MS)
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
        """Synchronously persist any pending debounced save.

        Stops the debounce timer so the pending write happens NOW and
        not 500 ms after the window is gone; ``config.save()`` itself
        drains any in-flight background write first.
        """
        if self._save_timer.isActive():
            self._save_timer.stop()
        self.config.save()

    @property
    def _remote_node(self) -> Optional["PacsNode"]:
        return self.config.remote_nodes.get(self.remote_key)

    @property
    def _restart_pending(self) -> bool:
        """Whether a Restart is waiting for the engine to go idle.

        Reads better than ``self._pending_restart is not None`` at the
        six call sites that only care about the yes/no.
        """
        return self._pending_restart is not None

    @property
    def _language(self) -> str:
        """UI language for ``tr()`` lookups.  Read fresh each time so a
        language change in Settings applies without rebuilding the
        dashboard."""
        return getattr(self.config, "language", "en")

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
        """Service Controls group: the parameter form on the left, the
        button column on the right.  The two halves share nothing but
        this layout, so each is built by its own helper."""
        ctrl_group = QGroupBox("Download Service")
        ctrl_layout = QHBoxLayout()
        ctrl_layout.addLayout(self._build_service_params_form())
        ctrl_layout.addStretch()
        ctrl_layout.addLayout(self._build_service_buttons())
        ctrl_group.setLayout(ctrl_layout)
        layout.addWidget(ctrl_group)

    def _build_service_params_form(self) -> QFormLayout:
        """The query parameters the engine is started with, plus the two
        per-source toggles that are not service parameters but belong in
        the same visual block."""
        node = self._remote_node
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

        return form

    def _build_service_buttons(self) -> QVBoxLayout:
        """Start / Stop / Restart, plus the three manual-selection
        buttons that stay hidden until the engine pauses for a
        selection."""
        btn_layout = QVBoxLayout()
        self.btn_start = QPushButton("  Start Service  ")
        self.btn_start.setFont(QFont("", 11, QFont.Bold))
        self.btn_start.setStyleSheet(BTN_START)
        # NOT ``connect(self._on_start_clicked)``: QPushButton.clicked
        # carries a ``checked`` bool, which would land in *auto_restart*.
        # It is False today only because the button isn't checkable —
        # making it checkable would silently turn every manual click into
        # a watchdog auto-restart.  The flag is keyword-only for the same
        # reason.
        self.btn_start.clicked.connect(
            lambda _checked=False: self._on_start_clicked())

        self.btn_stop = QPushButton("  Stop Service  ")
        self.btn_stop.setFont(QFont("", 11, QFont.Bold))
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(BTN_STOP)
        self.btn_stop.clicked.connect(self._on_stop_clicked)

        self.btn_restart = QPushButton("  Restart Service  ")
        self.btn_restart.setFont(QFont("", 11, QFont.Bold))
        self.btn_restart.setEnabled(False)
        self.btn_restart.setStyleSheet(BTN_AMBER)
        self.btn_restart.clicked.connect(
            lambda _checked=False: self._on_restart_clicked())

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
        return btn_layout

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
        self.filter_btn.setStyleSheet(TOOLBTN_FILTER)

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
        self.restart_banner.setStyleSheet(LBL_RESTART_BANNER)
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

        # All rendering lives in gui.queue_table; ``series_table`` is
        # republished here because call sites (and a large body of
        # tests) address the underlying QTableWidget directly.
        self._queue_view = QueueTableView(self.config)
        self.series_table = self._queue_view.table

        tl.addWidget(self._queue_view)
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
        if hasattr(self, "_queue_view"):
            self._queue_view.set_group_column_visible(enabled)
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

    def _on_start_clicked(self, *, auto_restart: bool = False) -> None:
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

    def _on_restart_clicked(self, *,
                            auto_restart: bool = False) -> None:
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
        self._pending_restart = PendingRestart(
            params=self._current_service_params(), is_auto=auto_restart)
        self.lbl_status.setText("Restarting…")
        # Must exceed the engine's DIMSE timeout: a wedged C-MOVE only
        # unblocks (and lets the loop emit service_stopped) once that
        # elapses.  A shorter window would fire while the engine is still
        # legitimately winding down and abandon the restart.
        self._restart_safety_timer.start(_RESTART_SAFETY_TIMEOUT_MS)
        self.stop_requested.emit(self.remote_key)

    def _on_restart_safety_timeout(self) -> None:
        """Fired if a Restart click never gets its corresponding
        ``set_service_running(False)`` callback within the safety window
        — the engine is genuinely wedged.

        Rather than abandon the restart (which left the service silently
        "Stopped"), force it through: re-issue the start with the
        snapshotted params so the service comes back up on a fresh
        engine.  An auto-restart (watchdog) forces the start with the
        auto flag so MainWindow keeps retrying if the PACS is still
        unreachable."""
        if self._pending_restart is None:
            return
        logger.warning(
            f"[{self.remote_key}] restart safety timeout fired; engine "
            f"did not emit service_stopped — forcing a fresh start")
        self._consume_pending_restart()

    def _consume_pending_restart(self) -> None:
        """Act on a waiting Restart now that the engine is (or is being
        treated as) idle: clear it, then either bring the service back
        up with the click-time snapshot or explain why it cannot be.

        Shared by the two paths that reach this point — the engine
        reporting ``service_stopped`` and the safety timer firing
        because it never did.  They used to carry a copy of this branch
        each, which is how they came to disagree about stopping the
        safety timer.  The decision itself is
        ``service_watchdog.resolve_pending_restart``; what stays here is
        the widget work.
        """
        self._restart_safety_timer.stop()
        pending = self._pending_restart
        self._pending_restart = None
        outcome = resolve_pending_restart(
            pending, self.remote_key in self.config.remote_nodes)
        if outcome == CANCEL_SOURCE_GONE:
            # MainWindow drops a start_requested for a source that no
            # longer exists, so say so instead of leaving the user with
            # a half-finished restart.
            self.lbl_status.setText("Restart cancelled — source removed.")
            return
        if outcome != RESUME:
            return
        params = pending.params if pending else None
        if params is not None:
            # Re-apply the snapshotted spinbox values so the visible
            # state matches the start request.
            self.hours_spin.setValue(params.hours)
            self.max_images_spin.setValue(params.max_images)
            self.interval_spin.setValue(params.sync_interval)
            self.manual_selection_check.setChecked(params.selection_mode)
        self._on_start_clicked(
            auto_restart=pending.is_auto if pending else False)

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
        # *message* arrives pre-rendered and already localized — the
        # caller (MainWindow._show_awaiting_pacs) resolves it through
        # core.i18n.tr(), so nothing here needs translating.
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
        self._queue_view.set_check_column_visible(False)
        # No transfer can be in flight once stopped — halt the live
        # Pending / ETE countdown (and the stall watchdog).
        self._disarm_active_transfer()
        if self._restart_pending:
            # Engine reached the idle state we were waiting for after a
            # Restart click.  The safety timer is cancelled and the
            # snapshot consumed in one place — see
            # ``_consume_pending_restart``.
            self._consume_pending_restart()

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
        transfer has already received this run so the count drops live.

        The rule itself belongs to ``ActiveTransfer`` — it is the object
        that knows which series is in flight and how much of it has
        landed.  This wrapper exists only because the queue view takes a
        one-argument ``pending_for`` callable; same delegation pattern
        as the ``_format_ete`` / ``_status_text`` staticmethods above.
        """
        return self._active.pending_for(job, _TERMINAL_STATUSES)

    # Pure queue-table formatters live in gui.queue_table.  These thin
    # staticmethod delegates preserve the historical
    # ``dashboard._format_ete(...)`` call sites (and the tests that
    # target them directly), the same pattern used for
    # core.queue_planner and gui.study_rate.
    _format_ete = staticmethod(queue_table.format_ete)
    _format_series_created = staticmethod(queue_table.format_series_created)
    _status_text = staticmethod(queue_table.status_text)
    _status_color = staticmethod(queue_table.status_color)

    def _update_series_summary(self, queue: list[dict]) -> None:
        """Refresh the 'Series: done / total' summary label."""
        done_count = sum(1 for job in queue if job["status"] == "done")
        self.lbl_total_series.setText(
            f"Series: {done_count} / {len(queue)}")

    # ── Slots called by engine signals ────────────────────────────────────

    def on_queue_updated(self, queue: list[dict]) -> None:
        """Render the series table from the full queue list."""
        self._last_queue = queue
        # Hide selection UI — engine is now downloading or idle.
        self._apply_selection_ui_visible(False)
        self._queue_view.render(queue, self._get_rate(), self._pending_for)
        self._update_series_summary(queue)

    def on_queue_ready_for_selection(self, queue: list[dict]) -> None:
        """Engine paused after query — show checkboxes for manual selection."""
        self._last_queue = queue
        self._queue_view.render_for_selection(queue)

        total = sum(max(j["remote_count"] - j["local_count"], 0)
                    for j in queue)
        self.lbl_total_series.setText(f"Series: 0 / {len(queue)}")
        self.lbl_total_images.setText(f"Pending: {total} images")
        self.lbl_status.setText(
            f"Awaiting selection — {len(queue)} series found")
        self._apply_selection_ui_visible(True)

    def _set_all_series_checked(self, checked: bool) -> None:
        """Check or uncheck every row of the selection table."""
        self._queue_view.set_all_checked(checked)

    def _on_download_selected_clicked(self) -> None:
        """Collect checked series UIDs and confirm selection to the engine."""
        self.selection_confirmed.emit(
            self.remote_key, self._queue_view.checked_series_uids())

    def on_series_started(self, info: dict) -> None:
        if self._restart_pending:
            return  # keep the "Restarting\u2026" label stable
        self.lbl_status.setText(
            f"Transferring: {info['patient_name']} \u2014 "
            f"[{info.get('modality', '')}] {info['series_description']}")
        self._active.begin(info.get("series_uid"),
                           self._series_deadline_s(info))
        if not self._slow_transfer_timer.isActive():
            self._slow_transfer_timer.start()
        # Drive the per-second Pending / ETE countdown for as long as a
        # transfer is in flight.
        if not self._live_refresh_timer.isActive():
            self._live_refresh_timer.start()

    def _series_deadline_s(self, info: dict) -> float:
        """Plausible maximum wall time (s) for the series described by
        *info* before it counts as wedged.

        Thin wrapper that feeds this dashboard's measured rate into
        the pure ``gui.service_watchdog.series_deadline_s`` — kept as an
        instance method because the rate is dashboard state."""
        return series_deadline_s(pending_images_of(info), self._get_rate())

    def on_series_progress(self, series_uid: str, completed: int,
                           total: int) -> None:
        """Per-image progress tick during a C-MOVE \u2014 records that the
        transfer is alive (resets the no-progress clock) and how many
        images have arrived so the Pending / ETE columns count down
        live.  The series-level watchdog deadline set at series start is
        left intact; genuine progress simply proves the series is not
        wedged.  Only acts while a transfer is actually active."""
        if not self._active.is_active or self._restart_pending:
            return
        self._active.note_progress(completed)

    def _disarm_active_transfer(self) -> None:
        """Tear down all per-transfer activity: stop the stall watchdog,
        forget the active series, and halt the per-second Pending / ETE
        countdown.  Shared by on_series_completed / on_series_error (a
        series finished) and by the stopped path of set_service_running /
        reset (no transfer can be in flight).  Each call is a no-op when
        the timers are already stopped, so it is safe at every site."""
        self._slow_transfer_timer.stop()
        self._active.clear()
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
        """Watchdog poll tick: auto-restart only if the active series is
        GENUINELY wedged.

        The two-condition wedge rule lives in
        ``ActiveTransfer.wedged_for`` — see gui.service_watchdog for why
        a single "no progress for N seconds" test is not enough.  This
        method owns only the parts that need the widget: the outage
        suppression, the sound, the notice and the restart.

        Skipped during a known PACS outage — the engine self-recovers
        there, so a restart would only get in the way."""
        if self._pacs_connection_lost:
            return
        no_progress_s = self._active.wedged_for()
        if no_progress_s is None:
            return
        uid = self._active.series_uid or "?"
        logger.warning(
            f"[{self.remote_key}] Stalled transfer detected \u2014 series "
            f"{uid} overran its expected duration with no progress for "
            f"{int(no_progress_s)}s \u2014 triggering auto-restart")
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
        text = tr("slow_transfer_msg", self._language,
                  name=self.remote_key)
        existing = self._slow_transfer_notice
        if existing is not None:
            # Already on screen \u2014 just refresh its text and re-arm the
            # auto-close timer instead of stacking another dialog.
            existing.setText(text)
            self._slow_transfer_notice_timer.start(
                _SLOW_TRANSFER_NOTICE_MS)
            return
        msg = QMessageBox(self)
        msg.setWindowTitle(tr("slow_transfer_title", self._language))
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
            # No qualifying series yet — show the placeholder rather than
            # leaving whatever the previous session's numbers were.
            self._blank_stats_labels()
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

    def _blank_stats_labels(self) -> None:
        """Reset the four throughput labels to the placeholder."""
        for lbl in (self.stat_last, self.stat_med5,
                    self.stat_med10, self.stat_medall):
            lbl.setText("—")

    def _update_ete_column(self) -> None:
        """Re-derive the ETE column after a stats update changed the rate."""
        self._queue_view.update_ete_column(
            self._last_queue, self._get_rate(), self._pending_for)

    def _refresh_pending_and_ete(self) -> None:
        """Per-second tick driven by ``_live_refresh_timer`` while a
        transfer is in flight, so the Pending / ETE columns count down
        smoothly between queue/stats signals.  When the window is hidden
        there is nothing to repaint, so skip the work entirely."""
        if not self._last_queue or not self.isVisible():
            return
        self._queue_view.refresh_pending_and_ete(
            self._last_queue, self._get_rate(), self._pending_for)

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

    def _compute_study_rates(self, queue: list[dict],
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

    def on_studies_queried(self, studies: list[dict]) -> None:
        """Slot for the studies_queried signal — receives raw query results."""
        self._update_study_rate_display(studies)

    def _update_study_rate_display(self, studies: list[dict]) -> None:
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

        self._maybe_warn_high_load(rates)

    def _maybe_warn_high_load(self, rates: dict[str, int]) -> None:
        """Warn once when a group newly crosses STUDY_RATE_HIGH_LOAD.

        Two guards, because ``studies_queried`` fires every query cycle:

        * the edge check (only groups that were NOT high last time) stops
          a sustained burst from popping a dialog per cycle;
        * ``_HIGH_LOAD_COOLDOWN_S`` additionally stops a rate that
          oscillates around the threshold from re-popping on every
          crossing — a group sitting right at the threshold otherwise
          alternated in and out of ``_last_high_load_groups`` and warned
          indefinitely.

        Non-modal so it cannot block signal processing.
        """
        high_groups = {g for g, c in rates.items()
                       if c >= STUDY_RATE_HIGH_LOAD}
        new_high = high_groups - self._last_high_load_groups
        self._last_high_load_groups = high_groups
        if not new_high or not self.config.high_load_alert_enabled:
            return
        now = time.monotonic()
        if now - self._last_high_load_alert_ts < _HIGH_LOAD_COOLDOWN_S:
            return
        self._last_high_load_alert_ts = now

        QApplication.beep()
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("High Study Load")
        msg.setText(
            # Threshold interpolated from the constant so the message
            # can never drift from the actual trigger.
            f"The incoming study rate has exceeded the threshold "
            f"(≥{STUDY_RATE_HIGH_LOAD} studies/hour). "
            f"Please check system capacity.",
        )
        msg.setModal(False)
        msg.setAttribute(Qt.WA_DeleteOnClose)
        msg.show()

    # ── Helpers ───────────────────────────────────────────────────────────

    def reset(self) -> None:
        # Empties the table and drops the render caches, so the next
        # on_queue_updated rebuilds from scratch.
        self._queue_view.clear()
        self._current_stats = None
        self._last_queue = []
        # Stop the live Pending / ETE countdown (and stall watchdog) —
        # no transfer is active.
        self._disarm_active_transfer()
        self._last_high_load_groups = set()
        self._blank_stats_labels()
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
