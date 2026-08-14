"""
Tests for gui.queue_table — the dashboard's Series Queue table view.

The rendering was extracted out of gui.dashboard; these tests target the
view's own API directly (the dashboard tests continue to cover it end to
end through ``on_queue_updated`` / ``on_queue_ready_for_selection``).
"""

from PySide6.QtCore import Qt

from gui.queue_table import (
    COL_CHECK, COL_ETE, COL_GROUP, COL_IPM, COL_PATIENT, COL_PENDING,
    COL_STATUS, COLUMN_COUNT, QueueTableView, format_ete,
    format_series_created, ipm_text, status_text,
)


def _job(uid="1.1", status="queued", remote=100, local=0, **kw):
    job = {
        "patient_name": "Doe^Jane",
        "study_description": "CT Abdomen",
        "series_description": "Axial 3mm",
        "modality": "CT",
        "series_uid": uid,
        "study_uid": "1",
        "remote_count": remote,
        "local_count": local,
        "status": status,
        "institution_name": "Hospital Alpha",
        "images_per_minute": 0.0,
        "series_date": "20260401",
        "series_time": "080000",
    }
    job.update(kw)
    return job


def _pending(job):
    if job["status"] in ("done", "error", "skipped", "unavailable"):
        return 0
    return max(job["remote_count"] - job["local_count"], 0)


class _Config:
    filter_groups_enabled = False
    institution_assignments = {"Hospital Alpha": "Group A"}


class TestConstruction:

    def test_column_count_and_headers(self, qapp):
        view = QueueTableView(_Config())
        assert view.table.columnCount() == COLUMN_COUNT
        assert view.table.horizontalHeaderItem(COL_PATIENT).text() == "Patient"

    def test_check_column_hidden_by_default(self, qapp):
        view = QueueTableView(_Config())
        assert view.table.isColumnHidden(COL_CHECK)

    def test_group_column_follows_filter_setting(self, qapp):
        cfg = _Config()
        cfg.filter_groups_enabled = True
        assert not QueueTableView(cfg).table.isColumnHidden(COL_GROUP)
        cfg.filter_groups_enabled = False
        assert QueueTableView(cfg).table.isColumnHidden(COL_GROUP)


class TestRenderPathSelection:
    """The engine emits the full queue after every completed series, so
    an unchanged uid sequence must take the cheap in-place path rather
    than rebuilding (which flickers and drops the user's selection)."""

    def test_first_render_builds_rows(self, qapp):
        view = QueueTableView(_Config())
        view.render([_job("a"), _job("b")], 0.0, _pending)
        assert view.table.rowCount() == 2

    def test_same_uid_sequence_preserves_row_identity(self, qapp):
        view = QueueTableView(_Config())
        queue = [_job("a"), _job("b")]
        view.render(queue, 0.0, _pending)
        first_item = view.table.item(0, COL_PATIENT)

        queue[0]["status"] = "transferring"
        view.render(queue, 0.0, _pending)

        assert view.table.item(0, COL_PATIENT) is first_item, (
            "an unchanged uid sequence must update in place, not rebuild")

    def test_changed_uid_sequence_rebuilds(self, qapp):
        view = QueueTableView(_Config())
        view.render([_job("a"), _job("b")], 0.0, _pending)
        first_item = view.table.item(0, COL_PATIENT)

        view.render([_job("c")], 0.0, _pending)

        assert view.table.rowCount() == 1
        assert view.table.item(0, COL_PATIENT) is not first_item

    def test_status_cell_recolours_on_change(self, qapp):
        view = QueueTableView(_Config())
        queue = [_job("a", status="queued")]
        view.render(queue, 0.0, _pending)
        queued_colour = view.table.item(0, COL_STATUS).foreground().color()

        queue[0]["status"] = "done"
        view.render(queue, 0.0, _pending)
        done_colour = view.table.item(0, COL_STATUS).foreground().color()

        assert queued_colour != done_colour

    def test_pending_uses_the_supplied_callable(self, qapp):
        """The view knows nothing about the active transfer — the
        discount rule lives with the dashboard, which owns that state."""
        view = QueueTableView(_Config())
        view.render([_job("a", remote=100)], 0.0, lambda j: 7)
        assert view.table.item(0, COL_PENDING).text() == "7"


