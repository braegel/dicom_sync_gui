"""
Tests for gui.settings_dialog — SettingsDialog and PacsNodeEditor.
"""

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt

from gui.settings_dialog import (
    AE_TITLE_MAX_LEN, SettingsDialog, PacsNodeEditor, is_valid_host,
)
from core.config import AppConfig, PacsNode, TRANSFER_SYNTAXES_NAMES


# ═══════════════════════════════════════════════════════════════════════════
# PacsNodeEditor — basic connection fields
# (The former is_local=True mode was removed — it had no caller.)
# ═══════════════════════════════════════════════════════════════════════════

class TestPacsNodeEditorBasics:

    @pytest.fixture(autouse=True)
    def _create(self, qapp):
        self.editor = PacsNodeEditor()

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
        assert self.editor.port_spin.value() == 104  # remote default

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


# ═══════════════════════════════════════════════════════════════════════════
# PacsNodeEditor — remote mode (with local destination)
# ═══════════════════════════════════════════════════════════════════════════

class TestPacsNodeEditorRemote:

    @pytest.fixture(autouse=True)
    def _create(self, qapp):
        self.editor = PacsNodeEditor()

    def test_default_port_remote(self):
        assert self.editor.port_spin.value() == 104

    def test_no_retrieve_combo(self):
        # The retrieve-method combo was removed together with the
        # never-implemented C-GET option; C-MOVE is the only path.  The
        # placeholder attribute that outlived it is gone too, so the
        # editor no longer advertises a widget it does not have.
        assert not hasattr(self.editor, "retrieve_combo")

    def test_has_service_param_spinboxes(self):
        assert self.editor.hours_spin is not None
        assert self.editor.max_images_spin is not None
        assert self.editor.interval_spin is not None

    def test_default_service_param_values(self):
        assert self.editor.hours_spin.value() == 3
        assert self.editor.max_images_spin.value() == 0
        assert self.editor.interval_spin.value() == 60

    def test_has_local_dest_fields(self):
        assert self.editor.local_ae_edit is not None
        assert self.editor.local_port_spin is not None
        assert self.editor.local_syntax_combo is not None
        assert self.editor.fallback_edit is not None
        assert self.editor.fallback_btn is not None

    def test_default_local_dest_values(self):
        assert self.editor.local_port_spin.value() == 11112
        items = [self.editor.local_syntax_combo.itemText(i)
                 for i in range(self.editor.local_syntax_combo.count())]
        assert "JPEG2000Lossless" in items

    def test_set_node_with_legacy_c_get_does_not_crash(self):
        # Old config files may still carry retrieve_method="C-GET";
        # loading such a node into the editor must work without the
        # (removed) retrieve combo.
        node = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="10.0.0.1", port=104,
            retrieve_method="C-GET",
        )
        self.editor.set_node(node)
        assert self.editor.name_edit.text() == "CT"

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

    def test_set_node_with_local_dest(self):
        node = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="10.0.0.1", port=104,
            local_ae_title="ARZT_4", local_port=11113,
            local_syntax="ExplicitVRLittleEndian",
            fallback_folder="/my/fallback",
        )
        self.editor.set_node(node)
        assert self.editor.local_ae_edit.text() == "ARZT_4"
        assert self.editor.local_port_spin.value() == 11113
        assert self.editor.local_syntax_combo.currentText() == "ExplicitVRLittleEndian"
        assert self.editor.fallback_edit.text() == "/my/fallback"

    def test_get_node_normalizes_retrieve_method(self):
        # get_node always emits "C-MOVE" — the only implemented path —
        # even when editing a node that carried a legacy "C-GET".
        self.editor.name_edit.setText("MRI")
        self.editor.ae_title_edit.setText("MRI_AE")
        legacy = PacsNode(
            name="MRI", ae_title="MRI_AE", retrieve_method="C-GET")
        node = self.editor.get_node(base=legacy)
        assert node.retrieve_method == "C-MOVE"

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

    def test_get_node_includes_local_dest(self):
        self.editor.name_edit.setText("CT")
        self.editor.ae_title_edit.setText("CT_AE")
        self.editor.local_ae_edit.setText("ARZT_4")
        self.editor.local_port_spin.setValue(11113)
        self.editor.local_syntax_combo.setCurrentText("JPEGLossless")
        self.editor.fallback_edit.setText("/data/incoming")
        node = self.editor.get_node()
        assert node.local_ae_title == "ARZT_4"
        assert node.local_port == 11113
        assert node.local_syntax == "JPEGLossless"
        assert node.fallback_folder == "/data/incoming"

    def test_clear_resets_port(self):
        self.editor.port_spin.setValue(5000)
        self.editor.clear_fields()
        assert self.editor.port_spin.value() == 104

    def test_clear_resets_service_params(self):
        self.editor.hours_spin.setValue(48)
        self.editor.max_images_spin.setValue(9999)
        self.editor.interval_spin.setValue(600)
        self.editor.clear_fields()
        assert self.editor.hours_spin.value() == 3
        assert self.editor.max_images_spin.value() == 0
        assert self.editor.interval_spin.value() == 60

    def test_clear_resets_local_dest(self):
        self.editor.local_ae_edit.setText("ARZT_4")
        self.editor.local_port_spin.setValue(22222)
        self.editor.fallback_edit.setText("/some/path")
        self.editor.clear_fields()
        assert self.editor.local_ae_edit.text() == ""
        assert self.editor.local_port_spin.value() == 11112
        assert self.editor.fallback_edit.text() == ""

    def test_has_notification_sound_field(self):
        """Remote editor has a notification sound path field."""
        assert self.editor.notification_sound_edit is not None

    def test_set_node_with_notification_sound(self):
        node = PacsNode(
            name="CT", ae_title="CT_AE",
            ip_address="10.0.0.1", port=104,
            notification_sound_path="/sounds/ding.wav",
        )
        self.editor.set_node(node)
        assert self.editor.notification_sound_edit.text() == "/sounds/ding.wav"

    def test_get_node_includes_notification_sound(self):
        self.editor.name_edit.setText("CT")
        self.editor.ae_title_edit.setText("CT_AE")
        self.editor.notification_sound_edit.setText("/my/sound.wav")
        node = self.editor.get_node()
        assert node.notification_sound_path == "/my/sound.wav"

    def test_clear_resets_notification_sound(self):
        self.editor.notification_sound_edit.setText("/some/file.wav")
        self.editor.clear_fields()
        assert self.editor.notification_sound_edit.text() == ""


