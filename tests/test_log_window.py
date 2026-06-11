"""
Tests for gui.log_window — log viewer save-to-file safety and
line-count / retention-cap behavior.
"""

from unittest.mock import patch

import pytest

from gui.log_window import LogWindow, MAX_LOG_LINES


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


class TestLineCount:
    """The "N lines" label must track the document's block count cheaply
    (no full-text scans) while preserving the old display behavior."""

    def test_empty_shows_zero_lines(self, win):
        # An empty QTextDocument reports blockCount() == 1, but the label
        # must still read "0 lines" for an empty viewer.
        assert win.lbl_lines.text() == "0 lines"

    def test_append_increments_count(self, win):
        win.append_log("first")
        assert win.lbl_lines.text() == "1 lines"
        win.append_log("second")
        assert win.lbl_lines.text() == "2 lines"

    def test_clear_resets_count(self, win):
        win.append_log("first")
        win.append_log("second")
        win._clear()
        assert win.lbl_lines.text() == "0 lines"

    def test_retention_cap_is_configured(self, win):
        # The widget must cap retention so a 24/7 service cannot grow the
        # log document without bound.
        assert win.log_text.document().maximumBlockCount() == MAX_LOG_LINES

    def test_appending_past_cap_drops_oldest_lines(self, win):
        # Use a small cap so the test stays fast; Qt drops the oldest
        # blocks automatically once the limit is reached.
        cap = 10
        win.log_text.document().setMaximumBlockCount(cap)

        for i in range(cap * 3):
            win.append_log(f"line {i}")

        doc = win.log_text.document()
        assert doc.blockCount() <= cap
        # The label shows the lines *retained* (plateaus at the cap),
        # not a lifetime total.
        assert win.lbl_lines.text() == f"{cap} lines"
        # Oldest lines scrolled off; newest line is still present.
        text = win.log_text.toPlainText()
        assert "line 0" not in text
        assert f"line {cap * 3 - 1}" in text
