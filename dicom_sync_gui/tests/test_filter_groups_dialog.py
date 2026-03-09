"""
Tests for gui.filter_groups_dialog — FilterGroupsDialog.
"""

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt

from gui.filter_groups_dialog import FilterGroupsDialog
from core.config import AppConfig, PacsNode


# ═══════════════════════════════════════════════════════════════════════════
# FilterGroupsDialog — initialization
# ═══════════════════════════════════════════════════════════════════════════

class TestFilterGroupsDialogInit:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dialog = FilterGroupsDialog(populated_config)
        self.config = populated_config

    def test_window_title(self):
        assert self.dialog.windowTitle() == "Manage Filter Groups"

    def test_group_list_populated(self):
        assert self.dialog.group_list.count() == 3
        texts = [self.dialog.group_list.item(i).text()
                 for i in range(self.dialog.group_list.count())]
        # Items show count: "Group A  (2 institutions)"
        assert any("Group A" in t for t in texts)
        assert any("Group B" in t for t in texts)
        assert any("Group C" in t for t in texts)

    def test_institution_tree_populated(self):
        count = self.dialog.institution_tree.topLevelItemCount()
        # populated_config has 4 institutions
        assert count == 4

    def test_assign_combo_has_groups(self):
        items = [self.dialog.assign_combo.itemText(i)
                 for i in range(self.dialog.assign_combo.count())]
        assert "Group A" in items
        assert "Group B" in items
        assert "Group C" in items

    def test_works_on_copies(self):
        # Dialog should work on copies, not modify config directly
        self.dialog._group_names.append("Temp Group")
        assert "Temp Group" not in self.config.filter_group_names

    def test_query_days_default(self):
        assert self.dialog.query_days_spin.value() == 1


# ═══════════════════════════════════════════════════════════════════════════
# FilterGroupsDialog — group CRUD
# ═══════════════════════════════════════════════════════════════════════════

class TestFilterGroupsCRUD:

    @pytest.fixture(autouse=True)
    def _create(self, default_config, qapp):
        self.dialog = FilterGroupsDialog(default_config)

    def test_add_group(self):
        self.dialog.group_name_edit.setText("New Group")
        self.dialog._add_group()
        assert "New Group" in self.dialog._group_names
        assert self.dialog.group_list.count() == 1

    def test_add_group_clears_input(self):
        self.dialog.group_name_edit.setText("Test")
        self.dialog._add_group()
        assert self.dialog.group_name_edit.text() == ""

    def test_add_group_empty_name_ignored(self):
        self.dialog.group_name_edit.setText("")
        self.dialog._add_group()
        assert self.dialog.group_list.count() == 0

    @patch("gui.filter_groups_dialog.QMessageBox.warning")
    def test_add_duplicate_group(self, mock_warning):
        self.dialog.group_name_edit.setText("GroupX")
        self.dialog._add_group()
        self.dialog.group_name_edit.setText("GroupX")
        self.dialog._add_group()
        mock_warning.assert_called_once()
        assert self.dialog._group_names.count("GroupX") == 1

    @patch("gui.filter_groups_dialog.QMessageBox.question",
           return_value=16384)  # Yes
    def test_remove_group(self, mock_question):
        self.dialog.group_name_edit.setText("ToRemove")
        self.dialog._add_group()
        self.dialog.group_list.setCurrentRow(0)
        self.dialog._remove_group()
        assert "ToRemove" not in self.dialog._group_names

    @patch("gui.filter_groups_dialog.QMessageBox.question",
           return_value=16384)  # Yes
    def test_remove_group_clears_assignments(self, mock_question):
        self.dialog.group_name_edit.setText("G1")
        self.dialog._add_group()
        self.dialog._assignments["Hosp"] = "G1"
        self.dialog.group_list.setCurrentRow(0)
        self.dialog._remove_group()
        assert "Hosp" not in self.dialog._assignments

    def test_remove_no_selection(self):
        self.dialog.group_list.setCurrentRow(-1)
        self.dialog._remove_group()  # Should not crash

    @patch("gui.filter_groups_dialog.QInputDialog.getText",
           return_value=("Renamed", True))
    def test_rename_group(self, mock_input):
        self.dialog.group_name_edit.setText("Original")
        self.dialog._add_group()
        self.dialog._assignments["SomeHospital"] = "Original"
        self.dialog.group_list.setCurrentRow(0)
        self.dialog._rename_group()
        assert "Renamed" in self.dialog._group_names
        assert "Original" not in self.dialog._group_names
        assert self.dialog._assignments["SomeHospital"] == "Renamed"

    @patch("gui.filter_groups_dialog.QInputDialog.getText",
           return_value=("", False))
    def test_rename_cancelled(self, mock_input):
        self.dialog.group_name_edit.setText("Keep")
        self.dialog._add_group()
        self.dialog.group_list.setCurrentRow(0)
        self.dialog._rename_group()
        assert "Keep" in self.dialog._group_names

    def test_rename_no_selection(self):
        self.dialog.group_list.setCurrentRow(-1)
        self.dialog._rename_group()  # Should not crash

    def test_group_selected_enables_buttons(self):
        self.dialog.group_name_edit.setText("G1")
        self.dialog._add_group()
        self.dialog.group_list.setCurrentRow(0)
        assert self.dialog.btn_rename_group.isEnabled()
        assert self.dialog.btn_remove_group.isEnabled()

    def test_no_group_selected_disables_buttons(self):
        self.dialog._on_group_selected(-1)
        assert not self.dialog.btn_rename_group.isEnabled()
        assert not self.dialog.btn_remove_group.isEnabled()


