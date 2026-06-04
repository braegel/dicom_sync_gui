"""
Tests for gui.log_window — log viewer save-to-file safety.
"""

from unittest.mock import patch

import pytest

from gui.log_window import LogWindow


@pytest.fixture
def win(qapp):
    w = LogWindow()
    yield w
    w.close()


class TestSaveToFile:
    """``_save_to_file`` must not crash the app when the user picks an
    unwritable destination (permission denied, disk full)."""

    def test_save_failure_shows_warning_does_not_raise(self, win):
        win.log_text.setPlainText("hello world\n")

        with patch("gui.log_window.QFileDialog.getSaveFileName",
                   return_value=("/nope/readonly.log", "")):
            with patch("builtins.open",
                       side_effect=PermissionError("Read-only file system")):
                with patch("gui.log_window.QMessageBox.warning") as mb:
                    # Must not raise; must show a message box instead.
                    win._save_to_file()
                    mb.assert_called_once()

    def test_save_success_writes_file(self, win, tmp_path):
        win.log_text.setPlainText("logged line\n")
        target = tmp_path / "out.log"

        with patch("gui.log_window.QFileDialog.getSaveFileName",
                   return_value=(str(target), "")):
            win._save_to_file()

        assert target.read_text() == "logged line\n"

    def test_save_cancelled_is_noop(self, win):
        # Empty path means the user cancelled the QFileDialog.
        with patch("gui.log_window.QFileDialog.getSaveFileName",
                   return_value=("", "")):
            with patch("builtins.open") as op:
                win._save_to_file()
                op.assert_not_called()
