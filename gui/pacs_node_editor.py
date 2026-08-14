"""
Editor widget for a single ``PacsNode``.

Extracted from ``gui.settings_dialog`` so the form-level logic of
editing a source PACS lives in its own module and the dialog file
focuses on list management + dialog flow.  Currently used by
``SettingsDialog``; the API is generic enough to plug into another
dialog if one ever needs it.

The former ``is_local=True`` mode (basic connection fields only, with
an Auto-detect-IP button) had no caller and was removed; the editor
always shows the full source-PACS form.
"""

from typing import Optional

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QWidget,
)

from core.config import PacsNode, TRANSFER_SYNTAXES_NAMES
from gui.styles import COLOR_LINK


class PacsNodeEditor(QWidget):
    """Widget for editing a single source PACS node.

    Includes:
      - Remote PACS connection fields (name, AE, IP, port, syntax)
      - Per-source service parameters (hours, max images, interval)
      - Local destination fields (local AE, port, syntax, fallback folder)
      - Notification sound file
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        self._build_remote_section(layout)
        self._build_service_section(layout)
        self._build_local_section(layout)
        self._build_sound_section(layout)

    def _build_remote_section(self, layout: QFormLayout) -> None:
        # ── Remote PACS connection ──
        self.name_edit = QLineEdit()
        self.ae_title_edit = QLineEdit()
        self.ip_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(104)
        self.syntax_combo = QComboBox()
        self.syntax_combo.addItems(TRANSFER_SYNTAXES_NAMES)

        layout.addRow("Name:", self.name_edit)
        layout.addRow("AE Title:", self.ae_title_edit)
        layout.addRow("IP Address:", self.ip_edit)
        layout.addRow("Port:", self.port_spin)
        layout.addRow("Transfer Syntax:", self.syntax_combo)


    def _build_service_section(self, layout: QFormLayout) -> None:
        # ── Per-source service parameters ──
        self._add_separator(layout, "Service Parameters")

        self.hours_spin = QSpinBox()
        self.hours_spin.setRange(1, 168)
        self.hours_spin.setValue(3)
        self.hours_spin.setSuffix(" hours")
        layout.addRow("Download last:", self.hours_spin)

        self.max_images_spin = QSpinBox()
        self.max_images_spin.setRange(0, 99999)
        self.max_images_spin.setSpecialValueText("No limit")
        self.max_images_spin.setSuffix(" images")
        self.max_images_spin.setValue(0)
        layout.addRow("Max images / series:", self.max_images_spin)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(10, 600)
        self.interval_spin.setValue(60)
        self.interval_spin.setSuffix(" sec")
        layout.addRow("Query interval:", self.interval_spin)

    def _build_local_section(self, layout: QFormLayout) -> None:
        # ── Local destination (C-MOVE target) ──
        self._add_separator(layout, "Local Destination (C-MOVE Target)")

        self.local_ae_edit = QLineEdit()
        self.local_ae_edit.setPlaceholderText("AE title the source sends to")
        layout.addRow("Local AE Title:", self.local_ae_edit)

        self.local_port_spin = QSpinBox()
        self.local_port_spin.setRange(1, 65535)
        self.local_port_spin.setValue(11112)
        layout.addRow("Local Port:", self.local_port_spin)

        self.local_syntax_combo = QComboBox()
        self.local_syntax_combo.addItems(TRANSFER_SYNTAXES_NAMES)
        layout.addRow("Preferred Syntax:", self.local_syntax_combo)

        self.fallback_edit = QLineEdit()
        self.fallback_edit.setPlaceholderText(
            "Folder to store images if no local PACS is reachable")
        self.fallback_btn = QPushButton("Browse...")
        self.fallback_btn.clicked.connect(self._browse_fallback)

        fb_layout = QHBoxLayout()
        fb_layout.setContentsMargins(0, 0, 0, 0)
        fb_layout.addWidget(self.fallback_edit)
        fb_layout.addWidget(self.fallback_btn)
        fb_widget = QWidget()
        fb_widget.setLayout(fb_layout)
        layout.addRow("Fallback Folder:", fb_widget)

    def _build_sound_section(self, layout: QFormLayout) -> None:
        # ── Notification sound ──
        self._add_separator(layout, "Notification Sound")

        self.notification_sound_edit = QLineEdit()
        self.notification_sound_edit.setPlaceholderText(
            "Custom WAV file (leave empty for default tone)")
        ns_btn = QPushButton("Browse...")
        ns_btn.clicked.connect(self._browse_notification_sound)

        ns_layout = QHBoxLayout()
        ns_layout.setContentsMargins(0, 0, 0, 0)
        ns_layout.addWidget(self.notification_sound_edit)
        ns_layout.addWidget(ns_btn)
        ns_widget = QWidget()
        ns_widget.setLayout(ns_layout)
        layout.addRow("Sound File:", ns_widget)

    @staticmethod
    def _add_separator(layout: QFormLayout, title: str) -> None:
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        sep.setStyleSheet("QFrame { color: #666; }")
        layout.addRow("", sep)
        lbl = QLabel(title)
        lbl.setFont(QFont("", -1, QFont.Bold))
        lbl.setStyleSheet(f"QLabel {{ color: {COLOR_LINK}; }}")
        layout.addRow("", lbl)

    def _browse_fallback(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Fallback Folder", self.fallback_edit.text())
        if path:
            self.fallback_edit.setText(path)

    def _browse_notification_sound(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Notification Sound",
            self.notification_sound_edit.text(),
            "WAV Files (*.wav);;All Files (*)")
        if path:
            self.notification_sound_edit.setText(path)

    @staticmethod
    def _select_combo_text(combo: Optional[QComboBox], text: str) -> None:
        """Set *combo*'s current item to the one whose label equals
        *text*.  Silently no-ops if *combo* is ``None`` or *text* is
        not in the list — same defensive shape every caller used to
        repeat by hand."""
        if combo is None:
            return
        idx = combo.findText(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def set_node(self, node: PacsNode) -> None:
        self.name_edit.setText(node.name)
        self.ae_title_edit.setText(node.ae_title)
        self.ip_edit.setText(node.ip_address)
        self.port_spin.setValue(node.port)
        self._select_combo_text(self.syntax_combo, node.transfer_syntax)
        self._select_combo_text(self.local_syntax_combo, node.local_syntax)
        self.hours_spin.setValue(node.hours)
        self.max_images_spin.setValue(node.max_images)
        self.interval_spin.setValue(node.sync_interval)
        self.local_ae_edit.setText(node.local_ae_title)
        self.local_port_spin.setValue(node.local_port)
        self.fallback_edit.setText(node.fallback_folder)
        self.notification_sound_edit.setText(node.notification_sound_path)

    def get_node(self, base: Optional[PacsNode] = None) -> PacsNode:
        """Build a ``PacsNode`` from the editor fields.

        *base* is the node being edited, if any.  The editor has no
        widgets for ``notification_sound_enabled`` (dashboard checkbox)
        or ``priority_series_terms`` (Priority Series dialog), so those
        are carried over from *base* — otherwise saving an edit would
        silently reset them to the defaults.
        """
        return PacsNode(
            name=self.name_edit.text().strip(),
            ae_title=self.ae_title_edit.text().strip(),
            ip_address=self.ip_edit.text().strip(),
            port=self.port_spin.value(),
            transfer_syntax=self.syntax_combo.currentText(),
            # Always C-MOVE — the only implemented retrieve path.  A
            # stale "C-GET" from an old config is normalized here so
            # the saved value matches what the engine actually does.
            retrieve_method="C-MOVE",
            hours=self.hours_spin.value(),
            max_images=self.max_images_spin.value(),
            sync_interval=self.interval_spin.value(),
            local_ae_title=self.local_ae_edit.text().strip(),
            local_port=self.local_port_spin.value(),
            local_syntax=self.local_syntax_combo.currentText(),
            fallback_folder=self.fallback_edit.text().strip(),
            notification_sound_path=self.notification_sound_edit.text().strip(),
            notification_sound_enabled=(base.notification_sound_enabled
                                        if base is not None else True),
            # ``None`` lets PacsNode seed the bundled defaults (new
            # node); an existing list is deep-copied by PacsNode.
            priority_series_terms=(base.priority_series_terms
                                   if base is not None else None),
        )

    def clear_fields(self) -> None:
        self.name_edit.clear()
        self.ae_title_edit.clear()
        self.ip_edit.clear()
        self.port_spin.setValue(104)
        self.syntax_combo.setCurrentIndex(0)
        self.hours_spin.setValue(3)
        self.max_images_spin.setValue(0)
        self.interval_spin.setValue(60)
        self.local_ae_edit.clear()
        self.local_port_spin.setValue(11112)
        self.local_syntax_combo.setCurrentIndex(0)
        self.fallback_edit.clear()
        self.notification_sound_edit.clear()

    def has_minimum_data(self) -> bool:
        """True if at least name and AE title are filled in."""
        return bool(self.name_edit.text().strip() and
                    self.ae_title_edit.text().strip())
