"""
Tests for gui.priority_series_dialog — PrioritySeriesDialog.

The dialog edits the per-source-PACS ``priority_series_terms`` list
(see core/config.py).  A QComboBox at the top switches the table to
the priority list of any configured source; edits are kept in a
working copy and only commit on Save.
"""

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QMessageBox

from gui.priority_series_dialog import PrioritySeriesDialog
from core.config import AppConfig, PacsNode, default_priority_terms


# ═══════════════════════════════════════════════════════════════════════════
# PrioritySeriesDialog — initialization
# ═══════════════════════════════════════════════════════════════════════════

class TestPrioritySeriesDialogInit:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        # Ensure both sources start with predictable lists.
        for node in populated_config.remote_nodes.values():
            node.priority_series_terms = default_priority_terms()
        self.dialog = PrioritySeriesDialog(populated_config)
        self.config = populated_config

    def test_window_title(self):
        assert "Priority" in self.dialog.windowTitle()

    def test_source_picker_lists_every_configured_pacs(self):
        items = [self.dialog.source_combo.itemText(i)
                 for i in range(self.dialog.source_combo.count())]
        # populated_config has remotes "ct" and "mri"; the picker can
        # render either as the bare key or with the human name.
        joined = " | ".join(items)
        assert "ct" in joined
        assert "mri" in joined

    def test_table_loads_active_source_priority_list(self):
        """On open, the table renders the current selection's list."""
        defaults = default_priority_terms()
        assert self.dialog.terms_table.rowCount() == len(defaults)
        for row, entry in enumerate(defaults):
            assert (self.dialog.terms_table.item(row, 0).text()
                    == entry["term"])
            # The regex cell is a QWidget wrapper around the
            # QCheckBox so the checkbox can be centred; query via
            # the dialog's helper rather than poking at the wrapper.
            cb = self.dialog._regex_checkbox(row)
            assert cb is not None
            assert isinstance(cb, QCheckBox)
            assert cb.isChecked() == entry["is_regex"]


# ═══════════════════════════════════════════════════════════════════════════
# Source switching
# ═══════════════════════════════════════════════════════════════════════════

class TestPrioritySeriesDialogSourceSwitching:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        populated_config.remote_nodes["ct"].priority_series_terms = [
            {"term": "cct", "is_regex": False},
            {"term": "cta", "is_regex": False},
        ]
        populated_config.remote_nodes["mri"].priority_series_terms = [
            {"term": "diffusion", "is_regex": False},
        ]
        self.dialog = PrioritySeriesDialog(populated_config)
        self.config = populated_config

    def _select_source(self, remote_key):
        for i in range(self.dialog.source_combo.count()):
            data = self.dialog.source_combo.itemData(i)
            if data == remote_key:
                self.dialog.source_combo.setCurrentIndex(i)
                return
        pytest.fail(f"source {remote_key!r} not in combo")

    def test_switching_source_renders_the_other_lists_entries(self):
        self._select_source("mri")
        assert self.dialog.terms_table.rowCount() == 1
        assert (self.dialog.terms_table.item(0, 0).text()
                == "diffusion")

    def test_switching_source_does_not_lose_in_progress_edits(self):
        # Edit on ct: append a new row.
        self._select_source("ct")
        self.dialog._on_add_row()
        n = self.dialog.terms_table.rowCount()
        self.dialog.terms_table.item(n - 1, 0).setText("perfusion")

        # Switch to mri and back; the new row must still be there.
        self._select_source("mri")
        self._select_source("ct")
        # n was after the add; on return rowCount equals n.
        assert self.dialog.terms_table.rowCount() == n
        last_terms = [
            self.dialog.terms_table.item(r, 0).text()
            for r in range(n)
        ]
        assert "perfusion" in last_terms

    def test_switching_source_commits_active_editor_delegate(self):
        """The realistic case: the user has the cell editor open (mid-
        typing) and switches source.  ``closePersistentEditor`` is a
        no-op because the table uses Qt's *transient* default editor —
        ``_close_pending_edit`` must instead force the editor to commit
        so the typed text survives the switch."""
        from PySide6.QtWidgets import QApplication, QLineEdit

        self._select_source("ct")
        item = self.dialog.terms_table.item(0, 0)
        # Open the transient editor delegate on row 0 (Qt's default
        # editor for a QTableWidgetItem is a QLineEdit).
        self.dialog.terms_table.editItem(item)
        QApplication.processEvents()
        editor = self.dialog.terms_table.findChild(QLineEdit)
        assert editor is not None, "no transient editor opened"
        # Type into the editor without ever committing manually —
        # text lives in the editor delegate, NOT yet in the item.
        editor.setText("perfusion-mid-edit")

        # Switch source — _close_pending_edit must commit the editor.
        self._select_source("mri")
        self._select_source("ct")

        # Row 0's text on return must reflect the typed value.
        committed = self.dialog.terms_table.item(0, 0).text()
        assert committed == "perfusion-mid-edit"


# ═══════════════════════════════════════════════════════════════════════════
# Row operations
# ═══════════════════════════════════════════════════════════════════════════

