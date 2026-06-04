"""
Source Dashboard — one widget per configured source PACS.
Shows per-source service controls (Start/Stop, hours, max images, interval),
the series queue with ETE (estimated time to completion),
and real-time throughput statistics with color-coded indicators.
"""

import io
import itertools
import logging
import math
import os
import struct
import tempfile
import wave
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Optional

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

    def __post_init__(self):
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

from core.transfer_engine import TransferStats
from gui.styles import (
    BTN_AMBER, BTN_BLUE, BTN_DOWNLOAD_SELECTED, BTN_START, BTN_STOP,
)

# ── UI thresholds and colors ─────────────────────────────────────────────
# A series rate is shaded green/red when it deviates by more than this
# ratio from the median-of-all baseline.
SPEED_BAND_RATIO = 0.2

# Studies-per-hour thresholds that switch the study rate label colour
# and trigger the high-load popup.
#
# Empirically tuned for mid-sized radiology workflows with 1–2 source
# PACS feeds:
#   ≤ 5  studies/h — calm, green
#   ≤ 11 studies/h — busy but tractable, yellow
#   ≥ 12 studies/h — sustained burst → red + popup to warn the user
#                   that the system may not keep up with reading flow
# Adjust here if a different deployment's workload doesn't match.
STUDY_RATE_GOOD_MAX = 5      # green at ≤
STUDY_RATE_WARN_MAX = 11     # yellow at ≤
STUDY_RATE_HIGH_LOAD = 12    # red + popup at ≥

# Stats refresh tick (ms)
STATS_REFRESH_MS = 2000
# Debounce window for coalescing config writes from UI events (ms)
CONFIG_SAVE_DEBOUNCE_MS = 500

# Palette
COLOR_GREEN = "#2ecc71"
COLOR_YELLOW = "#f1c40f"
COLOR_RED = "#e74c3c"
COLOR_ORANGE = "#f39c12"
COLOR_BLUE_ACCENT = "#3498db"
COLOR_MUTED = "#969696"

# Notification sound suppression threshold: studies whose total
# downloaded image count is below this are treated as "too small to
# be a real exam" and play no completion sound — typically isolated
# small series, stray priors, or partial fetches that would otherwise
# spam the user.
MIN_IMAGES_FOR_NOTIFICATION_SOUND = 21

_default_sound_path: str | None = None
_sad_sound_path: str | None = None

_NORMAL_FREQ_2 = 1174  # D6 — ascending interval from A5
_SAD_FREQ_2 = 660      # E5 — descending interval from A5


def _generate_default_sound(sad: bool = False) -> str:
    """Generate a two-tone notification WAV and return its path.

    Normal mode: A5 (880 Hz) → D6 (1174 Hz) — ascending, cheerful.
    Sad mode:    A5 (880 Hz) → E5 (660 Hz)  — descending, somber.
    """
    global _default_sound_path, _sad_sound_path
    cached = _sad_sound_path if sad else _default_sound_path
    if cached and os.path.exists(cached):
        return cached

    freq2 = _SAD_FREQ_2 if sad else _NORMAL_FREQ_2
    sample_rate = 44100
    duration = 0.68
    n_samples = int(sample_rate * duration)

    def _envelope(t: float, start: float, dur: float) -> float:
        rel = t - start
        if rel < 0.02:
            return 0.001 * (300.0) ** (rel / 0.02)
        remaining = dur - 0.02
        return 0.3 * (0.001 / 0.3) ** ((rel - 0.02) / remaining)

    raw = []
    for i in range(n_samples):
        t = i / sample_rate
        val = 0.0
        if t < 0.4:
            val += _envelope(t, 0, 0.4) * math.sin(2 * math.pi * 880 * t)
        if 0.18 <= t < 0.68:
            val += _envelope(t, 0.18, 0.5) * math.sin(
                2 * math.pi * freq2 * t)
        raw.append(int(max(-1.0, min(1.0, val)) * 32767))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(raw)}h", *raw))

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="dicom_notify_")
    os.write(fd, buf.getvalue())
    os.close(fd)
    if sad:
        _sad_sound_path = path
    else:
        _default_sound_path = path
    return path


