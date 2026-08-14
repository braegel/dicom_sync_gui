"""
Tests for gui.service_watchdog — active-transfer state and the stall
watchdog's wedge rule.

The logic was extracted from gui.dashboard, where it lived as eight
loose attributes mutated by eight methods.  These tests target the
extracted objects directly; tests/test_dashboard.py continues to cover
the widget's use of them.
"""

import pytest

from gui.service_watchdog import (
    WATCHDOG_MAX_DEADLINE_S, WATCHDOG_MIN_DEADLINE_S,
    WATCHDOG_NO_PROGRESS_S, ActiveTransfer, PendingRestart,
    pending_images_of, series_deadline_s,
)

TERMINAL = ("done", "error", "skipped", "unavailable")


class TestSeriesDeadline:
    """A "no status for 10 s" rule restarted healthy large series
    mid-download; the deadline has to scale with the series' own size."""

    def test_scales_with_size_when_a_rate_is_known(self):
        small = series_deadline_s(pending_images=100, rate=1.0)
        large = series_deadline_s(pending_images=2000, rate=10.0)
        assert large > small

    def test_cold_stats_get_the_floor(self):
        assert series_deadline_s(300, rate=0.0) == WATCHDOG_MIN_DEADLINE_S

    def test_tiny_series_never_below_the_floor(self):
        assert series_deadline_s(1, rate=100.0) == WATCHDOG_MIN_DEADLINE_S

    def test_pathological_rate_is_capped(self):
        assert series_deadline_s(10 ** 9, rate=1e-9) == WATCHDOG_MAX_DEADLINE_S


class TestPendingImagesOf:

    def test_normal(self):
        assert pending_images_of(
            {"remote_count": 300, "local_count": 40}) == 260

    def test_never_negative(self):
        assert pending_images_of(
            {"remote_count": 10, "local_count": 99}) == 0

    @pytest.mark.parametrize("info", [
        {}, {"remote_count": "n/a", "local_count": 0},
        {"remote_count": None, "local_count": None},
    ])
    def test_junk_from_the_pacs_does_not_raise(self, info):
        assert pending_images_of(info) == 0


class TestActiveTransferLifecycle:

    def test_starts_inactive(self):
        a = ActiveTransfer()
        assert not a.is_active and not a.is_armed

    def test_begin_arms_everything(self):
        a = ActiveTransfer()
        a.begin("u1", 300.0, now=1000.0)
        assert a.is_active and a.is_armed
        assert a.series_uid == "u1"
        assert a.images_done == 0
        assert a.last_progress_ts == 1000.0
        assert a.deadline_ts == 1300.0

    def test_begin_resets_a_previous_transfer(self):
        a = ActiveTransfer()
        a.begin("u1", 300.0, now=1000.0)
        a.note_progress(120, now=1100.0)
        a.begin("u2", 200.0, now=2000.0)
        assert a.series_uid == "u2"
        assert a.images_done == 0, "image count must not leak across series"

    def test_note_progress_leaves_the_deadline_alone(self):
        """Extending the deadline on every image would make the
        size-based estimate meaningless."""
        a = ActiveTransfer()
        a.begin("u1", 300.0, now=1000.0)
        a.note_progress(50, now=1200.0)
        assert a.deadline_ts == 1300.0
        assert a.last_progress_ts == 1200.0
        assert a.images_done == 50

    def test_negative_progress_clamped(self):
        a = ActiveTransfer()
        a.begin("u1", 300.0, now=0.0)
        a.note_progress(-5, now=1.0)
        assert a.images_done == 0

    def test_clear_disarms_every_field(self):
        a = ActiveTransfer()
        a.begin("u1", 300.0, now=1000.0)
        a.note_progress(50, now=1100.0)
        a.clear()
        assert (a.series_uid, a.images_done, a.last_progress_ts,
                a.deadline_ts) == (None, 0, 0.0, 0.0)


class TestWedgeRule:
    """BOTH conditions must hold: past the size-based deadline AND no
    progress for WATCHDOG_NO_PROGRESS_S."""

    def _overdue(self):
        a = ActiveTransfer()
        a.begin("u1", 100.0, now=1000.0)   # deadline 1100
        return a

    def test_inactive_is_never_wedged(self):
        assert ActiveTransfer().wedged_for(now=10 ** 9) is None

    def test_active_but_unarmed_is_never_wedged(self):
        a = ActiveTransfer(series_uid="u1", deadline_ts=0.0)
        assert a.wedged_for(now=10 ** 9) is None

    def test_within_the_deadline_is_not_wedged(self):
        a = self._overdue()
        assert a.wedged_for(now=1050.0) is None

    def test_overran_but_still_progressing_is_not_wedged(self):
        a = self._overdue()
        a.note_progress(10, now=1200.0)
        assert a.wedged_for(
            now=1200.0 + WATCHDOG_NO_PROGRESS_S / 2) is None

    def test_overran_and_silent_is_wedged(self):
        a = self._overdue()
        a.note_progress(10, now=1200.0)
        now = 1200.0 + WATCHDOG_NO_PROGRESS_S + 30
        assert a.wedged_for(now=now) == pytest.approx(
            WATCHDOG_NO_PROGRESS_S + 30)

    def test_exactly_at_the_no_progress_threshold_fires(self):
        """Boundary pinned as it has always behaved: the check is
        ``no_progress < THRESHOLD → not wedged``, so landing exactly on
        the threshold counts as wedged."""
        a = self._overdue()
        a.note_progress(10, now=1200.0)
        assert a.wedged_for(now=1200.0 + WATCHDOG_NO_PROGRESS_S) == (
            pytest.approx(WATCHDOG_NO_PROGRESS_S))


class TestPendingFor:
    """The live Pending / ETE countdown discounts images the active
    transfer has already received."""

    def _job(self, uid="u1", status="transferring", remote=100, local=0):
        return {"series_uid": uid, "status": status,
                "remote_count": remote, "local_count": local}

    def test_terminal_rows_are_zero(self):
        a = ActiveTransfer()
        assert a.pending_for(self._job(status="done"), TERMINAL) == 0

    def test_inactive_transfer_does_not_discount(self):
        a = ActiveTransfer()
        assert a.pending_for(self._job(), TERMINAL) == 100

    def test_active_series_is_discounted(self):
        a = ActiveTransfer()
        a.begin("u1", 300.0, now=0.0)
        a.note_progress(40, now=1.0)
        assert a.pending_for(self._job("u1"), TERMINAL) == 60

    def test_other_series_are_not_discounted(self):
        a = ActiveTransfer()
        a.begin("u1", 300.0, now=0.0)
        a.note_progress(40, now=1.0)
        assert a.pending_for(self._job("u2"), TERMINAL) == 100

    def test_over_delivery_floors_at_zero(self):
        a = ActiveTransfer()
        a.begin("u1", 300.0, now=0.0)
        a.note_progress(999, now=1.0)
        assert a.pending_for(self._job("u1"), TERMINAL) == 0


class TestPendingRestart:

    def test_carries_the_snapshot_and_the_auto_flag(self):
        pending = PendingRestart(params={"hours": 3}, is_auto=True)
        assert pending.params == {"hours": 3}
        assert pending.is_auto is True

    def test_defaults_to_a_manual_restart(self):
        assert PendingRestart(params=None).is_auto is False
