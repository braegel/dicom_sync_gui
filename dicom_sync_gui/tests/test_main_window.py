"""
Tests for gui.main_window — MainWindow menu, per-source service lifecycle, signal wiring.
"""

from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow
from core.config import AppConfig, PacsNode
from core.transfer_engine import TransferEngine


# ═══════════════════════════════════════════════════════════════════════════
# MainWindow — initialization
# ═══════════════════════════════════════════════════════════════════════════

class TestMainWindowInit:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)
        self.config = populated_config

    def test_window_title(self):
        assert self.win.windowTitle() == "DICOM Sync"

    def test_has_tab_widget(self):
        assert self.win.tab_widget is not None

    def test_has_dashboards(self):
        assert len(self.win.dashboards) == 2  # ct and mri
        assert "ct" in self.win.dashboards
        assert "mri" in self.win.dashboards

    def test_has_log_window(self):
        assert self.win.log_window is not None

    def test_engines_starts_empty(self):
        assert self.win.engines == {}

    def test_storage_scp_starts_none(self):
        assert self.win.storage_scp is None

    def test_statusbar_ready(self):
        assert "Ready" in self.win.statusBar().currentMessage()

    def test_tab_count_matches_remotes(self):
        assert self.win.tab_widget.count() == 2


# ═══════════════════════════════════════════════════════════════════════════
# MainWindow — no sources placeholder
# ═══════════════════════════════════════════════════════════════════════════

class TestMainWindowNoSources:

    @pytest.fixture(autouse=True)
    def _create(self, default_config, qapp):
        self.win = MainWindow(default_config)

    def test_placeholder_tab_shown(self):
        assert self.win.tab_widget.count() == 1
        assert "No Sources" in self.win.tab_widget.tabText(0)

    def test_dashboards_empty(self):
        assert len(self.win.dashboards) == 0


# ═══════════════════════════════════════════════════════════════════════════
# MainWindow — menu structure
# ═══════════════════════════════════════════════════════════════════════════

class TestMainWindowMenu:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    def test_settings_menu_exists(self):
        menubar = self.win.menuBar()
        menus = [a.text() for a in menubar.actions()]
        assert "Settings" in menus

    def test_view_menu_exists(self):
        menubar = self.win.menuBar()
        menus = [a.text() for a in menubar.actions()]
        assert "View" in menus

    def test_tools_menu_exists(self):
        menubar = self.win.menuBar()
        menus = [a.text() for a in menubar.actions()]
        assert "Tools" in menus

    def test_settings_menu_has_pacs_config(self):
        menubar = self.win.menuBar()
        for action in menubar.actions():
            if action.text() == "Settings":
                menu = action.menu()
                texts = [a.text() for a in menu.actions()]
                assert "PACS Configuration..." in texts
                return
        pytest.fail("Settings menu not found")

    def test_settings_menu_has_filter_groups(self):
        menubar = self.win.menuBar()
        for action in menubar.actions():
            if action.text() == "Settings":
                menu = action.menu()
                texts = [a.text() for a in menu.actions()]
                assert "Manage Filter Groups..." in texts
                return
        pytest.fail("Settings menu not found")

    def test_settings_menu_has_quit(self):
        menubar = self.win.menuBar()
        for action in menubar.actions():
            if action.text() == "Settings":
                menu = action.menu()
                texts = [a.text() for a in menu.actions()]
                assert "Quit" in texts
                return
        pytest.fail("Settings menu not found")

    def test_view_menu_has_log(self):
        menubar = self.win.menuBar()
        for action in menubar.actions():
            if action.text() == "View":
                menu = action.menu()
                texts = [a.text() for a in menu.actions()]
                assert "Show Log Window" in texts
                return
        pytest.fail("View menu not found")

    def test_tools_menu_has_echo(self):
        menubar = self.win.menuBar()
        for action in menubar.actions():
            if action.text() == "Tools":
                menu = action.menu()
                texts = [a.text() for a in menu.actions()]
                assert "C-ECHO Test..." in texts
                return
        pytest.fail("Tools menu not found")


# ═══════════════════════════════════════════════════════════════════════════
# MainWindow — per-source service start/stop
# ═══════════════════════════════════════════════════════════════════════════