# ═══════════════════════════════════════════════════════════════════════════
# FilterGroupsDialog — institution management
# ═══════════════════════════════════════════════════════════════════════════

class TestFilterGroupsInstitutions:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dialog = FilterGroupsDialog(populated_config)

    def test_add_institution_manually(self):
        self.dialog.manual_inst_edit.setText("New Hospital")
        self.dialog._add_institution_manually()
        assert "New Hospital" in self.dialog._assignments
        assert self.dialog._assignments["New Hospital"] == ""  # unassigned

    def test_add_institution_manually_clears_input(self):
        self.dialog.manual_inst_edit.setText("New Hospital")
        self.dialog._add_institution_manually()
        assert self.dialog.manual_inst_edit.text() == ""

    def test_add_institution_empty_ignored(self):
        count_before = len(self.dialog._assignments)
        self.dialog.manual_inst_edit.setText("")
        self.dialog._add_institution_manually()
        assert len(self.dialog._assignments) == count_before

    def test_add_existing_institution_no_duplicate(self):
        self.dialog._assignments["TestInst"] = "Group A"
        self.dialog.manual_inst_edit.setText("TestInst")
        self.dialog._add_institution_manually()
        # Should keep existing assignment
        assert self.dialog._assignments["TestInst"] == "Group A"

    def test_institution_tree_shows_unassigned(self):
        self.dialog._refresh_institution_tree()
        tree = self.dialog.institution_tree
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            name = item.text(0)
            group = self.dialog._assignments.get(name, "")
            if not group:
                assert "(unassigned)" in item.text(1)

    def test_institution_tree_shows_assigned_group(self):
        self.dialog._refresh_institution_tree()
        tree = self.dialog.institution_tree
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            name = item.text(0)
            group = self.dialog._assignments.get(name, "")
            if group:
                assert item.text(1) == group


# ═══════════════════════════════════════════════════════════════════════════
# FilterGroupsDialog — assign / unassign
# ═══════════════════════════════════════════════════════════════════════════

class TestFilterGroupsAssignment:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dialog = FilterGroupsDialog(populated_config)

    def test_assign_selected_updates_assignments(self):
        # Select an unassigned institution in the tree
        tree = self.dialog.institution_tree
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item.text(0) == "Unknown Clinic":
                item.setSelected(True)
                break

        self.dialog.assign_combo.setCurrentText("Group C")
        self.dialog._assign_selected()
        assert self.dialog._assignments["Unknown Clinic"] == "Group C"

    def test_unassign_selected(self):
        tree = self.dialog.institution_tree
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item.text(0) == "Hospital Alpha":
                item.setSelected(True)
                break

        self.dialog._unassign_selected()
        assert "Hospital Alpha" not in self.dialog._assignments

    @patch("gui.filter_groups_dialog.QMessageBox.warning")
    def test_assign_no_group_warns(self, mock_warning):
        self.dialog.assign_combo.clear()  # No groups available
        tree = self.dialog.institution_tree
        if tree.topLevelItemCount() > 0:
            tree.topLevelItem(0).setSelected(True)
        self.dialog._assign_selected()
        mock_warning.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# FilterGroupsDialog — save
# ═══════════════════════════════════════════════════════════════════════════

class TestFilterGroupsSave:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dialog = FilterGroupsDialog(populated_config)
        self.config = populated_config

    def test_save_updates_config(self):
        self.dialog._group_names.append("Group D")
        self.dialog._save()
        assert "Group D" in self.config.filter_group_names

    def test_save_cleans_orphan_assignments(self):
        # Assign to a non-existent group
        self.dialog._assignments["NewInst"] = "DeletedGroup"
        self.dialog._save()
        # Should be cleaned to "" since DeletedGroup is not in group_names
        assert self.config.institution_assignments["NewInst"] == ""

    def test_save_preserves_valid_assignments(self):
        self.dialog._save()
        assert self.config.institution_assignments["Hospital Alpha"] == "Group A"
        assert self.config.institution_assignments["Clinic Beta"] == "Group B"


# ═══════════════════════════════════════════════════════════════════════════
# FilterGroupsDialog — query PACS
# ═══════════════════════════════════════════════════════════════════════════

class TestFilterGroupsQuery:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.dialog = FilterGroupsDialog(populated_config)

    @patch("gui.filter_groups_dialog.QMessageBox.warning")
    def test_query_no_remotes_warns(self, mock_warning):
        self.dialog.config.remote_nodes = {}
        self.dialog._query_institutions()
        mock_warning.assert_called_once()

    @patch("gui.filter_groups_dialog.QApplication.processEvents")
    @patch("gui.filter_groups_dialog.DicomOperations")
    def test_query_discovers_institutions(
        self, MockOps, mock_events
    ):
        mock_ops = MagicMock()
        mock_ops.c_find_institution_names.return_value = [
            "New Hospital X", "New Hospital Y"
        ]
        MockOps.return_value = mock_ops

        self.dialog._query_institutions()

        assert "New Hospital X" in self.dialog._assignments
        assert "New Hospital Y" in self.dialog._assignments
        # Status label should mention "Found"
        assert "Found" in self.dialog.lbl_query_status.text()

    @patch("gui.filter_groups_dialog.QApplication.processEvents")
    @patch("gui.filter_groups_dialog.DicomOperations")
    def test_query_does_not_overwrite_existing_assignments(
        self, MockOps, mock_events
    ):
        mock_ops = MagicMock()
        # "Hospital Alpha" already exists with Group A
        mock_ops.c_find_institution_names.return_value = ["Hospital Alpha"]
        MockOps.return_value = mock_ops

        self.dialog._query_institutions()

        # Should keep existing assignment
        assert self.dialog._assignments["Hospital Alpha"] == "Group A"
