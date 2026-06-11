"""
Tests for filter groups export / import — AppConfig methods and dialog UI.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from core.config import (
    AppConfig, merge_filter_group_data, write_filter_groups_json,
)
from gui.filter_groups_dialog import FilterGroupsDialog


# ═══════════════════════════════════════════════════════════════════════════
# merge_filter_group_data — pure helper shared by AppConfig and the dialog
# ═══════════════════════════════════════════════════════════════════════════

class TestMergeFilterGroupData:
    """The pure merge/replace helper has no I/O — it is fed parsed
    import data and returns fresh copies plus a summary dict."""

    GROUPS = ["Group A", "Group B"]
    ASSIGNMENTS = {"Hospital Alpha": "Group A", "Clinic Beta": "Group B"}

    # ── merge mode ──

    def test_merge_appends_only_new_groups(self):
        groups, _, summary = merge_filter_group_data(
            self.GROUPS, self.ASSIGNMENTS,
            ["Group A", "Group C"], {}, merge=True)
        assert groups == ["Group A", "Group B", "Group C"]
        assert summary["groups_added"] == 1  # only Group C is new

    def test_merge_adds_new_institution(self):
        _, assignments, summary = merge_filter_group_data(
            self.GROUPS, self.ASSIGNMENTS,
            [], {"New Clinic": "Group A"}, merge=True)
        assert assignments["New Clinic"] == "Group A"
        assert summary["institutions_added"] == 1
        assert summary["institutions_updated"] == 0

    def test_merge_updates_changed_assignment(self):
        _, assignments, summary = merge_filter_group_data(
            self.GROUPS, self.ASSIGNMENTS,
            [], {"Hospital Alpha": "Group B"}, merge=True)
        assert assignments["Hospital Alpha"] == "Group B"
        assert summary["institutions_updated"] == 1
        assert summary["institutions_added"] == 0

    def test_merge_identical_assignment_not_counted_as_update(self):
        _, _, summary = merge_filter_group_data(
            self.GROUPS, self.ASSIGNMENTS,
            [], {"Hospital Alpha": "Group A"}, merge=True)
        assert summary["institutions_updated"] == 0

    def test_merge_preserves_existing_data(self):
        groups, assignments, _ = merge_filter_group_data(
            self.GROUPS, self.ASSIGNMENTS,
            ["Group D"], {"New Clinic": "Group D"}, merge=True)
        assert "Group A" in groups
        assert assignments["Clinic Beta"] == "Group B"

    def test_merge_summary_counts_combined(self):
        _, _, summary = merge_filter_group_data(
            self.GROUPS, self.ASSIGNMENTS,
            ["Group A", "Group C", "Group D"],
            {"Hospital Alpha": "Group C",   # update
             "Clinic Beta": "Group B",      # unchanged
             "New One": "Group D",          # added
             "New Two": "Group D"},         # added
            merge=True)
        assert summary == {"groups_added": 2,
                           "institutions_added": 2,
                           "institutions_updated": 1}

    # ── replace mode ──

    def test_replace_returns_imported_data(self):
        groups, assignments, _ = merge_filter_group_data(
            self.GROUPS, self.ASSIGNMENTS,
            ["X"], {"Inst": "X"}, merge=False)
        assert groups == ["X"]
        assert assignments == {"Inst": "X"}

    def test_replace_summary_counts_everything_as_added(self):
        _, _, summary = merge_filter_group_data(
            self.GROUPS, self.ASSIGNMENTS,
            ["X", "Y"], {"A": "X", "B": "Y"}, merge=False)
        assert summary == {"groups_added": 2,
                           "institutions_added": 2,
                           "institutions_updated": 0}

    # ── purity: inputs must never be mutated ──

    @pytest.mark.parametrize("merge", [True, False])
    def test_inputs_not_mutated(self, merge):
        groups_in = list(self.GROUPS)
        assignments_in = dict(self.ASSIGNMENTS)
        imported_groups = ["Group Z"]
        imported_assignments = {"Hospital Alpha": "Group Z"}
        merge_filter_group_data(
            groups_in, assignments_in,
            imported_groups, imported_assignments, merge)
        assert groups_in == self.GROUPS
        assert assignments_in == self.ASSIGNMENTS
        assert imported_groups == ["Group Z"]
        assert imported_assignments == {"Hospital Alpha": "Group Z"}

    @pytest.mark.parametrize("merge", [True, False])
    def test_returns_fresh_copies(self, merge):
        """Returned containers must be new objects so the caller can
        assign them without aliasing its inputs (the dialog reassigns
        them onto its working copies)."""
        imported_groups = ["Group Z"]
        imported_assignments = {"Inst": "Group Z"}
        groups, assignments, _ = merge_filter_group_data(
            self.GROUPS, self.ASSIGNMENTS,
            imported_groups, imported_assignments, merge)
        assert groups is not self.GROUPS
        assert groups is not imported_groups
        assert assignments is not self.ASSIGNMENTS
        assert assignments is not imported_assignments


# ═══════════════════════════════════════════════════════════════════════════
# write_filter_groups_json — shared export writer
# ═══════════════════════════════════════════════════════════════════════════

class TestWriteFilterGroupsJson:

    def test_writes_expected_shape(self, tmp_path):
        path = str(tmp_path / "out.json")
        write_filter_groups_json(
            path, ["G1"], {"Klinik München": "G1"})
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data == {
            "filter_group_names": ["G1"],
            "institution_assignments": {"Klinik München": "G1"},
        }


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig.export_filter_groups
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigExportFilterGroups:

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, tmp_path):
        self.config = populated_config
        self.export_path = str(tmp_path / "export.json")

    def test_export_creates_file(self):
        self.config.export_filter_groups(self.export_path)
        assert os.path.exists(self.export_path)

    def test_export_contains_group_names(self):
        self.config.export_filter_groups(self.export_path)
        with open(self.export_path) as f:
            data = json.load(f)
        assert data["filter_group_names"] == ["Group A", "Group B", "Group C"]

    def test_export_contains_assignments(self):
        self.config.export_filter_groups(self.export_path)
        with open(self.export_path) as f:
            data = json.load(f)
        assert data["institution_assignments"]["Hospital Alpha"] == "Group A"
        assert data["institution_assignments"]["Clinic Beta"] == "Group B"

    def test_export_is_valid_json(self):
        self.config.export_filter_groups(self.export_path)
        with open(self.export_path) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_export_preserves_unicode(self):
        self.config.institution_assignments["Klinik München"] = "Group A"
        self.config.export_filter_groups(self.export_path)
        with open(self.export_path, encoding="utf-8") as f:
            data = json.load(f)
        assert "Klinik München" in data["institution_assignments"]


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig.import_filter_groups — replace mode
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigImportReplace:

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, tmp_path):
        self.config = populated_config
        self.import_path = str(tmp_path / "import.json")

    def _write_import(self, data):
        with open(self.import_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_replace_overwrites_groups(self):
        self._write_import({
            "filter_group_names": ["X", "Y"],
            "institution_assignments": {},
        })
        self.config.import_filter_groups(self.import_path, merge=False)
        assert self.config.filter_group_names == ["X", "Y"]

    def test_replace_overwrites_assignments(self):
        self._write_import({
            "filter_group_names": ["X"],
            "institution_assignments": {"NewHospital": "X"},
        })
        self.config.import_filter_groups(self.import_path, merge=False)
        assert self.config.institution_assignments == {"NewHospital": "X"}
        assert "Hospital Alpha" not in self.config.institution_assignments

    def test_replace_returns_summary(self):
        self._write_import({
            "filter_group_names": ["X", "Y"],
            "institution_assignments": {"A": "X", "B": "Y"},
        })
        summary = self.config.import_filter_groups(
            self.import_path, merge=False)
        assert summary["groups_added"] == 2
        assert summary["institutions_added"] == 2
        assert summary["institutions_updated"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig.import_filter_groups — merge mode
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigImportMerge:

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, tmp_path):
        self.config = populated_config
        self.import_path = str(tmp_path / "import.json")

    def _write_import(self, data):
        with open(self.import_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_merge_adds_new_groups(self):
        self._write_import({
            "filter_group_names": ["Group A", "Group D"],
            "institution_assignments": {},
        })
        summary = self.config.import_filter_groups(
            self.import_path, merge=True)
        assert "Group D" in self.config.filter_group_names
        assert summary["groups_added"] == 1  # only Group D is new

    def test_merge_does_not_duplicate_groups(self):
        self._write_import({
            "filter_group_names": ["Group A"],
            "institution_assignments": {},
        })
        self.config.import_filter_groups(self.import_path, merge=True)
        assert self.config.filter_group_names.count("Group A") == 1

    def test_merge_adds_new_institutions(self):
        self._write_import({
            "filter_group_names": [],
            "institution_assignments": {"Brand New Clinic": "Group A"},
        })
        summary = self.config.import_filter_groups(
            self.import_path, merge=True)
        assert self.config.institution_assignments[
            "Brand New Clinic"] == "Group A"
        assert summary["institutions_added"] == 1

    def test_merge_updates_existing_institution(self):
        self._write_import({
            "filter_group_names": [],
            "institution_assignments": {"Hospital Alpha": "Group B"},
        })
        summary = self.config.import_filter_groups(
            self.import_path, merge=True)
        assert self.config.institution_assignments[
            "Hospital Alpha"] == "Group B"
        assert summary["institutions_updated"] == 1

    def test_merge_no_update_when_same(self):
        self._write_import({
            "filter_group_names": [],
            "institution_assignments": {"Hospital Alpha": "Group A"},
        })
        summary = self.config.import_filter_groups(
            self.import_path, merge=True)
        assert summary["institutions_updated"] == 0

    def test_merge_preserves_unrelated_data(self):
        self._write_import({
            "filter_group_names": ["Group D"],
            "institution_assignments": {"NewHosp": "Group D"},
        })
        self.config.import_filter_groups(self.import_path, merge=True)
        # Existing data still present
        assert "Group A" in self.config.filter_group_names
        assert self.config.institution_assignments[
            "Hospital Alpha"] == "Group A"


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig — round-trip export → import
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigExportImportRoundTrip:

    @pytest.fixture(autouse=True)
    def _setup(self, populated_config, tmp_path):
        self.config = populated_config
        self.path = str(tmp_path / "roundtrip.json")

    def test_roundtrip_preserves_data(self):
        original_groups = list(self.config.filter_group_names)
        original_assignments = dict(self.config.institution_assignments)

        self.config.export_filter_groups(self.path)

        # Wipe existing data
        self.config.filter_group_names = []
        self.config.institution_assignments = {}

        self.config.import_filter_groups(self.path, merge=False)

        assert self.config.filter_group_names == original_groups
        assert self.config.institution_assignments == original_assignments


# ═══════════════════════════════════════════════════════════════════════════
# FilterGroupsDialog — export button
# ═══════════════════════════════════════════════════════════════════════════

class TestDialogExport:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp, tmp_path):
        self.dialog = FilterGroupsDialog(populated_config)
        self.tmp_path = tmp_path

    @patch("gui.filter_groups_dialog.QMessageBox.information")
    @patch("gui.filter_groups_dialog.QFileDialog.getSaveFileName")
    def test_export_writes_file(self, mock_save, mock_info):
        export_path = str(self.tmp_path / "out.json")
        mock_save.return_value = (export_path, "JSON Files (*.json)")
        self.dialog._export_groups()
        assert os.path.exists(export_path)
        with open(export_path) as f:
            data = json.load(f)
        assert "filter_group_names" in data
        assert "institution_assignments" in data
        mock_info.assert_called_once()

    @patch("gui.filter_groups_dialog.QFileDialog.getSaveFileName")
    def test_export_cancelled(self, mock_save):
        mock_save.return_value = ("", "")
        # Should not raise
        self.dialog._export_groups()

    @patch("gui.filter_groups_dialog.QMessageBox.information")
    @patch("gui.filter_groups_dialog.QFileDialog.getSaveFileName")
    def test_export_uses_dialog_state(self, mock_save, mock_info):
        """Export should use dialog's working copies, not config directly."""
        self.dialog._group_names.append("TempGroup")
        export_path = str(self.tmp_path / "out2.json")
        mock_save.return_value = (export_path, "JSON Files (*.json)")
        self.dialog._export_groups()
        with open(export_path) as f:
            data = json.load(f)
        assert "TempGroup" in data["filter_group_names"]


