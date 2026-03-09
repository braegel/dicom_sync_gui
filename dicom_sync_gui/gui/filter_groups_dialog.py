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
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QGroupBox, QListWidget, QListWidgetItem, QComboBox,
    QSpinBox, QMessageBox, QSplitter, QInputDialog, QHeaderView,
    QTreeWidget, QTreeWidgetItem, QAbstractItemView, QApplication,
    QProgressDialog, QFileDialog,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QColor

from core.config import AppConfig
from core.dicom_ops import DicomOperations
from gui.styles import BTN_GREEN, BTN_GREEN_LARGE, BTN_RED, BTN_BLUE, BTN_BLUE_LARGE

logger = logging.getLogger("dicom_sync")


class FilterGroupsDialog(QDialog):
    """Dialog for creating and managing institution filter groups."""

    def __init__(self, config: AppConfig, parent=None):
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

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── Top: Query institutions from PACS ──
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
        layout.addWidget(query_group)

        # ── Middle: splitter with groups on left, institutions on right ──
        splitter = QSplitter(Qt.Horizontal)

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
        splitter.addWidget(left_widget)

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
        splitter.addWidget(right_widget)

        splitter.setSizes([300, 600])
        layout.addWidget(splitter, 1)

        # ── Bottom: Export / Import / Save / Cancel ──
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

        layout.addLayout(btn_layout)

    # ── Group management ──────────────────────────────────────────────────

    def _refresh_group_list(self):
        self.group_list.clear()
        for name in self._group_names:
            count = sum(
                1 for g in self._assignments.values() if g == name)
            self.group_list.addItem(f"{name}  ({count} institutions)")
        self._refresh_assign_combo()

    def _refresh_assign_combo(self):
        current = self.assign_combo.currentText()
        self.assign_combo.clear()
        for name in self._group_names:
            self.assign_combo.addItem(name)
        idx = self.assign_combo.findText(current)
        if idx >= 0:
            self.assign_combo.setCurrentIndex(idx)

    def _on_group_selected(self, row):
        enabled = row >= 0
        self.btn_rename_group.setEnabled(enabled)
        self.btn_remove_group.setEnabled(enabled)

    def _add_group(self):
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

    def _rename_group(self):
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

    def _remove_group(self):
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

    def _refresh_institution_tree(self):
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

    def _assign_selected(self):
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

    def _unassign_selected(self):
        selected = self.institution_tree.selectedItems()
        if not selected:
            return

        for item in selected:
            inst_name = item.text(0)
            if inst_name in self._assignments:
                del self._assignments[inst_name]

        self._refresh_institution_tree()
        self._refresh_group_list()

    def _add_institution_manually(self):
        name = self.manual_inst_edit.text().strip()
        if not name:
            return
        if name not in self._assignments:
            self._assignments[name] = ""  # unassigned
        self.manual_inst_edit.clear()
        self._refresh_institution_tree()

    # ── Query PACS for institutions ───────────────────────────────────────

    def _query_institutions(self):
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
        QApplication.processEvents()

        discovered: Set[str] = set()

        for remote_key, remote_node in self.config.remote_nodes.items():
            try:
                ops = DicomOperations(
                    self.config.get_local_dict(),
                    remote_node.to_dict(),
                    remote_key,
                )
                self.lbl_query_status.setText(
                    f"Querying {remote_key} (study + series level)...")
                QApplication.processEvents()

                # Use dedicated method that falls back to series-level
                # queries when InstitutionName is not at study level
                names = ops.c_find_institution_names(
                    study_date=date_range)
                discovered.update(names)

            except Exception as e:
                logger.error(f"Query failed for {remote_key}: {e}")

        # Merge discovered institutions with existing ones
        new_count = 0
        for inst in discovered:
            if inst not in self._assignments:
                self._assignments[inst] = ""  # unassigned
                new_count += 1

        self.btn_query.setEnabled(True)
        total = len(discovered)
        self.lbl_query_status.setText(
            f"Found {total} unique institutions "
            f"({new_count} new).")

        self._refresh_institution_tree()
        self._refresh_group_list()

    # ── Export / Import ────────────────────────────────────────────────

    def _export_groups(self):
        """Export the current (unsaved) filter groups to a JSON file."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Filter Groups",
            "filter_groups.json",
            "JSON Files (*.json);;All Files (*)")
        if not path:
            return
        try:
            # Temporarily apply working copies so config can serialise them
            orig_names = self.config.filter_group_names
            orig_assign = self.config.institution_assignments
            self.config.filter_group_names = list(self._group_names)
            self.config.institution_assignments = dict(self._assignments)
            self.config.export_filter_groups(path)
            self.config.filter_group_names = orig_names
            self.config.institution_assignments = orig_assign

            QMessageBox.information(
                self, "Export Complete",
                f"Exported {len(self._group_names)} groups and "
                f"{len(self._assignments)} institutions to:\n{path}")
        except Exception as e:
            QMessageBox.critical(
                self, "Export Failed", f"Could not write file:\n{e}")

    def _import_groups(self):
        """Import filter groups from a JSON file into the dialog's working data."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Filter Groups", "",
            "JSON Files (*.json);;All Files (*)")
        if not path:
            return

        # Peek at file to show summary in confirmation dialog
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            QMessageBox.critical(
                self, "Import Failed",
                f"Could not read file:\n{e}")
            return

        imported_groups = data.get("filter_group_names", [])
        imported_assignments = data.get("institution_assignments", {})

        if not imported_groups and not imported_assignments:
            QMessageBox.warning(
                self, "Import Empty",
                "The selected file contains no filter group data.")
            return

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

        # Delegate to config (operates on working copies)
        orig_names = self.config.filter_group_names
        orig_assign = self.config.institution_assignments
        self.config.filter_group_names = list(self._group_names)
        self.config.institution_assignments = dict(self._assignments)

        summary = self.config.import_filter_groups(path, merge=merge)

        # Read back the result into working copies
        self._group_names = list(self.config.filter_group_names)
        self._assignments = dict(self.config.institution_assignments)

        # Restore config originals (will be persisted on Save)
        self.config.filter_group_names = orig_names
        self.config.institution_assignments = orig_assign

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

        self._refresh_group_list()
        self._refresh_institution_tree()

    # ── Save ──────────────────────────────────────────────────────────

    def _save(self):
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
