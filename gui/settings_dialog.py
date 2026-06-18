"""
Settings dialog for DICOM Sync GUI.

Manages source PACS nodes (each with its own local destination) and
general settings.

The per-node editor widget itself lives in ``gui.pacs_node_editor``;
it is re-exported here so existing import paths
``from gui.settings_dialog import PacsNodeEditor`` keep working.

Source PACS workflow
--------------------
- Editor fields are always enabled so the user can fill them in first.
- "Add New" takes the current field values and creates a new list entry.
- Clicking an existing entry loads its values into the editor.
- "Save Changes" writes the editor values back into the selected entry.
- "Remove" deletes the selected entry.
"""

from typing import Optional, Tuple

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QSpinBox, QComboBox, QPushButton, QGroupBox,
    QListWidget, QFileDialog, QMessageBox, QCheckBox,
    QDialogButtonBox,
)

from core.config import AppConfig, PacsNode
from core.i18n import SUPPORTED_LANGUAGES
from gui.pacs_node_editor import PacsNodeEditor  # noqa: F401 — re-export
from gui.styles import BTN_GREEN, BTN_BLUE

# Localised display names for the language combo.  Module-level so
# the dict is built once at import time, not on every dialog open.
_LANG_LABELS = {"en": "English", "de": "Deutsch",
                "fr": "Français", "es": "Español"}