class TestSelectionMode:

    def test_render_for_selection_shows_checkboxes(self, qapp):
        view = QueueTableView(_Config())
        view.render_for_selection([_job("a"), _job("b")])
        assert not view.table.isColumnHidden(COL_CHECK)
        assert view.table.item(0, COL_CHECK).checkState() == Qt.Checked

    def test_checked_series_uids(self, qapp):
        view = QueueTableView(_Config())
        view.render_for_selection([_job("a"), _job("b")])
        view.table.item(1, COL_CHECK).setCheckState(Qt.Unchecked)
        assert view.checked_series_uids() == ["a"]

    def test_set_all_checked(self, qapp):
        view = QueueTableView(_Config())
        view.render_for_selection([_job("a"), _job("b")])
        view.set_all_checked(False)
        assert view.checked_series_uids() == []
        view.set_all_checked(True)
        assert view.checked_series_uids() == ["a", "b"]

    def test_next_render_rebuilds_after_selection(self, qapp):
        """Selection rows carry a checkbox and 'Waiting' statuses, so the
        next normal render must rebuild even if the uids match."""
        view = QueueTableView(_Config())
        queue = [_job("a")]
        view.render_for_selection(queue)
        view.render(queue, 0.0, _pending)
        assert view.table.item(0, COL_STATUS).text() == status_text("queued")
        assert view.table.isColumnHidden(COL_CHECK)


class TestCountdownColumns:

    def test_ete_uses_cumulative_pending(self, qapp):
        view = QueueTableView(_Config())
        # 60 + 60 images pending at 1 image/s → 1:00 then 2:00.
        queue = [_job("a", remote=60), _job("b", remote=60)]
        view.render(queue, 1.0, _pending)
        assert view.table.item(0, COL_ETE).text() == "1:00"
        assert view.table.item(1, COL_ETE).text() == "2:00"

    def test_refresh_updates_in_place(self, qapp):
        view = QueueTableView(_Config())
        queue = [_job("a", remote=60)]
        view.render(queue, 1.0, _pending)
        ete_item = view.table.item(0, COL_ETE)
        view.refresh_pending_and_ete(queue, 1.0, lambda j: 30)
        assert view.table.item(0, COL_ETE) is ete_item, (
            "the 1 Hz tick must not reallocate items")
        assert view.table.item(0, COL_PENDING).text() == "30"

    def test_terminal_rows_are_left_alone(self, qapp):
        view = QueueTableView(_Config())
        queue = [_job("a", status="done")]
        view.render(queue, 1.0, _pending)
        assert view.table.item(0, COL_ETE).text() == "✓"
        view.refresh_pending_and_ete(queue, 1.0, _pending)
        assert view.table.item(0, COL_ETE).text() == "✓"

    def test_refresh_tolerates_a_queue_longer_than_the_table(self, qapp):
        view = QueueTableView(_Config())
        view.render([_job("a")], 1.0, _pending)
        view.refresh_pending_and_ete(
            [_job("a"), _job("b")], 1.0, _pending)  # must not raise


class TestClear:

    def test_clear_empties_and_forces_rebuild(self, qapp):
        view = QueueTableView(_Config())
        queue = [_job("a")]
        view.render(queue, 0.0, _pending)
        view.clear()
        assert view.table.rowCount() == 0
        view.render(queue, 0.0, _pending)
        assert view.table.rowCount() == 1


class TestFormatters:
    """Pure helpers — the dashboard re-exports these as staticmethods, so
    they are covered from both sides."""

    def test_format_ete_minutes_and_hours(self):
        assert format_ete(0) == "—"
        assert format_ete(90) == "1:30"
        assert format_ete(3661) == "1:01:01"

    def test_format_series_created(self):
        assert format_series_created("20260401", "081500") == "01.04.2026 08:15"
        assert format_series_created("20260401", "") == "01.04.2026"
        assert format_series_created("", "") == "—"

    def test_ipm_text_only_for_done_series(self):
        assert ipm_text(_job(status="done", images_per_minute=42.4)) == "42"
        assert ipm_text(_job(status="transferring",
                             images_per_minute=42.4)) == "—"

    def test_status_text_falls_back_to_the_raw_value(self):
        assert status_text("done") == "✓ Done"
        assert status_text("something-new") == "something-new"