class TestMainWindowService:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)
        self.config = populated_config

    @patch.object(MainWindow, '_ensure_storage_scp')
    @patch("gui.main_window.TransferEngine")
    def test_start_creates_engine_for_source(self, MockEngine, mock_scp):
        mock_engine = MagicMock()
        mock_engine.signals = MagicMock()
        MockEngine.return_value = mock_engine

        self.win._on_start_service(
            "ct", {"hours": 6, "max_images": 500, "sync_interval": 120})

        MockEngine.assert_called_once_with(self.config, "ct")
        mock_engine.start.assert_called_once_with(
            hours=6, max_images=500, sync_interval=120)

    @patch.object(MainWindow, '_ensure_storage_scp')
    @patch("gui.main_window.TransferEngine")
    def test_start_connects_signals(self, MockEngine, mock_scp):
        mock_engine = MagicMock()
        mock_signals = MagicMock()
        mock_engine.signals = mock_signals
        MockEngine.return_value = mock_engine

        self.win._on_start_service(
            "ct", {"hours": 3, "max_images": 0, "sync_interval": 60})

        mock_signals.queue_updated.connect.assert_called_once()
        mock_signals.series_started.connect.assert_called_once()
        mock_signals.service_stopped.connect.assert_called_once()
        mock_signals.unknown_institution.connect.assert_called_once()

    def test_stop_without_engine(self):
        # Should not crash
        self.win._on_stop_service("ct")

    def test_stop_with_running_engine(self):
        mock_engine = MagicMock()
        mock_engine.is_running = True
        self.win.engines["ct"] = mock_engine
        self.win._on_stop_service("ct")
        mock_engine.stop.assert_called_once()

    def test_on_service_stopped_updates_dashboard(self):
        self.win._on_service_stopped("ct")
        assert self.win.dashboards["ct"].btn_start.isEnabled()
        assert "stopped" in self.win.statusBar().currentMessage().lower()

    def test_start_invalid_key_does_nothing(self):
        # Should not crash
        self.win._on_start_service(
            "nonexistent", {"hours": 3, "max_images": 0, "sync_interval": 60})


# ═══════════════════════════════════════════════════════════════════════════
# MainWindow — settings dialog
# ═══════════════════════════════════════════════════════════════════════════

class TestMainWindowSettings:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    @patch("gui.main_window.QMessageBox.information")
    def test_open_settings_blocked_when_running(self, mock_info):
        mock_engine = MagicMock()
        mock_engine.is_running = True
        self.win.engines["ct"] = mock_engine
        self.win._open_settings()
        mock_info.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# MainWindow — log window
# ═══════════════════════════════════════════════════════════════════════════

class TestMainWindowLog:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    def test_log_appends_to_window(self):
        self.win._log("Test message")
        text = self.win.log_window.log_text.toPlainText()
        assert "Test message" in text

    def test_log_has_timestamp(self):
        self.win._log("Timestamped message")
        text = self.win.log_window.log_text.toPlainText()
        # Should contain HH:MM:SS pattern
        assert "]" in text and "[" in text

    def test_show_log_window(self):
        self.win._show_log_window()
        assert self.win.log_window.isVisible()


# ═══════════════════════════════════════════════════════════════════════════
# MainWindow — unknown institution handling
# ═══════════════════════════════════════════════════════════════════════════

class TestMainWindowUnknownInstitution:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    @patch("gui.main_window.UnknownInstitutionPopup")
    def test_popup_created_with_correct_args(self, MockPopup):
        mock_popup = MagicMock()
        mock_popup.exec.return_value = 0  # Rejected
        MockPopup.return_value = mock_popup

        self.win._on_unknown_institution("New Hospital")
        MockPopup.assert_called_once_with(
            "New Hospital",
            self.win.config.filter_group_names,
            self.win,
        )

    @patch("gui.main_window.UnknownInstitutionPopup")
    def test_assignment_saved_on_accept(self, MockPopup):
        from PySide6.QtWidgets import QDialog
        mock_popup = MagicMock()
        mock_popup.exec.return_value = MockPopup.Accepted
        mock_popup.assigned_group = "Group A"
        MockPopup.return_value = mock_popup

        self.win._on_unknown_institution("Brand New Hospital")

        assert self.win.config.institution_assignments[
            "Brand New Hospital"] == "Group A"


# ═══════════════════════════════════════════════════════════════════════════
# MainWindow — C-ECHO test
# ═══════════════════════════════════════════════════════════════════════════

class TestMainWindowCEcho:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    @patch("gui.main_window.QMessageBox.warning")
    def test_echo_no_remotes_warns(self, mock_warning):
        self.win.config.remote_nodes = {}
        self.win._test_echo()
        mock_warning.assert_called_once()

    @patch("gui.main_window.QApplication.processEvents")
    @patch("gui.main_window.QMessageBox.information")
    @patch("gui.main_window.DicomOperations")
    def test_echo_runs_for_all_remotes(
        self, MockOps, mock_info, mock_events
    ):
        mock_ops = MagicMock()
        mock_ops.c_echo.return_value = True
        MockOps.return_value = mock_ops

        self.win._test_echo()

        # Should call c_echo for remote + local
        assert mock_ops.c_echo.call_count >= 2
        mock_info.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# MainWindow — close event
# ═══════════════════════════════════════════════════════════════════════════

class TestMainWindowClose:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    def test_close_no_engine(self):
        event = MagicMock()
        self.win.closeEvent(event)
        event.accept.assert_called_once()

    @patch("gui.main_window.QMessageBox.question",
           return_value=16384)  # Yes
    def test_close_with_running_engine_confirms(self, mock_question):
        mock_engine = MagicMock()
        mock_engine.is_running = True
        self.win.engines["ct"] = mock_engine
        event = MagicMock()
        self.win.closeEvent(event)
        mock_engine.stop.assert_called_once()
        event.accept.assert_called_once()

    @patch("gui.main_window.QMessageBox.question",
           return_value=65536)  # No
    def test_close_with_running_engine_cancel(self, mock_question):
        mock_engine = MagicMock()
        mock_engine.is_running = True
        self.win.engines["ct"] = mock_engine
        event = MagicMock()
        self.win.closeEvent(event)
        event.ignore.assert_called_once()
