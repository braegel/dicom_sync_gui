"""
Detachable log viewer window for DICOM Sync GUI.
Accessible from the menu bar.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QPushButton, QHBoxLayout,
    QFileDialog, QLabel, QMessageBox,
)
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont

# Maximum number of log lines retained in the viewer.  The GUI can run as a
# 24/7 service, so an unbounded QTextEdit would grow without limit and make
# every repaint/line-count more expensive over time.  With a maximum block
# count set on the underlying QTextDocument, Qt automatically drops the
# oldest lines as new ones scroll in, capping both memory and CPU cost.
MAX_LOG_LINES = 5000


class LogWindow(QWidget):
    """Floating window that shows the application log in real time."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DICOM Sync — Log")
        self.setWindowFlags(Qt.Window)
        self.setMinimumSize(700, 400)
        self.resize(800, 500)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        font = QFont()
        font.setFamilies(["Menlo", "Consolas", "Courier New"])
        font.setPointSize(10)
        self.log_text.setFont(font)
        # Cap retention: Qt silently discards the oldest blocks (lines) once
        # the limit is reached, so a long-running service cannot exhaust
        # memory through the log viewer.
        self.log_text.document().setMaximumBlockCount(MAX_LOG_LINES)
        layout.addWidget(self.log_text, 1)

        # Bottom bar
        bottom = QHBoxLayout()
        self.lbl_lines = QLabel("0 lines")
        bottom.addWidget(self.lbl_lines)
        bottom.addStretch()

        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear)
        bottom.addWidget(btn_clear)

        btn_save = QPushButton("Save to File...")
        btn_save.clicked.connect(self._save_to_file)
        bottom.addWidget(btn_save)

        layout.addLayout(bottom)

    @Slot(str)
    def append_log(self, message: str):
        """Append a log line. Thread-safe if called via signal/slot."""
        self.log_text.append(message)
        self._update_line_count()

    def _clear(self):
        self.log_text.clear()
        self._update_line_count()

    def _save_to_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Log", "dicom_sync.log", "Text Files (*.log *.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_text.toPlainText())
        except OSError as e:
            # Permission denied, disk full, read-only FS, etc.  Surface
            # the failure as a dialog instead of crashing to the console.
            QMessageBox.warning(
                self, "Save failed",
                f"Could not save log to {path}:\n{e}")

    def _update_line_count(self):
        # Use the document's block count (O(1)) instead of scanning the full
        # text with toPlainText() (O(document size)), which made appending N
        # lines quadratic overall.  Each block corresponds to one displayed
        # line, so the value matches the old newline-based count.
        #
        # Once MAX_LOG_LINES is exceeded the oldest blocks are dropped, so
        # this is the number of lines *retained* (it plateaus at
        # MAX_LOG_LINES), not a lifetime total.
        doc = self.log_text.document()
        # An empty QTextDocument still reports blockCount() == 1; keep
        # showing "0 lines" for an empty viewer as before.
        count = 0 if doc.isEmpty() else doc.blockCount()
        self.lbl_lines.setText(f"{count} lines")
