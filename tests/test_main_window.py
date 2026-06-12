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

    def test_storage_scps_starts_empty(self):
        assert self.win.storage_scps == {}

    def test_statusbar_ready(self):
        assert "Ready" in self.win.statusBar().currentMessage()

    def test_tab_count_matches_remotes(self):
        assert self.win.tab_widget.count() == 2

    def test_completions_window_uses_config_language(
            self, populated_config, qapp):
        """MainWindow must pass config.language into the
        LiveCompletionsWindow so the Copy button produces localized
        clipboard text."""
        populated_config.language = "de"
        win = MainWindow(populated_config)
        assert win.completions_window._language == "de"

    def test_study_completed_live_passes_image_count(self):
        """_on_study_completed_live must sum transferred_images across
        all done series of the study and pass it to add_completion
        so the Images and img/min columns can be populated."""
        engine = MagicMock()
        engine.queue_snapshot.return_value = [
            MagicMock(study_uid="S1", status="done",
                      transferred_images=200, patient_name="A",
                      study_description="CT", study_time="080000",
                      institution_name="H"),
            MagicMock(study_uid="S1", status="done",
                      transferred_images=250, patient_name="A",
                      study_description="CT", study_time="080000",
                      institution_name="H"),
            MagicMock(study_uid="S2", status="done",
                      transferred_images=999, patient_name="B",
                      study_description="MR", study_time="090000",
                      institution_name="H"),
        ]
        engine.pop_study_wall_clock.return_value = 30.0

        with patch.object(
                self.win.completions_window, "add_completion") as mock_add:
            self.win._on_study_completed_live(
                engine, "S1", fully_complete=True)

        mock_add.assert_called_once()
        kwargs = mock_add.call_args.kwargs
        assert kwargs["image_count"] == 450, (
            "image_count must be the sum of transferred_images "
            "for done series of this study only")
        assert kwargs["download_duration_seconds"] == 30.0

    def test_study_completed_live_forwards_threshold_below_min_images(self):
        """Studies whose total downloaded image count is below
        MIN_IMAGES_FOR_COMPLETIONS_ENTRY (10) must pass the threshold
        through to ``add_completion`` — the LiveCompletionsWindow
        applies the cutoff against the cumulative running total so
        late-arriving second-wave images can still create the row."""
        engine = MagicMock()
        engine.queue_snapshot.return_value = [
            MagicMock(study_uid="S1", status="done",
                      transferred_images=5, patient_name="A",
                      study_description="CT", study_time="080000",
                      institution_name="H"),
            MagicMock(study_uid="S1", status="done",
                      transferred_images=4, patient_name="A",
                      study_description="CT", study_time="080000",
                      institution_name="H"),
        ]
        engine.pop_study_wall_clock.return_value = 12.0

        with patch.object(
                self.win.completions_window, "add_completion") as mock_add:
            self.win._on_study_completed_live(
                engine, "S1", fully_complete=True)

        mock_add.assert_called_once()
        kwargs = mock_add.call_args.kwargs
        assert kwargs["image_count"] == 9
        assert kwargs["min_images_threshold"] == 10

    def test_study_completed_live_forwards_threshold_just_below(self):
        """Boundary: 9 downloaded images forward the cutoff so the
        window can decide based on the cumulative count."""
        engine = MagicMock()
        engine.queue_snapshot.return_value = [
            MagicMock(study_uid="S1", status="done",
                      transferred_images=9, patient_name="A",
                      study_description="CT", study_time="080000",
                      institution_name="H"),
        ]
        engine.pop_study_wall_clock.return_value = 5.0

        with patch.object(
                self.win.completions_window, "add_completion") as mock_add:
            self.win._on_study_completed_live(
                engine, "S1", fully_complete=True)

        mock_add.assert_called_once()
        kwargs = mock_add.call_args.kwargs
        assert kwargs["image_count"] == 9
        assert kwargs["min_images_threshold"] == 10

    def test_study_completed_live_includes_entry_at_threshold(self):
        """Boundary: exactly 10 downloaded images must appear."""
        engine = MagicMock()
        engine.queue_snapshot.return_value = [
            MagicMock(study_uid="S1", status="done",
                      transferred_images=10, patient_name="A",
                      study_description="CT", study_time="080000",
                      institution_name="H"),
        ]
        engine.pop_study_wall_clock.return_value = 5.0

        with patch.object(
                self.win.completions_window, "add_completion") as mock_add:
            self.win._on_study_completed_live(
                engine, "S1", fully_complete=True)

        mock_add.assert_called_once()
        kwargs = mock_add.call_args.kwargs
        assert kwargs["image_count"] == 10

    def test_study_completed_live_passes_study_uid(self):
        """_on_study_completed_live must forward the study_uid to
        add_completion so the completions window can aggregate later
        emits for the same study into a single row."""
        engine = MagicMock()
        engine.queue_snapshot.return_value = [
            MagicMock(study_uid="S1", status="done",
                      transferred_images=100, patient_name="A",
                      study_description="CT", study_time="080000",
                      institution_name="H"),
        ]
        engine.pop_study_wall_clock.return_value = 12.0

        with patch.object(
                self.win.completions_window, "add_completion") as mock_add:
            self.win._on_study_completed_live(
                engine, "S1", fully_complete=True)

        mock_add.assert_called_once()
        assert mock_add.call_args.kwargs["study_uid"] == "S1"

    def test_study_completed_live_passes_source(self):
        """_on_study_completed_live must forward the source-PACS key to
        add_completion so the completions window can separate delay /
        duration statistics per source."""
        engine = MagicMock()
        engine.queue_snapshot.return_value = [
            MagicMock(study_uid="S1", status="done",
                      transferred_images=100, patient_name="A",
                      study_description="CT", study_time="080000",
                      institution_name="H"),
        ]
        engine.pop_study_wall_clock.return_value = 12.0

        with patch.object(
                self.win.completions_window, "add_completion") as mock_add:
            self.win._on_study_completed_live(
                engine, "S1", fully_complete=True, source="PACS-Nord")

        mock_add.assert_called_once()
        assert mock_add.call_args.kwargs["source"] == "PACS-Nord"

    def test_connect_engine_signals_forwards_remote_key_as_source(self):
        """The study_completed lambda wired in _connect_engine_signals
        is the single point where the source-PACS key reaches the
        completions window — pin that it actually passes
        ``source=remote_key`` (the parameter defaults to "", so losing
        it would regress silently)."""
        engine = MagicMock()
        dashboard = MagicMock()
        self.win._connect_engine_signals("PACS-Nord", engine, dashboard)

        with patch.object(self.win, "_on_study_completed_live") as live:
            for call in engine.signals.study_completed.connect \
                    .call_args_list:
                call.args[0]("UID1", "Inst", True, 42)

        live.assert_called_once_with(engine, "UID1", True,
                                     source="PACS-Nord")

    def test_min_images_for_completions_entry_is_ten(self):
        """The threshold constant for Download Completions filtering
        must be exposed and equal 10.  Pinning it here so the value
        is not silently changed without updating the spec."""
        from gui.main_window import MIN_IMAGES_FOR_COMPLETIONS_ENTRY
        assert MIN_IMAGES_FOR_COMPLETIONS_ENTRY == 10

    def test_update_completions_progress_aggregates_engines(self):
        """_update_completions_progress must sum pending images and
        transfer rates across all running engines and forward them
        to the completions window's ETE countdown."""
        import time as _time
        from core.transfer_engine import SeriesJob, TransferStats

        now = _time.time()

        eng1 = MagicMock()
        eng1.is_running = True
        eng1.queue_snapshot.return_value = [
            SeriesJob(series_uid="1", remote_count=200,
                      local_count=0, status="queued"),
            SeriesJob(series_uid="2", remote_count=100,
                      local_count=100, status="done"),
        ]
        eng1.stats = TransferStats()
        eng1.stats.start_time = now - 60  # 60 s ago
        eng1.stats.total_images = 100     # → 100 img/min raw

        eng2 = MagicMock()
        eng2.is_running = True
        eng2.queue_snapshot.return_value = [
            SeriesJob(series_uid="3", remote_count=300,
                      local_count=0, status="queued"),
        ]
        eng2.stats = TransferStats()
        eng2.stats.start_time = now - 60
        eng2.stats.total_images = 200     # → 200 img/min raw

        self.win.engines = {"ct": eng1, "mri": eng2}

        with patch.object(
                self.win.completions_window,
                "update_transfer_progress") as mock_up:
            self.win._update_completions_progress()

        mock_up.assert_called_once()
        args = mock_up.call_args
        pending = args[0][0] if args[0] else args[1]["pending_images"]
        ipm = args[0][1] if len(args[0]) > 1 else args[1]["images_per_minute"]
        assert pending == 500, (
            "200 (eng1 queued) + 300 (eng2 queued) = 500 pending")
        # Raw rate: eng1 100/min + eng2 200/min ≈ 300/min
        assert 290 < ipm < 310, (
            f"aggregate raw rate should be ~300 img/min, got {ipm}")


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

    @patch.object(MainWindow, '_ensure_storage_scp_for',
                  lambda self, rk: self._start_engine(rk, self._pending_start_params.pop(rk, {})))
    @patch("gui.main_window.TransferEngine")
    def test_start_creates_engine_for_source(self, MockEngine):
        mock_engine = MagicMock()
        mock_engine.signals = MagicMock()
        MockEngine.return_value = mock_engine

        self.win._on_start_service(
            "ct", {"hours": 6, "max_images": 500, "sync_interval": 120})

        MockEngine.assert_called_once_with(
            self.config, "ct", transfer_log=self.win._transfer_log)
        mock_engine.start.assert_called_once_with(
            hours=6, max_images=500, sync_interval=120,
            selection_mode=False)

    @patch.object(MainWindow, '_ensure_storage_scp_for')
    @patch("gui.main_window.TransferEngine")
    def test_start_calls_ensure_scp_for_source(self, MockEngine, mock_scp):
        mock_engine = MagicMock()
        mock_engine.signals = MagicMock()
        MockEngine.return_value = mock_engine

        self.win._on_start_service(
            "ct", {"hours": 3, "max_images": 0, "sync_interval": 60})

        mock_scp.assert_called_once_with("ct")

    @patch.object(MainWindow, '_ensure_storage_scp_for',
                  lambda self, rk: self._start_engine(rk, self._pending_start_params.pop(rk, {})))
    @patch("gui.main_window.TransferEngine")
    def test_start_connects_signals(self, MockEngine):
        mock_engine = MagicMock()
        mock_signals = MagicMock()
        mock_engine.signals = mock_signals
        MockEngine.return_value = mock_engine

        self.win._on_start_service(
            "ct", {"hours": 3, "max_images": 0, "sync_interval": 60})

        # queue_updated: dashboard + ETE countdown = 2 connections
        assert mock_signals.queue_updated.connect.call_count == 2
        # stats_updated: dashboard + ETE countdown = 2 connections
        assert mock_signals.stats_updated.connect.call_count == 2
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

    def test_on_service_stopped_prunes_engine(self):
        """A stopped engine must be removed from window.engines so
        stale TransferEngine objects (with connected signal lambdas)
        don't accumulate over restart cycles."""
        mock_engine = MagicMock()
        mock_engine.is_running = False  # engine has wound down
        self.win.engines["ct"] = mock_engine

        self.win._on_service_stopped("ct", mock_engine)

        assert "ct" not in self.win.engines

    def test_on_service_stopped_prunes_without_emitter(self):
        """Direct calls without an emitter reference (engine=None)
        still prune whatever non-running engine is registered."""
        mock_engine = MagicMock()
        mock_engine.is_running = False
        self.win.engines["ct"] = mock_engine

        self.win._on_service_stopped("ct")

        assert "ct" not in self.win.engines

    def test_on_service_stopped_keeps_running_engine(self):
        """A late stopped-signal from a STALE engine must not evict a
        newer engine already registered (and running) under the same
        key — e.g. after a quick stop/start cycle."""
        old_engine = MagicMock()
        old_engine.is_running = False
        new_engine = MagicMock()
        new_engine.is_running = True
        self.win.engines["ct"] = new_engine

        self.win._on_service_stopped("ct", old_engine)

        assert self.win.engines["ct"] is new_engine

    def test_on_service_stopped_keeps_engine_still_running(self):
        """If the registered engine still reports is_running (signal
        raced ahead of the flag), it must not be pruned — closeEvent's
        any_running/join logic still needs it."""
        mock_engine = MagicMock()
        mock_engine.is_running = True
        self.win.engines["ct"] = mock_engine

        self.win._on_service_stopped("ct", mock_engine)

        assert self.win.engines["ct"] is mock_engine

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
        # ``_log`` routes through a Qt.QueuedConnection signal so the
        # append happens on the next event-loop iteration; pump
        # events so the assertion sees the result.
        self.win._log("Test message")
        QApplication.processEvents()
        text = self.win.log_window.log_text.toPlainText()
        assert "Test message" in text

    def test_log_has_timestamp(self):
        self.win._log("Timestamped message")
        QApplication.processEvents()
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

    @staticmethod
    def _finished_handler(mock_popup):
        """Return the slot that ``_on_unknown_institution`` connected
        to the popup's ``finished`` signal."""
        return mock_popup.finished.connect.call_args[0][0]

    @patch("gui.main_window.UnknownInstitutionPopup")
    def test_popup_created_with_correct_args(self, MockPopup):
        mock_popup = MagicMock()
        MockPopup.return_value = mock_popup

        self.win._on_unknown_institution("New Hospital")
        MockPopup.assert_called_once_with(
            "New Hospital",
            self.win.config.filter_group_names,
            self.win,
        )

    @patch("gui.main_window.UnknownInstitutionPopup")
    def test_popup_is_non_modal(self, MockPopup):
        """The popup must be shown with show(), never exec() — a modal
        exec() spins a nested event loop inside an engine signal slot
        while engines keep emitting."""
        mock_popup = MagicMock()
        MockPopup.return_value = mock_popup

        self.win._on_unknown_institution("New Hospital")

        mock_popup.show.assert_called_once()
        mock_popup.exec.assert_not_called()
        # The assignment is deferred to the finished handler.
        mock_popup.finished.connect.assert_called_once()

    @patch("gui.main_window.UnknownInstitutionPopup")
    def test_assignment_saved_on_accept(self, MockPopup):
        mock_popup = MagicMock()
        mock_popup.assigned_group = "Group A"
        MockPopup.return_value = mock_popup

        self.win._on_unknown_institution("Brand New Hospital")
        # Simulate the user clicking OK: the dialog emits
        # finished(Accepted), which runs the connected handler.
        self._finished_handler(mock_popup)(MockPopup.Accepted)

        assert self.win.config.institution_assignments[
            "Brand New Hospital"] == "Group A"

    @patch("gui.main_window.UnknownInstitutionPopup")
    def test_accept_without_group_registers_unassigned(self, MockPopup):
        """OK with '(do not assign)' selected still registers the
        institution as known-but-unassigned (empty group)."""
        mock_popup = MagicMock()
        mock_popup.assigned_group = None
        MockPopup.return_value = mock_popup

        self.win._on_unknown_institution("Lone Hospital")
        self._finished_handler(mock_popup)(MockPopup.Accepted)

        assert self.win.config.institution_assignments[
            "Lone Hospital"] == ""

    @patch("gui.main_window.UnknownInstitutionPopup")
    def test_rejected_popup_saves_nothing(self, MockPopup):
        """Closing the dialog (Rejected) must not register the
        institution — same outcome as the old modal flow."""
        mock_popup = MagicMock()
        mock_popup.assigned_group = None
        MockPopup.return_value = mock_popup

        self.win._on_unknown_institution("Closed Hospital")
        self._finished_handler(mock_popup)(0)  # QDialog.Rejected

        assert "Closed Hospital" not in (
            self.win.config.institution_assignments)
        # Popup must still be deleted and the dedupe slot freed.
        mock_popup.deleteLater.assert_called_once()
        assert "Closed Hospital" not in self.win._open_institution_popups

    @patch("gui.main_window.UnknownInstitutionPopup")
    def test_second_emit_same_name_reuses_open_popup(self, MockPopup):
        """A second unknown_institution emit for the same name (e.g.
        from a second source) must NOT create a second popup — the
        existing one is raised instead."""
        mock_popup = MagicMock()
        MockPopup.return_value = mock_popup

        self.win._on_unknown_institution("Dup Hospital")
        self.win._on_unknown_institution("Dup Hospital")

        MockPopup.assert_called_once()
        mock_popup.raise_.assert_called_once()
        mock_popup.activateWindow.assert_called_once()

    @patch("gui.main_window.UnknownInstitutionPopup")
    def test_new_popup_allowed_after_previous_finished(self, MockPopup):
        """Once the popup for a name has finished, a later emit for
        the same name opens a fresh popup (the dedupe entry is
        cleared in the finished handler)."""
        mock_popup = MagicMock()
        MockPopup.return_value = mock_popup

        self.win._on_unknown_institution("Repeat Hospital")
        self._finished_handler(mock_popup)(0)  # dismissed
        assert "Repeat Hospital" not in self.win._open_institution_popups

        self.win._on_unknown_institution("Repeat Hospital")
        assert MockPopup.call_count == 2

    @patch("gui.main_window.UnknownInstitutionPopup")
    def test_distinct_names_get_distinct_popups(self, MockPopup):
        """Different institution names each get their own popup."""
        MockPopup.side_effect = lambda *a, **k: MagicMock()

        self.win._on_unknown_institution("Hospital A")
        self.win._on_unknown_institution("Hospital B")

        assert MockPopup.call_count == 2
        assert set(self.win._open_institution_popups) == {
            "Hospital A", "Hospital B"}


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

    @patch("gui.main_window.QMessageBox.information")
    @patch("gui.main_window.DicomOperations")
    @patch("gui.main_window.threading.Thread")
    def test_echo_runs_for_all_remotes(
        self, MockThread, MockOps, mock_info
    ):
        mock_ops = MagicMock()
        mock_ops.c_echo.return_value = True
        MockOps.return_value = mock_ops

        # Capture the target function and run it synchronously
        def run_synchronously(**kwargs):
            thread = MagicMock()
            thread.start = lambda: kwargs["target"]()
            return thread
        MockThread.side_effect = run_synchronously

        self.win._test_echo()

        # Should call c_echo for each remote + each unique local dest
        # 2 remotes + 2 unique local (different AE/port) = 4 echo calls
        assert mock_ops.c_echo.call_count >= 3

    @patch("gui.main_window.QMessageBox.information")
    def test_on_echo_results_shows_info_dialog(self, mock_info):
        self.win._on_echo_results(["  ct (CT Scanner): Reachable"])
        mock_info.assert_called_once()
        assert "Reachable" in mock_info.call_args[0][2]


