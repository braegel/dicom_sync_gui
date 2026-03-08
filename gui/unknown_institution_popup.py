"""
Unknown Institution Popup — shown when a study arrives from an institution
that is not assigned to any filter group.

Features:
- Sound alert to draw attention
- Lists the unknown institution name
- Option to assign it to an existing group or dismiss
- Auto-loads the study regardless (unknown institutions are always downloaded)
"""

import logging
from typing import List, Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QDialogButtonBox,
)
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QFont

logger = logging.getLogger("dicom_sync")

# Try to import QSoundEffect for the alert sound
try:
    from PySide6.QtMultimedia import QSoundEffect
    HAS_MULTIMEDIA = True
except ImportError:
    HAS_MULTIMEDIA = False


def _play_alert():
    """Play system beep / alert sound."""
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            app.beep()
    except Exception:
        pass


class UnknownInstitutionPopup(QDialog):
    """
    Non-modal popup alerting the user about an unknown institution.
    The study is downloaded regardless; this just prompts for assignment.
    """

    def __init__(self, institution_name: str, group_names: List[str],
                 parent=None):
        super().__init__(parent)
        self.institution_name = institution_name
        self.group_names = group_names
        self.assigned_group: Optional[str] = None

        self.setWindowTitle("Unknown Institution Detected")
        self.setMinimumWidth(480)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowStaysOnTopHint)

        self._setup_ui()

        # Play alert sound
        _play_alert()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Warning icon + message
        header = QLabel(
            "\u26a0  Unknown Institution")
        header.setFont(QFont("", 14, QFont.Bold))
        header.setStyleSheet(
            "QLabel { color: #f39c12; padding: 4px; }")
        layout.addWidget(header)

        msg = QLabel(
            f"A study from an institution that is not assigned to any "
            f"filter group has been found:\n\n"
            f"\"{self.institution_name}\"\n\n"
            f"The study will be downloaded automatically. "
            f"You can assign this institution to a group now, or "
            f"manage it later in Settings \u2192 Manage Filter Groups.")
        msg.setWordWrap(True)
        msg.setStyleSheet("QLabel { padding: 4px; }")
        layout.addWidget(msg)

        # Assignment controls
        if self.group_names:
            assign_layout = QHBoxLayout()
            assign_layout.addWidget(QLabel("Assign to group:"))

            self.group_combo = QComboBox()
            self.group_combo.addItem("(do not assign)")
            for name in self.group_names:
                self.group_combo.addItem(name)
            self.group_combo.setMinimumWidth(200)
            assign_layout.addWidget(self.group_combo)

            assign_layout.addStretch()
            layout.addLayout(assign_layout)
        else:
            layout.addWidget(QLabel(
                "No filter groups configured yet. You can create groups "
                "in Settings \u2192 Manage Filter Groups."))
            self.group_combo = None

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_ok = QPushButton("  OK  ")
        btn_ok.setStyleSheet(
            "QPushButton { background: #2980b9; color: white; "
            "padding: 8px 24px; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background: #3498db; }")
        btn_ok.clicked.connect(self._on_ok)
        btn_layout.addWidget(btn_ok)

        layout.addLayout(btn_layout)

    def _on_ok(self):
        if self.group_combo and self.group_combo.currentIndex() > 0:
            self.assigned_group = self.group_combo.currentText()
        self.accept()
