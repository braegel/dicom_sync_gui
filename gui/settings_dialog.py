"""
Settings dialog for DICOM Sync GUI.
Manages local PACS, source PACS nodes, storage fallback, and prior study settings.

Source PACS workflow:
 - Editor fields are always enabled so the user can fill them in first.
 - "Add New" takes the current field values and creates a new list entry.
 - Clicking an existing entry loads its values into the editor.
 - "Save Changes" writes the editor values back into the selected entry.
 - "Remove" deletes the selected entry.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QSpinBox, QComboBox, QPushButton, QGroupBox,
    QListWidget, QListWidgetItem, QFileDialog, QMessageBox, QCheckBox,
    QDialogButtonBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from core.config import (
    AppConfig, PacsNode, TRANSFER_SYNTAXES_NAMES, RETRIEVE_METHODS, get_local_ip,
)


class PacsNodeEditor(QWidget):
    """Widget for editing a single PACS node."""

    def __init__(self, is_local: bool = False, parent=None):
        super().__init__(parent)
        self.is_local = is_local
        layout = QFormLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

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
        if not is_local:
            self.retrieve_combo = QComboBox()
            self.retrieve_combo.addItems(RETRIEVE_METHODS)
            layout.addRow("Retrieve Method:", self.retrieve_combo)

    def _auto_detect_ip(self):
        self.ip_edit.setText(get_local_ip())

    def set_node(self, node: PacsNode):
        self.name_edit.setText(node.name)
        self.ae_title_edit.setText(node.ae_title)
        self.ip_edit.setText(node.ip_address)
        self.port_spin.setValue(node.port)
        idx = self.syntax_combo.findText(node.transfer_syntax)
        if idx >= 0:
            self.syntax_combo.setCurrentIndex(idx)
        if self.retrieve_combo:
            idx = self.retrieve_combo.findText(node.retrieve_method)
            if idx >= 0:
                self.retrieve_combo.setCurrentIndex(idx)

    def get_node(self) -> PacsNode:
        return PacsNode(
            name=self.name_edit.text().strip(),
            ae_title=self.ae_title_edit.text().strip(),
            ip_address=self.ip_edit.text().strip(),
            port=self.port_spin.value(),
            transfer_syntax=self.syntax_combo.currentText(),
            retrieve_method=(self.retrieve_combo.currentText()
                             if self.retrieve_combo else "C-MOVE"),
        )

    def clear_fields(self):
        self.name_edit.clear()
        self.ae_title_edit.clear()
        self.ip_edit.clear()
        self.port_spin.setValue(104 if not self.is_local else 11112)
        self.syntax_combo.setCurrentIndex(0)
        if self.retrieve_combo:
            self.retrieve_combo.setCurrentIndex(0)

    def has_minimum_data(self) -> bool:
        """True if at least name and AE title are filled in."""
        return bool(self.name_edit.text().strip() and
                    self.ae_title_edit.text().strip())


class SettingsDialog(QDialog):
    """Main settings dialog with tabs."""

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Settings")
        self.setMinimumSize(720, 580)

        # Internal tracking — must init before _setup_ui
        self._remote_keys: list = []
        self._remote_nodes: dict = {}

        self._setup_ui()
        self._load_config()
        # Start in "new entry" mode
        self._switch_to_new_mode()

    # ── UI setup ──────────────────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        tabs = QTabWidget()

        # ── Tab 1: Local PACS ──
        local_tab = QWidget()
        local_layout = QVBoxLayout(local_tab)
        self.local_editor = PacsNodeEditor(is_local=True)
        local_layout.addWidget(self.local_editor)

        # Fallback storage
        self.fallback_group = QGroupBox(
            "Download to folder if local PACS is not available")
        self.fallback_group.setCheckable(True)
        self.fallback_group.setChecked(False)
        fg = QHBoxLayout()
        self.storage_edit = QLineEdit()
        self.storage_edit.setPlaceholderText("Path to storage folder...")
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_storage)
        fg.addWidget(self.storage_edit)
        fg.addWidget(browse_btn)
        self.fallback_group.setLayout(fg)
        local_layout.addWidget(self.fallback_group)

        local_layout.addStretch()
        tabs.addTab(local_tab, "Local PACS")

        # ── Tab 2: Source PACS ──
        remote_tab = QWidget()
        remote_layout = QHBoxLayout(remote_tab)

        # Left: list of existing sources
        left = QVBoxLayout()
        left.addWidget(QLabel("Configured Source PACS:"))
        self.remote_list = QListWidget()
        self.remote_list.currentRowChanged.connect(self._on_remote_selected)
        left.addWidget(self.remote_list)

        self.remove_btn = QPushButton("Remove Selected")
        self.remove_btn.setEnabled(False)
        self.remove_btn.clicked.connect(self._remove_remote)
        left.addWidget(self.remove_btn)

        self.btn_new_entry = QPushButton("New Entry")
        self.btn_new_entry.setToolTip(
            "Clear the editor so you can fill in a new source PACS.")
        self.btn_new_entry.clicked.connect(self._switch_to_new_mode)
        left.addWidget(self.btn_new_entry)

        # Right: editor (always enabled)
        right = QVBoxLayout()

        # Mode label — tells user what they are doing
        self.mode_label = QLabel("Fill in the fields and click \"Add New\".")
        self.mode_label.setStyleSheet(
            "QLabel { color: #2980b9; font-weight: bold; padding: 4px; }")
        right.addWidget(self.mode_label)

        key_layout = QFormLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("Short name (e.g. 'ct', 'mri')")
        key_layout.addRow("Short Name:", self.key_edit)
        right.addLayout(key_layout)

        self.remote_editor = PacsNodeEditor(is_local=False)
        right.addWidget(self.remote_editor)

        # Action buttons for the editor
        editor_btns = QHBoxLayout()

        self.btn_add_new = QPushButton("Add New")
        self.btn_add_new.setStyleSheet(
            "QPushButton { background: #27ae60; color: white; padding: 6px 16px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background: #2ecc71; }"
            "QPushButton:disabled { background: #7f8c8d; }")
        self.btn_add_new.clicked.connect(self._add_remote)

        self.btn_save_changes = QPushButton("Save Changes")
        self.btn_save_changes.setStyleSheet(
            "QPushButton { background: #2980b9; color: white; padding: 6px 16px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background: #3498db; }"
            "QPushButton:disabled { background: #7f8c8d; }")
        self.btn_save_changes.clicked.connect(self._save_changes_to_selected)
        self.btn_save_changes.setVisible(False)

        editor_btns.addStretch()
        editor_btns.addWidget(self.btn_add_new)
        editor_btns.addWidget(self.btn_save_changes)
        right.addLayout(editor_btns)

        right.addStretch()

        remote_layout.addLayout(left, 1)
        remote_layout.addLayout(right, 2)
        tabs.addTab(remote_tab, "Source PACS")

        # ── Tab 3: General ──
        general_tab = QWidget()
        gl = QFormLayout(general_tab)

        # Prior studies
        group = QGroupBox("Prior Studies")
        pl = QFormLayout()
        self.prior_spin = QSpinBox()
        self.prior_spin.setRange(0, 20)
        self.prior_spin.setSpecialValueText("Disabled")
        pl.addRow("Number of prior studies:", self.prior_spin)

        self.prior_modality_check = QCheckBox("Same modality only")
        self.prior_modality_check.setToolTip(
            "When enabled, only prior studies with matching modality "
            "are downloaded.")
        pl.addRow("", self.prior_modality_check)
        group.setLayout(pl)
        gl.addRow(group)

        tabs.addTab(general_tab, "General")

        layout.addWidget(tabs)

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ── Mode switching ────────────────────────────────────────────────────

    def _switch_to_new_mode(self):
        """Clear editor, deselect list, show 'Add New' button."""
        self.remote_list.blockSignals(True)
        self.remote_list.setCurrentRow(-1)
        self.remote_list.blockSignals(False)

        self.remote_editor.clear_fields()
        self.key_edit.clear()

        self.mode_label.setText(
            "Fill in the fields and click \"Add New\".")
        self.btn_add_new.setVisible(True)
        self.btn_save_changes.setVisible(False)
        self.remove_btn.setEnabled(False)

    def _switch_to_edit_mode(self, key: str):
        """Load entry into editor, show 'Save Changes' button."""
        self.mode_label.setText(
            f"Editing \"{key}\" — modify fields and click \"Save Changes\".")
        self.btn_add_new.setVisible(False)
        self.btn_save_changes.setVisible(True)
        self.remove_btn.setEnabled(True)

    # ── Config loading ────────────────────────────────────────────────────

    def _load_config(self):
        self.local_editor.set_node(self.config.local_node)
        self.fallback_group.setChecked(self.config.fallback_storage_enabled)
        self.storage_edit.setText(self.config.fallback_storage_path)
        self.prior_spin.setValue(self.config.prior_studies_count)
        self.prior_modality_check.setChecked(
            self.config.prior_studies_same_modality)

        # Load remotes
        self._remote_keys = []
        self._remote_nodes = {}
        self.remote_list.clear()
        for key, node in self.config.remote_nodes.items():
            self._remote_keys.append(key)
            self._remote_nodes[key] = node
            self.remote_list.addItem(f"{key} \u2014 {node.name}")

    # ── List selection ────────────────────────────────────────────────────

    def _on_remote_selected(self, row):
        if 0 <= row < len(self._remote_keys):
            key = self._remote_keys[row]
            node = self._remote_nodes[key]
            self.key_edit.setText(key)
            self.remote_editor.set_node(node)
            self._switch_to_edit_mode(key)
        else:
            self._switch_to_new_mode()

    # ── Add new entry from editor fields ─────────────────────────────────

    def _add_remote(self):
        if not self.remote_editor.has_minimum_data():
            QMessageBox.warning(
                self, "Incomplete",
                "Please fill in at least \"Name\" and \"AE Title\".")
            return

        key = self.key_edit.text().strip()
        if not key:
            QMessageBox.warning(
                self, "Missing Short Name",
                "Please enter a short name for this source PACS.")
            return

        if key in self._remote_nodes:
            QMessageBox.warning(
                self, "Duplicate",
                f"A source with the short name \"{key}\" already exists.\n"
                "Please choose a different short name or select the "
                "existing entry to edit it.")
            return

        node = self.remote_editor.get_node()
        self._remote_keys.append(key)
        self._remote_nodes[key] = node
        self.remote_list.addItem(f"{key} \u2014 {node.name}")

        # Select the newly added entry (switches to edit mode)
        self.remote_list.setCurrentRow(len(self._remote_keys) - 1)

    # ── Save changes to existing entry ───────────────────────────────────

    def _save_changes_to_selected(self):
        row = self.remote_list.currentRow()
        if row < 0 or row >= len(self._remote_keys):
            return

        if not self.remote_editor.has_minimum_data():
            QMessageBox.warning(
                self, "Incomplete",
                "Please fill in at least \"Name\" and \"AE Title\".")
            return

        new_key = self.key_edit.text().strip()
        if not new_key:
            QMessageBox.warning(
                self, "Missing Short Name",
                "Please enter a short name.")
            return

        old_key = self._remote_keys[row]
        node = self.remote_editor.get_node()

        # Check for duplicate key (only if key changed)
        if new_key != old_key and new_key in self._remote_nodes:
            QMessageBox.warning(
                self, "Duplicate",
                f"A source with the short name \"{new_key}\" already exists.")
            return

        # Remove old key, insert new
        if old_key in self._remote_nodes:
            del self._remote_nodes[old_key]
        self._remote_keys[row] = new_key
        self._remote_nodes[new_key] = node
        self.remote_list.item(row).setText(f"{new_key} \u2014 {node.name}")

        self.mode_label.setText(
            f"\"{new_key}\" saved. Select another entry or "
            "click \"New Entry\" to add more.")

    # ── Remove entry ─────────────────────────────────────────────────────

    def _remove_remote(self):
        row = self.remote_list.currentRow()
        if row < 0:
            return
        key = self._remote_keys[row]

        reply = QMessageBox.question(
            self, "Remove Source PACS",
            f"Remove \"{key}\"?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        del self._remote_nodes[key]
        self._remote_keys.pop(row)
        self.remote_list.takeItem(row)
        self._switch_to_new_mode()

    # ── Browse storage folder ────────────────────────────────────────────

    def _browse_storage(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Storage Folder", self.storage_edit.text())
        if path:
            self.storage_edit.setText(path)

    # ── Save all settings ────────────────────────────────────────────────

    def _save(self):
        if not self._remote_nodes:
            QMessageBox.warning(
                self, "Warning",
                "At least one source PACS must be configured.")
            return

        # Apply to config
        self.config.local_node = self.local_editor.get_node()
        self.config.remote_nodes = dict(self._remote_nodes)
        self.config.fallback_storage_enabled = self.fallback_group.isChecked()
        self.config.fallback_storage_path = self.storage_edit.text().strip()
        self.config.prior_studies_count = self.prior_spin.value()
        self.config.prior_studies_same_modality = (
            self.prior_modality_check.isChecked())

        self.config.save()
        self.accept()