class SettingsDialog(QDialog):
    """Main settings dialog with tabs."""

    def __init__(self, config: AppConfig,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Settings")
        self.setMinimumSize(780, 640)

        # Internal tracking — must init before _setup_ui
        self._remote_keys: list[str] = []
        self._remote_nodes: dict[str, PacsNode] = {}

        self._setup_ui()
        self._load_config()
        # Start in "new entry" mode
        self._switch_to_new_mode()

    # ── UI setup ──────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        tabs = QTabWidget()

        tabs.addTab(self._build_source_tab(), "Source PACS")
        tabs.addTab(self._build_general_tab(), "General")

        layout.addWidget(tabs)

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_source_tab(self) -> QWidget:
        # ── Tab 1: Source PACS ──
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

        self.remote_editor = PacsNodeEditor()
        right.addWidget(self.remote_editor)

        # Action buttons for the editor
        editor_btns = QHBoxLayout()

        self.btn_add_new = QPushButton("Add New")
        self.btn_add_new.setStyleSheet(BTN_GREEN)
        self.btn_add_new.clicked.connect(self._add_remote)

        self.btn_save_changes = QPushButton("Save Changes")
        self.btn_save_changes.setStyleSheet(BTN_BLUE)
        self.btn_save_changes.clicked.connect(self._save_changes_to_selected)
        self.btn_save_changes.setVisible(False)

        editor_btns.addStretch()
        editor_btns.addWidget(self.btn_add_new)
        editor_btns.addWidget(self.btn_save_changes)
        right.addLayout(editor_btns)

        right.addStretch()

        remote_layout.addLayout(left, 1)
        remote_layout.addLayout(right, 2)
        return remote_tab

    def _build_general_tab(self) -> QWidget:
        # ── Tab 2: General ──
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

        # Language.  The tooltip is honest about scope: the rest of
        # the UI is English-only today; widening the translation table
        # in core/i18n.py is where a fuller i18n pass would start.
        self.language_combo = QComboBox()
        for code in SUPPORTED_LANGUAGES:
            self.language_combo.addItem(_LANG_LABELS.get(code, code), code)
        self.language_combo.setToolTip(
            "Currently only affects the \"Image transfer completed\" "
            "text copied from the Download Completions window.")
        gl.addRow("Language:", self.language_combo)

        return general_tab

    # ── Mode switching ────────────────────────────────────────────────────

    def _switch_to_new_mode(self) -> None:
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

    def _switch_to_edit_mode(self, key: str) -> None:
        """Load entry into editor, show 'Save Changes' button."""
        self.mode_label.setText(
            f"Editing \"{key}\" — modify fields and click \"Save Changes\".")
        self.btn_add_new.setVisible(False)
        self.btn_save_changes.setVisible(True)
        self.remove_btn.setEnabled(True)

    # ── Config loading ────────────────────────────────────────────────────

    def _load_config(self) -> None:
        self.prior_spin.setValue(self.config.prior_studies_count)
        self.prior_modality_check.setChecked(
            self.config.prior_studies_same_modality)

        idx = self.language_combo.findData(self.config.language)
        if idx < 0:
            idx = self.language_combo.findData("en")
        self.language_combo.setCurrentIndex(max(idx, 0))

        # Load remotes
        self._remote_keys = []
        self._remote_nodes = {}
        self.remote_list.clear()
        for key, node in self.config.remote_nodes.items():
            self._remote_keys.append(key)
            self._remote_nodes[key] = node
            self.remote_list.addItem(f"{key} — {node.name}")

    # ── List selection ────────────────────────────────────────────────────

    def _on_remote_selected(self, row: int) -> None:
        if 0 <= row < len(self._remote_keys):
            key = self._remote_keys[row]
            node = self._remote_nodes[key]
            self.key_edit.setText(key)
            self.remote_editor.set_node(node)
            self._switch_to_edit_mode(key)
        else:
            self._switch_to_new_mode()

    # ── Add new entry from editor fields ─────────────────────────────────

    def _add_remote(self) -> None:
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
        self.remote_list.addItem(f"{key} — {node.name}")

        # Select the newly added entry (switches to edit mode)
        self.remote_list.setCurrentRow(len(self._remote_keys) - 1)

    # ── Save changes to existing entry ───────────────────────────────────

    def _save_changes_to_selected(self) -> None:
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
        # Pass the node being edited so fields without editor widgets
        # (sound on/off, priority series terms) survive the save.
        node = self.remote_editor.get_node(
            base=self._remote_nodes.get(old_key))

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
        self.remote_list.item(row).setText(f"{new_key} — {node.name}")

        self.mode_label.setText(
            f"\"{new_key}\" saved. Select another entry or "
            "click \"New Entry\" to add more.")

    # ── Remove entry ─────────────────────────────────────────────────────

    def _remove_remote(self) -> None:
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

    # ── Save all settings ────────────────────────────────────────────────

    def _save(self) -> None:
        if not self._remote_nodes:
            QMessageBox.warning(
                self, "Warning",
                "At least one source PACS must be configured.")
            return

        # Two sources MUST NOT share the same local AE title + port —
        # otherwise the engine quietly funnels both into one
        # StorageSCP, or the second source fails to bind its port at
        # startup.  Catch that here so the user gets a clear message
        # instead of a silent merge / a confusing runtime error.
        duplicate = self._find_duplicate_local_endpoint()
        if duplicate is not None:
            key_a, key_b, ae, port = duplicate
            QMessageBox.warning(
                self, "Duplicate Local Endpoint",
                f"Source PACS \"{key_a}\" and \"{key_b}\" share the "
                f"same local AE title + port ({ae}:{port}).\n\n"
                f"Each source needs its own (AE, port) pair so the "
                f"per-source built-in Storage SCP can bind cleanly. "
                f"Change one of them before saving.")
            return

        # Apply to config
        self.config.remote_nodes = dict(self._remote_nodes)
        self.config.prior_studies_count = self.prior_spin.value()
        self.config.prior_studies_same_modality = (
            self.prior_modality_check.isChecked())
        self.config.language = self.language_combo.currentData() or "en"

        self.config.save()
        self.accept()

    def _find_duplicate_local_endpoint(
            self) -> Optional[Tuple[str, str, str, int]]:
        """Return ``(key_a, key_b, ae_title, port)`` for the first
        pair of sources whose local AE + port collide, or ``None``."""
        seen: dict = {}
        for key, node in self._remote_nodes.items():
            endpoint = (node.local_ae_title, node.local_port)
            if endpoint in seen:
                return (seen[endpoint], key,
                        node.local_ae_title, node.local_port)
            seen[endpoint] = key
        return None
