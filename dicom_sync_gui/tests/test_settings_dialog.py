"""
Tests for gui.settings_dialog — SettingsDialog and PacsNodeEditor.
"""

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt

from gui.settings_dialog import SettingsDialog, PacsNodeEditor
from core.config import AppConfig, PacsNode, TRANSFER_SYNTAXES_NAMES, RETRIEVE_METHODS


# ═══════════════════════════════════════════════════════════════════════════
# PacsNodeEditor
# ═══════════════════════════════════════════════════════════════════════════

class TestPacsNodeEditorLocal:

    @pytest.fixture(autouse=True)
    def _create(self, qapp):
        self.editor = PacsNodeEditor(is_local=True)

    def test_default_port_local(self):
        assert self.editor.port_spin.value() == 11112

    def test_no_retrieve_combo_for_local(self):
        assert self.editor.retrieve_combo is None

    def test_no_service_param_spinboxes_for_local(self):
        assert self.editor.hours_spin is None
        assert self.editor.max_images_spin is None
        assert self.editor.interval_spin is None

    def test_syntax_combo_populated(self):
        items = [self.editor.syntax_combo.itemText(i)
                 for i in range(self.editor.syntax_combo.count())]
        assert "JPEG2000Lossless" in items
        assert "ExplicitVRLittleEndian" in items

    def test_set_node(self):
        node = PacsNode(
            name="Test", ae_title="TST_AE",
            ip_address="10.0.0.1", port=5555,
            transfer_syntax="JPEGLossless",
        )
        self.editor.set_node(node)
        assert self.editor.name_edit.text() == "Test"
        assert self.editor.ae_title_edit.text() == "TST_AE"
        assert self.editor.ip_edit.text() == "10.0.0.1"
        assert self.editor.port_spin.value() == 5555
        assert self.editor.syntax_combo.currentText() == "JPEGLossless"

    def test_get_node(self):
        self.editor.name_edit.setText("My Node")
        self.editor.ae_title_edit.setText("MY_AE")
        self.editor.ip_edit.setText("1.2.3.4")
        self.editor.port_spin.setValue(8042)
        node = self.editor.get_node()
        assert node.name == "My Node"
        assert node.ae_title == "MY_AE"
        assert node.ip_address == "1.2.3.4"
        assert node.port == 8042

    def test_clear_fields(self):
        self.editor.name_edit.setText("Something")
        self.editor.ae_title_edit.setText("SOME_AE")
        self.editor.clear_fields()
        assert self.editor.name_edit.text() == ""
        assert self.editor.ae_title_edit.text() == ""
        assert self.editor.port_spin.value() == 11112  # local default

    def test_has_minimum_data_true(self):
        self.editor.name_edit.setText("Node")
        self.editor.ae_title_edit.setText("AE")
        assert self.editor.has_minimum_data() is True

    def test_has_minimum_data_false_no_name(self):
        self.editor.name_edit.setText("")
        self.editor.ae_title_edit.setText("AE")
        assert self.editor.has_minimum_data() is False

    def test_has_minimum_data_false_no_ae(self):
        self.editor.name_edit.setText("Node")
        self.editor.ae_title_edit.setText("")
        assert self.editor.has_minimum_data() is False

    def test_has_minimum_data_whitespace_only(self):
        self.editor.name_edit.setText("  ")
        self.editor.ae_title_edit.setText("  ")
        assert self.editor.has_minimum_data() is False