# ═══════════════════════════════════════════════════════════════════════════
# MainWindow — per-source SCP
# ═══════════════════════════════════════════════════════════════════════════

class TestMainWindowSCP:

    @pytest.fixture(autouse=True)
    def _create(self, populated_config, qapp):
        self.win = MainWindow(populated_config)

    @patch("gui.main_window.StorageSCP")
    def test_scp_started_when_local_unreachable(self, MockSCP):
        mock_scp = MagicMock()
        mock_scp.running = False
        MockSCP.return_value = mock_scp

        node = self.win.config.remote_nodes["ct"]
        params = {"hours": 3, "max_images": 0, "sync_interval": 60}
        self.win._pending_start_params = {"ct": params}
        self.win._on_scp_check_done("ct", False, node.to_dict())

        MockSCP.assert_called_once()
        mock_scp.start.assert_called_once()
        assert ("LOCAL_AE", 11112) in self.win.storage_scps

    def test_scp_skipped_when_local_reachable(self):
        node = self.win.config.remote_nodes["ct"]
        params = {"hours": 3, "max_images": 0, "sync_interval": 60}
        self.win._pending_start_params = {"ct": params}
        self.win._on_scp_check_done("ct", True, node.to_dict())

        assert len(self.win.storage_scps) == 0

    @patch("gui.main_window.TransferEngine")
    def test_scp_reuses_existing(self, MockEngine):
        mock_engine = MagicMock()
        mock_engine.signals = MagicMock()
        MockEngine.return_value = mock_engine

        existing_scp = MagicMock()
        existing_scp.running = True
        self.win.storage_scps[("LOCAL_AE", 11112)] = existing_scp

        params = {"hours": 3, "max_images": 0, "sync_interval": 60}
        self.win._pending_start_params = {"ct": params}
        # SCP already running → skips thread, jumps to _start_engine
        self.win._ensure_storage_scp_for("ct")

        assert len(self.win.storage_scps) == 1

    def test_scp_check_pops_pending_when_node_gone(self):
        """If the source has been removed between scheduling the SCP
        check and the check result coming back, the pending-start
        params must be dropped — otherwise they leak forever and may
        be consumed by a future same-keyed re-add."""
        params = {"hours": 3}
        self.win._pending_start_params = {"ct": params}

        # Pull the source out from under us.
        node_dict = self.win.config.remote_nodes["ct"].to_dict()
        del self.win.config.remote_nodes["ct"]

        self.win._on_scp_check_done("ct", True, node_dict)

        assert "ct" not in self.win._pending_start_params

    @patch("gui.main_window.StorageSCP")
    @patch.object(MainWindow, "_start_engine")
    def test_scp_bind_failure_is_handled(self, mock_start_engine, MockSCP):
        """When ``StorageSCP.start()`` raises (port already in use),
        the handler must log it, drop the pending-params entry, and
        NOT start the engine — otherwise the engine fires C-MOVEs to
        a dead local SCP."""
        mock_scp = MagicMock()
        mock_scp.start.side_effect = RuntimeError(
            "Storage SCP failed to bind on port 11112")
        MockSCP.return_value = mock_scp

        node = self.win.config.remote_nodes["ct"]
        params = {"hours": 3, "max_images": 0, "sync_interval": 60}
        self.win._pending_start_params = {"ct": params}
        # Must not raise out to the Qt event loop.
        self.win._on_scp_check_done("ct", False, node.to_dict())

        mock_start_engine.assert_not_called()
        assert "ct" not in self.win._pending_start_params
        # The (ae_title, port) key must not be left in storage_scps as
        # a half-built SCP — otherwise the next start would skip the
        # init path on a dead instance.
        assert ("LOCAL_AE", 11112) not in self.win.storage_scps


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
           return_value=16384)  # Yes
    def test_close_waits_for_engine_thread(self, mock_question):
        """After Quit-Anyway, the engine must be join()ed so the
        in-flight C-MOVE can finish — otherwise the daemon thread is
        killed mid-transfer and the SQLite log is inconsistent.

        Drive ``engine.join`` to report 'still running' once then
        'finished' so the responsive-join loop runs one slice and
        terminates."""
        mock_engine = MagicMock()
        mock_engine.is_running = True
        mock_engine.join.side_effect = [False, True]
        self.win.engines["ct"] = mock_engine
        event = MagicMock()
        self.win.closeEvent(event)
        mock_engine.stop.assert_called_once()
        assert mock_engine.join.called
        # Sanity: a finite, positive timeout was supplied (don't hang
        # forever).
        _, kwargs = mock_engine.join.call_args
        assert "timeout" in kwargs and kwargs["timeout"] > 0

    @patch("gui.main_window.QMessageBox.question",
           return_value=16384)  # Yes
    def test_close_skips_join_when_thread_already_dead(self, mock_question):
        """Don't call join on a thread that already finished."""
        mock_engine = MagicMock()
        mock_engine.is_running = True
        mock_engine._thread.is_alive.return_value = False
        self.win.engines["ct"] = mock_engine
        event = MagicMock()
        self.win.closeEvent(event)
        mock_engine._thread.join.assert_not_called()

    @patch("gui.main_window.QMessageBox.question",
           return_value=65536)  # No
    def test_close_with_running_engine_cancel(self, mock_question):
        mock_engine = MagicMock()
        mock_engine.is_running = True
        self.win.engines["ct"] = mock_engine
        event = MagicMock()
        self.win.closeEvent(event)
        event.ignore.assert_called_once()

    def test_close_stops_scps(self):
        mock_scp = MagicMock()
        mock_scp.running = True
        self.win.storage_scps[("LOCAL_AE", 11112)] = mock_scp
        event = MagicMock()
        self.win.closeEvent(event)
        mock_scp.stop.assert_called_once()
        event.accept.assert_called_once()
