"""
Editor widget for a single ``PacsNode``.

Extracted from ``gui.settings_dialog`` so the form-level logic of
editing a source PACS lives in its own module and the dialog file
focuses on list management + dialog flow.  Currently used by
``SettingsDialog``; the API is generic enough to plug into another
dialog if one ever needs it.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QWidget,
)

from core.config import (
    PacsNode, RETRIEVE_METHODS, TRANSFER_SYNTAXES_NAMES, get_local_ip,
)


class PacsNodeEditor(QWidget):
    """Widget for editing a single source PACS node.

    When *is_local=False* (default), includes:
      - Remote PACS connection fields (name, AE, IP, port, syntax, retrieve)
      - Per-source service parameters (hours, max images, interval)
      - Local destination fields (local AE, port, syntax, fallback folder)
    When *is_local=True*, only the basic connection fields are shown.
    """

    def __init__(self, is_local: bool = False, parent=None):
        super().__init__(parent)
        self.is_local = is_local
        layout = QFormLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # ── Remote PACS connection ──
        self.name_edit = QLineEdit()
        self.ae_title_edit = QLineEdit()
        self.ip_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(11112 if is_local else 104)
        self.syntax_combo = QComboBox()
        self.syntax_combo.addItems(TRANSFER_SYNTAXES_NAMES)

        layout.addRow("Name:", self.name_edit)
        layout.addRow("AE Title:", self.ae_title_edit)
        layout.addRow("IP Address:", self.ip_edit)
        layout.addRow("Port:", self.port_spin)
        layout.addRow("Transfer Syntax:", self.syntax_combo)

        if is_local:
            btn = QPushButton("Auto-detect IP")
            btn.clicked.connect(self._auto_detect_ip)
            layout.addRow("", btn)

        # Retrieve method (only for remote nodes)
        self.retrieve_combo = None
        self.hours_spin = None
        self.max_images_spin = None
        self.interval_spin = None
        self.local_ae_edit = None
        self.local_port_spin = None
        self.local_syntax_combo = None
        self.fallback_edit = None
        self.fallback_btn = None
        self.notification_sound_edit = None

        if not is_local:
            self.retrieve_combo = QComboBox()
            self.retrieve_combo.addItems(RETRIEVE_METHODS)
            layout.addRow("Retrieve Method:", self.retrieve_combo)

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
    def _add_separator(layout: QFormLayout, title: str):
        sep = QLabel("─" * 30)
        sep.setStyleSheet("QLabel { color: #666; }")
        layout.addRow("", sep)
        lbl = QLabel(title)
        lbl.setFont(QFont("", -1, QFont.Bold))
        lbl.setStyleSheet("QLabel { color: #2980b9; }")
        layout.addRow("", lbl)

    def _auto_detect_ip(self):
        self.ip_edit.setText(get_local_ip())

    def _browse_fallback(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Fallback Folder",
            self.fallback_edit.text() if self.fallback_edit else "")
        if path:
            self.fallback_edit.setText(path)

    def _browse_notification_sound(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Notification Sound",
            self.notification_sound_edit.text() if self.notification_sound_edit else "",
            "WAV Files (*.wav);;All Files (*)")
        if path:
            self.notification_sound_edit.setText(path)

    @staticmethod
    def _select_combo_text(combo, text: str):
        """Set *combo*'s current item to the one whose label equals
        *text*.  Silently no-ops if *combo* is ``None`` or *text* is
        not in the list — same defensive shape every caller used to
        repeat by hand."""
        if combo is None:
            return
        idx = combo.findText(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def set_node(self, node: PacsNode):
        self.name_edit.setText(node.name)
        self.ae_title_edit.setText(node.ae_title)
        self.ip_edit.setText(node.ip_address)
        self.port_spin.setValue(node.port)
        self._select_combo_text(self.syntax_combo, node.transfer_syntax)
        self._select_combo_text(self.retrieve_combo, node.retrieve_method)
        self._select_combo_text(self.local_syntax_combo, node.local_syntax)
        if self.hours_spin:
            self.hours_spin.setValue(node.hours)
        if self.max_images_spin:
            self.max_images_spin.setValue(node.max_images)
        if self.interval_spin:
            self.interval_spin.setValue(node.sync_interval)
        if self.local_ae_edit:
            self.local_ae_edit.setText(node.local_ae_title)
        if self.local_port_spin:
            self.local_port_spin.setValue(node.local_port)
        if self.fallback_edit:
            self.fallback_edit.setText(node.fallback_folder)
        if self.notification_sound_edit:
            self.notification_sound_edit.setText(node.notification_sound_path)

    def get_node(self) -> PacsNode:
        return PacsNode(
            name=self.name_edit.text().strip(),
            ae_title=self.ae_title_edit.text().strip(),
            ip_address=self.ip_edit.text().strip(),
            port=self.port_spin.value(),
            transfer_syntax=self.syntax_combo.currentText(),
            retrieve_method=(self.retrieve_combo.currentText()
                             if self.retrieve_combo else "C-MOVE"),
            hours=(self.hours_spin.value()
                   if self.hours_spin else 3),
            max_images=(self.max_images_spin.value()
                        if self.max_images_spin else 0),
            sync_interval=(self.interval_spin.value()
                           if self.interval_spin else 60),
            local_ae_title=(self.local_ae_edit.text().strip()
                            if self.local_ae_edit else "LOCAL_AE"),
            local_port=(self.local_port_spin.value()
                        if self.local_port_spin else 11112),
            local_syntax=(self.local_syntax_combo.currentText()
                          if self.local_syntax_combo else "JPEG2000Lossless"),
            fallback_folder=(self.fallback_edit.text().strip()
                             if self.fallback_edit else ""),
            notification_sound_path=(self.notification_sound_edit.text().strip()
                                     if self.notification_sound_edit else ""),
        )

    def clear_fields(self):
        self.name_edit.clear()
        self.ae_title_edit.clear()
        self.ip_edit.clear()
        self.port_spin.setValue(104 if not self.is_local else 11112)
        self.syntax_combo.setCurrentIndex(0)
        if self.retrieve_combo:
            self.retrieve_combo.setCurrentIndex(0)
        if self.hours_spin:
            self.hours_spin.setValue(3)
        if self.max_images_spin:
            self.max_images_spin.setValue(0)
        if self.interval_spin:
            self.interval_spin.setValue(60)
        if self.local_ae_edit:
            self.local_ae_edit.clear()
        if self.local_port_spin:
            self.local_port_spin.setValue(11112)
        if self.local_syntax_combo:
            self.local_syntax_combo.setCurrentIndex(0)
        if self.fallback_edit:
            self.fallback_edit.clear()
        if self.notification_sound_edit:
            self.notification_sound_edit.clear()

    def has_minimum_data(self) -> bool:
        """True if at least name and AE title are filled in."""
        return bool(self.name_edit.text().strip() and
                    self.ae_title_edit.text().strip())