# ═══════════════════════════════════════════════════════════════════════════
# SettingsDialog — initialization and loading
# ═══════════════════════════════════════════════════════════════════════════

class TestSettingsDialogInit:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dialog = SettingsDialog(populated_config)

    def test_window_title(self):
        assert self.dialog.windowTitle() == "Settings"

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

    def test_add_new_preserves_local_dest(self):
        self.dialog.key_edit.setText("ct")
        self.dialog.remote_editor.name_edit.setText("CT")
        self.dialog.remote_editor.ae_title_edit.setText("CT_AE")
        self.dialog.remote_editor.local_ae_edit.setText("ARZT_4")
        self.dialog.remote_editor.local_port_spin.setValue(11113)
        self.dialog.remote_editor.fallback_edit.setText("/fallback")

        self.dialog._add_remote()

        node = self.dialog._remote_nodes["ct"]
        assert node.local_ae_title == "ARZT_4"
        assert node.local_port == 11113
        assert node.fallback_folder == "/fallback"

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

    def test_save_changes_preserves_local_dest(self):
        # Add entry
        self.dialog.key_edit.setText("ct")
        self.dialog.remote_editor.name_edit.setText("CT")
        self.dialog.remote_editor.ae_title_edit.setText("CT_AE")
        self.dialog.remote_editor.local_ae_edit.setText("ARZT_4")
        self.dialog.remote_editor.local_port_spin.setValue(11113)
        self.dialog._add_remote()

        # Change local dest
        self.dialog.remote_editor.local_ae_edit.setText("ARZT_5")
        self.dialog.remote_editor.local_port_spin.setValue(22222)
        self.dialog._save_changes_to_selected()

        node = self.dialog._remote_nodes["ct"]
        assert node.local_ae_title == "ARZT_5"
        assert node.local_port == 22222

    def test_save_changes_preserves_fields_without_editor_widgets(self):
        # Regression: the editor has no widgets for the sound on/off
        # flag and the priority series terms — saving an edit must not
        # reset them to the PacsNode defaults.
        self.dialog.key_edit.setText("ct")
        self.dialog.remote_editor.name_edit.setText("CT")
        self.dialog.remote_editor.ae_title_edit.setText("CT_AE")
        self.dialog._add_remote()

        # Customize the hidden fields directly on the stored node
        # (in the app these are set via the dashboard checkbox and
        # the Priority Series dialog).
        custom_terms = [{"term": "stroke", "is_regex": False}]
        node = self.dialog._remote_nodes["ct"]
        node.priority_series_terms = custom_terms
        node.notification_sound_enabled = False

        self.dialog.remote_editor.name_edit.setText("CT Updated")
        self.dialog._save_changes_to_selected()

        node = self.dialog._remote_nodes["ct"]
        assert node.name == "CT Updated"
        assert node.priority_series_terms == custom_terms
        assert node.notification_sound_enabled is False


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


