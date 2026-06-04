"""
Priority Series Dialog — manage per-source-PACS priority series terms.

The user picks a source PACS in the top combo and edits its ordered
list of priority terms in the table.  Each row has a Term and a
Regex flag.  Save commits every source's working copy back to the
matching ``PacsNode.priority_series_terms`` and persists the config.
"""

import logging
import re
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.config import AppConfig, default_priority_terms
from gui.styles import BTN_BLUE, BTN_GREEN_LARGE, BTN_RED, LBL_HINT_STYLE

logger = logging.getLogger("dicom_sync")


class PrioritySeriesDialog(QDialog):
    """Edit the per-source priority series term list."""

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        # Deep-copied working list per source so cancel discards
        # cleanly and switching sources mid-edit doesn't lose state.
        self._working_lists: Dict[str, List[Dict]] = {
            key: [dict(entry) for entry in node.priority_series_terms]
            for key, node in config.remote_nodes.items()
        }
        self._current_key: str = ""

        self.setWindowTitle("Manage Priority Series")
        self.setMinimumSize(680, 460)

        self._setup_ui()
        # Render the first source's list (if any are configured).
        if self.source_combo.count() > 0:
            self.source_combo.setCurrentIndex(0)
            self._on_source_changed(0)

    # ── UI ────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── Source picker ──
        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Source PACS:"))
        self.source_combo = QComboBox()
        for key, node in self.config.remote_nodes.items():
            label = f"{key} — {node.name}" if node.name else key
            self.source_combo.addItem(label, key)
        self.source_combo.currentIndexChanged.connect(
            self._on_source_changed)
        picker_row.addWidget(self.source_combo, 1)
        layout.addLayout(picker_row)

        # Hint label
        hint = QLabel(
            "Series whose description matches a term promote their "
            "entire study to the top of the download queue.  List "
            "order is priority order (top = highest).  Tick the "
            "Regex column to treat the term as a regular expression.")
        hint.setWordWrap(True)
        hint.setStyleSheet(LBL_HINT_STYLE)
        layout.addWidget(hint)

        # ── Table + side buttons ──
        body = QHBoxLayout()

        self.terms_table = QTableWidget()
        self.terms_table.setColumnCount(2)
        self.terms_table.setHorizontalHeaderLabels(["Term", "Regex"])
        self.terms_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.terms_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self.terms_table.verticalHeader().setVisible(False)
        self.terms_table.setSelectionBehavior(
            QAbstractItemView.SelectRows)
        self.terms_table.setSelectionMode(
            QAbstractItemView.SingleSelection)
        body.addWidget(self.terms_table, 1)

        side = QVBoxLayout()
        self.btn_add = QPushButton("Add")
        self.btn_add.setStyleSheet(BTN_BLUE)
        self.btn_add.setToolTip(
            "Append a blank row to the bottom of the list.")
        self.btn_add.clicked.connect(self._on_add_row)
        side.addWidget(self.btn_add)

        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setStyleSheet(BTN_RED)
        self.btn_remove.setToolTip(
            "Delete the selected row from this source's list.")
        self.btn_remove.clicked.connect(self._on_remove_row)
        side.addWidget(self.btn_remove)

        self.btn_up = QPushButton("Move Up")
        self.btn_up.setStyleSheet(BTN_BLUE)
        self.btn_up.setToolTip(
            "Move the selected term one position up (= higher priority).")
        self.btn_up.clicked.connect(self._on_move_up)
        side.addWidget(self.btn_up)

        self.btn_down = QPushButton("Move Down")
        self.btn_down.setStyleSheet(BTN_BLUE)
        self.btn_down.setToolTip(
            "Move the selected term one position down "
            "(= lower priority).")
        self.btn_down.clicked.connect(self._on_move_down)
        side.addWidget(self.btn_down)

        self.btn_reset = QPushButton("Reset to Defaults")
        self.btn_reset.setStyleSheet(BTN_BLUE)
        self.btn_reset.setToolTip(
            "Replace this source's list with the bundled defaults "
            "(cct, cta, ct-a, angio, …).  Other sources unaffected.")
        self.btn_reset.clicked.connect(self._on_reset_to_defaults)
        side.addWidget(self.btn_reset)

        side.addStretch()
        body.addLayout(side)

        layout.addLayout(body, 1)

        # ── Save / Cancel ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_save = QPushButton("  Save  ")
        btn_save.setStyleSheet(BTN_GREEN_LARGE)
        btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(btn_save)
        layout.addLayout(btn_row)

    # ── Source switching ─────────────────────────────────────────────────

    def _close_pending_edit(self):
        """Commit any in-progress cell edit so the typed text lands
        in the item model before any subsequent ``_collect_table``
        read.  Without this, a half-edited Term cell is still owned
        by the editor delegate when we switch sources, and the edit
        is silently discarded.

        The table only ever uses Qt's *transient* default editor (no
        ``openPersistentEditor`` call site), so ``closePersistentEditor``
        is a no-op here.  The transient editor lives as a child of
        the table while open — find it, route its value back through
        the delegate's ``commitData`` signal, then close.  ``focusWidget``
        would be more direct but does not update in headless contexts.
        """
        if self.terms_table.state() != QAbstractItemView.EditingState:
            return
        editor = self.terms_table.findChild(QLineEdit)
        if editor is None:
            return
        delegate = self.terms_table.itemDelegate()
        delegate.commitData.emit(editor)
        delegate.closeEditor.emit(editor)

    def _on_source_changed(self, index: int):
        self._close_pending_edit()

        # Persist current source's edits back into the working dict
        # before rendering the new source.
        if self._current_key:
            self._working_lists[self._current_key] = self._collect_table()
        if index < 0:
            self._current_key = ""
            return
        new_key = self.source_combo.itemData(index)
        self._current_key = new_key
        self._render_table(self._working_lists.get(new_key, []))

    def _render_table(self, entries: List[Dict]):
        self.terms_table.setRowCount(0)
        for entry in entries:
            self._append_table_row(
                entry.get("term", ""),
                bool(entry.get("is_regex")),
            )

    def _append_table_row(self, term: str, is_regex: bool):
        row = self.terms_table.rowCount()
        self.terms_table.insertRow(row)
        self.terms_table.setItem(row, 0, QTableWidgetItem(term))

        # Centre the checkbox in the Regex column by wrapping it in a
        # layout-only QWidget.  Putting the QCheckBox directly into
        # setCellWidget would left-align it because the cell does not
        # apply any horizontal alignment of its own.  ``cb_wrap`` is
        # the actual cell widget; ``_collect_table`` looks the
        # QCheckBox up via ``findChild`` so the wrap is transparent
        # to callers.
        cb_wrap = QWidget()
        cb_layout = QHBoxLayout(cb_wrap)
        cb_layout.setContentsMargins(0, 0, 0, 0)
        cb_layout.setAlignment(Qt.AlignCenter)
        cb = QCheckBox()
        cb.setChecked(is_regex)
        cb_layout.addWidget(cb)
        self.terms_table.setCellWidget(row, 1, cb_wrap)

    def _regex_checkbox(self, row: int) -> Optional[QCheckBox]:
        """Return the QCheckBox living inside the regex cell-widget
        wrapper of *row*, or ``None`` if the cell is empty."""
        widget = self.terms_table.cellWidget(row, 1)
        if widget is None:
            return None
        if isinstance(widget, QCheckBox):
            return widget  # belt-and-suspenders for legacy rows
        return widget.findChild(QCheckBox)

    def _collect_table(self) -> List[Dict]:
        out = []
        for row in range(self.terms_table.rowCount()):
            term_item = self.terms_table.item(row, 0)
            term = term_item.text() if term_item is not None else ""
            cb = self._regex_checkbox(row)
            is_regex = bool(cb.isChecked()) if cb is not None else False
            out.append({"term": term, "is_regex": is_regex})
        return out

    # ── Row ops ──────────────────────────────────────────────────────────

    def _selected_row(self) -> int:
        rows = self.terms_table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _on_add_row(self):
        """Append a blank row.

        Selects the new row but does NOT enter the cell's edit mode
        — multiple consecutive ``Add`` clicks would otherwise tab the
        user in and out of edit mode and fragment the focus flow.
        The user clicks the row to type.
        """
        self._append_table_row("", False)
        new_row = self.terms_table.rowCount() - 1
        self.terms_table.selectRow(new_row)

    def _on_remove_row(self):
        row = self._selected_row()
        if row < 0:
            return
        self.terms_table.removeRow(row)

    def _row_payload(self, row: int) -> tuple[str, bool]:
        """Return ``(term, is_regex)`` for *row*, defensively."""
        t = self.terms_table
        item = t.item(row, 0)
        term = item.text() if item else ""
        cb = self._regex_checkbox(row)
        is_regex = bool(cb.isChecked()) if cb is not None else False
        return term, is_regex

    def _set_row_payload(self, row: int, term: str, is_regex: bool):
        """Write *term* + *is_regex* into *row*, creating the term
        item if it doesn't yet exist."""
        t = self.terms_table
        item = t.item(row, 0)
        if item is None:
            t.setItem(row, 0, QTableWidgetItem(term))
        else:
            item.setText(term)
        cb = self._regex_checkbox(row)
        if cb is not None:
            cb.setChecked(is_regex)

    def _swap_rows(self, a: int, b: int):
        """Swap the two rows in place — avoids re-creating every
        QCheckBox widget every time the user nudges a row up or
        down, which would otherwise churn the GC for no reason."""
        t = self.terms_table
        n = t.rowCount()
        if not (0 <= a < n and 0 <= b < n) or a == b:
            return
        payload_a = self._row_payload(a)
        payload_b = self._row_payload(b)
        self._set_row_payload(a, *payload_b)
        self._set_row_payload(b, *payload_a)
        # Drop the prior selection before re-selecting so any listener
        # on ``currentRowChanged`` is not invoked over a half-swapped
        # snapshot of the two rows.
        t.clearSelection()
        t.selectRow(b)

    def _on_move_up(self):
        row = self._selected_row()
        if row > 0:
            self._swap_rows(row, row - 1)

    def _on_move_down(self):
        row = self._selected_row()
        if 0 <= row < self.terms_table.rowCount() - 1:
            self._swap_rows(row, row + 1)

    def _on_reset_to_defaults(self):
        self._render_table(default_priority_terms())

    # ── Save / Cancel ────────────────────────────────────────────────────

    def _on_save(self):
        # Flush the currently-displayed list back into the working dict.
        if self._current_key:
            self._working_lists[self._current_key] = self._collect_table()

        # Validate before committing anything.  Each validator returns
        # either ``None`` (OK) or a tuple ``(key, row_idx, title,
        # message)`` describing the first offending row.
        for validator in (
            self._find_whitespace_only_term,
            self._find_invalid_regex,
        ):
            problem = validator()
            if problem is not None:
                key, row_idx, title, message = problem
                QMessageBox.warning(self, title, message)
                self._show_source(key)
                self.terms_table.selectRow(row_idx)
                return

        self._commit_to_config()
        self.config.save()
        self.accept()

    def _find_whitespace_only_term(self):
        """Return ``(key, row_idx, title, message)`` for the first
        non-empty whitespace-only term, or ``None`` if all clean."""
        for key, entries in self._working_lists.items():
            if not entries:
                continue
            for row_idx, entry in enumerate(entries):
                term = entry.get("term", "")
                if term and not term.strip():
                    return (
                        key, row_idx,
                        "Empty Term",
                        f"Source \"{key}\", row {row_idx + 1}: "
                        f"the term contains only whitespace.\n\n"
                        f"Remove the row or enter a real term before "
                        f"saving.")
        return None

    def _find_invalid_regex(self):
        """Return ``(key, row_idx, title, message)`` for the first
        regex row that fails to compile, or ``None`` if all clean."""
        for key, entries in self._working_lists.items():
            if not entries:
                continue
            for row_idx, entry in enumerate(entries):
                term = entry.get("term", "")
                if not term or not entry.get("is_regex"):
                    continue
                try:
                    re.compile(term, re.IGNORECASE)
                except re.error as e:
                    same_source = (key == self._current_key)
                    context = ("" if same_source else
                               f"\n\n(The dialog has switched to "
                               f"source \"{key}\" to highlight the "
                               f"offending row — your edits on the "
                               f"previous source are preserved.)")
                    return (
                        key, row_idx,
                        "Invalid Regular Expression",
                        f"Source \"{key}\", row {row_idx + 1}: "
                        f"\"{term}\" is not a valid regex.\n\n"
                        f"{e}{context}")
        return None

    def _commit_to_config(self):
        """Write every working list back onto its ``PacsNode``, dropping
        rows whose term is the empty string (no input from the user)."""
        for key, entries in self._working_lists.items():
            cleaned = [
                {"term": e.get("term", ""),
                 "is_regex": bool(e.get("is_regex"))}
                for e in entries
                if e.get("term", "") != ""
            ]
            node = self.config.remote_nodes.get(key)
            if node is not None:
                node.priority_series_terms = cleaned

    def _show_source(self, remote_key: str):
        """Programmatically switch the combo to *remote_key*.

        Renders the target source's table without re-triggering the
        ``_on_source_changed`` flush of the currently-displayed table
        — useful when called from the save validator after the user
        has already corrected an offending row.
        """
        for i in range(self.source_combo.count()):
            if self.source_combo.itemData(i) == remote_key:
                self.source_combo.blockSignals(True)
                self.source_combo.setCurrentIndex(i)
                self.source_combo.blockSignals(False)
                self._current_key = remote_key
                self._render_table(
                    self._working_lists.get(remote_key, []))
                return