class TestPacsNodeEditorRemote:

    @pytest.fixture(autouse=True)
    def _create(self, qapp):
        self.editor = PacsNodeEditor(is_local=False)

    def test_default_port_remote(self):
        assert self.editor.port_spin.value() == 104

    def test_has_retrieve_combo(self):
        assert self.editor.retrieve_combo is not None
        items = [self.editor.retrieve_combo.itemText(i)
                 for i in range(self.editor.retrieve_combo.count())]
        assert items == RETRIEVE_METHODS

    def test_has_service_param_spinboxes(self):
        assert self.editor.hours_spin is not None
        assert self.editor.max_images_spin is not None
        assert self.editor.interval_spin is not None

    def test_default_service_param_values(self):
        assert self.editor.hours_spin.value() == 3
        assert self.editor.max_images_spin.value() == 0
        assert self.editor.interval_spin.value() == 60

    def test_set_node_with_retrieve_method(self):
        node = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="10.0.0.1", port=104,
            retrieve_method="C-GET",
        )
        self.editor.set_node(node)
        assert self.editor.retrieve_combo.currentText() == "C-GET"

    def test_set_node_with_service_params(self):
        node = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="10.0.0.1", port=104,
            hours=24, max_images=1000, sync_interval=300,
        )
        self.editor.set_node(node)
        assert self.editor.hours_spin.value() == 24
        assert self.editor.max_images_spin.value() == 1000
        assert self.editor.interval_spin.value() == 300

    def test_get_node_includes_retrieve_method(self):
        self.editor.name_edit.setText("MRI")
        self.editor.ae_title_edit.setText("MRI_AE")
        self.editor.retrieve_combo.setCurrentText("C-GET")
        node = self.editor.get_node()
        assert node.retrieve_method == "C-GET"

    def test_get_node_includes_service_params(self):
        self.editor.name_edit.setText("MRI")
        self.editor.ae_title_edit.setText("MRI_AE")
        self.editor.hours_spin.setValue(12)
        self.editor.max_images_spin.setValue(500)
        self.editor.interval_spin.setValue(120)
        node = self.editor.get_node()
        assert node.hours == 12
        assert node.max_images == 500
        assert node.sync_interval == 120

    def test_clear_resets_to_first_retrieve(self):
        self.editor.retrieve_combo.setCurrentText("C-GET")
        self.editor.clear_fields()
        assert self.editor.retrieve_combo.currentText() == "C-MOVE"
        assert self.editor.port_spin.value() == 104

    def test_clear_resets_service_params(self):
        self.editor.hours_spin.setValue(48)
        self.editor.max_images_spin.setValue(9999)
        self.editor.interval_spin.setValue(600)
        self.editor.clear_fields()
        assert self.editor.hours_spin.value() == 3
        assert self.editor.max_images_spin.value() == 0
        assert self.editor.interval_spin.value() == 60


# ═══════════════════════════════════════════════════════════════════════════
# SettingsDialog — initialization and loading
# ═══════════════════════════════════════════════════════════════════════════

class TestSettingsDialogInit:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dialog = SettingsDialog(populated_config)

    def test_window_title(self):
        assert self.dialog.windowTitle() == "Settings"

    def test_local_editor_loaded(self):
        assert self.dialog.local_editor.ae_title_edit.text() == "LOCAL_AE"
        assert self.dialog.local_editor.port_spin.value() == 11112

    def test_fallback_loaded(self):
        assert self.dialog.fallback_group.isChecked() is True
        assert self.dialog.storage_edit.text() == "/tmp/dicom_test"

    def test_prior_studies_loaded(self):
        assert self.dialog.prior_spin.value() == 2
        assert self.dialog.prior_modality_check.isChecked() is True

    def test_remote_list_populated(self):
        assert self.dialog.remote_list.count() == 2

    def test_starts_in_new_mode(self):
        # New mode: Add New should not be hidden, Save Changes should be hidden.
        # Note: isVisible() returns False when the parent dialog isn't shown,
        # so we check isHidden() or the explicit visibility flag instead.
        assert not self.dialog.btn_add_new.isHidden()
        assert self.dialog.btn_save_changes.isHidden()

    def test_remote_keys_tracked(self):
        assert set(self.dialog._remote_keys) == {"ct", "mri"}


# ═══════════════════════════════════════════════════════════════════════════
# SettingsDialog — fill-first workflow
# ═══════════════════════════════════════════════════════════════════════════