# ═══════════════════════════════════════════════════════════════════════════
# SettingsDialog — UI language selector
# ═══════════════════════════════════════════════════════════════════════════

class TestSettingsDialogLanguage:
    """The General tab exposes a language combo box for picking the
    UI language (en / de / fr / es). The choice is persisted to
    AppConfig.language and written to disk on Save."""

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.config = populated_config
        self.dialog = SettingsDialog(self.config)

    def test_has_language_combo(self):
        assert hasattr(self.dialog, "language_combo")

    def test_language_combo_contains_all_four_languages(self):
        codes = [self.dialog.language_combo.itemData(i)
                 for i in range(self.dialog.language_combo.count())]
        assert set(codes) == {"en", "de", "fr", "es"}

    def test_language_combo_loads_current_config_value(
            self, populated_config, qapp):
        populated_config.language = "fr"
        dialog = SettingsDialog(populated_config)
        assert dialog.language_combo.currentData() == "fr"

    def test_language_combo_defaults_to_english_when_unset(self):
        assert self.dialog.language_combo.currentData() == "en"

    def test_save_writes_language_to_config(self):
        # Pick German.
        idx = self.dialog.language_combo.findData("de")
        self.dialog.language_combo.setCurrentIndex(idx)
        with patch.object(self.config, "save"):
            self.dialog._save()
        assert self.config.language == "de"

    def test_save_persists_language_to_disk(self, tmp_config_path):
        import json
        cfg = AppConfig(config_path=tmp_config_path)
        cfg.remote_nodes = dict(self.config.remote_nodes)
        dlg = SettingsDialog(cfg)
        idx = dlg.language_combo.findData("es")
        dlg.language_combo.setCurrentIndex(idx)
        dlg._save()
        with open(tmp_config_path) as f:
            data = json.load(f)
        assert data["language"] == "es"


# ═══════════════════════════════════════════════════════════════════════════
# SettingsDialog — network field validation
# ═══════════════════════════════════════════════════════════════════════════