# ═══════════════════════════════════════════════════════════════════════════
# FilterGroupsDialog — import button
# ═══════════════════════════════════════════════════════════════════════════

class TestDialogImport:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp, tmp_path):
        self.dialog = FilterGroupsDialog(populated_config)
        self.tmp_path = tmp_path

    def _write_import_file(self, data):
        path = str(self.tmp_path / "import.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    @patch("gui.filter_groups_dialog.QMessageBox.information")
    @patch("gui.filter_groups_dialog.QMessageBox.question",
           return_value=65536)  # No → replace
    @patch("gui.filter_groups_dialog.QFileDialog.getOpenFileName")
    def test_import_replace(self, mock_open, mock_question, mock_info):
        path = self._write_import_file({
            "filter_group_names": ["Imported"],
            "institution_assignments": {"NewInst": "Imported"},
        })
        mock_open.return_value = (path, "JSON Files (*.json)")
        self.dialog._import_groups()
        assert self.dialog._group_names == ["Imported"]
        assert self.dialog._assignments == {"NewInst": "Imported"}
        mock_info.assert_called_once()

    @patch("gui.filter_groups_dialog.QMessageBox.information")
    @patch("gui.filter_groups_dialog.QMessageBox.question",
           return_value=16384)  # Yes → merge
    @patch("gui.filter_groups_dialog.QFileDialog.getOpenFileName")
    def test_import_merge(self, mock_open, mock_question, mock_info):
        path = self._write_import_file({
            "filter_group_names": ["Group D"],
            "institution_assignments": {"NewInst": "Group D"},
        })
        mock_open.return_value = (path, "JSON Files (*.json)")
        self.dialog._import_groups()
        # Original groups still present
        assert "Group A" in self.dialog._group_names
        # New group added
        assert "Group D" in self.dialog._group_names
        # New institution added
        assert self.dialog._assignments["NewInst"] == "Group D"

    @patch("gui.filter_groups_dialog.QFileDialog.getOpenFileName")
    def test_import_cancelled(self, mock_open):
        mock_open.return_value = ("", "")
        self.dialog._import_groups()  # Should not raise

    @patch("gui.filter_groups_dialog.QMessageBox.critical")
    @patch("gui.filter_groups_dialog.QFileDialog.getOpenFileName")
    def test_import_invalid_json(self, mock_open, mock_critical):
        bad_path = str(self.tmp_path / "bad.json")
        with open(bad_path, "w") as f:
            f.write("not json{{{")
        mock_open.return_value = (bad_path, "JSON Files (*.json)")
        self.dialog._import_groups()
        mock_critical.assert_called_once()

    @patch("gui.filter_groups_dialog.QMessageBox.warning")
    @patch("gui.filter_groups_dialog.QFileDialog.getOpenFileName")
    def test_import_empty_data_warns(self, mock_open, mock_warning):
        path = self._write_import_file({
            "filter_group_names": [],
            "institution_assignments": {},
        })
        mock_open.return_value = (path, "JSON Files (*.json)")
        self.dialog._import_groups()
        mock_warning.assert_called_once()

    @patch("gui.filter_groups_dialog.QMessageBox.question",
           return_value=4194304)  # Cancel
    @patch("gui.filter_groups_dialog.QFileDialog.getOpenFileName")
    def test_import_cancel_on_mode_question(
            self, mock_open, mock_question):
        path = self._write_import_file({
            "filter_group_names": ["X"],
            "institution_assignments": {},
        })
        mock_open.return_value = (path, "JSON Files (*.json)")
        original_groups = list(self.dialog._group_names)
        self.dialog._import_groups()
        # Nothing should have changed
        assert self.dialog._group_names == original_groups

    @patch("gui.filter_groups_dialog.QMessageBox.information")
    @patch("gui.filter_groups_dialog.QMessageBox.question",
           return_value=16384)  # Yes → merge
    @patch("gui.filter_groups_dialog.QFileDialog.getOpenFileName")
    def test_import_refreshes_ui(self, mock_open, mock_question, mock_info):
        path = self._write_import_file({
            "filter_group_names": ["Imported Group"],
            "institution_assignments": {"ImportedInst": "Imported Group"},
        })
        mock_open.return_value = (path, "JSON Files (*.json)")
        self.dialog._import_groups()
        # Group list and institution tree should be refreshed
        group_texts = [
            self.dialog.group_list.item(i).text()
            for i in range(self.dialog.group_list.count())
        ]
        assert any("Imported Group" in t for t in group_texts)