class TestSettingsDialogWorkflow:

    @pytest.fixture(autouse=True)
    def _create(self, default_config, qapp):
        # Start with no remotes so we can test adding
        self.dialog = SettingsDialog(default_config)

    def test_add_new_with_valid_data(self):
        self.dialog.key_edit.setText("scanner1")
        self.dialog.remote_editor.name_edit.setText("Scanner One")
        self.dialog.remote_editor.ae_title_edit.setText("SC1_AE")
        self.dialog.remote_editor.ip_edit.setText("192.168.1.10")

        self.dialog._add_remote()

        assert self.dialog.remote_list.count() == 1
        assert "scanner1" in self.dialog._remote_keys

    @patch("gui.settings_dialog.QMessageBox.warning")
    def test_add_new_missing_data(self, mock_warning):
        self.dialog.key_edit.setText("x")
        # Don't set name/ae_title
        self.dialog._add_remote()
        mock_warning.assert_called_once()
        assert self.dialog.remote_list.count() == 0

    @patch("gui.settings_dialog.QMessageBox.warning")
    def test_add_new_missing_key(self, mock_warning):
        self.dialog.remote_editor.name_edit.setText("Scanner")
        self.dialog.remote_editor.ae_title_edit.setText("AE")
        self.dialog.key_edit.setText("")
        self.dialog._add_remote()
        mock_warning.assert_called_once()

    @patch("gui.settings_dialog.QMessageBox.warning")
    def test_add_duplicate_key(self, mock_warning):
        # Add first entry
        self.dialog.key_edit.setText("ct")
        self.dialog.remote_editor.name_edit.setText("CT1")
        self.dialog.remote_editor.ae_title_edit.setText("CT_AE")
        self.dialog._add_remote()

        # Switch to new mode
        self.dialog._switch_to_new_mode()

        # Try duplicate
        self.dialog.key_edit.setText("ct")
        self.dialog.remote_editor.name_edit.setText("CT2")
        self.dialog.remote_editor.ae_title_edit.setText("CT2_AE")
        self.dialog._add_remote()
        mock_warning.assert_called_once()

    def test_select_entry_switches_to_edit_mode(self):
        # Add an entry first
        self.dialog.key_edit.setText("test")
        self.dialog.remote_editor.name_edit.setText("Test")
        self.dialog.remote_editor.ae_title_edit.setText("T_AE")
        self.dialog._add_remote()

        # After adding, should be in edit mode (newly added is selected).
        # Use isHidden() since the dialog itself is not shown.
        assert not self.dialog.btn_save_changes.isHidden()
        assert self.dialog.btn_add_new.isHidden()

    def test_new_entry_button_clears_editor(self):
        self.dialog.key_edit.setText("something")
        self.dialog.remote_editor.name_edit.setText("Something")
        self.dialog._switch_to_new_mode()
        assert self.dialog.key_edit.text() == ""
        assert self.dialog.remote_editor.name_edit.text() == ""

    def test_save_changes_to_selected(self):
        # Add entry
        self.dialog.key_edit.setText("mri")
        self.dialog.remote_editor.name_edit.setText("MRI Unit")
        self.dialog.remote_editor.ae_title_edit.setText("MRI_AE")
        self.dialog._add_remote()

        # Modify name
        self.dialog.remote_editor.name_edit.setText("MRI Unit Updated")
        self.dialog._save_changes_to_selected()

        assert self.dialog._remote_nodes["mri"].name == "MRI Unit Updated"


# ═══════════════════════════════════════════════════════════════════════════
# SettingsDialog — remove
# ═══════════════════════════════════════════════════════════════════════════

class TestSettingsDialogRemove:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dialog = SettingsDialog(populated_config)

    @patch("gui.settings_dialog.QMessageBox.question",
           return_value=4)  # QMessageBox.Yes = 16384 in Qt6, but mocked
    def test_remove_deselected(self, mock_question):
        # No selection
        self.dialog.remote_list.setCurrentRow(-1)
        initial_count = self.dialog.remote_list.count()
        self.dialog._remove_remote()
        assert self.dialog.remote_list.count() == initial_count