class TestIsValidHost:
    """``is_valid_host`` is purely syntactic — it must accept anything
    a PACS could legitimately be reached at and reject the classic
    typos that otherwise only surface as a failed association."""

    @pytest.mark.parametrize("value", [
        "192.168.1.10",
        "10.0.0.1",
        "255.255.255.255",
        "::1",
        "2001:db8::1",
        "pacs",
        "pacs-01",
        "pacs_01",                      # Windows-domain hosts do this
        "pacs.hospital.example.com",
        "PACS.Hospital.Example",
    ])
    def test_accepts_valid_hosts(self, value):
        assert is_valid_host(value) is True

    @pytest.mark.parametrize("value", [
        "192.168.1.300",                # octet out of range
        "10.0.0",                       # truncated address
        "192.168..1",                   # empty label
        "192.168.1.10 ",                # trailing space (unstripped)
        "pacs host",                    # space in the middle
        "-pacs",                        # leading hyphen
        "pacs-",                        # trailing hyphen
        "pacs..local",
        "a" * 254,
        "",
    ])
    def test_rejects_invalid_hosts(self, value):
        assert is_valid_host(value) is False


class TestSettingsDialogNetworkValidation:

    @pytest.fixture(autouse=True)
    def _create(self, default_config, qapp):
        self.dialog = SettingsDialog(default_config)

    def _fill_minimum(self, key="ct", ae="CT_AE"):
        self.dialog.key_edit.setText(key)
        self.dialog.remote_editor.name_edit.setText("CT")
        self.dialog.remote_editor.ae_title_edit.setText(ae)

    @patch("gui.settings_dialog.QMessageBox.warning")
    def test_add_rejects_overlong_ae_title(self, mock_warning):
        self._fill_minimum(ae="A" * (AE_TITLE_MAX_LEN + 1))
        self.dialog._add_remote()
        mock_warning.assert_called_once()
        assert self.dialog.remote_list.count() == 0

    def test_add_accepts_ae_title_at_the_limit(self):
        self._fill_minimum(ae="A" * AE_TITLE_MAX_LEN)
        self.dialog._add_remote()
        assert self.dialog.remote_list.count() == 1

    @patch("gui.settings_dialog.QMessageBox.warning")
    def test_add_rejects_overlong_local_ae_title(self, mock_warning):
        self._fill_minimum()
        self.dialog.remote_editor.local_ae_edit.setText(
            "L" * (AE_TITLE_MAX_LEN + 1))
        self.dialog._add_remote()
        mock_warning.assert_called_once()
        assert self.dialog.remote_list.count() == 0

    @patch("gui.settings_dialog.QMessageBox.warning")
    def test_add_rejects_malformed_ip(self, mock_warning):
        self._fill_minimum()
        self.dialog.remote_editor.ip_edit.setText("192.168.1.300")
        self.dialog._add_remote()
        mock_warning.assert_called_once()
        assert self.dialog.remote_list.count() == 0

    def test_add_accepts_hostname(self):
        self._fill_minimum()
        self.dialog.remote_editor.ip_edit.setText("pacs.hospital.local")
        self.dialog._add_remote()
        assert self.dialog.remote_list.count() == 1

    def test_add_accepts_empty_ip(self):
        """The IP is optional at entry time — the dialog is built
        around filling entries in gradually, so an empty field must
        not block the add."""
        self._fill_minimum()
        self.dialog.remote_editor.ip_edit.setText("")
        self.dialog._add_remote()
        assert self.dialog.remote_list.count() == 1

    @patch("gui.settings_dialog.QMessageBox.warning")
    def test_save_changes_rejects_malformed_ip(self, mock_warning):
        self._fill_minimum()
        self.dialog.remote_editor.ip_edit.setText("192.168.1.10")
        self.dialog._add_remote()

        self.dialog.remote_editor.ip_edit.setText("192.168.1.300")
        self.dialog._save_changes_to_selected()

        mock_warning.assert_called_once()
        # The stored node keeps the last good value.
        assert self.dialog._remote_nodes["ct"].ip_address == "192.168.1.10"

    @patch("gui.settings_dialog.QMessageBox.warning")
    def test_save_changes_rejects_overlong_ae_title(self, mock_warning):
        self._fill_minimum()
        self.dialog._add_remote()

        self.dialog.remote_editor.ae_title_edit.setText("A" * 20)
        self.dialog._save_changes_to_selected()

        mock_warning.assert_called_once()
        assert self.dialog._remote_nodes["ct"].ae_title == "CT_AE"
