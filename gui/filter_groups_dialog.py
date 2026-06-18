"""
Filter Groups Dialog — manage institution-based filter groups.

Workflow:
 - User opens via Settings → Manage Filter Groups
 - A query is sent to source PACS to discover unique InstitutionName values
 - User creates named groups and assigns institutions to them
 - Each institution belongs to at most one group
 - Groups and assignments are persisted in AppConfig
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QWidget,
    QPushButton, QGroupBox, QListWidget, QComboBox,
    QSpinBox, QMessageBox, QSplitter, QInputDialog, QHeaderView,
    QTreeWidget, QTreeWidgetItem, QAbstractItemView,
    QFileDialog,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from core.config import (
    AppConfig, merge_filter_group_data, write_filter_groups_json,
)
from core.dicom_ops import DicomOperations
from gui.async_helpers import run_in_background
from gui.styles import BTN_GREEN, BTN_GREEN_LARGE, BTN_RED, BTN_BLUE, BTN_BLUE_LARGE

logger = logging.getLogger("dicom_sync")


class FilterGroupsDialog(QDialog):
    """Dialog for creating and managing institution filter groups."""

    def __init__(self, config: AppConfig,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config

        # Work on copies so we can cancel
        self._assignments: Dict[str, str] = dict(
            config.institution_assignments)
        self._group_names: List[str] = list(config.filter_group_names)

        self.setWindowTitle("Manage Filter Groups")
        self.setMinimumSize(900, 620)
        self.resize(1000, 680)

        self._setup_ui()
        self._refresh_group_list()
        self._refresh_institution_tree()

    # ── UI ────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ── Top: Query institutions from PACS ──
        layout.addWidget(self._build_query_box())

        # ── Middle: splitter with groups on left, institutions on right ──
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_groups_panel())
        splitter.addWidget(self._build_institutions_panel())
        splitter.setSizes([300, 600])
        layout.addWidget(splitter, 1)

        # ── Bottom: Export / Import / Save / Cancel ──
        layout.addLayout(self._build_button_bar())

    def _build_query_box(self) -> QGroupBox:
        query_group = QGroupBox("Discover Institutions from Source PACS")
        ql = QHBoxLayout()

        ql.addWidget(QLabel("Search last:"))
        self.query_days_spin = QSpinBox()
        self.query_days_spin.setRange(1, 365)
        self.query_days_spin.setValue(1)
        self.query_days_spin.setSuffix(" days")
        ql.addWidget(self.query_days_spin)

        self.btn_query = QPushButton("  Query Institutions  ")
        self.btn_query.setStyleSheet(BTN_BLUE_LARGE)
        self.btn_query.clicked.connect(self._query_institutions)
        ql.addWidget(self.btn_query)

        ql.addStretch()

        self.lbl_query_status = QLabel("")
        ql.addWidget(self.lbl_query_status)

        query_group.setLayout(ql)
        return query_group

    def _build_groups_panel(self) -> QGroupBox:
        # Left: Group management
        left_widget = QGroupBox("Filter Groups")
        left_layout = QVBoxLayout()

        add_grp_layout = QHBoxLayout()
        self.group_name_edit = QLineEdit()
        self.group_name_edit.setPlaceholderText("New group name...")
        add_grp_layout.addWidget(self.group_name_edit)

        self.btn_add_group = QPushButton("Add Group")
        self.btn_add_group.setStyleSheet(BTN_GREEN)
        self.btn_add_group.clicked.connect(self._add_group)
        add_grp_layout.addWidget(self.btn_add_group)

        left_layout.addLayout(add_grp_layout)

        self.group_list = QListWidget()
        self.group_list.currentRowChanged.connect(
            self._on_group_selected)
        left_layout.addWidget(self.group_list)

        grp_btn_layout = QHBoxLayout()
        self.btn_rename_group = QPushButton("Rename")
        self.btn_rename_group.setEnabled(False)
        self.btn_rename_group.clicked.connect(self._rename_group)
        grp_btn_layout.addWidget(self.btn_rename_group)

        self.btn_remove_group = QPushButton("Remove")
        self.btn_remove_group.setEnabled(False)
        self.btn_remove_group.setStyleSheet(BTN_RED)
        self.btn_remove_group.clicked.connect(self._remove_group)
        grp_btn_layout.addWidget(self.btn_remove_group)

        left_layout.addLayout(grp_btn_layout)
        left_widget.setLayout(left_layout)
        return left_widget

    def _build_institutions_panel(self) -> QGroupBox:
        # Right: Institution assignment
        right_widget = QGroupBox("Institutions")
        right_layout = QVBoxLayout()

        right_layout.addWidget(QLabel(
            "Assign each institution to a group. "
            "Unassigned institutions are loaded by default when filtering "
            "is active."))

        self.institution_tree = QTreeWidget()
        self.institution_tree.setHeaderLabels(
            ["Institution Name", "Assigned Group"])
        self.institution_tree.setAlternatingRowColors(True)
        self.institution_tree.setRootIsDecorated(False)
        self.institution_tree.setSelectionMode(
            QAbstractItemView.ExtendedSelection)
        header = self.institution_tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        right_layout.addWidget(self.institution_tree)

        # Assignment controls
        assign_layout = QHBoxLayout()
        assign_layout.addWidget(QLabel("Assign selected to:"))

        self.assign_combo = QComboBox()
        self.assign_combo.setMinimumWidth(180)
        assign_layout.addWidget(self.assign_combo)

        self.btn_assign = QPushButton("Assign")
        self.btn_assign.setStyleSheet(BTN_BLUE)
        self.btn_assign.clicked.connect(self._assign_selected)
        assign_layout.addWidget(self.btn_assign)

        self.btn_unassign = QPushButton("Unassign")
        self.btn_unassign.clicked.connect(self._unassign_selected)
        assign_layout.addWidget(self.btn_unassign)

        assign_layout.addStretch()

        # Manual add institution
        self.manual_inst_edit = QLineEdit()
        self.manual_inst_edit.setPlaceholderText(
            "Add institution manually...")
        self.manual_inst_edit.setMaximumWidth(200)
        assign_layout.addWidget(self.manual_inst_edit)

        self.btn_add_inst = QPushButton("Add")
        self.btn_add_inst.clicked.connect(self._add_institution_manually)
        assign_layout.addWidget(self.btn_add_inst)

        right_layout.addLayout(assign_layout)
        right_widget.setLayout(right_layout)
        return right_widget

    def _build_button_bar(self) -> QHBoxLayout:
        btn_layout = QHBoxLayout()

        self.btn_export = QPushButton("Export...")
        self.btn_export.setToolTip(
            "Export filter groups and institution assignments to a JSON file")
        self.btn_export.clicked.connect(self._export_groups)
        btn_layout.addWidget(self.btn_export)

        self.btn_import = QPushButton("Import...")
        self.btn_import.setToolTip(
            "Import filter groups and institution assignments from a JSON file")
        self.btn_import.clicked.connect(self._import_groups)
        btn_layout.addWidget(self.btn_import)

        btn_layout.addStretch()

        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)

        btn_save = QPushButton("  Save  ")
        btn_save.setStyleSheet(BTN_GREEN_LARGE)
        btn_save.clicked.connect(self._save)
        btn_layout.addWidget(btn_save)

        return btn_layout

    # ── Group management ──────────────────────────────────────────────────

    def _refresh_group_list(self) -> None:
        self.group_list.clear()
        for name in self._group_names:
            count = sum(
                1 for g in self._assignments.values() if g == name)
            self.group_list.addItem(f"{name}  ({count} institutions)")
        self._refresh_assign_combo()

    def _refresh_assign_combo(self) -> None:
        current = self.assign_combo.currentText()
        self.assign_combo.clear()
        for name in self._group_names:
            self.assign_combo.addItem(name)
        idx = self.assign_combo.findText(current)
        if idx >= 0:
            self.assign_combo.setCurrentIndex(idx)

    def _on_group_selected(self, row: int) -> None:
        enabled = row >= 0
        self.btn_rename_group.setEnabled(enabled)
        self.btn_remove_group.setEnabled(enabled)

    def _add_group(self) -> None:
        name = self.group_name_edit.text().strip()
        if not name:
            return
        if name in self._group_names:
            QMessageBox.warning(
                self, "Duplicate",
                f"A group named \"{name}\" already exists.")
            return
        self._group_names.append(name)
        self.group_name_edit.clear()
        self._refresh_group_list()

    def _rename_group(self) -> None:
        row = self.group_list.currentRow()
        if row < 0:
            return
        old_name = self._group_names[row]
        new_name, ok = QInputDialog.getText(
            self, "Rename Group",
            "New name:", text=old_name)
        if not ok or not new_name.strip():
            return
        new_name = new_name.strip()
        if new_name == old_name:
            return
        if new_name in self._group_names:
            QMessageBox.warning(
                self, "Duplicate",
                f"A group named \"{new_name}\" already exists.")
            return

        # Update assignments
        for inst, grp in list(self._assignments.items()):
            if grp == old_name:
                self._assignments[inst] = new_name

        self._group_names[row] = new_name
        self._refresh_group_list()
        self._refresh_institution_tree()

    def _remove_group(self) -> None:
        row = self.group_list.currentRow()
        if row < 0:
            return
        name = self._group_names[row]

        reply = QMessageBox.question(
            self, "Remove Group",
            f"Remove group \"{name}\"?\n\n"
            "Institutions assigned to this group will become unassigned.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # Remove assignments for this group
        for inst in list(self._assignments.keys()):
            if self._assignments[inst] == name:
                del self._assignments[inst]

        self._group_names.pop(row)
        self._refresh_group_list()
        self._refresh_institution_tree()

    # ── Institution tree ──────────────────────────────────────────────────

    def _refresh_institution_tree(self) -> None:
        self.institution_tree.clear()
        # Collect all known institutions (from assignments + discovered)
        all_institutions = sorted(
            set(self._assignments.keys()),
            key=lambda x: x.lower())

        for inst in all_institutions:
            group = self._assignments.get(inst, "")
            item = QTreeWidgetItem([inst, group])
            if group:
                item.setForeground(1, QColor("#2ecc71"))
            else:
                item.setForeground(1, QColor("#969696"))
                item.setText(1, "(unassigned)")
            self.institution_tree.addTopLevelItem(item)

    def _assign_selected(self) -> None:
        group = self.assign_combo.currentText()
        if not group:
            QMessageBox.warning(
                self, "No Group",
                "Please create a group first or select one.")
            return

        selected = self.institution_tree.selectedItems()
        if not selected:
            return

        for item in selected:
            inst_name = item.text(0)
            self._assignments[inst_name] = group

        self._refresh_institution_tree()
        self._refresh_group_list()

    def _unassign_selected(self) -> None:
        selected = self.institution_tree.selectedItems()
        if not selected:
            return

        for item in selected:
            inst_name = item.text(0)
            if inst_name in self._assignments:
                del self._assignments[inst_name]

        self._refresh_institution_tree()
        self._refresh_group_list()

    def _add_institution_manually(self) -> None:
        name = self.manual_inst_edit.text().strip()
        if not name:
            return
        if name not in self._assignments:
            self._assignments[name] = ""  # unassigned
        self.manual_inst_edit.clear()
        self._refresh_institution_tree()

    # ── Query PACS for institutions ───────────────────────────────────────

    def _query_institutions(self) -> None:
        if not self.config.remote_nodes:
            QMessageBox.warning(
                self, "No Source PACS",
                "No source PACS configured. Please add one in Settings.")
            return

        days = self.query_days_spin.value()
        now = datetime.now()
        cutoff = now - timedelta(days=days)
        date_range = f"{cutoff.strftime('%Y%m%d')}-{now.strftime('%Y%m%d')}"

        self.btn_query.setEnabled(False)
        self.lbl_query_status.setText("Querying...")

        # Snapshot config data for thread safety.  Each source must
        # be queried with ITS OWN local destination AE — on multi-source
        # setups the local AE/port can differ per source (different
        # workstations), and reusing one source's local config for
        # everyone makes the C-FIND fire from the wrong calling AE.
        remotes = {k: (self.config.get_local_dict_for(k),
                       v.to_dict(), k)
                   for k, v in self.config.remote_nodes.items()}

        # Run the C-FIND on a background thread; ``run_in_background``
        # owns the weakref-shim that keeps a closed dialog from being
        # finalized on the worker thread (which used to segfault the
        # QDialog destructor).  ``run_in_background`` also marshals the
        # ``on_done`` callback back onto the GUI thread, so we can hand
        # it ``_on_query_results`` directly.
        def discover() -> Set[str]:
            discovered: Set[str] = set()
            for remote_key, (local_cfg, remote_cfg, name) in remotes.items():
                try:
                    ops = DicomOperations(local_cfg, remote_cfg, name)
                    # ``finally`` so the AE's threads are shut down
                    # before the object is dropped — see
                    # DicomOperations.close().
                    try:
                        names = ops.c_find_institution_names(
                            study_date=date_range)
                    finally:
                        ops.close()
                    discovered.update(names)
                except Exception as e:
                    logger.error(f"Query failed for {remote_key}: {e}")
            return discovered

        run_in_background(
            self, discover, self._on_query_results,
            label="filter_groups_query")

    def _on_query_results(self, discovered: set) -> None:
        """Handle query results on the main thread."""
        new_count = 0
        for inst in discovered:
            if inst not in self._assignments:
                self._assignments[inst] = ""
                new_count += 1

        self.btn_query.setEnabled(True)
        total = len(discovered)
        self.lbl_query_status.setText(
            f"Found {total} unique institutions "
            f"({new_count} new).")

        self._refresh_institution_tree()
        self._refresh_group_list()

    # ── Export / Import ────────────────────────────────────────────────

    def _export_groups(self) -> None:
        """Export the current (unsaved) filter groups to a JSON file."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Filter Groups",
            "filter_groups.json",
            "JSON Files (*.json);;All Files (*)")
        if not path:
            return
        try:
            # Shared helper — writes the same JSON shape as
            # AppConfig.export_filter_groups, but from the dialog's
            # unsaved working copies.
            write_filter_groups_json(path, self._group_names,
                                     self._assignments)

            QMessageBox.information(
                self, "Export Complete",
                f"Exported {len(self._group_names)} groups and "
                f"{len(self._assignments)} institutions to:\n{path}")
        except Exception as e:
            QMessageBox.critical(
                self, "Export Failed", f"Could not write file:\n{e}")

    def _import_groups(self) -> None:
        """Import filter groups from a JSON file into the dialog's working data."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Filter Groups", "",
            "JSON Files (*.json);;All Files (*)")
        if not path:
            return

        peeked = self._peek_import_file(path)
        if peeked is None:
            return
        imported_groups, imported_assignments = peeked

        # Ask whether to replace or merge
        reply = QMessageBox.question(
            self, "Import Mode",
            f"The file contains {len(imported_groups)} groups and "
            f"{len(imported_assignments)} institutions.\n\n"
            "Click \"Yes\" to MERGE with existing data "
            "(add new groups, update assignments).\n"
            "Click \"No\" to REPLACE all existing groups and assignments.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes)

        if reply == QMessageBox.Cancel:
            return

        merge = (reply == QMessageBox.Yes)

        # Shared pure helper — same merge/replace semantics as
        # AppConfig.import_filter_groups, applied to the dialog's
        # unsaved working copies (committed only on Save).
        self._group_names, self._assignments, summary = \
            merge_filter_group_data(
                self._group_names, self._assignments,
                imported_groups, imported_assignments, merge)

        self._report_import_result(summary, merge)

        self._refresh_group_list()
        self._refresh_institution_tree()

    def _peek_import_file(self, path: str):
        """Read + parse the import file and validate it is non-empty.

        Returns ``(imported_groups, imported_assignments)`` on success,
        or ``None`` after showing the appropriate error/warning dialog.
        """
        # Peek at file to show summary in confirmation dialog
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            QMessageBox.critical(
                self, "Import Failed",
                f"Could not read file:\n{e}")
            return None

        imported_groups = data.get("filter_group_names", [])
        imported_assignments = data.get("institution_assignments", {})

        if not imported_groups and not imported_assignments:
            QMessageBox.warning(
                self, "Import Empty",
                "The selected file contains no filter group data.")
            return None

        return imported_groups, imported_assignments

    def _report_import_result(self, summary: dict, merge: bool) -> None:
        """Show the merge/replace completion dialog for *summary*."""
        if merge:
            QMessageBox.information(
                self, "Import Complete",
                f"Merged: {summary['groups_added']} new groups, "
                f"{summary['institutions_added']} new institutions, "
                f"{summary['institutions_updated']} updated assignments.")
        else:
            QMessageBox.information(
                self, "Import Complete",
                f"Replaced with {summary['groups_added']} groups and "
                f"{summary['institutions_added']} institutions.")

    # ── Save ──────────────────────────────────────────────────────────

    def _save(self) -> None:
        # Clean up assignments: remove entries whose group no longer exists
        cleaned = {}
        for inst, grp in self._assignments.items():
            if grp and grp in self._group_names:
                cleaned[inst] = grp
            else:
                cleaned[inst] = ""  # keep institution, mark unassigned

        self.config.filter_group_names = list(self._group_names)
        self.config.institution_assignments = cleaned
        self.config.save()
        self.accept()
