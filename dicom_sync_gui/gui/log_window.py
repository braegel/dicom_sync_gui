"""
Detachable log viewer window for DICOM Sync GUI.
Accessible from the menu bar.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QPushButton, QHBoxLayout,
    QFileDialog, QLabel,
)
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont


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
        if path:
            with open(path, "w") as f:
                f.write(self.log_text.toPlainText())

    def _update_line_count(self):
        text = self.log_text.toPlainText()
        count = text.count("\n") + (1 if text else 0)
        self.lbl_lines.setText(f"{count} lines")