class StatsLabel(QLabel):
    """A label that is color-coded relative to the median-all baseline."""

    def __init__(self, text="\u2014", parent=None):
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

    def set_value(self, value: float, median_all: float):
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
        bg = "#2c2c2c" if color == "white" else (
            "#1a3a1a" if color == COLOR_GREEN else
            "#3a1a1a" if color == COLOR_RED else "#2c2c2c"
        )
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

    def __init__(self, config, remote_key: str, parent=None):
        super().__init__(parent)
        self.config = config
        self.remote_key = remote_key
        self._current_stats: Optional[TransferStats] = None
        self._last_queue: list = []
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

    def _save_config_debounced(self):
        """Schedule a config save 500ms in the future; coalesces bursts."""
        self._save_timer.start()

    def flush_pending_save(self):
        """Synchronously persist any pending debounced save."""
        if self._save_timer.isActive():
            self._save_timer.stop()
        self.config.save()

    @property
    def _remote_node(self):
        return self.config.remote_nodes.get(self.remote_key)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        node = self._remote_node

        # ── Service Controls ──
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

        # ── Filter Groups ──
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
            "QLabel { color: #f39c12; font-style: italic; }")
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

        self._populate_filter_menu()
        self._update_filter_button_text()
        self._update_filter_enabled_state()

        # ── Restart Required Banner ──
        self.restart_banner = QLabel(
            "\u26a0  Settings changed. Restart the service for "
            "changes to take effect.")
        self.restart_banner.setStyleSheet(
            "QLabel { background: #7f6000; color: #fff; padding: 8px; "
            "border-radius: 4px; font-weight: bold; }")
        self.restart_banner.setAlignment(Qt.AlignCenter)
        self.restart_banner.setVisible(False)
        layout.addWidget(self.restart_banner)

        # ── Throughput Statistics ──
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

        # ── Study Rate Display ──
        self.study_rate_group = QGroupBox("Studies / Hour")
        self.study_rate_layout = QHBoxLayout()
        self.study_rate_labels: dict[str, QLabel] = {}
        self._rebuild_study_rate_labels()
        self.study_rate_group.setLayout(self.study_rate_layout)
        layout.addWidget(self.study_rate_group)

        # ── Series Queue Table ──
        table_group = QGroupBox("Series Queue")
        tl = QVBoxLayout()

        self.series_table = QTableWidget()
        self.series_table.setColumnCount(11)
        self.series_table.setHorizontalHeaderLabels([
            "☑", "Patient", "Study", "Series", "Modality",
            "Images", "Pending", "img/min", "Status", "ETE", "Group"
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
        self.series_table.setAlternatingRowColors(True)
        self.series_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.series_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.series_table.setColumnHidden(0, True)
        self.series_table.setColumnHidden(10,
                                          not self.config.filter_groups_enabled)

        tl.addWidget(self.series_table)
        table_group.setLayout(tl)
        layout.addWidget(table_group, 1)  # stretch factor 1 = takes all space

        # ── Summary bar ──
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

        # Construction complete — accept user-driven toggle events now.
        self._initializing = False

    # ── Filter group handling ─────────────────────────────────────────

    def _populate_filter_menu(self):
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

    def _on_filter_group_toggled(self, checked: bool):
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

    def _on_filter_toggled(self, enabled: bool):
        """Master switch for filtering."""
        if self._initializing:
            return
        self.config.filter_groups_enabled = enabled
        self._save_config_debounced()
        self._update_filter_enabled_state()
        self._update_filter_button_text()
        self._rebuild_study_rate_labels()
        self._on_settings_changed()

    def _on_small_series_toggled(self, checked: bool):
        if self._initializing:
            return
        self.config.filter_allow_small_series = checked
        self._save_config_debounced()
        self._update_filter_enabled_state()
        self._on_settings_changed()

    def _on_small_series_max_changed(self, value: int):
        if self._initializing:
            return
        self.config.filter_small_series_max = value
        self._save_config_debounced()
        self._on_settings_changed()

    def _update_filter_enabled_state(self):
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

    def _update_filter_button_text(self):
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

    def refresh_filter_groups(self):
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

    def _on_sound_toggled(self, checked: bool):
        node = self._remote_node
        if node:
            node.notification_sound_enabled = checked
            self._save_config_debounced()

    # ── Service control handlers ──────────────────────────────────────────

    def _on_settings_changed(self):
        if self._service_running:
            self._settings_dirty = True
            self.restart_banner.setVisible(True)

    def _on_start_clicked(self):
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
        # Give the user immediate feedback during the startup window;
        # MainWindow may take a moment to bring the engine up.
        self.lbl_status.setText("Starting service…")
        self.start_requested.emit(self.remote_key, params)

    def _on_stop_clicked(self):
        self._settings_dirty = False
        self.restart_banner.setVisible(False)
        self.stop_requested.emit(self.remote_key)

    def _on_restart_clicked(self):
        """Stop the service and start it again as soon as it has
        actually stopped.  The two-phase flow piggybacks on the
        existing stop/start signals — no new wiring needed in
        MainWindow.

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
        self._restart_params = self._current_service_params()
        self.lbl_status.setText("Restarting…")
        # 60 s = 2 × the closeEvent join timeout in MainWindow; if
        # the engine hasn't stopped by then something is wedged.
        self._restart_safety_timer.start(60_000)
        self.stop_requested.emit(self.remote_key)

    def _on_restart_safety_timeout(self):
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

    def set_service_running(self, running: bool):
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
        if self._restart_pending:
            # Engine reached the idle state we were waiting for after
            # a Restart click; bring it back up with the params
            # snapshotted at click time.  Clear the flags BEFORE
            # ``_on_start_clicked`` so any synchronous follow-up
            # callbacks see the clean state.  Cancel the safety timer
            # since the expected callback arrived in time.
            self._restart_safety_timer.stop()
            params = self._restart_params
            self._restart_pending = False
            self._restart_params = None
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
            self._on_start_clicked()

    def _apply_control_enabled(self, running: bool):
        """Enable / disable the service-control buttons and spinboxes
        for the running / idle state."""
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_restart.setEnabled(running)
        self.hours_spin.setEnabled(not running)
        self.max_images_spin.setEnabled(not running)
        self.interval_spin.setEnabled(not running)

    def _apply_selection_ui_visible(self, visible: bool):
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

    @staticmethod
    def _compute_cumulative_pending(queue: list) -> list:
        """Return a list of cumulative pending-image counts per queue row."""
        def pending_for(job: dict) -> int:
            if job["status"] in ("done", "error", "skipped"):
                return 0
            return max(job["remote_count"] - job["local_count"], 0)
        return list(itertools.accumulate(pending_for(j) for j in queue))

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

    # ── Slots called by engine signals ────────────────────────────────────

    def on_queue_updated(self, queue: list):
        """Rebuild the series table from the full queue list."""
        self._last_queue = queue
        # Hide selection UI — engine is now downloading or idle
        self.btn_download_selected.setVisible(False)
        self.btn_select_all.setVisible(False)
        self.btn_deselect_all.setVisible(False)
        self.series_table.setColumnHidden(0, True)

        rate = self._get_rate()

        self.series_table.setRowCount(0)
        done_count = 0
        cumulative = self._compute_cumulative_pending(queue)

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

            pending = job["remote_count"] - job["local_count"]
            pending_item = QTableWidgetItem(str(max(pending, 0)))
            self.series_table.setItem(row, 6, pending_item)

            # img/min column
            ipm = job.get("images_per_minute", 0.0)
            status = job["status"]
            if status == "done" and ipm > 0:
                ipm_item = QTableWidgetItem(f"{ipm:.0f}")
                ipm_item.setForeground(QColor("#3498db"))
            else:
                ipm_item = QTableWidgetItem("\u2014")
                ipm_item.setForeground(QColor("#969696"))
            ipm_item.setTextAlignment(Qt.AlignCenter)
            self.series_table.setItem(row, 7, ipm_item)

            status_item = QTableWidgetItem(self._status_text(status))
            status_item.setForeground(self._status_color(status))
            self.series_table.setItem(row, 8, status_item)

            # ETE column
            if status in ("done", "error", "skipped"):
                ete_text = "\u2014" if status != "done" else "\u2713"
                ete_item = QTableWidgetItem(ete_text)
                if status == "done":
                    ete_item.setForeground(QColor("#2ecc71"))
                else:
                    ete_item.setForeground(QColor("#969696"))
            elif rate > 0:
                ete_seconds = cumulative[i] / rate
                ete_item = QTableWidgetItem(self._format_ete(ete_seconds))
                ete_item.setForeground(QColor("#f39c12"))
            else:
                ete_item = QTableWidgetItem("\u2014")
                ete_item.setForeground(QColor("#969696"))
            ete_item.setTextAlignment(Qt.AlignCenter)
            self.series_table.setItem(row, 9, ete_item)

            # Group column
            group = self.config.institution_assignments.get(
                job.get("institution_name", ""), "")
            self.series_table.setItem(
                row, 10, QTableWidgetItem(group))

            if status == "done":
                done_count += 1

        self.lbl_total_series.setText(
            f"Series: {done_count} / {len(queue)}")

    def on_queue_ready_for_selection(self, queue: list):
        """Engine paused after query — show checkboxes for manual selection."""
        self._last_queue = queue
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
            status_item.setForeground(QColor("#f39c12"))
            self.series_table.setItem(row, 8, status_item)
            self.series_table.setItem(row, 9, QTableWidgetItem("\u2014"))

            # Group column
            group = self.config.institution_assignments.get(
                job.get("institution_name", ""), "")
            self.series_table.setItem(
                row, 10, QTableWidgetItem(group))

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

    def _set_all_series_checked(self, checked: bool):
        """Check or uncheck every row of the selection table."""
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.series_table.rowCount()):
            cb_item = self.series_table.item(row, 0)
            if cb_item is not None:
                cb_item.setCheckState(state)

    def _on_download_selected_clicked(self):
        """Collect checked series UIDs and confirm selection to the engine."""
        selected_uids = []
        for row in range(self.series_table.rowCount()):
            cb_item = self.series_table.item(row, 0)
            if cb_item and cb_item.checkState() == Qt.Checked:
                uid = cb_item.data(Qt.UserRole)
                if uid:
                    selected_uids.append(uid)
        self.selection_confirmed.emit(self.remote_key, selected_uids)

    def on_series_started(self, info: dict):
        if self._restart_pending:
            return  # keep the "Restarting\u2026" label stable
        self.lbl_status.setText(
            f"Transferring: {info['patient_name']} \u2014 "
            f"[{info.get('modality', '')}] {info['series_description']}")

    def on_stats_updated(self, stats: TransferStats):
        self._current_stats = stats
        self.lbl_total_images.setText(f"Total: {stats.total_images} images")
        self._refresh_stats_display()
        # Re-render ETE values with updated rate
        if self._last_queue:
            self._update_ete_column()

    def on_cycle_started(self, cycle: int):
        self.lbl_cycle.setText(f"Cycle: {cycle}")
        if self._restart_pending:
            return
        self.lbl_status.setText(f"Cycle {cycle} \u2014 querying...")

    def on_cycle_finished(self, cycle: int, images: int):
        if self._restart_pending:
            return
        if images > 0:
            self.lbl_status.setText(
                f"Cycle {cycle} done \u2014 {images} images")
        else:
            self.lbl_status.setText(
                f"Cycle {cycle} \u2014 waiting...")

    # ── Stats display ─────────────────────────────────────────────────────

    def _refresh_stats_display(self):
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

    def _update_ete_column(self):
        """Update only the ETE column without rebuilding the whole table."""
        rate = self._get_rate()
        queue = self._last_queue
        cumulative = self._compute_cumulative_pending(queue)

        for i, job in enumerate(queue):
            if i >= self.series_table.rowCount():
                break
            status = job["status"]
            if status in ("done", "error", "skipped"):
                ete_text = "\u2713" if status == "done" else "\u2014"
                ete_item = QTableWidgetItem(ete_text)
                if status == "done":
                    ete_item.setForeground(QColor("#2ecc71"))
                else:
                    ete_item.setForeground(QColor("#969696"))
            elif rate > 0:
                ete_seconds = cumulative[i] / rate
                ete_item = QTableWidgetItem(self._format_ete(ete_seconds))
                ete_item.setForeground(QColor("#f39c12"))
            else:
                ete_item = QTableWidgetItem("\u2014")
                ete_item.setForeground(QColor("#969696"))
            ete_item.setTextAlignment(Qt.AlignCenter)
            self.series_table.setItem(i, 9, ete_item)

    # ── Study rate ────────────────────────────────────────────────────────

    def _rebuild_study_rate_labels(self):
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
        """Count unique studies within the last 60 minutes, grouped."""
        now = now or datetime.now()
        cutoff = now - timedelta(minutes=60)
        seen: dict[str, set] = {}  # group -> set of study_uids

        for job in queue:
            sd = job.get("study_date") or ""
            # ``or ""`` so a job dict that ever carried an explicit
            # ``study_time=None`` (engine currently never does, but
            # ``.get("study_time", "")`` would still return None then)
            # doesn't crash ``.ljust`` with AttributeError.
            st = (job.get("study_time") or "").ljust(6, "0")[:6]
            if not sd:
                continue
            try:
                dt = datetime.strptime(f"{sd}{st}", "%Y%m%d%H%M%S")
            except ValueError as e:
                # Surface in the log so an unexpected DICOM time
                # format doesn't silently zero the studies/hour
                # display — a user with 10 studies in the queue but
                # "0 studies/h" otherwise has no way to debug this.
                logger.debug(
                    f"_compute_study_rates: could not parse "
                    f"study_date+study_time {sd!r}+{st!r}: {e}")
                continue
            if dt <= cutoff:
                continue

            if self.config.filter_groups_enabled:
                group = self.config.institution_assignments.get(
                    job.get("institution_name", ""), "")
            else:
                group = "_total"

            seen.setdefault(group, set()).add(job.get("study_uid", ""))

        return {g: len(uids) for g, uids in seen.items()}

    @staticmethod
    def _study_rate_color(n: int) -> str | None:
        """Return CSS color string for a study rate value."""
        if n <= 0:
            return None
        if n <= STUDY_RATE_GOOD_MAX:
            return COLOR_GREEN
        if n <= STUDY_RATE_WARN_MAX:
            return COLOR_YELLOW
        return COLOR_RED

    def on_study_completed(self, study_uid: str,
                           institution_name: str,
                           fully_complete: bool = True,
                           image_count: Optional[int] = None):
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

    def _play_notification_if_allowed(self, institution_name: str):
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

    def _play_sound(self, path: str):
        """Play a WAV file via the long-lived QSoundEffect (lazy)."""
        if self._sound_effect is None:
            self._sound_effect = QSoundEffect(self)
        self._sound_effect.stop()
        self._sound_effect.setSource(QUrl.fromLocalFile(path))
        self._sound_effect.play()

    def on_studies_queried(self, studies: list):
        """Slot for the studies_queried signal — receives raw query results."""
        self._update_study_rate_display(studies)

    def _update_study_rate_display(self, studies: list):
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
                "The incoming study rate has exceeded the threshold "
                "(≥12 studies/hour). Please check system capacity.",
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
        }.get(status, status)

    @staticmethod
    def _status_color(status: str) -> QColor:
        return {
            "queued": QColor("#969696"),
            "transferring": QColor("#f39c12"),
            "done": QColor("#2ecc71"),
            "error": QColor("#e74c3c"),
            "skipped": QColor("#969696"),
        }.get(status, QColor("#d4d4d4"))

    def reset(self):
        self.series_table.setRowCount(0)
        self._current_stats = None
        self._last_queue = []
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

    def sync_from_config(self):
        """Update spinboxes from the current config node values."""
        node = self._remote_node
        if node:
            self.hours_spin.setValue(node.hours)
            self.max_images_spin.setValue(node.max_images)
            self.interval_spin.setValue(node.sync_interval)