class TestPrioritySeriesDialogRowOps:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        populated_config.remote_nodes["ct"].priority_series_terms = [
            {"term": "alpha", "is_regex": False},
            {"term": "beta", "is_regex": False},
            {"term": "gamma", "is_regex": False},
        ]
        self.dialog = PrioritySeriesDialog(populated_config)
        # Ensure we're operating on the "ct" source.
        for i in range(self.dialog.source_combo.count()):
            if self.dialog.source_combo.itemData(i) == "ct":
                self.dialog.source_combo.setCurrentIndex(i)
                break

    def _column_terms(self):
        return [self.dialog.terms_table.item(r, 0).text()
                for r in range(self.dialog.terms_table.rowCount())]

    def test_add_appends_blank_row(self):
        self.dialog._on_add_row()
        terms = self._column_terms()
        assert len(terms) == 4
        assert terms[-1] == ""

    def test_remove_deletes_selected_row(self):
        self.dialog.terms_table.selectRow(1)  # "beta"
        self.dialog._on_remove_row()
        assert self._column_terms() == ["alpha", "gamma"]

    def test_move_up_swaps_with_row_above(self):
        self.dialog.terms_table.selectRow(2)  # "gamma"
        self.dialog._on_move_up()
        assert self._column_terms() == ["alpha", "gamma", "beta"]

    def test_move_down_swaps_with_row_below(self):
        self.dialog.terms_table.selectRow(0)  # "alpha"
        self.dialog._on_move_down()
        assert self._column_terms() == ["beta", "alpha", "gamma"]

    def test_reset_to_defaults_replaces_active_source_only(self):
        # Sanity: mri starts with its own (different) list.
        mri = self.dialog.config.remote_nodes["mri"]
        mri.priority_series_terms = [
            {"term": "diffusion", "is_regex": False}]
        # Re-construct so the dialog sees the new mri list.
        self.dialog = PrioritySeriesDialog(self.dialog.config)
        for i in range(self.dialog.source_combo.count()):
            if self.dialog.source_combo.itemData(i) == "ct":
                self.dialog.source_combo.setCurrentIndex(i)
                break
        self.dialog._on_reset_to_defaults()
        # ct working list now equals defaults.
        defaults = default_priority_terms()
        assert self._column_terms() == [t["term"] for t in defaults]
        # mri working list untouched.
        mri_working = self.dialog._working_lists["mri"]
        assert mri_working == [
            {"term": "diffusion", "is_regex": False}]


# ═══════════════════════════════════════════════════════════════════════════
# Save / Cancel
# ═══════════════════════════════════════════════════════════════════════════

class TestPrioritySeriesDialogSave:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        populated_config.remote_nodes["ct"].priority_series_terms = [
            {"term": "alpha", "is_regex": False},
        ]
        populated_config.remote_nodes["mri"].priority_series_terms = [
            {"term": "diffusion", "is_regex": False},
        ]
        self.dialog = PrioritySeriesDialog(populated_config)
        self.config = populated_config

    def _select(self, remote_key):
        for i in range(self.dialog.source_combo.count()):
            if self.dialog.source_combo.itemData(i) == remote_key:
                self.dialog.source_combo.setCurrentIndex(i)
                return

    def test_save_commits_changes_to_correct_pacs_node(self):
        self._select("ct")
        self.dialog._on_add_row()
        new_row = self.dialog.terms_table.rowCount() - 1
        self.dialog.terms_table.item(new_row, 0).setText("zeta")
        with patch.object(self.config, "save") as mock_save:
            self.dialog._on_save()
        mock_save.assert_called_once()
        ct_terms = [t["term"] for t in
                    self.config.remote_nodes["ct"].priority_series_terms]
        assert "zeta" in ct_terms
        # mri list untouched.
        mri_terms = [t["term"] for t in
                     self.config.remote_nodes["mri"].priority_series_terms]
        assert mri_terms == ["diffusion"]

    def test_cancel_discards_all_per_source_changes(self):
        self._select("ct")
        self.dialog._on_add_row()
        new_row = self.dialog.terms_table.rowCount() - 1
        self.dialog.terms_table.item(new_row, 0).setText("zeta")
        # Simulate clicking Cancel.
        self.dialog.reject()
        ct_terms = [t["term"] for t in
                    self.config.remote_nodes["ct"].priority_series_terms]
        assert ct_terms == ["alpha"]  # unchanged

    @patch("gui.priority_series_dialog.QMessageBox.warning")
    def test_invalid_regex_blocks_save(self, mock_warning):
        self._select("ct")
        # Mark the existing row as regex with an invalid pattern.
        self.dialog.terms_table.item(0, 0).setText("[")
        cb = self.dialog._regex_checkbox(0)
        cb.setChecked(True)
        with patch.object(self.config, "save") as mock_save:
            self.dialog._on_save()
        # Save blocked → config.save not invoked, warning surfaced.
        mock_save.assert_not_called()
        mock_warning.assert_called_once()

    def test_empty_term_rows_are_dropped_on_save(self):
        """Blank rows accumulated by accidental Adds must not survive
        as zero-length matchers — they'd otherwise act like 'match
        nothing' clutter in the on-disk config."""
        self._select("ct")
        self.dialog._on_add_row()  # blank
        with patch.object(self.config, "save"):
            self.dialog._on_save()
        terms = [t["term"] for t in
                 self.config.remote_nodes["ct"].priority_series_terms]
        assert "" not in terms
